#!/usr/bin/env python3
"""BedCast v1 receiver — timestamped, restart-invariant playback.

The whole point: capture-to-ear latency converges to a CHOSEN constant B
(--buffer-ms, default 300), independent of connection history. Restart the
receiver, seek the video, survive a WiFi stall — latency returns to B, so the
video player's audio offset is tuned ONCE, ever.

Mechanism:
  1. NTP-style handshake: 5 pings, keep the min-RTT sample -> clock offset o.
  2. Every packet carries capture_ts. Playback error = now - (ts + o + B).
  3. err < -20ms (early)  -> write silence to push content later (converges to B
     at connect: first packet is ~B early -> ~B of silence primes the pipe).
  4. err > +120ms (late, e.g. post-stall burst or restart backlog) -> drop
     packet until back inside the band. The paced sink holds it there.

Sinks: mpv (default; needs mpv installed), null (paced, for tests), file:PATH.
ASCII-only output (Windows cp1252 consoles kill fancy glyphs).
"""

import argparse
import socket
import struct
import subprocess
import sys
import time

MAGIC_V1 = b"BEDCAST1"
EARLY_FILL_US = 20_000      # more than 20ms early -> silence-fill down to band
LATE_DROP_US = 120_000      # more than 120ms late -> drop to catch up
MAX_FILL_US = 3_000_000     # sanity cap on a single silence fill


def now_us() -> int:
    return time.time_ns() // 1000


def handshake(sock: socket.socket, rounds: int = 5) -> int:
    """Returns clock offset o (us): server_clock ~= local_clock + o."""
    best = None  # (rtt, offset)
    for _ in range(rounds):
        t0 = now_us()
        sock.sendall(b"BC1H" + struct.pack("<q", t0))
        reply = read_exactly(sock, 20)
        t1 = now_us()
        tag, t0_echo, t_server = reply[:4], *struct.unpack("<qq", reply[4:])
        if tag != b"BC1R" or t0_echo != t0:
            raise ProtocolMismatch("bad handshake reply (v0 header/PCM instead of BC1R?)")
        rtt = t1 - t0
        offset = t_server - (t0 + t1) // 2
        if best is None or rtt < best[0]:
            best = (rtt, offset)
    sock.sendall(b"BC1G" + struct.pack("<q", 0))
    print("[bedcast] clock offset %+d us (best rtt %d us)" % (best[1], best[0]), file=sys.stderr)
    return best[1]


def read_exactly(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed")
        buf += chunk
    return buf


class ProtocolMismatch(ConnectionError):
    pass


class Sink:
    """Paced byte sink. mpv paces via blocking pipe; null paces by clock."""

    def __init__(self, kind: str, rate: int, ch: int):
        self.kind, self.rate, self.ch = kind, rate, ch
        self.bytes_per_us = rate * ch * 2 / 1_000_000
        self.proc = None
        self.file = None
        self.consumed_us = 0.0
        self.t_start = None
        if kind == "mpv":
            self.proc = subprocess.Popen(
                ["mpv", "--no-terminal", "--no-video", "--cache=no",
                 "--audio-buffer=0.10", "--demuxer=rawaudio",
                 "--demuxer-rawaudio-rate=%d" % rate,
                 "--demuxer-rawaudio-channels=%d" % ch,
                 "--demuxer-rawaudio-format=s16le", "-"],
                stdin=subprocess.PIPE)
        elif kind.startswith("file:"):
            self.file = open(kind[5:], "wb")
        elif kind != "null":
            raise ValueError("sink must be mpv, null, or file:PATH")

    def write(self, data: bytes):
        if self.proc:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        elif self.file:
            self.file.write(data)
        else:  # null: pace like a real audio device
            if self.t_start is None:
                self.t_start = now_us()
            self.consumed_us += len(data) / self.bytes_per_us
            ahead_us = (self.t_start + self.consumed_us) - now_us()
            if ahead_us < 0:
                # underrun: a real device idles - it does not bank catch-up credit
                # (unbounded credit made post-stall depth readings unphysical)
                self.t_start = now_us() - self.consumed_us
                ahead_us = 0
            if ahead_us > 100_000:  # keep only 100ms device-buffer illusion
                time.sleep((ahead_us - 100_000) / 1e6)

    def close(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        if self.file:
            self.file.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=48100)
    ap.add_argument("--buffer-ms", type=int, default=300)
    ap.add_argument("--sink", default="mpv")
    ap.add_argument("--duration", type=float, default=0, help="seconds; 0 = endless")
    ap.add_argument("--stats-secs", type=float, default=5)
    args = ap.parse_args()

    b_us = args.buffer_ms * 1000
    sock = socket.create_connection((args.host, args.port), timeout=5)
    sock.settimeout(30)
    try:
        offset = handshake(sock)
        header = read_exactly(sock, 16)
        if header[:8] != MAGIC_V1:
            raise ProtocolMismatch("not a BEDCAST1 server (got %r)" % header[:8])
    except ProtocolMismatch as e:
        # v0-only server (e.g. server-linux.sh): it streams BEDCAST0+PCM instead of
        # answering BC1H. Exit 3 tells the launcher to fall back to the v0 pipe
        # (cross-family review 2026-07-20, finding: default receiver could not use
        # the Linux server and reconnect-looped forever).
        print("[bedcast] server is v0-only (%s) - exiting for v0 fallback" % e, file=sys.stderr)
        sock.close()
        return 3
    rate = struct.unpack("<I", header[8:12])[0]
    ch, bits = header[12], header[13]
    print("[bedcast] stream: %d Hz, %d ch, %d-bit; target latency %d ms"
          % (rate, ch, bits, args.buffer_ms), file=sys.stderr)

    sink = Sink(args.sink, rate, ch)
    bytes_per_us = rate * ch * 2 / 1_000_000

    fills = drops = pkts = corrections = 0
    dep_min = dep_max = dep_sum = dep_n = 0
    t_end = time.monotonic() + args.duration if args.duration else None
    t_stats = time.monotonic() + args.stats_secs

    # v1.1 control (redesign 2026-07-20, converged from three independent analyses:
    # builder repro, termux field forensics, Eve skew rig): ANCHOR ONCE by clock,
    # then STEER by queue depth. The old per-packet wall-clock steering measured
    # pipe backpressure, not latency, and oscillated (fills~=drops storms).
    #   prime: first packet -> transit = now - (ts - offset); write (B - transit)
    #          of silence so capture-to-ear starts at B (restart-invariant, skew-safe).
    #   steer: depth_us = written_us - (now - t_first_write)  [sink consumes realtime]
    #          correct ONLY on sustained excursion outside a wide deadband,
    #          rate-limited, sized to return depth to B. No per-packet judgment.
    DEADBAND_US = 80_000
    STALE_GUARD_US = 250_000   # packet older than B+this = stall-backlog relic: drop
                               # (well above observed real-WiFi jitter; a stall then
                               # behaves like a restart - gap, then clean re-anchor)
    SUSTAIN_PKTS = 40          # ~0.4s of consecutive out-of-band packets
    CORRECTION_GAP_S = 2.0     # min seconds between corrections
    written_us = 0.0
    t_first_write = None       # monotonic seconds of first sink write
    sustain = 0
    t_last_corr = 0.0
    primed = False
    depth_target_us = b_us     # replaced at prime time (F-v1.1-1)
    fill_blocked = False       # set when sink proves saturated (F-v1.1-2)
    reprime_pending = False    # set when the staleness guard drains a stall backlog
    GAP_REPRIME_S = 1.0        # a packet gap this long = stream discontinuity: re-prime
    t_last_pkt = None

    def sink_write(data: bytes):
        nonlocal written_us, t_first_write
        if not data:
            return
        if t_first_write is None:
            t_first_write = time.monotonic()
        sink.write(data)
        written_us += len(data) / bytes_per_us

    def silence(us: float) -> bytes:
        frame_bytes = ch * 2
        n = int(us * bytes_per_us) // frame_bytes * frame_bytes
        return b"\x00" * max(0, n)

    try:
        while t_end is None or time.monotonic() < t_end:
            hdr = read_exactly(sock, 16)
            seq, ts, ln = struct.unpack("<IqI", hdr)
            if ln > 1_048_576:
                raise ConnectionError("insane frame length %d" % ln)
            payload = read_exactly(sock, ln)
            if ln == 0:
                continue  # heartbeat: connection alive during render-silence (pause)
            pkts += 1
            if t_last_pkt is not None and time.monotonic() - t_last_pkt > GAP_REPRIME_S:
                # silence-type gap (pause/no-render): nothing was stale, but elapsed
                # spanned the gap - the depth estimator must re-base (Sol xhigh:
                # naive resume inserted the stall duration as fresh silence)
                reprime_pending = True
            t_last_pkt = time.monotonic()

            # Staleness guard (web-Fable5 review, executed finding: 3s stall ->
            # 295-pkt burst, drops=0, +3s permanent latency, depth blind because
            # the blocking sink write paces the loop). Also covers Sol's initial-
            # late-packet case: stale content never reaches the sink at all.
            lateness_us = now_us() - (ts - offset) - b_us
            if lateness_us > STALE_GUARD_US:
                drops += 1
                if primed:
                    reprime_pending = True
                sustain = 0
                continue
            if reprime_pending:
                # stall drained: re-base the depth estimator (elapsed includes the
                # stall, written does not) and re-anchor latency = restart semantics.
                print("[bedcast] stall drained (stale pkts dropped) - re-priming",
                      file=sys.stderr)
                primed = False
                written_us = 0.0
                t_first_write = None
                reprime_pending = False

            if not primed:
                # Server ts -> local clock is ts MINUS offset (server ~= local + offset).
                # Sign error costs 2x skew (cross-operator review 2026-07-20) - the fix
                # lives on in the anchor: transit uses (ts - offset).
                transit_us = now_us() - (ts - offset)
                prime_us = min(max(b_us - transit_us, 0), b_us)
                sink_write(silence(prime_us))
                sink_write(payload)
                primed = True
                # F-v1.1-1 (Eve): prime targets LATENCY B, so post-prime depth is
                # B - transit. Steering must target THAT, not b_us - else transit
                # > deadband causes spurious fills and latency ends at B + transit.
                depth_target_us = written_us - 0.0  # post-prime depth = the baseline
                print("[bedcast] primed: transit %.1fms, prime %.1fms -> latency target %dms"
                      % (transit_us / 1000, prime_us / 1000, args.buffer_ms), file=sys.stderr)
                continue

            depth_us = written_us - (time.monotonic() - t_first_write) * 1e6
            dep_n += 1
            dep_sum += depth_us
            dep_min = min(dep_min, depth_us) if dep_n > 1 else depth_us
            dep_max = max(dep_max, depth_us) if dep_n > 1 else depth_us

            # Stats BEFORE any control action: all-drop mode must stay visible
            # (review F-v1-2: a fully-dropping receiver was mute).
            if time.monotonic() >= t_stats:
                print("[stats] pkts=%d depth(ms) avg=%.1f min=%.1f max=%.1f fills=%d drops=%d corr=%d"
                      % (pkts, dep_sum / dep_n / 1000, dep_min / 1000, dep_max / 1000,
                         fills, drops, corrections), file=sys.stderr)
                dep_min = dep_max = dep_sum = dep_n = 0
                t_stats = time.monotonic() + args.stats_secs

            excursion = depth_us - depth_target_us
            if abs(excursion) > DEADBAND_US:
                sustain += 1
            else:
                sustain = 0

            if sustain >= SUSTAIN_PKTS and time.monotonic() - t_last_corr > CORRECTION_GAP_S:
                if excursion < 0:
                    # F-v1.1-2 (Eve): if the sink saturates below target, depth reads
                    # low forever and naive refills inject unbounded silence bursts
                    # (latency grows per fill, invisible to the depth metric). Guard:
                    # a fill must RAISE measured depth; if it didn't, the sink is
                    # saturated - stop filling, warn once, accept the sink's ceiling.
                    if fill_blocked:
                        sustain = 0
                        continue
                    pre_depth = written_us - (time.monotonic() - t_first_write) * 1e6
                    sink_write(silence(min(-excursion, MAX_FILL_US)))
                    post_depth = written_us - (time.monotonic() - t_first_write) * 1e6
                    if post_depth - pre_depth < -excursion * 0.5:
                        fill_blocked = True
                        print("[bedcast] WARN: sink saturated below target depth "
                              "(fill did not raise depth: %.1f -> %.1fms). Steering "
                              "disabled for fills; effective latency = sink capacity."
                              % (pre_depth / 1000, post_depth / 1000), file=sys.stderr)
                    fills += 1
                else:
                    # sustained backlog: enter a DRAIN EPISODE - drop consecutive
                    # payloads until depth is back inside the band, bounded to 2s of
                    # content per episode. Rate-limit episodes, not individual drops
                    # (cross-family review 2026-07-20: the old 1-drop-per-2s pace
                    # needed ~200s to clear a 1s backlog).
                    draining_us = 0.0
                    corrections += 1
                    t_last_corr = time.monotonic()
                    sustain = 0
                    drain_target = depth_target_us + DEADBAND_US // 2
                    while draining_us < 2_000_000:
                        drops += 1
                        draining_us += ln / bytes_per_us
                        # depth falls on its own while draining (we stop writing,
                        # the sink keeps consuming) - compare it directly; draining_us
                        # only bounds the audible discontinuity per episode.
                        depth_now = written_us - (time.monotonic() - t_first_write) * 1e6
                        if depth_now <= drain_target:
                            break
                        hdr = read_exactly(sock, 16)
                        seq, ts, ln = struct.unpack("<IqI", hdr)
                        if ln > 1_048_576:
                            raise ConnectionError("insane frame length %d" % ln)
                        payload = read_exactly(sock, ln)
                        pkts += 1
                    continue
                corrections += 1
                t_last_corr = time.monotonic()
                sustain = 0

            sink_write(payload)
    except (ConnectionError, socket.timeout) as e:
        print("[bedcast] stream ended: %s" % e, file=sys.stderr)
    finally:
        sink.close()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

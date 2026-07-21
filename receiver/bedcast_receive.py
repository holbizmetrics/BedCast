#!/usr/bin/env python3
"""BedCast v1.1 receiver — timestamped, restart-invariant playback.

The point: pipeline latency converges to a CHOSEN constant B (--buffer-ms,
default 300), independent of connection history. Restart the receiver, survive
a stall or a paused movie — latency re-anchors to B, so the video player's
audio offset is tuned ONCE, ever.

Control law (v1.1 "prime once, steer by depth", 2026-07-20, converged from
three independent analyses — builder repro, termux field forensics, Eve skew
rig; hardened by cross-family review the same night):
  prime:  first packet -> transit = now - (ts - offset); write (B - transit)
          of silence. Skew-safe (sign rule: local time of ts = ts - offset).
  steer:  depth = written_us - elapsed_us, corrected only on sustained
          excursion outside a wide deadband, rate-limited, episode-drained.
  guards: staleness (stall backlog -> drop + re-prime = restart semantics),
          gap re-prime (paused stream), saturation (a fill must raise measured
          depth or fills disable), fill cap, drain discontinuity cap.

The law lives in Controller — pure of I/O (clock injected, sink writes via
callback) so the virtual-time harness (tests/test_controller_virtual.py)
drives the EXACT shipped object, not a copy.

Sinks: mpv (default), null (paced, for tests), file:PATH.
ASCII-only output (Windows cp1252 consoles kill fancy glyphs).
"""

import argparse
import socket
import struct
import subprocess
import sys
import time

MAGIC_V1 = b"BEDCAST1"


def now_us() -> int:
    return time.time_ns() // 1000


class ProtocolMismatch(ConnectionError):
    pass


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


class Controller:
    """The v1.1 control law, pure of I/O.

    Inject: b_us (target), offset (handshake), rate/ch (stream format),
    now_fn (clock, us), write_fn (sink byte writer), log_fn (diagnostics).
    Feed AUDIO packets (heartbeats filtered by the caller) via on_packet().
    The controller owns all writes (silence AND payload) so the saturation
    guard can sample the clock around its own fill — same as the shipped
    inline code always did.
    """

    DEADBAND_US = 80_000
    STALE_GUARD_US = 250_000   # older than B+this = stall-backlog relic: drop
    SUSTAIN_PKTS = 40          # ~0.4s of consecutive out-of-band packets
    CORRECTION_GAP_US = 2_000_000
    GAP_REPRIME_US = 1_000_000  # audio-packet gap this long = discontinuity
    MAX_FILL_US = 3_000_000
    DRAIN_BUDGET_US = 2_000_000  # max audible discontinuity per drain episode

    def __init__(self, b_us, offset, rate, ch, now_fn, write_fn, log_fn=None):
        self.b_us = b_us
        self.offset = offset
        self.ch = ch
        self.bytes_per_us = rate * ch * 2 / 1_000_000
        self.now = now_fn
        self._write = write_fn
        self.log = log_fn or (lambda msg: None)
        # state
        self.primed = False
        self.reprime_pending = False
        self.fill_blocked = False
        self.written_us = 0.0
        self.t_first_write = None      # us, controller clock
        self.depth_target_us = b_us    # replaced at prime (F-v1.1-1)
        self.sustain = 0
        self.t_last_corr = None        # us
        self.t_last_audio = None       # us
        self.drain_budget_us = 0.0     # >0 while a drain episode is active
        # counters (read by stats)
        self.fills = self.drops = self.corrections = self.reprimes = 0

    # -- helpers ---------------------------------------------------------

    def depth_us(self):
        if self.t_first_write is None:
            return 0.0
        return self.written_us - (self.now() - self.t_first_write)

    def _sink_write(self, data: bytes):
        if not data:
            return
        if self.t_first_write is None:
            self.t_first_write = self.now()
        self._write(data)
        self.written_us += len(data) / self.bytes_per_us

    def _silence(self, us: float) -> bytes:
        frame_bytes = self.ch * 2
        n = int(us * self.bytes_per_us) // frame_bytes * frame_bytes
        return b"\x00" * max(0, n)

    def _reprime(self, why):
        # Re-base the depth estimator (elapsed spans the discontinuity, written
        # does not) and re-anchor latency = restart semantics.
        self.log("[bedcast] %s - re-priming" % why)
        self.primed = False
        self.written_us = 0.0
        self.t_first_write = None
        self.reprime_pending = False
        self.drain_budget_us = 0.0
        self.reprimes += 1

    # -- the law ---------------------------------------------------------

    def on_packet(self, ts_us: int, payload: bytes):
        """Feed one AUDIO packet (len>0). Executes writes/drops internally."""
        now = self.now()

        # Gap re-prime (paused stream / no-render: nothing stale arrives, but
        # elapsed spanned the gap — Sol xhigh: naive resume inserted the gap
        # as fresh silence).
        if self.t_last_audio is not None and now - self.t_last_audio > self.GAP_REPRIME_US:
            self.reprime_pending = True
        self.t_last_audio = now

        # Staleness guard (web-Fable5, executed: 3s stall -> burst all played,
        # +3s permanent latency, depth blind because the blocking sink write
        # paces the loop). Also covers the initial-late-packet case.
        lateness_us = now - (ts_us - self.offset) - self.b_us
        if lateness_us > self.STALE_GUARD_US:
            self.drops += 1
            if self.primed:
                self.reprime_pending = True
            self.sustain = 0
            return

        if self.reprime_pending:
            self._reprime("discontinuity drained (stale/gap)")

        if not self.primed:
            # Sign rule: local time of a server ts is ts MINUS offset — the +
            # form costs 2x the skew (Eve, CRITICAL, invisible same-machine).
            transit_us = now - (ts_us - self.offset)
            prime_us = min(max(self.b_us - transit_us, 0), self.b_us)
            self._sink_write(self._silence(prime_us))
            self._sink_write(payload)
            self.primed = True
            # F-v1.1-1: steering targets POST-PRIME depth (B - transit), not B.
            self.depth_target_us = self.written_us
            self.log("[bedcast] primed: transit %.1fms, prime %.1fms -> latency target %.0fms"
                     % (transit_us / 1000, prime_us / 1000, self.b_us / 1000))
            return

        # Active drain episode: drop until back inside band or budget spent.
        if self.drain_budget_us > 0:
            depth = self.depth_us()
            drain_target = self.depth_target_us + self.DEADBAND_US // 2
            if depth <= drain_target:
                self.drain_budget_us = 0.0
            else:
                self.drops += 1
                self.drain_budget_us -= len(payload) / self.bytes_per_us
                return

        excursion = self.depth_us() - self.depth_target_us
        if abs(excursion) > self.DEADBAND_US:
            self.sustain += 1
        else:
            self.sustain = 0

        corr_ok = (self.t_last_corr is None
                   or now - self.t_last_corr > self.CORRECTION_GAP_US)
        if self.sustain >= self.SUSTAIN_PKTS and corr_ok:
            if excursion < 0:
                # F-v1.1-2 saturation guard: a fill must RAISE measured depth,
                # else the sink is saturated below target — disable fills, warn
                # once, accept the sink's ceiling (no unbounded silence bursts).
                if not self.fill_blocked:
                    pre = self.depth_us()
                    self._sink_write(self._silence(min(-excursion, self.MAX_FILL_US)))
                    post = self.depth_us()
                    if post - pre < -excursion * 0.5:
                        self.fill_blocked = True
                        self.log("[bedcast] WARN: sink saturated below target depth "
                                 "(fill did not raise depth: %.1f -> %.1fms). Steering "
                                 "disabled for fills; effective latency = sink capacity."
                                 % (pre / 1000, post / 1000))
                    self.fills += 1
                    self.corrections += 1
                    self.t_last_corr = now
                self.sustain = 0
            else:
                # Drain episode: consecutive drops bounded to DRAIN_BUDGET of
                # audible discontinuity; episodes (not drops) are rate-limited
                # (Sol: 1-drop-per-2s needed ~200s for a 1s backlog).
                self.drain_budget_us = self.DRAIN_BUDGET_US
                self.drops += 1
                self.drain_budget_us -= len(payload) / self.bytes_per_us
                self.corrections += 1
                self.t_last_corr = now
                self.sustain = 0
                return

        self._sink_write(payload)


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
                # underrun: a real device idles - it does not bank catch-up
                # credit (unbounded credit made post-stall depth unphysical)
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

    sock = socket.create_connection((args.host, args.port), timeout=5)
    sock.settimeout(30)
    try:
        offset = handshake(sock)
        header = read_exactly(sock, 16)
        if header[:8] != MAGIC_V1:
            raise ProtocolMismatch("not a BEDCAST1 server (got %r)" % header[:8])
    except ProtocolMismatch as e:
        # v0-only server (e.g. server-linux.sh): exit 3 tells the launcher to
        # fall back to the v0 pipe (cross-family review: the old behavior
        # reconnect-looped forever).
        print("[bedcast] server is v0-only (%s) - exiting for v0 fallback" % e, file=sys.stderr)
        sock.close()
        return 3
    rate = struct.unpack("<I", header[8:12])[0]
    ch, bits = header[12], header[13]
    print("[bedcast] stream: %d Hz, %d ch, %d-bit; target latency %d ms"
          % (rate, ch, bits, args.buffer_ms), file=sys.stderr)

    sink = Sink(args.sink, rate, ch)
    ctl = Controller(args.buffer_ms * 1000, offset, rate, ch,
                     now_fn=now_us, write_fn=sink.write,
                     log_fn=lambda m: print(m, file=sys.stderr))

    pkts = 0
    dep_min = dep_max = dep_sum = dep_n = 0
    t_end = time.monotonic() + args.duration if args.duration else None
    t_stats = time.monotonic() + args.stats_secs

    try:
        while t_end is None or time.monotonic() < t_end:
            hdr = read_exactly(sock, 16)
            seq, ts, ln = struct.unpack("<IqI", hdr)
            if ln > 1_048_576:
                raise ConnectionError("insane frame length %d" % ln)
            payload = read_exactly(sock, ln)
            if ln == 0:
                continue  # heartbeat: connection alive during render-silence
            pkts += 1

            ctl.on_packet(ts, payload)

            d = ctl.depth_us()
            dep_n += 1
            dep_sum += d
            dep_min = min(dep_min, d) if dep_n > 1 else d
            dep_max = max(dep_max, d) if dep_n > 1 else d
            # Stats always print (F-v1-2: the all-drop mode must stay visible).
            if time.monotonic() >= t_stats:
                print("[stats] pkts=%d depth(ms) avg=%.1f min=%.1f max=%.1f "
                      "fills=%d drops=%d corr=%d reprimes=%d"
                      % (pkts, dep_sum / dep_n / 1000, dep_min / 1000,
                         dep_max / 1000, ctl.fills, ctl.drops,
                         ctl.corrections, ctl.reprimes), file=sys.stderr)
                dep_min = dep_max = dep_sum = dep_n = 0
                t_stats = time.monotonic() + args.stats_secs
    except (ConnectionError, socket.timeout) as e:
        print("[bedcast] stream ended: %s" % e, file=sys.stderr)
    finally:
        sink.close()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

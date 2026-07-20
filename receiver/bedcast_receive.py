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
            raise ConnectionError("bad handshake reply")
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
    sock.settimeout(10)
    offset = handshake(sock)

    header = read_exactly(sock, 16)
    if header[:8] != MAGIC_V1:
        raise ConnectionError("not a BEDCAST1 server (got %r)" % header[:8])
    rate = struct.unpack("<I", header[8:12])[0]
    ch, bits = header[12], header[13]
    print("[bedcast] stream: %d Hz, %d ch, %d-bit; target latency %d ms"
          % (rate, ch, bits, args.buffer_ms), file=sys.stderr)

    sink = Sink(args.sink, rate, ch)
    bytes_per_us = rate * ch * 2 / 1_000_000

    fills = drops = pkts = 0
    err_min = err_max = err_sum = err_n = 0
    t_end = time.monotonic() + args.duration if args.duration else None
    t_stats = time.monotonic() + args.stats_secs

    try:
        while t_end is None or time.monotonic() < t_end:
            hdr = read_exactly(sock, 16)
            seq, ts, ln = struct.unpack("<IqI", hdr)
            if ln > 1_048_576:
                raise ConnectionError("insane frame length %d" % ln)
            payload = read_exactly(sock, ln)
            pkts += 1

            err = now_us() - (ts + offset + b_us)  # + late / - early
            err_n += 1
            err_sum += err
            err_min = min(err_min, err) if err_n > 1 else err
            err_max = max(err_max, err) if err_n > 1 else err

            if err > LATE_DROP_US:
                drops += 1
                continue
            if err < -EARLY_FILL_US:
                fill_us = min(-err - EARLY_FILL_US // 2, MAX_FILL_US)
                n_bytes = int(fill_us * bytes_per_us) // 4 * 4
                sink.write(b"\x00" * n_bytes)
                fills += 1
            sink.write(payload)

            if time.monotonic() >= t_stats:
                print("[stats] pkts=%d err(ms) avg=%.1f min=%.1f max=%.1f fills=%d drops=%d"
                      % (pkts, err_sum / err_n / 1000, err_min / 1000, err_max / 1000, fills, drops),
                      file=sys.stderr)
                err_min = err_max = err_sum = err_n = 0
                t_stats = time.monotonic() + args.stats_secs
    except (ConnectionError, socket.timeout) as e:
        print("[bedcast] stream ended: %s" % e, file=sys.stderr)
    finally:
        sink.close()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

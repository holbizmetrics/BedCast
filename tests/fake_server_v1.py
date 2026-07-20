#!/usr/bin/env python3
"""Fake BEDCAST1 server, written from docs/WIRE-FORMAT.md ONLY (not from Program.cs).

Scenarios:
  happy            steady 10ms packets
  skew SECONDS     server clock offset by +SECONDS (tests handshake offset math)
  stall AT DUR     stop sending at t=AT for DUR seconds, then burst the backlog
  silence AT DUR   WASAPI-style: no packets during silence (no backlog after)
"""
import socket, struct, sys, threading, time

RATE, CH = 48000, 2
PKT_MS = 10
PKT_BYTES = RATE * CH * 2 * PKT_MS // 1000

def now_us(skew_s=0.0):
    return time.time_ns() // 1000 + int(skew_s * 1e6)

def serve(scenario, arg1=0.0, arg2=0.0, port=48100, total_s=20.0):
    skew = arg1 if scenario == "skew" else 0.0
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port)); srv.listen(1)
    print(f"[fake] {scenario} on :{port}", file=sys.stderr)
    conn, _ = srv.accept()
    conn.settimeout(5)

    # handshake per spec: answer BC1H with BC1R until BC1G
    while True:
        msg = b""
        while len(msg) < 12:
            c = conn.recv(12 - len(msg))
            if not c: return
            msg += c
        tag = msg[:4]
        if tag == b"BC1G": break
        assert tag == b"BC1H", tag
        t0 = struct.unpack("<q", msg[4:])[0]
        conn.sendall(b"BC1R" + struct.pack("<qq", t0, now_us(skew)))

    # header
    conn.sendall(b"BEDCAST1" + struct.pack("<IBBH", RATE, CH, 16, 0))

    # framed packets, paced realtime; payload = quiet ramp (nonzero)
    payload = bytes((i % 7) for i in range(PKT_BYTES))
    seq = 0
    start = time.monotonic()
    stall_at = arg1 if scenario in ("stall", "silence") else None
    stall_dur = arg2
    stalled = False
    backlog = []
    try:
        while time.monotonic() - start < total_s:
            t = time.monotonic() - start
            ts = now_us(skew) - PKT_MS * 1000  # capture ts = now - packet duration
            frame = struct.pack("<IqI", seq, ts, PKT_BYTES) + payload
            seq += 1
            if stall_at is not None and stall_at <= t < stall_at + stall_dur:
                if scenario == "stall":
                    backlog.append(frame)   # network stall: data queues
                stalled = True              # silence: data simply doesn't exist
            else:
                if stalled and backlog:
                    for f in backlog: conn.sendall(f)   # burst
                    print(f"[fake] burst {len(backlog)} pkts", file=sys.stderr)
                    backlog = []
                stalled = False
                conn.sendall(frame)
            time.sleep(PKT_MS / 1000)
    except (BrokenPipeError, ConnectionResetError, socket.timeout):
        pass
    conn.close(); srv.close()
    print("[fake] done", file=sys.stderr)

if __name__ == "__main__":
    sc = sys.argv[1]
    a1 = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    a2 = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 48100
    serve(sc, a1, a2, port)

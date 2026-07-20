# BedCast Wire Format (v0)

The protocol is the product: any capture shim that speaks this format works with any
BedCast receiver, on any platform. TCP, one stream per connection, sender→receiver only.

## Connection flow

1. Receiver opens a TCP connection to the server (default port **48100**).
2. Server sends the **16-byte header** once.
3. Server sends **endless raw PCM** until either side closes. No framing, no trailer.

## Header (16 bytes)

| Offset | Size | Field | Value (v0) |
|--------|------|-------|------------|
| 0 | 8 | magic | ASCII `BEDCAST0` |
| 8 | 4 | sample rate | uint32, little-endian (typically 48000) |
| 12 | 1 | channels | byte (typically 2) |
| 13 | 1 | bits per sample | byte, always 16 in v0 |
| 14 | 2 | reserved | zero |

A robust receiver SHOULD check the magic and read rate/channels from the header —
the server sends whatever the OS mix format is. **Honest note:** the bundled v0
receiver scripts take a shortcut — they strip the header blind (`tail -c +17`) and
assume 48000/2, which matches the common Windows mix format. If your mix format
differs, pass the real values to mpv or parse the header properly.

## Payload

Interleaved signed 16-bit little-endian PCM (`S16LE`), `channels` samples per frame,
`rate` frames per second. At 48000 Hz stereo that is 192,000 bytes/sec (~1.5 Mbps).

## Design notes

- **No compression.** Home WiFi has headroom; raw PCM keeps the receiver a one-liner
  (`nc | mpv`) and adds zero codec latency or artifacts.
- **No timestamps in v0.** Sync rests on connection-lifetime buffer depth, which
  pause/seek/dropouts can disturb. This is v0's known, deliberate weakness.
- **Trust model: your LAN.** No auth, no encryption. Anyone who can reach the port
  hears your system audio. Run it on a network you trust; don't port-forward it.

## v1 (`BEDCAST1`, timestamped — implemented 2026-07-20)

Auto-negotiated at connect: a v1 receiver speaks first; a silent client gets legacy
v0 after 700 ms. The server is multi-client — several receivers (mixed v0/v1) stream
simultaneously.

**Handshake** (before the header; all integers little-endian):

| Message | Direction | Layout (12 or 20 bytes) |
|---|---|---|
| ping | receiver → server | `"BC1H"` + `t0_us` (i64, receiver clock) — repeat up to 8× |
| echo | server → receiver | `"BC1R"` + `t0_us` echoed + `t_server_us` (i64) |
| go | receiver → server | `"BC1G"` + `0` (i64) |

Receiver keeps the minimum-RTT sample: `offset = t_server − (t0 + t1)/2`, so
`server_clock ≈ local_clock + offset` and a server timestamp converts to local time
as **`ts − offset`** (the sign matters: the + form costs 2× the skew — caught by
cross-operator review after shipping, invisible in same-machine tests).

Normative counts: a receiver SHOULD send at most 8 pings (the reference sends 5);
a server MUST tolerate at least 8 before `BC1G`.

**Stream:** 16-byte header (magic `BEDCAST1`, same layout as v0), then framed packets:

| Field | Size | Meaning |
|---|---|---|
| `seq` | u32 | packet counter, per connection |
| `capture_ts_us` | i64 | capture time of first sample, unix epoch µs, server clock |
| `len` | u32 | payload bytes (sanity cap 1 MiB) |
| payload | `len` | S16LE PCM |

**Receiver policy — v1.1 "prime once, steer by depth"** (replaced the v1.0 per-packet
wall-clock law, which measured pipe backpressure rather than latency and oscillated
into audible fill/drop storms on real sinks; three independent analyses converged on
this redesign 2026-07-20):

1. **Prime:** on the first packet, `transit = now − (ts − offset)`; write
   `max(B − transit, 0)` of silence. This anchors pipeline latency at the chosen
   target `B` (default 300 ms), skew-safe, identically on every (re)connect.
2. **Steer:** estimated queue depth `= written_us − elapsed_us` (the sink consumes
   in real time once playing). Steering targets the **post-prime depth** (= `B −
   transit`), not `B` itself. Corrections require a sustained excursion (~0.4 s)
   outside an 80 ms deadband and are rate-limited per episode.
3. **Guards:** a silence-fill that fails to raise measured depth means the sink is
   saturated below target — fills disable with a warning rather than looping
   (a metric must be able to observe its own action's failure). A high backlog
   drains in one episode (consecutive drops, bounded to 2 s of discontinuity),
   not one packet per rate-limit window.

**Honest wording** (per cross-family review): the depth estimate measures bytes
handed to the sink, not acoustic capture-to-ear latency — sink and device buffers
sit outside it as a constant the user tunes away once. The tested claim is
**restart-stable sync in the tested configurations** (real phone/PC WiFi bench:
3× restart, no re-tune needed; by-ear residual sub-0.3 s), not a fixed acoustic
latency guarantee. A server that only speaks v0 (e.g. `server-linux.sh`) causes a
v1 receiver to exit with code 3; the reference launcher falls back to the v0 pipe.

Clock drift bound: consumer crystals drift ≤50 ppm → ≤180 ms over a 2 h movie worst
case, absorbed continuously by the fill/drop band. Multi-hour sessions may accumulate
audible re-syncs; a periodic re-handshake is v2 material if it ever matters in practice.

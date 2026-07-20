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

Receiver keeps the minimum-RTT sample: `offset = t_server − (t0 + t1)/2`.

**Stream:** 16-byte header (magic `BEDCAST1`, same layout as v0), then framed packets:

| Field | Size | Meaning |
|---|---|---|
| `seq` | u32 | packet counter, per connection |
| `capture_ts_us` | i64 | capture time of first sample, unix epoch µs, server clock |
| `len` | u32 | payload bytes (sanity cap 1 MiB) |
| payload | `len` | S16LE PCM |

**Receiver policy** (the point of all this): playback error `e = now − (ts + offset + B)`
where `B` is the chosen target latency (default 300 ms). `e < −20 ms` → write silence
to converge down to `B`; `e > +120 ms` → drop the packet to catch up. Capture-to-ear
latency therefore returns to `B` after any restart, stall, or seek — measured locally
at ±2.4 ms across receiver restarts (2026-07-20; v0's equivalent was ±seconds).

Clock drift bound: consumer crystals drift ≤50 ppm → ≤180 ms over a 2 h movie worst
case, absorbed continuously by the fill/drop band. Multi-hour sessions may accumulate
audible re-syncs; a periodic re-handshake is v2 material if it ever matters in practice.

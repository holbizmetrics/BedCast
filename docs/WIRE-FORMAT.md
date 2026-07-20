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

## v1 (planned, will bump magic to `BEDCAST1`)

Framed packets — `seq` (uint32), `capture_ts_us` (uint64, sender clock), `n_bytes`
(uint32), payload — plus a one-shot clock-sync handshake at connect. Receiver plays
each packet at `capture_ts + offset`, making sync independent of buffer depth and
self-correcting across pause/seek/dropouts. `BEDCAST0` receivers stay supported by a
`--v0` server flag (or a v0-compat first byte negotiation, decided at v1 design time).

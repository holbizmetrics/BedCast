# BedCast

**Stream Windows system audio to an Android device over WiFi — so you can watch a movie on the PC screen and hear it on the phone in bed. No Bluetooth.**

## The design principle (why this beats Bluetooth structurally)

The stream is one-way and non-interactive, so **latency costs nothing — only jitter and lip-sync drift cost anything.** That inversion buys stability with buffer, for free: half a second of buffer is intolerable in a call, invisible here, because the video player compensates once (`mpv`: `+`/`-`, VLC: `j`/`k`).

The failure mode of naive streamers: sync depends on *queue depth*, which pause/seek/WiFi hiccups scramble → re-tune every time. BedCast's fix (v1): **stamp every packet with a capture timestamp, sync clocks once, play each packet at `timestamp + offset`** — never "when it arrives." Buffer depth stops mattering; sync self-corrects across pause, seek, and dropouts. (Same idea as PTS in video containers / RTP sender reports.)

## Non-goals

- **No compression.** 48 kHz × 16-bit × stereo ≈ 1.5 Mbps; home WiFi has tens of Mbps. Raw PCM. (If a weak link ever forces it: Opus, nothing else.)
- **No video.** The PC screen is the display. For phone-as-display, use Sunshine/Moonlight instead.
- **No Android APK (yet).** The receiver runs in Termux (mpv/pacat) — kills the entire app-build pipeline until the design is proven.

## Architecture

```
Windows (C# / NAudio)                    Android (Termux)
┌────────────────────────┐              ┌──────────────────────┐
│ WASAPI loopback capture │──TCP/WiFi──▶│ v0: mpv/pacat plays  │
│ float32 → S16LE PCM     │  raw PCM    │     raw PCM stream   │
│ v1: +capture timestamps │              │ v1: PTS-scheduled    │
└────────────────────────┘              │     jitter buffer    │
                                        └──────────────────────┘
```

## Roadmap

- **v0 — dumb pipe (prove transport):** capture → TCP → play. Sync via player offset, manually tuned. Expected to break on pause/seek (that's the point — it reproduces the problem v1 solves).
- **v1 — timestamps:** packet framing (`seq`, `capture_ts`, `n_samples`), one-shot clock sync handshake, receiver schedules playback at `capture_ts + offset`.
- **v2 — polish:** auto-reconnect, silence suppression, maybe Opus for weak links, maybe a real Android app if Termux friction annoys.

## Status

- v0 server: building (2026-07-20)
- Probe result that motivated this: AudioRelay works for basic playback (verified 2026-07-20, Star Trek Enterprise E01); pause/seek/lock stress verdict pending.

# BedCast

**Stream your computer's audio to an Android phone (or any device) over WiFi — watch a movie on the PC screen, hear it on the phone in bed. No Bluetooth.**

Born from one evening's actual problem: wanting to watch a movie from bed without Bluetooth. The existing apps worked but stuttered; this doesn't, because it makes one deliberate trade they can't.

## The design principle

The stream is one-way and non-interactive, so **latency costs nothing — only jitter and lip-sync drift cost anything.** That inversion buys stability with buffer, for free: half a second of buffer would be intolerable in a game headset, and is invisible here, because the video player compensates once (VLC: `j`/`k` at 50 ms per press; mpv: `Ctrl`+`+`/`Ctrl`+`-` at 100 ms).

Low-latency streamers (built for calls/gaming) *must* run small buffers, so every WiFi hiccup pokes through as a stutter. BedCast runs a fat buffer instead — WiFi jitter disappears into it. In our first side-by-side evening (one listener, one network — n=1, not a benchmark), that difference was clearly audible.

## Status — honest labels

| Piece | Platform | State |
|-------|----------|-------|
| Server (capture+send) | Windows | **TESTED** — byte-exact rate + live listening session, 2026-07-20 |
| Server | Linux | `server-linux.sh` — **UNTESTED** (syntax-checked; PulseAudio/PipeWire monitor-source approach is standard). Reports welcome. |
| Server | macOS | **NOT BUILT** — CoreAudio has no native loopback; needs [BlackHole](https://github.com/ExistentialAudio/BlackHole) + a pipe shim, or a Swift audio-tap (macOS 14.2+). Contributor with a Mac wanted. |
| Receiver v0 (`nc \| mpv`) | Android (Termux) | **TESTED** live 2026-07-20 |
| Receiver v1.1 (`bedcast_receive.py`, timestamped) | any python + mpv | **PASSED real-device acceptance bench** (2026-07-20, phone over real WiFi, operator listening): depth pinned ~230 ms for 15+ min with zero corrections; 3× receiver restart with **no re-tune needed** (v0: ~2.5 s shift + re-tune each time; by-ear residual honestly sub-0.3 s); 3× reconnect-compose. Now the launcher default (`BEDCAST_V0=1` forces legacy). History: the v1.0 loop oscillated on real sinks — fixed by prime-once + depth-steering after a three-way diagnosis (field data, local repro, cross-operator skew rig). |
| Server v1 protocol (handshake, framing, multi-client, v0 fallback) | Windows | **TESTED** locally 2026-07-20 — v1 + legacy v0 clients simultaneously |
| v0 stress behavior (pause/seek/reconnect) | — | **CONFIRMED WEAK** (2026-07-20, live): receiver restart → ~2.5 s sync shift, needs re-tune. This is the failure v1 fixes — see the v1 receiver row. |

**Trust model: your LAN.** No auth, no encryption — anyone who can reach the port hears your system audio. Don't port-forward it. (See `docs/WIRE-FORMAT.md`.)

## Quick start

### Windows server

Grab `bedcast-server.exe` from [Releases](../../releases) (no .NET install needed), or build from source (`dotnet build src/BedCast.Server -c Release`).

> **SmartScreen note:** the exe is unsigned, so Windows may show "Windows protected your PC" on first run — click *More info → Run anyway*, or build from source if you'd rather not trust a downloaded binary.

```
bedcast-server.exe                      # captures default output device, serves :48100
bedcast-server.exe --list-devices       # see your output devices
bedcast-server.exe --device "CABLE"     # capture a specific device (substring match)
bedcast-server.exe --smoke-test 5 t.raw # prove capture works, no network needed
```

### Linux server

```bash
./server-linux.sh                       # streams the default sink's monitor on :48100
```

### Android receiver (Termux, no app install)

```bash
pkg install git python mpv netcat-openbsd
git clone https://github.com/holbizmetrics/BedCast
BedCast/receiver/bedcast-receive.sh YOUR_PC_IP
```

### Any other receiver (Linux/macOS/Windows with mpv)

```bash
nc -d YOUR_PC_IP 48100 | tail -c +17 | mpv --demuxer=rawaudio \
  --demuxer-rawaudio-rate=48000 --demuxer-rawaudio-channels=2 \
  --demuxer-rawaudio-format=s16le -
```

(The `tail -c +17` strips the 16-byte header — or read `docs/WIRE-FORMAT.md` and parse it properly.)

### Then: sync once

Play your movie, nudge the player's audio offset until lips match (VLC: `j`/`k` at 50 ms per press; mpv: `Ctrl`+`+`/`Ctrl`+`-` at 100 ms). Done — that's the buffer trade paying out.

## Silent-PC mode (sound *only* on the phone)

Loopback-capturing your speakers means the room hears the movie too. If you want true silence: route Windows' default output to a virtual device ([VB-Audio Cable](https://vb-audio.com/Cable/), free) and capture that: `--device "CABLE Input"`. The speakers get nothing; the phone gets everything.

## Architecture

```
Windows/Linux (capture)                  Android/anything (playback)
┌────────────────────────┐              ┌──────────────────────┐
│ system-audio loopback   │──TCP/WiFi──▶│ v0: mpv plays raw    │
│ → S16LE PCM + header    │  raw PCM    │     PCM stream       │
│ v1: +capture timestamps │              │ v1: PTS-scheduled    │
└────────────────────────┘              │     jitter buffer    │
                                        └──────────────────────┘
```

Wire protocol: `docs/WIRE-FORMAT.md`. **The protocol is the product** — any capture shim that speaks it works with any receiver. New platform = new shim, nothing else changes.

## Non-goals

- **No compression.** 48 kHz × 16-bit × stereo ≈ 1.5 Mbps; home WiFi has tens of Mbps. Raw PCM, zero codec artifacts, receiver stays a one-liner. (If a weak link ever forces it: Opus, nothing else.)
- **No video.** The PC screen is the display. For phone-as-display, use Sunshine/Moonlight instead.
- **No Android APK (yet).** Termux + mpv kills the entire app-build pipeline until the design is proven.

## Roadmap

- **v0 — dumb pipe** (shipped): capture → TCP → play. Sync via player offset; drifts ~seconds on restart (measured). Still served to header-less clients as the legacy fallback.
- **v1 — timestamps** (shipped 2026-07-20): framed packets + clock-sync handshake; receiver holds capture-to-ear latency at a chosen constant (±2.4 ms across restarts, measured locally). Spec: `docs/WIRE-FORMAT.md`. Multi-client server: phone + tablet + tests simultaneously.
- **v2 — polish:** auto-reconnect, macOS shim, periodic re-handshake if multi-hour drift ever bites in practice, maybe Opus for weak links, maybe a real Android app if Termux friction annoys.

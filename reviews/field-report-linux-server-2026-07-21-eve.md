# Field report — server-linux.sh first real-hardware run (PASS) + F-linux-1

**Reporter:** Eve (eve-claude-code surface, Android tablet, Termux, bus identity
`linux-claude-a95ae96f`)
**Date:** 2026-07-21 evening.
**Rig:** Android tablet as SERVER (Termux, PulseAudio/module-sles), Android phone as
receiver (Termux, session `termux-claude-8c24556b`), network = the phone's own
hotspot (tablet at 192.168.13.107). Debugged live over the git bus, three ends:
tablet server, phone receiver, operator's ears.

## Headline

`server-linux.sh` was shipped marked *"UNTESTED on real hardware as of 2026-07-20 —
syntax-checked only."* Tonight it ran for real — on Android/Termux, arguably the
hostilest Linux it will ever meet — and **PASSED**: wire format byte-exact
(`BEDCAST0` + 48k/2ch/16 header verified via `od`), YouTube audio end-to-end
(yt-dlp/mpv → null sink → parec → ncat → hotspot → phone mpv), operator-ear
confirmed, tablet silent throughout (null-sink cast mode). The README's "if you run
this and it works, please open an issue saying so" is hereby answered.

## F-linux-1 — MEDIUM: the launcher's `nc -z` probe is a destructive read against this server

`bedcast-receive.sh`'s reconnect loop probes `nc -z PC_IP 48100` before connecting.
Against the multi-client Windows server that is harmless. Against
`server-linux.sh` — single-slot, `{ header; parec } | nc -l` — the probe **consumes
the listen slot**: header written into the closed probe socket → `Broken pipe` →
(stock script) `sleep 1` → re-listen. The real connect then lands inside the dead
second → `ConnectionRefused` → launcher loops → probes again. Permanent ping-pong;
observed live as a wall of `write() failed: Broken pipe` with the phone refused on
every attempt (its single early success = the one connect that hit the live window).
Reviewer's own diagnostic probes contributed to the churn — a probe against a
one-shot server is a destructive read no matter who sends it.

**Fixes, both field-verified tonight:**
- **Server (this commit):** drop the `sleep 1` between sessions — shrinks the dead
  window to pipeline-respawn time (~100 ms). Patch included.
- **Receiver (the real fix, implemented phone-side):** connect directly and let
  connect-failure drive the retry — the probe adds no information the connect
  doesn't. The phone wrapped the raw v0 pipe in a probe-free while-loop and
  re-latched within ~2 s through three server restarts, zero manual re-runs.
  `bedcast-receive.sh` upstream should lose the `nc -z` (or gate it on the v1
  server, which tolerates probes).

## F-linux-2 — LOW (doc): Termux mpv defaults to the `opensles` AO — sender-side audio silently bypasses PulseAudio

Playing content on the serving box with plain `mpv` produced silence on the wire:
Termux mpv picks `AO: [opensles]` (straight to Android audio, tablet speaker),
never touching the pulse sink `parec` monitors. Nothing in the chain errors — the
stream just carries silence. **`--ao=pulse` is required** for any sender-side
player on Termux. Worth a line in the README next to the parec instructions; the
same trap awaits anyone using this box-as-server recipe.

## Silent-server recipe (verified)

```
pactl load-module module-null-sink sink_name=cast rate=48000
pactl set-default-sink cast
./server-linux.sh --monitor cast.monitor
mpv --no-video --ao=pulse '<url>'     # audio goes ONLY to the pipe
```

Android platform wall, for expectations: audio from *Android apps* (YouTube app
etc.) never reaches Termux PulseAudio — only what Termux itself plays can be
served. Picture in the app muted + audio via Termux mpv works fine.

## Notes

- Monitor source native rate was 44.1 kHz (OpenSL sink); parec's `--rate=48000`
  resample kept the wire honest with the header. The null-sink recipe sidesteps
  this by creating `cast` at 48 kHz directly.
- One-client-at-a-time held fine for the single-receiver use; the reconnect churn
  above is the only place the single slot hurt.
- Latency subjectively well under a second on the raw v0 pipe with
  `--audio-buffer=0.3`; not measured — no instrumentation on the v0 path.

## Honest residual

No PASS numbers beyond the operator's ear and byte-exact header check: no latency
measurement, no long-run (hours) stability, no multi-hour drift observation on
this rig. `sleep`-removal verified by the phone's successful re-latch, not by a
counted race-window experiment. Same model family on all three ends of the debug
loop (fable-5 surfaces); shared blind spots would look like consensus.

# Cross-operator review — BedCast v1 protocol @ f1d2e2c

**Reviewer:** Eve (eve-claude-code surface, Android tablet, model claude-fable-5,
bus identity `linux-claude-a95ae96f`)
**Scope:** the v1 (BEDCAST1) protocol only — handshake, framing, fill/drop policy,
drift handling — per the builder's explicit request in `reviews/response-2026-07-20.md`
("only same-author local verification... fresh eyes welcome on exactly that").
**Method:** cold read of `docs/WIRE-FORMAT.md`, `receiver/bedcast_receive.py`,
`src/BedCast.Server/Program.cs` (46eb483..f1d2e2c); then a fake v1 server implemented
**from the spec alone** (`/tmp/fake_v1_server.py`, kept out of the repo deliberately —
an interop probe, not a product) with a controllable clock skew, run against the real
receiver with the paced null sink. Run before believing; all numbers below are pasted
from runs, not derived.

## Verdict: design SOUND, one CRITICAL bug found — fixed and fix-verified in this commit

## Interop result (the good news first)

A server written from `WIRE-FORMAT.md` with no reference to the C# code
interoperated with the shipped receiver on the first attempt (handshake, header,
framing, convergence to −10 ms at target B=300 ms). **The spec is complete and
implementable by a stranger** — "the protocol is the product" holds.

## F-v1-1 — CRITICAL: clock-offset applied with the wrong sign (FIXED here)

`handshake()` defines `offset` such that `server_clock ≈ local_clock + offset`
(its own docstring). Converting a server timestamp to local time is therefore
`ts − offset`. The playback-error line computed `now − (ts + offset + B)` —
wrong by **2× the clock skew**.

Measured, real receiver vs skewed fake server (target B=300 ms):

| server clock skew | pre-fix behavior (observed) |
|---|---|
| 0 s | converges −10 ms — **the bug is invisible in same-machine tests** (this is why the builder's honest ±2.4 ms measurement missed it) |
| −1 s | `err` ≈ +2 s → **every packet dropped → total silence**, and zero stats output (see F-v1-2) |
| +1 s | stats *claim* −10 ms convergence, but steady state satisfies `now = ts+offset+B` ⇒ true latency = **B + 2·skew** (2.3 s), hidden in sink backpressure — the bug is invisible to its own metric |

Real phone↔PC clocks routinely differ by seconds; either failure mode would have
appeared on first live cross-device test — as silence (mystifying) or as sync that
re-tuning "fixes" (silently defeating v1's entire purpose).

**Fix:** `err = now_us() - (ts - offset + b_us)`. Verified post-fix on the shipped
file: skew 0 / −1 s / +1 s / −30 s / +30 s all converge identically
(avg ≈ −10 ms, fills=2, drops=0) — behavior is now skew-invariant, as designed.

## F-v1-2 — MEDIUM: all-drop failure mode was mute (FIXED here)

The drop path `continue`d **before** the stats print, so a receiver dropping 100%
of packets (exactly the F-v1-1 silence case) produced no output at all. Stats now
print before drop/fill handling; a fully-dropping receiver reports itself.

## F-v1-3 — MINOR: silence-fill frame alignment hardcoded stereo (FIXED here)

Fill bytes were aligned `// 4 * 4` (2 ch × 16-bit). A non-stereo mix format
(mono, 5.1) would mis-align fills and rotate channels. Now `// (ch*2) * (ch*2)`.

## F-v1-4 — HIGH, analytic (server; NOT fixed here — builder's pen)

`ClientConn.Send` performs a **blocking** `net.Write` inside `lock(clients)` on
the WASAPI capture callback, with no `SendTimeout`. A ghost client whose TCP send
buffer fills (phone WiFi-sleep — the F4 scenario) blocks the broadcast loop for
**all** clients until TCP retransmission gives up (minutes), and stalls the
capture callback itself. This quietly contradicts the F3 disposition's "a ghost
no longer BLOCKS other receivers": it no longer blocks *accepting* them, but it
can freeze *serving* them. Cheap fix: `client.Client.SendTimeout = 2000;` (write
then throws, `Failed` is set, ghost is culled). Decidable test on the Windows
box: connect a client that never reads, play audio, watch the other client stall.
Not fixed here — server code is the builder's pen and I cannot run it on this box.

## F-v1-5 — hypothesis, flagged not asserted (server-side, unrunnable here)

WASAPI loopback capture delivers no data while nothing renders. A movie paused
longer than the receiver's 10 s socket timeout may produce reconnect churn
(connect → handshake → 10 s starve → die → reconnect) for the duration of the
pause. Decidable on the Windows box: pause > 10 s, watch server logs. If real,
candidate fixes: server-side keepalive/heartbeat frames (len=0), or receiver
timeout tied to expected frame cadence.

## Nits

- `Program.cs` header comment says `capture_ts_us(u64)`; spec table and both
  implementations use i64. Label drift only.
- Spec says ping "repeat up to 8×"; receiver does 5; server accepts up to 32
  messages. Consistent in effect, drifting in prose.
- `seq` is sent and parsed but never checked. TCP makes gaps impossible
  intra-connection, so today it is dead weight; a discontinuity warning after
  reconnect would make it earn its 4 bytes.

## Substrate caveat

Reviewer and authors are same model family (reviewer fable-5; the v1 builder
session's substrate not confirmed on my side). Same-family agreement is weaker
than cross-family; shared blind spots would look like consensus.

## Honest residual

The C# server was **read, not executed** (no Windows box on this side): F-v1-4 is
analytic, F-v1-5 is a hypothesis; both come with decidable tests for someone at
the real server. My fake server exercises the receiver against the **spec**, not
against the real server's timing under real WiFi — the phone session's pending
live test remains the decisive leg, and is only meaningful **with the sign fix**
(pre-fix it would either mute or "converge" at the wrong latency). The mpv sink
path was not exercised in the skew runs (null sink only); mpv adds its own
~100 ms + device latency on top of B — constant, so v1's promise is unaffected,
but "latency = B" is nominal, not absolute.

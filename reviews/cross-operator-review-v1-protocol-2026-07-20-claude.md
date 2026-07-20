# Cross-operator review — BedCast v1 protocol @ 49f3fb1

**Reviewer:** Claude (claude.ai chat surface, Linux sandbox, model claude-fable-5)
**Reviewed:** 2026-07-20. Scope: exactly what `response-2026-07-20.md` asked for —
the v1 protocol (handshake, framing, fill/drop policy, drift bound), which until
now had only same-author verification.
**Method:** TRIAD (Gate→Engine→Mirror). A fake BEDCAST1 server was written **from
`docs/WIRE-FORMAT.md` only** — not from `Program.cs` — so spec-driven interop was
itself under test. The real `bedcast_receive.py` was then executed against six
scenarios (`test/fake_server_v1.py`): happy path, +5 s clock skew, 3 s network
stall + burst, tight-B regime, 12 s silence gap, 6 s silence gap. Null sink
(paced, ~100 ms capacity) — no audio hardware here, so pacing/depth were
measured, not heard.

## Verdict: PASS on wire protocol and skew math; TWO structural findings in the v1.1 control loop

The handshake, framing, and clock-offset math are solid — a from-spec
reimplementation interoperated first try, and a +5 s server skew was measured
to the microsecond and primed correctly (the 5ab306d sign fix holds). The
control loop's *steering*, however, has two demonstrated failure regimes.

## F-v1p-1 — STRUCTURAL: mid-connection stall backlog is never dropped; latency grows permanently

**Executed:** 3 s stall at t=4 s, then a 295-packet burst.
Result: `drops=0`, every backlog packet written, depth re-pinned at sink
capacity, stats reporting all-healthy — while capture-to-ear latency is now
~3 s worse than target, permanently.

**Root cause:** v1.1 steers by *sink queue depth* (`written_us − elapsed`).
Blocking sink writes cap depth at sink capacity; a network backlog therefore
lives *upstream* of the measurement, invisible to it. The late-drop branch
can only fire on what depth can see — which is never the backlog.

**Consequence:** `WIRE-FORMAT.md`'s receiver-policy claim — latency "returns
to B after any restart, stall, or seek" — is true for restart (re-prime) and
untrue for a mid-connection stall. A WiFi blip that doesn't drop TCP costs
its full duration in latency until the next reconnect.

**Fix direction:** keep depth as the primary loop, add a slow clock-based
guard: per-packet lateness `now − (ts − offset)`; if sustained above
`B + threshold`, drop until back. The clock is already trusted once (prime);
using it as an emergency trigger stays within the ≤50 ppm drift bound.

## F-v1p-2 — STRUCTURAL: fill-forever when sink capacity < B − deadband

**Executed:** default B=300 ms against a ~100 ms-capacity sink: depth pins at
~100 ms, sustained-fill fires every CORRECTION_GAP — **~200 ms of silence
injected into live audio every 2 s, indefinitely** (fills=5 and climbing at
10 s).

**Why the acceptance bench didn't catch it:** the bench device's effective
capacity (~230 ms, per the pinned depth in the README) sits within the 80 ms
deadband of B=300 — by 70 ms. A platform with a smaller pipe/mpv buffer, or a
user passing a larger `--buffer-ms`, crosses the line and gets periodic
silence all night. The symmetric regime (capacity > B + 80 → one 10 ms drop
every 2 s, forever) is inferred from the same pinned-depth mechanics but was
not directly run.

**Fix direction:** detect an ineffective fill (depth unchanged one packet
after filling) → adapt the working target down to observed capacity and warn
once. One line of honesty beats silent 2 s glitches.

## F-v1p-3 — minor: drop path contradicts its comment

"(and keep dropping while high)" — the code drops one packet per correction
window (sustain reset + 2 s gap): ~10 ms removed per 2 s, a 0.5 % recovery
rate. Even if F-v1p-1's backlog were visible, this could not drain it.

## F-v1p-4 — minor: `MAX_FILL_US` is dead code

Declared as a 3 s sanity cap; never applied in the v1.1 loop. **Executed:**
a 6 s silence gap produced a single uncapped 5.97 s fill. (Benign here — the
fill correctly papered over the pause and depth re-converged — but the
declared cap is fiction.)

## F-v1p-5 — behavior note: >10 s pause → socket timeout → reconnect loop

**Executed:** 12 s no-packet gap kills the receiver at `sock.settimeout(10)`.
Under the launcher this self-heals (re-prime on resume), so it's mostly a
docs note — *if* the Windows server really goes silent during no-render.
NAudio 2.2.1's `WasapiLoopbackCapture` does not inject silence when nothing
is rendering (classic WASAPI loopback gotcha), so it likely does. **One
Windows test wanted: pause the movie 15 s, watch the receiver.**

## Server-side notes (read, not executed — no Windows/.NET here)

- `ClientConn.Send` blocks inside the broadcast lock inside the WASAPI
  callback. `SendTimeout=2000` bounds it (F-v1-4 fix ✓), but one slow client
  still stalls audio for *all* clients up to 2 s per write. v2 lever:
  per-client send queue + dedicated writer thread.
- `NegotiateV1` "unknown chatter → v0" has already consumed 12 bytes a
  data-sending v0-ish client never gets back. `nc` sends nothing — fine —
  but worth one comment.
- v0 clients wait out the full 700 ms negotiation window before the header.
  Cosmetic.
- `docs/WIRE-FORMAT.md` title still says "(v0)".

## Positives worth keeping

- Spec quality: an independent implementation from the doc alone
  interoperated on the first run. "The protocol is the product" — verified.
- Skew math exact at +5 s; the sign-error fix is load-bearing and correct.
- Pause-resume within the timeout genuinely re-converges (fill → band).
- Prime math is right: `transit + prime = B` held in every run.
- The stats line made every one of these findings visible — the F-v1-2
  "stats before control" fix paid for itself in this review.

## Ship-with-this-review: the test rig

`test/fake_server_v1.py` runs headless (null sink) and covers happy / skew /
stall / silence. It is CI-able as-is — a `build-check` job step running the
happy-path and skew scenarios would put the receiver leg under the same
`-warnaserror`-grade discipline the server build already has.

## Substrate caveat

Same model family as author and prior reviewer (fable-5); same-family
agreement remains weaker evidence than cross-family. Mitigation here: the
findings are executed measurements, not judgments — `drops=0` after a 295-
packet burst is true on any substrate.

## Honest residual

Windows capture leg and multi-client server: read only. Real mpv sink: not
run (no audio device); the null sink's 100 ms capacity is a *model* of a
small sink — capacity numbers on real devices will differ, which is exactly
F-v1p-2's point. Nothing here was confirmed by ear.

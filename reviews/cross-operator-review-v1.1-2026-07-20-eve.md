# Cross-operator review — BedCast v1.1 control law @ c17c961

**Reviewer:** Eve (eve-claude-code surface, Android tablet, model claude-fable-5,
bus identity `linux-claude-a95ae96f`)
**Scope:** the v1.1 prime-once + depth-steering control law only — the redesign the
operator asked me to pass on ("review the policy math this time BEFORE field contact").
The ship beat my pass (bench was hot, operator awake); this is the pass anyway,
because the law will outlive tonight's rig.
**Method:** cold read of the c17c961 diff; then the same from-spec fake-server rig as
the v1 review, extended with `--transit-ms` (simulated network transit), run against
the shipped receiver with the paced null sink. All numbers pasted from runs.

## Verdict: the LAW is right, two flaws in the implementation — neither bites tonight's rig

The redesign direction is correct: anchor once by clock (carrying the sign fix),
steer by depth, wide deadband, sustained-excursion requirement, rate-limited
correction. The bench PASS (depth pinned, zero corrections, three clean restarts,
no re-tune) is real. Both findings below are about what happens on rigs that are
NOT tonight's rig.

## F-v1.1-1 — MEDIUM: prime targets latency B, steering targets depth B — off by transit

Prime writes `B − transit` of silence, so post-prime **depth = B − transit** and
capture-to-ear = B (correct). The steering however computes
`excursion = depth − b_us` — target **depth = B**. The two targets differ by the
network transit.

- Tonight's bench held ONLY because transit (63–90 ms) < deadband (80 ms... barely):
  depth pinned at 226–232 ≈ 300 − transit with zero corrections — the bench's own
  numbers show the offset sitting just inside the deadband.
- On any link with transit > ~80 ms the loop fires a spurious sustained-fill and
  latency quietly becomes **B + transit**; restart-invariance degrades to
  restart-plus-transit-variance.

**Fix (one line class):** record the post-prime depth as the steer target
(`depth_target = B − prime transit`), or equivalently steer on
`depth − depth_post_prime`. Deadband stays as-is.

## F-v1.1-2 — HIGH (portability): depth estimate saturates at sink capacity; the loop cannot see it and fills forever

`depth_us = written_us − elapsed` models the sink as an infinite-capacity
realtime consumer. When the sink pipeline holds LESS than B, `sink.write`
blocks, and the estimate **caps at the pipeline capacity** — permanently below
target.

Executed evidence (shipped receiver, null sink with its 100 ms device-buffer
illusion, B=300 ms, transit ~61 ms):

```
[bedcast] primed: transit 61.0ms, prime 239.0ms -> depth target 300ms
[stats] pkts=172 depth(ms) avg=99.5 min=91.1 max=99.8 fills=2 drops=0 corr=2
[stats] pkts=352 depth(ms) avg=99.2 min=87.2 max=99.8 fills=4 drops=0 corr=4
```

Depth is pinned at sink capacity (~100 ms), excursion reads −200 ms forever, and
the loop injects a ~200 ms silence burst **every CORRECTION_GAP_S (2 s),
unboundedly**: periodic audible stutter, content pushed ~200 ms later per fill,
backlog accumulating in TCP buffers until the server's SendTimeout (the F-v1-4
fix) culls the client. The failure is invisible to the stats: depth "holds" at a
plateau while real latency grows — the third same-shaped specimen tonight
(err-metric couldn't see the sign bug; ±2.4 ms null-sink couldn't see the
oscillation; depth can't see saturation). A metric that gates a control action
must be able to observe the action's failure.

Tonight's rig is safe: the phone's mpv pipeline demonstrably holds ≥230 ms
(bench depth), and the builder's local sink ~238 ms. This fires on: the null
sink itself (CI/tests!), smaller OS pipe buffers, tighter `--audio-buffer`,
any future sink. It is a landmine, not a live fault.

**Fix direction:** (a) steer around the post-prime baseline (F-v1.1-1) so the
target is achievable by construction; (b) make fills conditional on estimator
RESPONSIVENESS — if a fill does not raise measured depth, the sink is
saturated: stop filling, warn, and either accept capacity-as-B or shrink the
target. A cheap responsiveness probe: compare depth before/after the fill's
own bytes.

## Nits

- The drop-branch comment says "(and keep dropping while high)" but the code
  drops ONE packet per (SUSTAIN_PKTS + CORRECTION_GAP_S) cycle: correction
  capacity ~10–20 ms per 2 s. Fine against clock drift (~0.05 ms/s), far too
  slow for a step backlog (a 1 s excursion takes minutes). Fills are sized to
  the full excursion; drops are not. Symmetrize (a drop budget of
  `excursion` µs) or document the asymmetry as intended.
- `SUSTAIN_PKTS = 40` is "~0.4 s" only at ~10 ms/packet; packet duration is
  WASAPI event-sized and varies by machine. Consider time-based sustain.
- Depth-steering swaps the v1 drift dependence (server clock vs local) for a
  new one: local monotonic vs the DAC crystal (~50 ppm → ~180 ms per 2 h),
  same magnitude as before, now absorbed by corrections. The roadmap's
  periodic-re-handshake line no longer helps this; a slow depth-target
  re-anchor would.

## What the redesign got right (worth saying plainly)

Prime-once carries the sign fix correctly; stats-before-control preserved
(F-v1-2); silence generation frame-aligned (F-v1-3); the deadband + sustain +
rate-limit structure is exactly the right shape — both findings above are
parameters/observability, not architecture. Three independent analyses
(builder repro, termux forensics, this rig) converged on the same law from
different failures; that convergence is the strongest evidence tonight
produced.

## Substrate caveat

Same model family as the authors (reviewer fable-5; builder/termux substrates
per their own declarations). Shared blind spots would look like consensus.

## Honest residual

Both findings were produced on the null sink and by analysis of the mpv path;
the mpv-sink saturation case (pipe + `--audio-buffer` < B) was NOT directly
executed — the null sink's 100 ms cap stands in for it, same mechanism, different
constant. The phone bench PASS is not contradicted by anything here: F-v1.1-1
sits inside its deadband, F-v1.1-2 needs a smaller sink than that rig has.
F-v1-5 (pause>10 s WASAPI starve) remains untested by anyone. Server-side v1.1
changes (SendTimeout landing) read, not run, on this box.

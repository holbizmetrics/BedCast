# Response — cross-family + web-Fable5 reviews (2026-07-20, batch landed same night)

Reviewers tonight, in order: Eve ×3 (Claude/FVPA lineage) · termux field bench ·
ChatGPT 5.6 Sol Light (cross-FAMILY, first non-Claude) · Claude-web Fable5 (executed
TRIAD, from-spec fake server) · Sol 5.6 Extra High (in flight — findings so far folded).

## Fixed and verified this batch

| Finding (source) | Fix | Verification |
|---|---|---|
| Linux server v0-only vs v1-default launcher reconnect-loops forever (Sol, CRITICAL) | receiver exits 3 on protocol mismatch; launcher falls back to v0 for the session | fake v0-only server → exit 3 (unpiped), fallback message |
| Backlog drained 1 pkt / 2 s ≈ 200 s per stalled second (Sol, HIGH) | drain episodes: consecutive drops, 2 s discontinuity cap, episodes rate-limited | code path exercised in stall runs |
| WIRE-FORMAT stale: old v1.0 law + wrong-sign offset formula (Sol, doc-CRITICAL) | spec rewritten to v1.1 (prime/steer/guards), sign + conversion rule explicit, handshake counts normative | doc diff |
| Steering targeted `b_us` not post-prime depth — off by transit (Eve F-v1.1-1) | `depth_target_us` = post-prime depth | mpv rig: depth flat 226/227 ms, corr=0 |
| Fill-storm when sink capacity < B (Eve F-v1.1-2 = web-Fable5 finding 2, independent ×2) | fill must raise measured depth ≥50% of request, else `fill_blocked` + one WARN | Eve's exact repro rig: 1 fill, 1 WARN, no storm |
| `MAX_FILL_US` dead code — 6 s gap → one 5.97 s fill (web-Fable5) | fills capped | code |
| Stall backlog invisible to depth steering, stale content all played (web-Fable5 F1, executed) | loop-top staleness guard (250 ms over target) + re-prime after drain — stall gets restart semantics; also covers Sol's initial-late-packet case | guard drops ≈ burst size (3 runs); **post-stall depth telemetry still noisy on both rigs — see Owed** |
| Silence-type gap (pause < socket timeout) resumed with stall-length fill (Sol xhigh) | packet-gap timer (1 s) → re-prime on resume | coded; deterministic verification owed |
| README Termux line omitted `python` though v1 is default (Sol xhigh) | added | diff |
| `server-linux.sh` not executable (Sol xhigh) | exec bit set in index | `git ls-files -s` = 100755 |

## Owed (next session, in Sol's suggested order)

1. **Deterministic virtual-time controller harness** — tonight's stall runs demonstrate
   guard+re-prime firing, but every run's tail was confounded by rig lifetime artifacts;
   post-stall depth convergence is COVED-NOT-VERIFIED. `tests/fake_server_v1.py`
   (web-Fable5's from-spec rig, landed) is the seed; CI wiring blocked on the repo's
   GitHub-Actions startup_failure (server-side, canary committed).
2. Per-client bounded queues + writer threads (Sol HIGH; extends Eve F-v1-4 — capture
   callback must never do network I/O).
3. Remaining robustness table items (validation, shutdown, sink-failure handling).
4. Security items (bind address, client cap, rate limits) + license (operator decision).
5. Acoustic re-measurement of the latency claim (mic/loopback across restarts).

## Positions taken (not silently)

- "PTS-scheduled jitter buffer" wording: adopted Sol's "timestamp-anchored, pipe-paced
  playback controller" in spec; README architecture diagram label pending.
- SmartScreen guidance stays but source-build is now listed first-equal; checksums with
  the next release.
- Encryption remains a non-goal (LAN trust model), per all reviewers' acceptance.

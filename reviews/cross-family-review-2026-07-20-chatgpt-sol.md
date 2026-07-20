# Cross-FAMILY review — BedCast @ 419d2da (ChatGPT 5.6 "Sol", 2026-07-20)

**Provenance:** first non-Claude review of this repo — the cross-family rung. Operator-pasted
verbatim from ChatGPT 5.6 (Sol Light), which reviewed a source archive the operator supplied
(the archive-hygiene findings refer to that archive, not the GitHub release zip). They could
not compile the C# (no .NET SDK in their sandbox) — C# findings are static-analysis findings.
Dispositions: `reviews/response-crossfamily-2026-07-20.md`.

---

Overall: BedCast is a strong, unusually disciplined prototype with a genuinely good product insight. The v1.1 direction is sensible, the trust boundary is stated honestly, and the earlier reviews demonstrably caused real improvements.

I would not call it production-ready yet. I found two concrete architectural defects, one cross-platform compatibility failure, and several claims/documentation mismatches.

## Highest-priority findings

| Priority | Finding | Why it matters | Recommended change |
| -------- | ------- | -------------- | ------------------ |
| Critical | The default receiver cannot use the Linux server | `server-linux.sh` speaks only BEDCAST0, while the launcher now defaults to the Python BEDCAST1 handshake. The Python receiver treats the Linux server's immediate header/PCM as a malformed handshake and reconnects indefinitely. The claimed automatic fallback does not happen. | Either add BEDCAST1 to the Linux server, or make the launcher retry once using v0 when the Python receiver reports negotiation failure. Until then, document `BEDCAST_V0=1` prominently for Linux servers. |
| High | One slow client can still disrupt every receiver and the capture callback | Every `ClientConn.Send()` occurs synchronously while holding `lock(clients)` inside the WASAPI callback. `SendTimeout=2000` limits the freeze but does not isolate it: one client can still stall all audio for up to two seconds, potentially repeatedly. | Give each client a bounded queue and dedicated writer task/thread. The capture callback should only enqueue or drop — not perform network I/O. Evict clients whose queue remains full. |
| High | The v1.1 controller is only a weak approximation of a fixed-latency jitter buffer | `written_us − elapsed` estimates bytes handed to mpv, not capture-to-ear latency or necessarily even mpv's internal queue. Pipe buffering, mpv buffering and the device buffer remain outside the measurement. | Introduce a receiver-side bounded PCM queue paced from timestamps, or obtain playback position from an audio API rather than treating pipe writes as playback. State the guarantee as "restart-stable in tested configuration" until measured acoustically. |
| High | Backlog correction is extremely slow | When depth is high, it drops only one packet and then enforces a two-second correction gap. If packets are roughly 10 ms, removing a 1-second backlog could take roughly 200 seconds. | When above the upper threshold, drop consecutive packets until the estimated target depth is reached, with a maximum discontinuity/correction bound. Rate-limit correction episodes, not individual packet drops. |
| High | There are effectively no automated behavioral tests | CI builds C# and syntax-checks shell, but does not exercise handshake, framing, clock skew, malformed input, restart invariance, slow clients, or controller convergence. Earlier bugs were exactly in these areas. | Commit the fake server/test rig and run it in CI. Add protocol golden vectors and deterministic virtual-time controller tests. |

## Correctness and robustness

| Area | Current issue | Improvement |
| ---- | ------------- | ----------- |
| Initial late packet | Priming adds silence when early, but if the first packet is already older than `B`, it writes it anyway. | Drop initial packets until their timestamp falls within the startup window, then prime. |
| Sequence numbers | `seq` is transmitted and parsed but never checked. | Report gaps, repeats and resets; expose them in statistics. |
| Stream validation | Receiver checks magic and maximum length but not `bits == 16`, valid channel count, frame alignment, zero-length policy, or realistic sample rate. | Reject unsupported/invalid headers and payload lengths explicitly. |
| Server negotiation | Unknown or partial v1 chatter is silently reinterpreted as a v0 connection. | Close invalid negotiation attempts. Reserve timeout fallback only for genuinely silent legacy clients. |
| Capture formats | Windows supports only float32 and PCM16. Other valid WASAPI mix formats fail at runtime. | Convert through an established sample provider/converter or clearly enumerate supported formats during startup. |
| Silent playback | A pause longer than the receiver's ten-second socket timeout causes reconnect cycling. | Use a protocol heartbeat, or make stream-idle timeout configurable and much longer. |
| Shutdown | Server has no graceful cancellation or `Ctrl+C` cleanup path. | Add cancellation, stop capture/listener, close clients, and produce a clean exit code. |
| CLI validation | Invalid ports, missing values, negative duration/buffer and ambiguous `--smoke-test` output paths cause exceptions or strange behaviour. | Use structured parsing and bounded validation with friendly errors. |
| Sink failure | Broken mpv pipes, missing executables and a process that does not exit cleanly are not handled robustly. | Catch `BrokenPipeError`, check child exit status, terminate on timeout and return non-zero on receiver failure. |

## Documentation claims that should be corrected

The most important discrepancy is that WIRE-FORMAT.md still describes the old per-packet wall-clock fill/drop algorithm, while the actual receiver uses prime-once plus queue-depth steering. Its offset expression also uses the previously incorrect sign. The protocol specification and implementation have therefore diverged.

Other claim adjustments:

| Current wording | Problem | Better wording |
| --------------- | ------- | -------------- |
| "PTS-scheduled jitter buffer" | No PTS-aware playback API or explicit jitter-buffer queue exists; audio is written to mpv stdin. | "Timestamp-anchored, pipe-paced playback controller." |
| "Latency returns to B after any restart, stall, or seek" | That is stronger than the implementation and evidence support. | "In the tested phone/PC configuration, restart-induced offset remained stable without retuning." |
| "Capture-to-ear latency" | The measured depth excludes mpv and audio-device latency. | Distinguish protocol queue depth, sink latency and actual acoustic capture-to-ear latency. |
| "auto-reconnect" remains in the v2 roadmap | Auto-reconnect is already implemented. | Remove it from v2 or qualify the missing cases. |
| Handshake count | Source comment, documentation and implementation say 8, 32 and 5 rounds. | Define one normative maximum and one default. |
| Linux compatibility | README presents the Linux server and default receiver as composable. | State that Linux currently requires v0, until automatic fallback is implemented. |

README.md is otherwise one of the project's strongest parts. The honest status labels and explicit n=1 qualification should stay.

## Security and network exposure

"No authentication on a trusted LAN" is acceptable for a personal prototype, but binding to `0.0.0.0` makes accidental exposure easy.

| Improvement | Value |
| ----------- | ----- |
| Add `--bind ADDRESS`, defaulting conservatively or clearly displaying every listening interface | Reduces unintended exposure |
| Optional pre-shared token in the negotiation | Stops casual LAN eavesdropping and resource abuse |
| Maximum simultaneous clients | Prevents unlimited thread/socket creation |
| Per-IP connection and handshake rate limits | Protects against trivial LAN denial of service |
| Optional Windows Firewall setup guidance | Helps nontechnical users understand the exposure |
| Do not recommend bypassing SmartScreen as the normal path | Prefer checksum/signature verification or source build instructions |

Encryption can remain a non-goal for now, but authentication and resource bounds would be relatively cheap.

## Product improvements worth considering

| Horizon | Improvement |
| ------- | ----------- |
| Near-term | A `--diagnose` command showing device, actual format, bind address, client count, RTT, estimated depth, drops and sink failures |
| Near-term | Automatic server discovery via mDNS, avoiding manual IP entry |
| Near-term | A receiver command that reports "server supports only v0 — falling back" rather than looping |
| Near-term | Configurable port in the launcher via argument or environment variable |
| Medium-term | Native Android receiver with proper AudioTrack buffer-position measurements |
| Medium-term | Per-client buffer settings and telemetry |
| Medium-term | Optional Opus mode for poor Wi-Fi, while keeping raw PCM as the simple default |
| Later | Windows tray app, device selector, silent-PC routing assistance and start-on-login |
| Later | macOS capture shim and a genuinely v1-capable Linux server |

## Repository and release hygiene

The supplied archive contains `.git`, `bin`, `obj`, PDBs and compiled dependencies. That is useful as a forensic snapshot but poor as a source distribution. Source archives should exclude build products and repository internals; release archives should contain only the published executable, receiver, docs and license.

Also add:

| Missing item | Recommendation |
| ------------ | -------------- |
| License | Add one before encouraging contributors or redistribution |
| Version reporting | Add `--version` and include protocol compatibility |
| Dependency automation | Enable Dependabot or equivalent |
| Reproducibility | Pin GitHub Actions by commit SHA for stronger supply-chain hygiene |
| Test workflow | Run Python unit/integration tests on Linux and C# protocol/server tests on Windows |
| Coverage | Track protocol/parser/controller coverage rather than only compilation |
| Changelog | Record compatibility-breaking protocol or controller changes |

## Suggested implementation order

| Order | Deliverable | Completion criterion |
| ----: | ----------- | -------------------- |
| 1 | Fix Linux/default-receiver incompatibility | Launcher automatically falls back to v0, verified in an integration test |
| 2 | Isolate clients with bounded queues | A non-reading client causes no audible or measured interruption for another client |
| 3 | Commit protocol/controller test harness | Clock skews, fragmentation, malformed frames, stalls and reconnects run in CI |
| 4 | Make the specification match v1.1 | Independent implementation can reproduce current behaviour from the document |
| 5 | Strengthen backlog/startup correction | Deterministic tests show bounded convergence after 0.1–5 s stalls |
| 6 | Improve validation, shutdown and observability | Failures produce actionable messages and meaningful exit codes |
| 7 | Reassess the latency claim acoustically | Repeated microphone/loopback measurements across restarts and Wi-Fi disturbances |

My verdict is: **excellent problem-driven prototype; promising architecture at the protocol boundary; not yet reliable enough to promise a true fixed capture-to-ear latency.** The Linux interoperability problem and synchronous client writes should be fixed before expanding the feature set.

I successfully syntax-checked the Python and both shell scripts. I could not compile the C# project in this environment because the .NET SDK is not installed, so the C# findings above are static-analysis findings rather than a fresh Windows execution.

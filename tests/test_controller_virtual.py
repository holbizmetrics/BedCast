#!/usr/bin/env python3
"""Virtual-time harness for the v1.1 Controller — the shipped control law
driven by a simulated clock, sink, and packet schedule. No sleeps, no sockets,
no mpv: a 60s scenario runs in milliseconds, identically every run.

Why this exists (2026-07-21): every real-time stall test the night before was
polluted by rig artifacts — and the fake TCP server itself under-delivers at
~65% real-time on Windows (time.sleep(0.01) -> 15.6ms timer granularity).
Virtual time removes the OS from the loop entirely; when a number is wrong
here, it is the controller. This closes the "stall/gap recovery CODED-NOT-
VERIFIED" asterisk from the v1.1 ship — or finds the bug.

What it verifies: the CONTROL LAW only. mpv pipe quirks and device latency are
outside it by construction — field tests remain the outer check.

Run:  python tests/test_controller_virtual.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "receiver"))
from bedcast_receive import Controller  # noqa: E402

RATE, CH = 48000, 2
BYTES_PER_US = RATE * CH * 2 / 1_000_000
PKT_US = 10_000
PKT_BYTES = int(PKT_US * BYTES_PER_US)
PAYLOAD = b"\x11\x22" * (PKT_BYTES // 2)


class VirtualClock:
    def __init__(self):
        self.t = 1_000_000_000  # arbitrary epoch, us

    def now(self):
        return self.t

    def advance_to(self, t):
        if t > self.t:
            self.t = t


class SinkModel:
    """A device that consumes in real time up to `capacity_us` of buffer.
    Writing past capacity BLOCKS (advances the virtual clock) — the same
    backpressure semantics as a real audio pipe. `rate_scale` models a sink
    crystal that runs slightly off nominal (DAC drift)."""

    def __init__(self, clock, capacity_us=300_000, rate_scale=1.0):
        self.clock = clock
        self.capacity_us = capacity_us
        self.rate_scale = rate_scale
        self.written_us = 0.0
        self.consumed_us = 0.0
        self.t_last = None
        self.fill_us = 0.0     # silence (all-zero) bytes seen
        self.audio_us = 0.0

    def _consume_to(self, t):
        if self.t_last is None:
            self.t_last = t
            return
        dt = (t - self.t_last) * self.rate_scale
        self.consumed_us = min(self.written_us, self.consumed_us + max(dt, 0))
        self.t_last = t

    def buffered_us(self):
        self._consume_to(self.clock.now())
        return self.written_us - self.consumed_us

    def write(self, data: bytes):
        us = len(data) / BYTES_PER_US
        if any(data[:16]):
            self.audio_us += us
        else:
            self.fill_us += us
        self._consume_to(self.clock.now())
        # blocking semantics: wait (advance clock) until the buffer has room
        over = (self.written_us + us) - (self.consumed_us + self.capacity_us)
        if over > 0:
            self.clock.advance_to(self.clock.now() + int(over / self.rate_scale))
            self._consume_to(self.clock.now())
        self.written_us += us


def schedule(total_s, skew_us=0, transit_us=1_000,
             stall=None, gap=None):
    """Yield (arrival_us, server_ts_us) for a 10ms-packet stream.
    stall=(at_s, dur_s): packets in the window arrive as a burst at the end
    (network backlog — content preserved).  gap=(at_s, dur_s): packets in the
    window never exist (render silence / pause)."""
    out = []
    t = 0
    while t < total_s * 1_000_000:
        ts_server = t + skew_us
        arr = t + transit_us
        if stall and stall[0] * 1e6 <= t < (stall[0] + stall[1]) * 1e6:
            arr = int((stall[0] + stall[1]) * 1e6) + transit_us
        if gap and gap[0] * 1e6 <= t < (gap[0] + gap[1]) * 1e6:
            t += PKT_US
            continue
        out.append((arr, ts_server))
        t += PKT_US
    return out


def run(sched, b_us=300_000, skew_us=0, capacity_us=600_000, rate_scale=1.0,
        log=False):
    clock = VirtualClock()
    base = clock.now()
    sink = SinkModel(clock, capacity_us, rate_scale)
    ctl = Controller(
        b_us=b_us, offset=skew_us, rate=RATE, ch=CH,
        now_fn=clock.now, write_fn=sink.write,
        log_fn=(lambda m: print("  " + m)) if log else None)
    depth_series = []  # (virtual_t_s, depth_us) after each packet
    for arr, ts in sched:
        clock.advance_to(base + arr)
        ctl.on_packet(base + ts, PAYLOAD)
        depth_series.append(((clock.now() - base) / 1e6, ctl.depth_us()))
    return ctl, sink, depth_series


def band_after(depth_series, t_from_s, target_us, tol_us):
    """True iff depth stays within target±tol from t_from_s to the end."""
    seen = False
    for t, d in depth_series:
        if t < t_from_s:
            continue
        seen = True
        if abs(d - target_us) > tol_us:
            return False, (t, d)
    return seen, None


def band_after_dyn(depth_series, t_from_s, ctl, tol_us):
    """Band check against the controller's OWN final depth target (it moves
    legitimately at re-prime: post-prime depth = B - transit, F-v1.1-1)."""
    return band_after(depth_series, t_from_s, ctl.depth_target_us, tol_us)


def main():
    failures = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               (" — " + str(detail)) if (detail and not cond) else ""))
        if not cond:
            failures.append(name)

    B = 300_000
    DEAD = Controller.DEADBAND_US
    TRANSIT = 1_000
    TARGET = B - TRANSIT  # post-prime depth target (F-v1.1-1)

    print("scenario: happy 30s")
    ctl, sink, ds = run(schedule(30))
    ok, bad = band_after(ds, 1.0, TARGET, DEAD)
    check("depth in band from t=1s", ok, bad)
    check("zero corrective actions", ctl.fills == 0 and ctl.drops == 0
          and ctl.corrections == 0 and ctl.reprimes == 0,
          (ctl.fills, ctl.drops, ctl.corrections, ctl.reprimes))

    for skew_s in (-30, 30):
        print("scenario: clock skew %+ds" % skew_s)
        ctl, sink, ds = run(schedule(20, skew_us=skew_s * 1_000_000),
                            skew_us=skew_s * 1_000_000)
        ok, bad = band_after(ds, 1.0, TARGET, DEAD)
        check("skew-invariant band", ok, bad)
        check("no actions under skew", ctl.drops == 0 and ctl.fills == 0,
              (ctl.fills, ctl.drops))

    print("scenario: 0.5s network stall (sub-guard: no re-prime by design)")
    # A stall shorter than STALE_GUARD leaves no stale packets: the backlog
    # lands in the pipeline and drains via episodes. Cooldown between episodes
    # makes recovery ~2 episodes (~3s) - measured behavior, documented.
    ctl, sink, ds = run(schedule(30, stall=(10.0, 0.5)))
    ok, bad = band_after_dyn(ds, 15.0, ctl, DEAD)
    check("no re-prime for sub-guard stall", ctl.reprimes == 0, ctl.reprimes)
    check("drain engaged", ctl.drops >= 5, ctl.drops)
    check("depth back in band by resume+4.5s", ok, bad)

    for stall_s in (3.0, 8.0):
        print("scenario: %.1fs network stall (backlog burst)" % stall_s)
        ctl, sink, ds = run(schedule(30, stall=(10.0, stall_s)))
        burst_pkts = int(stall_s * 1e6 / PKT_US)
        recover_by = 10.0 + stall_s + 2.0
        ok, bad = band_after_dyn(ds, recover_by, ctl, DEAD)
        check("re-primes after stall", ctl.reprimes >= 1, ctl.reprimes)
        check("stale backlog dropped (~burst)", burst_pkts * 0.6 <= ctl.drops
              <= burst_pkts + 60, (ctl.drops, burst_pkts))
        check("depth in band (vs re-primed target) within 2s of resume", ok, bad)
        check("silence bounded (<= 2.2x B total)", sink.fill_us <= 2.2 * B,
              sink.fill_us)

    print("scenario: 3s render-silence gap (pause)")
    ctl, sink, ds = run(schedule(30, gap=(10.0, 3.0)))
    ok, bad = band_after_dyn(ds, 15.0, ctl, DEAD)
    check("re-primes after gap", ctl.reprimes >= 1, ctl.reprimes)
    check("no mass drops on gap (nothing stale)", ctl.drops <= 5, ctl.drops)
    check("depth back in band after resume", ok, bad)
    check("gap not inserted as silence (fills bounded)",
          sink.fill_us <= 2.2 * B, sink.fill_us)

    print("scenario: sink capacity 100ms < B=300ms (saturation)")
    ctl, sink, ds = run(schedule(30), capacity_us=100_000)
    check("saturation detected (fill_blocked)", ctl.fill_blocked)
    check("exactly one fill attempt, no storm", ctl.fills == 1, ctl.fills)

    print("scenario: realistic DAC drift (-100ppm, 60s)")
    ctl, sink, ds = run(schedule(60), rate_scale=0.9999)
    ok, bad = band_after_dyn(ds, 1.0, ctl, DEAD)
    check("stable under realistic drift", ok, bad)
    check("no false corrections", ctl.corrections == 0, ctl.corrections)

    print("scenario: aggressive drift -0.3%, LARGE sink (blind-spot documentation)")
    # KNOWN LIMITATION (Eve's DAC-drift nit, quantified here): with a sink
    # buffer larger than the accumulating drift, written paces off ARRIVAL,
    # so controller depth cannot see consumption-rate drift - latency piles
    # up inside the sink invisibly. Real mpv (small pipe+buffer) blocks
    # writes, which throttles written and makes the law self-correct: next
    # scenario. This case ASSERTS the blindness so a future fix (slow target
    # re-anchor) has a failing test to flip.
    ctl, sink, ds = run(schedule(60), rate_scale=0.997, capacity_us=1_000_000)
    drifted_us = sink.buffered_us() - (ctl.depth_target_us)
    check("blindness documented: controller sees nothing", ctl.corrections == 0,
          ctl.corrections)
    check("drift accumulated in sink (~180ms +- 80)", 100_000 <= drifted_us <= 260_000,
          drifted_us)

    print("scenario: aggressive drift -0.3%, SMALL sink (real-mpv-like: self-corrects)")
    ctl, sink, ds = run(schedule(60), rate_scale=0.997, capacity_us=100_000)
    check("blocking sink exposes drift: drains engage", ctl.corrections >= 1,
          ctl.corrections)
    # Drop economy is sink-model-dependent (episodes re-fire as drift refills
    # the excursion; each carries deadband/2 of overhead) - bound loosely; the
    # load-bearing assertion is that correction ENGAGES and remains bounded.
    check("drops bounded", ctl.drops <= 200, ctl.drops)

    print()
    if failures:
        print("FAIL: %d case(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("PASS: all virtual-time controller scenarios green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Generator run planner.

Simulates battery SOC forward from now using the hourly PV and load forecast,
and if the SOC would fall below the floor, proposes one generator run:

* **when** – as late as possible (just-in-time), so solar is used first and the
  battery is at low SOC when charging starts (the Chargeverter then runs at
  full current for the whole run, which is where a Honda EU3000is is most
  fuel-efficient), but never inside configured quiet hours and never so late
  that the SOC dips under the floor;
* **how long** – only as much energy as is needed to stay above the floor
  until the sun refills the bank, capped at ``soc_target`` where LFP charging
  leaves the constant-current region and the generator would idle along.

Everything is advisory: the plan is exposed on the API/MQTT and your Home
Assistant automation decides what to do with it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .model import HourForecast

STEP_S = 900  # 15-minute simulation step


@dataclass
class PlannerParams:
    soc_floor: float = 30.0
    soc_target: float = 50.0
    soc_target_storm: float = 80.0
    storm_lookahead_h: float = 36.0
    soc_margin: float = 5.0
    generator_dc_kw: float = 2.4
    charge_efficiency: float = 0.94
    min_run_h: float = 1.0
    max_run_h: float = 8.0
    quiet_hours: list[tuple[int, int]] = field(default_factory=list)  # [(start_h, end_h)] local, end exclusive, wraps
    horizon_h: int = 72
    battery_kwh: float = 15.36


@dataclass
class Plan:
    needed: bool
    recommended_now: bool
    start_at: float | None
    end_at: float | None
    run_h: float
    energy_kwh: float
    start_soc: float | None
    end_soc: float | None
    min_soc_without: float
    min_soc_at: float | None
    reason: str
    baseline: list[tuple[int, float]]  # (ts, soc) without generator
    planned: list[tuple[int, float]]  # (ts, soc) with the run
    stop_recommended: bool = False
    next_deficit_at: float | None = None  # when another run will be needed after this one

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["baseline"] = [[t, round(s, 1)] for t, s in self.baseline]
        d["planned"] = [[t, round(s, 1)] for t, s in self.planned]
        return d


def _in_quiet(ts: float, quiet: list[tuple[int, int]]) -> bool:
    h = time.localtime(ts).tm_hour
    for a, b in quiet:
        if a <= b:
            if a <= h < b:
                return True
        elif h >= a or h < b:
            return True
    return False


def _net_kw_at(ts: float, hours: dict[int, HourForecast]) -> float:
    h = hours.get(int(ts // 3600) * 3600)
    if h is None:
        return 0.0
    return h.pv_kw - h.load_kw


def simulate(soc0: float, start: float, hours: dict[int, HourForecast], p: PlannerParams,
             gen_start: float | None = None, gen_end: float | None = None) -> list[tuple[int, float]]:
    """Return SOC trajectory at STEP_S resolution. Generator adds power during [gen_start, gen_end)."""
    soc = soc0
    out = [(int(start), soc)]
    t = start
    end = start + p.horizon_h * 3600
    while t < end:
        net = _net_kw_at(t, hours)
        if gen_start is not None and gen_end is not None and gen_start <= t < gen_end:
            net += p.generator_dc_kw * p.charge_efficiency
        if net > 0:
            net *= 0.97  # charge losses in battery
        else:
            net /= 0.95  # inverter/discharge losses
        soc += net * (STEP_S / 3600.0) / p.battery_kwh * 100.0
        soc = max(0.0, min(100.0, soc))
        t += STEP_S
        out.append((int(t), soc))
    return out


def _run_until_target(soc0: float, start: float, hours: dict[int, HourForecast], p: PlannerParams,
                      gen_start: float, end_soc: float) -> tuple[list[tuple[int, float]], float]:
    """Simulate with a generator run from gen_start until SOC reaches end_soc (or max_run_h)."""
    # find gen_end by stepping
    soc = soc0
    t = start
    gen_end = None
    traj = [(int(start), soc)]
    horizon = start + p.horizon_h * 3600
    while t < horizon:
        net = _net_kw_at(t, hours)
        running = gen_start <= t and (gen_end is None)
        if running:
            net += p.generator_dc_kw * p.charge_efficiency
        net = net * 0.97 if net > 0 else net / 0.95
        soc = max(0.0, min(100.0, soc + net * (STEP_S / 3600.0) / p.battery_kwh * 100.0))
        t += STEP_S
        traj.append((int(t), soc))
        if running and (soc >= end_soc or t - gen_start >= p.max_run_h * 3600):
            gen_end = t
    if gen_end is None:
        gen_end = min(horizon, gen_start + p.max_run_h * 3600)
    return traj, gen_end


def _end_target(p: PlannerParams, floor: float, needed_soc: float, drawdown_window_ok: bool) -> float:
    """Normal runs stop at ``soc_target``. If the deficit after the run cannot be
    covered by solar within ``storm_lookahead_h`` (another run would follow
    soon), allow charging up to ``soc_target_storm`` so one longer run replaces
    several short ones."""
    cap = p.soc_target if drawdown_window_ok else max(p.soc_target, p.soc_target_storm)
    return min(cap, needed_soc)


def _needs_more_soon(baseline: list[tuple[int, float]], t_from: float, floor: float, soc_gain_cap: float,
                     lookahead_h: float) -> bool:
    """Would the bank cross the floor again within lookahead even after a run to soc_gain_cap?"""
    soc_ref = next((s for t, s in baseline if t >= t_from), None)
    if soc_ref is None:
        return False
    for t, s in baseline:
        if t_from < t <= t_from + lookahead_h * 3600:
            if soc_gain_cap - (soc_ref - s) < floor:
                return True
    return False


def plan_generator(soc_now: float, now: float, forecast: list[HourForecast], p: PlannerParams,
                   generator_running: bool = False) -> Plan:
    hours = {h.ts: h for h in forecast}
    floor = p.soc_floor + p.soc_margin
    baseline = simulate(soc_now, now, hours, p)
    min_soc, min_at = min(((s, t) for t, s in baseline), key=lambda x: x[0])

    if generator_running:
        # advise when to stop: once we've banked enough to cover the horizon, or at target
        drawdown_after = 0.0
        # energy needed from now: largest drawdown of the baseline relative to now
        for t, s in baseline:
            drawdown_after = max(drawdown_after, soc_now - s)
        storm = _needs_more_soon(baseline, now, floor, p.soc_target, p.storm_lookahead_h)
        end_soc = max(_end_target(p, floor, floor + drawdown_after, not storm), min(soc_now + 1.0, 100.0))
        stop = soc_now >= end_soc - 0.5
        traj, gen_end = _run_until_target(soc_now, now, hours, p, now, end_soc)
        return Plan(needed=True, recommended_now=not stop, start_at=now, end_at=gen_end,
                    run_h=round((gen_end - now) / 3600.0, 2), energy_kwh=round((end_soc - soc_now) / 100.0 * p.battery_kwh, 1),
                    start_soc=soc_now, end_soc=round(end_soc, 1), min_soc_without=round(min_soc, 1), min_soc_at=min_at,
                    reason=(f"Generator is running. Stop at about {end_soc:.0f} % SOC"
                            + (" (reached)." if stop else f", enough to stay above {p.soc_floor:.0f} % until solar takes over.")),
                    baseline=baseline, planned=traj, stop_recommended=stop)

    if min_soc >= floor:
        return Plan(needed=False, recommended_now=False, start_at=None, end_at=None, run_h=0.0, energy_kwh=0.0,
                    start_soc=None, end_soc=None, min_soc_without=round(min_soc, 1), min_soc_at=min_at,
                    reason=f"No generator needed in the next {p.horizon_h} h: forecast SOC bottoms out at {min_soc:.0f} % "
                           f"around {time.strftime('%a %H:%M', time.localtime(min_at))}.",
                    baseline=baseline, planned=baseline)

    # When does the baseline first cross the floor?
    t_cross = next(t for t, s in baseline if s < floor)
    # Try start times from just before the crossing backwards to now; pick the latest feasible one.
    best: tuple[float, list, float, float] | None = None
    t_start = (t_cross // STEP_S) * STEP_S
    while t_start >= now:
        if not _in_quiet(t_start, p.quiet_hours):
            soc_at_start = next((s for t, s in baseline if t >= t_start), soc_now)
            # energy needed: drawdown after the start, relative to SOC at start
            draw = max((soc_at_start - s) for t, s in baseline if t >= t_start)
            storm = _needs_more_soon(baseline, t_start, floor, p.soc_target, p.storm_lookahead_h)
            end_soc = max(soc_at_start + 1.0, _end_target(p, floor, floor + draw, not storm))
            # enforce minimum run length by raising end_soc if necessary
            min_gain = p.min_run_h * p.generator_dc_kw * p.charge_efficiency * 0.97 / p.battery_kwh * 100.0
            end_soc = min(100.0, max(end_soc, soc_at_start + min_gain))
            traj, gen_end = _run_until_target(soc_now, now, hours, p, t_start, end_soc)
            ok_floor = min(s for t, s in traj if t <= gen_end) >= p.soc_floor - 0.01
            if ok_floor and not any(_in_quiet(t, p.quiet_hours) for t in range(int(t_start), int(gen_end), STEP_S)):
                best = (t_start, traj, gen_end, end_soc)
                break
        t_start -= STEP_S
    if best is None:
        # nothing fully satisfies the constraints: start now
        soc_at_start = soc_now
        draw = max((soc_now - s) for t, s in baseline)
        storm = _needs_more_soon(baseline, now, floor, p.soc_target, p.storm_lookahead_h)
        end_soc = max(soc_now + 1.0, _end_target(p, floor, floor + draw, not storm))
        traj, gen_end = _run_until_target(soc_now, now, hours, p, now, end_soc)
        best = (now, traj, gen_end, end_soc)
        reason_prefix = "Constraints (quiet hours / floor) cannot all be met; start as soon as possible. "
    else:
        reason_prefix = ""
    t_start, traj, gen_end, end_soc = best
    start_soc = next((s for t, s in baseline if t >= t_start), soc_now)
    run_h = (gen_end - t_start) / 3600.0
    lead = t_start - now
    recommended_now = lead <= 10 * 60
    when = "now" if recommended_now else time.strftime("%a %H:%M", time.localtime(t_start))
    next_deficit = next((t for t, s in traj if t > gen_end and s < floor), None)
    storm_note = f" (charging past the usual {p.soc_target:.0f} % because no solar surplus is coming soon)" if end_soc > p.soc_target + 0.5 else ""
    tail = (f"that covers the deficit until solar takes over{storm_note}"
            if next_deficit is None else
            f"another run will be needed around {time.strftime('%a %H:%M', time.localtime(next_deficit))}{storm_note}")
    reason = (f"{reason_prefix}Without the generator the bank would drop to {min_soc:.0f} % around "
              f"{time.strftime('%a %H:%M', time.localtime(min_at))}. Start {when} at about {start_soc:.0f} % SOC and run "
              f"{run_h:.1f} h to {end_soc:.0f} %; {tail}, keeping the Chargeverter at full current.")
    return Plan(needed=True, recommended_now=recommended_now, start_at=t_start, end_at=gen_end, run_h=round(run_h, 2),
                energy_kwh=round(run_h * p.generator_dc_kw, 1), start_soc=round(start_soc, 1), end_soc=round(end_soc, 1),
                min_soc_without=round(min_soc, 1), min_soc_at=min_at, reason=reason, baseline=baseline, planned=traj,
                next_deficit_at=next_deficit)


def parse_quiet_hours(items: list[str]) -> list[tuple[int, int]]:
    out = []
    for s in items or []:
        a, b = s.split("-")
        out.append((int(a.split(":")[0]), int(b.split(":")[0])))
    return out

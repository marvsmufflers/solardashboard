"""Generator run planner.

The generator's load is fixed by the Chargeverter (constant current), so every
hour it runs costs about the same fuel and engine wear. The planner therefore
minimises, over a 15-minute simulation of the battery using the solar and
load forecast:

* **engine hours**, including hours that would only replace solar the
  battery could otherwise have stored (charging so high that sun is spilled
  later is wasted fuel);
* **starts** — each cold start and warm-up costs fuel and wear, so one longer
  run beats two short ones (``start_cost_h`` puts a start in engine hours);

subject to: the bank never planned below ``soc_floor``; runs at least
``min_run_h`` (an engine that never gets fully hot wears faster) and at most
``max_run_h`` (one tank); never charging past ``soc_ceiling`` where LFP
charging tapers and the generator would burn the same fuel for less charge;
and never during quiet hours. Runs start as late as is safe so unexpected sun
can still help.

Everything is advisory: the plan is exposed on the API/MQTT and your Home
Assistant automation decides what to do with it.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .model import HourForecast

STEP_S = 900  # 15-minute simulation step


@dataclass
class PlannerParams:
    soc_floor: float = 25.0
    soc_ceiling: float = 90.0
    storm_lookahead_h: float = 48.0  # a second start within this window counts against a short run
    soc_margin: float = 3.0
    generator_dc_kw: float = 1.56  # Chargeverter at 30 A
    charge_efficiency: float = 0.94
    min_run_h: float = 1.5
    max_run_h: float = 8.0  # one tank
    start_cost_h: float = 0.5  # a cold start + warm-up, expressed in engine hours
    idle_kw: float = 0.066  # the inverter's own use on top of the cabin load (measured: ~66 W)
    tank_left_h: float | None = None
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
    floor_at: float | None = None  # when the bank would reach the floor without a generator

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["baseline"] = [[t, round(max(0.0, s), 1)] for t, s in self.baseline]
        d["planned"] = [[t, round(max(0.0, s), 1)] for t, s in self.planned]
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
        net = _net_kw_at(t, hours) - p.idle_kw
        if gen_start is not None and gen_end is not None and gen_start <= t < gen_end:
            net += p.generator_dc_kw * p.charge_efficiency
        if net > 0:
            net *= 0.97  # charge losses in battery
        soc += net * (STEP_S / 3600.0) / p.battery_kwh * 100.0
        soc = max(0.0, min(100.0, soc))
        t += STEP_S
        out.append((int(t), soc))
    return out


def _sim(soc0: float, start: float, hours: dict[int, HourForecast], p: PlannerParams,
         gen_start: float | None = None, end_soc: float = 100.0) -> tuple[list[tuple[int, float]], float | None, float]:
    """SOC trajectory; the generator runs from gen_start until end_soc or max_run_h.
    Returns (trajectory, gen_end, spilled_kwh) where spilled is solar that could not be stored."""
    soc, t = soc0, start
    traj = [(int(start), soc)]
    horizon = start + p.horizon_h * 3600
    gen_end = None
    spilled = 0.0
    dt_h = STEP_S / 3600.0
    while t < horizon:
        net = _net_kw_at(t, hours) - p.idle_kw  # the inverter's own use comes on top of the cabin load
        running = gen_start is not None and gen_start <= t and gen_end is None
        if running:
            net += p.generator_dc_kw * p.charge_efficiency
        net = net * 0.97 if net > 0 else net
        new = soc + net * dt_h / p.battery_kwh * 100.0
        if new > 100.0:
            spilled += (new - 100.0) / 100.0 * p.battery_kwh
        soc = min(100.0, new)  # may go below 0 so deficits stay measurable; clamped for display
        t += STEP_S
        traj.append((int(t), soc))
        if running and (soc >= end_soc or t - gen_start >= p.max_run_h * 3600):
            gen_end = t
    if gen_start is not None and gen_end is None:
        gen_end = min(horizon, gen_start + p.max_run_h * 3600)
    return traj, gen_end, spilled


def _gain_per_h(p: PlannerParams) -> float:
    """SOC percentage points one hour of generator adds (before solar/load)."""
    return p.generator_dc_kw * p.charge_efficiency * 0.97 / p.battery_kwh * 100.0


def _choose_end(soc_now: float, now: float, hours: dict[int, HourForecast], p: PlannerParams, gen_start: float,
                low: float, base_spill: float, floor_m: float) -> dict:
    """Try every stop point from ``low`` to the ceiling and keep the cheapest in engine hours."""
    gph = _gain_per_h(p)
    # a later run also has to carry the cabin while it charges: net gain per hour is lower than gph
    loads = [h.load_kw for h in hours.values()]
    load_pct_h = (sum(loads) / len(loads) + p.idle_kw) / p.battery_kwh * 100.0 if loads else 0.0
    net_gph = max(1.0, gph - load_pct_h)
    best = None
    e = low
    while True:
        e = min(e, p.soc_ceiling)
        traj, gen_end, spill = _sim(soc_now, now, hours, p, gen_start, e)
        if not any(_in_quiet(t, p.quiet_hours) for t in range(int(gen_start), int(gen_end), STEP_S)) or best is None:
            run_h = (gen_end - gen_start) / 3600.0
            after = [(t, s) for t, s in traj if t > gen_end]
            # Charge that was never needed: if the sun refills the bank before it gets back down to the
            # floor, everything still above the floor at the low point was fuel the sun would have replaced.
            t_full = next((t for t, s in after if s >= 99.5), None)
            wasted_h = 0.0
            if t_full is not None:
                low_pt = min((s for t, s in after if t <= t_full), default=floor_m)
                if low_pt > floor_m:
                    wasted_h = (low_pt - floor_m) / gph
            next_def = next((t for t, s in after if s < floor_m), None)
            extra = 0.0
            if next_def is not None:
                # later runs: their hours, plus a start for every refill from the floor to the ceiling they need
                deficit = floor_m - min(s for t, s in after)
                starts = max(1, math.ceil(deficit / max(1.0, p.soc_ceiling - floor_m) - 1e-9))
                if next_def - gen_end > p.storm_lookahead_h * 3600:
                    starts *= 0.5  # far-off deficits are uncertain: weigh their starts less
                extra = starts * p.start_cost_h + max(p.min_run_h, deficit / net_gph)
            cost = run_h + wasted_h + extra
            cand = {"cost": cost, "end_soc": max(s for t, s in traj if gen_start <= t <= gen_end), "traj": traj,
                    "gen_end": gen_end, "run_h": run_h, "wasted_h": wasted_h, "next_def": next_def, "target": e}
            if best is None or cost < best["cost"] - 0.05:
                best = cand
        if e >= p.soc_ceiling:
            break
        e += 1.0
    return best


def plan_generator(soc_now: float, now: float, forecast: list[HourForecast], p: PlannerParams,
                   generator_running: bool = False, run_elapsed_h: float = 0.0) -> Plan:
    hours = {h.ts: h for h in forecast}
    floor_m = p.soc_floor + p.soc_margin
    baseline, _, base_spill = _sim(soc_now, now, hours, p)
    min_soc, min_at = min(((s, t) for t, s in baseline), key=lambda x: x[0])
    gph = _gain_per_h(p)
    fuel_note = ""

    if generator_running:
        left_min_h = max(0.0, p.min_run_h - run_elapsed_h)
        low = min(p.soc_ceiling, soc_now + max(0.5, left_min_h * gph))
        best = _choose_end(soc_now, now, hours, p, now, low, base_spill, floor_m)
        target = best["target"]
        stop = soc_now >= min(target, p.soc_ceiling) - 0.5 and run_elapsed_h >= p.min_run_h - 0.01
        tail = ("enough to reach the next sunny stretch without another start" if best["next_def"] is None
                else f"another run will still be needed around {time.strftime('%a %H:%M', time.localtime(best['next_def']))}")
        reason = (f"Generator is running. Stop at about {target:.0f} %"
                  + (" (reached)." if stop else f": {tail}, without charging what the sun can cover."))
        return Plan(needed=True, recommended_now=not stop, start_at=now, end_at=best["gen_end"],
                    run_h=round(best["run_h"], 2), energy_kwh=round(best["run_h"] * p.generator_dc_kw, 1),
                    start_soc=round(soc_now, 1), end_soc=round(target, 1), min_soc_without=round(max(0.0, min_soc), 1), min_soc_at=min_at,
                    reason=reason, baseline=baseline, planned=best["traj"], stop_recommended=stop, next_deficit_at=best["next_def"])

    if min_soc >= floor_m:
        return Plan(needed=False, recommended_now=False, start_at=None, end_at=None, run_h=0.0, energy_kwh=0.0,
                    start_soc=None, end_soc=None, min_soc_without=round(max(0.0, min_soc), 1), min_soc_at=min_at,
                    reason=f"No generator needed in the next {p.horizon_h} h: forecast charge bottoms out at {max(0.0, min_soc):.0f} % "
                           f"around {time.strftime('%a %H:%M', time.localtime(min_at))}.",
                    baseline=baseline, planned=baseline)

    # Latest safe start: walk back from the floor crossing, first slot where a minimum run keeps the bank above the floor.
    t_cross = next(t for t, s in baseline if s < floor_m)
    t_start = (t_cross // STEP_S) * STEP_S
    chosen = None
    while t_start >= now:
        if not _in_quiet(t_start, p.quiet_hours):
            soc_at_start = next((s for t, s in baseline if t >= t_start), soc_now)
            low = min(p.soc_ceiling, soc_at_start + p.min_run_h * gph)
            traj, gen_end, _ = _sim(soc_now, now, hours, p, t_start, low)
            quiet = any(_in_quiet(t, p.quiet_hours) for t in range(int(t_start), int(gen_end), STEP_S))
            if not quiet and min(s for t, s in traj if t <= gen_end) >= p.soc_floor - 0.01:
                chosen = (t_start, low)
                break
        t_start -= STEP_S
    prefix = ""
    if chosen is None:
        chosen = (now, min(p.soc_ceiling, soc_now + p.min_run_h * gph))
        prefix = "Constraints (quiet hours and the floor) cannot both be met; start as soon as possible. "
    t_start, low = chosen
    best = _choose_end(soc_now, now, hours, p, t_start, low, base_spill, floor_m)
    start_soc = next((s for t, s in baseline if t >= t_start), soc_now)
    run_h, end_soc = best["run_h"], best["target"]
    recommended_now = t_start - now <= 10 * 60
    when = "now" if recommended_now else time.strftime("%a %H:%M", time.localtime(t_start))
    if best["next_def"] is None:
        tail = "that carries the bank to the next sunny stretch with no further start"
    else:
        tail = f"another run will be needed around {time.strftime('%a %H:%M', time.localtime(best['next_def']))}"
    if p.tank_left_h is not None and run_h > p.tank_left_h + 0.05:
        fuel_note = f" Refill first: about {p.tank_left_h:.1f} h of fuel left in the tank."
    t_floor = next((t for t, s in baseline if s < p.soc_floor), min_at)
    reason = (f"{prefix}Without the generator the bank would reach the {p.soc_floor:.0f} % floor around "
              f"{time.strftime('%a %H:%M', time.localtime(t_floor))}. Start {when} at about {start_soc:.0f} % and run "
              f"{run_h:.1f} h to {end_soc:.0f} %; {tail}.{fuel_note}")
    return Plan(needed=True, recommended_now=recommended_now, start_at=t_start, end_at=best["gen_end"], run_h=round(run_h, 2),
                energy_kwh=round(run_h * p.generator_dc_kw, 1), start_soc=round(start_soc, 1), end_soc=round(end_soc, 1),
                min_soc_without=round(max(0.0, min_soc), 1), min_soc_at=min_at, reason=reason, baseline=baseline, planned=best["traj"],
                next_deficit_at=best["next_def"], floor_at=t_floor)


def parse_quiet_hours(items: list[str]) -> list[tuple[int, int]]:
    out = []
    for s in items or []:
        a, b = s.split("-")
        out.append((int(a.split(":")[0]), int(b.split(":")[0])))
    return out

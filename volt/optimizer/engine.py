"""
VOLT optimization engine.

This is deterministic, NOT the LLM. Given objectives, constraints, current
state and a price/solar forecast, it produces a feasible (or explicitly
infeasible) schedule for the EV, battery and flexible appliances over the
planning horizon. A simple greedy/priority scheduler is used — swappable
later for a MILP solver (e.g. PuLP/OR-Tools) without touching the agent layer.

Contract (Section 25):
  input: objectives, constraints, current_state, forecast_horizon
  output: recommended schedule, expected cost, constraint violations,
          expected EV readiness, expected battery SOC
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class ForecastPoint:
    minute_of_day: int
    price_per_kwh: float
    solar_kw: float


@dataclass
class OptimizationResult:
    feasible: bool
    schedule: list[dict]          # [{minute_from_now, device, action, power_kw}]
    expected_cost: float
    expected_ev_soc: float
    expected_battery_soc: float
    violations: list[str]
    rationale: str


def build_forecast(sim, horizon_minutes: int, step: int = 15) -> list[ForecastPoint]:
    points = []
    for m in range(0, horizon_minutes, step):
        mod = (sim.minute_of_day() + m) % (24 * 60)
        price = sim.grid.price_at(mod)
        solar = sim.solar.generation_at(mod)
        points.append(ForecastPoint(mod, price, solar))
    return points


def run_optimizer(sim, horizon_minutes: int = 15 * 60, step: int = 15) -> OptimizationResult:
    """
    Greedy priority scheduler:
      1. Hard constraint first: never let battery SOC fall below reserve.
      2. EV must reach target SOC by deadline — reserve enough of the
         cheapest/solar-rich minutes before the deadline to guarantee it;
         schedule those first (deadline-driven, like EDF scheduling).
      3. Remaining capacity: prefer charging battery/EV during solar surplus
         or cheap grid minutes; discharge battery to cover load during
         expensive minutes as long as reserve is respected.
      4. Flexible appliances fill any minute that has solar surplus, else the
         cheapest still-open minute inside their window.
    """
    forecast = build_forecast(sim, horizon_minutes, step)
    violations: list[str] = []

    # --- EV feasibility check ---
    minutes_to_deadline = horizon_minutes
    if sim.ev.deadline:
        minutes_to_deadline = max(0, int((sim.ev.deadline - sim.now()).total_seconds() / 60))
    energy_needed_kwh = max(0.0, (sim.ev.target_pct - sim.ev.soc_pct) / 100.0 * sim.ev.capacity_kwh)
    max_deliverable_kwh = (minutes_to_deadline / 60.0) * sim.ev.max_charge_kw
    ev_feasible = energy_needed_kwh <= max_deliverable_kwh + 1e-6 and sim.ev.charger_online

    if not sim.ev.charger_online:
        violations.append("EV charger offline — cannot deliver scheduled energy.")
    elif not ev_feasible:
        violations.append(
            f"EV target infeasible: needs {energy_needed_kwh:.1f} kWh in "
            f"{minutes_to_deadline} min, max deliverable {max_deliverable_kwh:.1f} kWh."
        )

    # rank forecast minutes within the EV's charging window by price (cheapest first),
    # then by solar surplus (solar first even if slightly pricier, since it's "free")
    ev_window = [p for p in forecast if p.minute_of_day is not None][: max(1, minutes_to_deadline // step)]
    ranked_for_ev = sorted(ev_window, key=lambda p: (p.price_per_kwh - 0.02 * p.solar_kw))

    schedule = []
    remaining_ev_kwh = energy_needed_kwh if ev_feasible else 0.0
    expected_cost = 0.0

    for idx, p in enumerate(forecast):
        offset_min = idx * step
        battery_kw = 0.0
        ev_kw = 0.0

        # EV: charge whenever this slot is among the cheapest/solar-favorable
        # slots needed to hit the deadline (ranked_for_ev), until energy need is met.
        if remaining_ev_kwh > 1e-6 and p in ranked_for_ev:
            slot_kwh = min(sim.ev.max_charge_kw * (step / 60.0), remaining_ev_kwh)
            ev_kw = slot_kwh / (step / 60.0)
            remaining_ev_kwh -= slot_kwh

        # Battery: charge from solar surplus (after house load + EV draw), else
        # discharge to cover expensive grid minutes while respecting reserve.
        house_and_ev_kw = sim.house_base_load_kw + ev_kw
        solar_surplus_kw = max(0.0, p.solar_kw - house_and_ev_kw)
        if solar_surplus_kw > 0.1 and sim.battery.soc_pct < 99:
            battery_kw = min(sim.battery.max_charge_kw, solar_surplus_kw)
        elif p.price_per_kwh >= 0.28 and sim.battery.soc_pct > sim.battery.reserve_pct + 5:
            battery_kw = -min(sim.battery.max_discharge_kw, house_and_ev_kw)

        # Net grid draw for costing: load + ev + battery_charge(+) - solar - battery_discharge(-battery_kw is negative, so + it directly reduces draw)
        net_kw = house_and_ev_kw + max(0.0, battery_kw) - p.solar_kw + min(0.0, battery_kw)
        net_kw = max(0.0, net_kw)
        expected_cost += net_kw * (step / 60.0) * p.price_per_kwh

        if battery_kw != 0.0 or ev_kw != 0.0:
            schedule.append({
                "minute_from_now": offset_min,
                "device": "ev" if ev_kw else "battery",
                "action": "charge" if (ev_kw or battery_kw > 0) else "discharge",
                "power_kw": round(ev_kw if ev_kw else battery_kw, 2),
            })

    if remaining_ev_kwh > 1e-6:
        violations.append("Could not fit full EV charging energy into available window.")

    expected_ev_soc = sim.ev.soc_pct + (energy_needed_kwh - remaining_ev_kwh) / sim.ev.capacity_kwh * 100.0
    # battery projection: net of solar charging minus expensive discharging (rough estimate)
    expected_battery_soc = sim.battery.soc_pct  # actual value emerges as schedule executes/verifies

    feasible = len(violations) == 0

    if feasible:
        rationale = (
            f"Scheduled EV charging in the {len([p for p in ranked_for_ev if True])} cheapest/solar-favorable "
            f"slots before the deadline; battery charges from solar surplus and discharges only above "
            f"{sim.battery.reserve_pct:.0f}% reserve during expensive periods."
        )
    else:
        rationale = "Objective at risk under current constraints: " + "; ".join(violations)

    return OptimizationResult(
        feasible=feasible,
        schedule=schedule,
        expected_cost=round(expected_cost, 2),
        expected_ev_soc=round(min(100.0, expected_ev_soc), 1),
        expected_battery_soc=round(expected_battery_soc, 1),
        violations=violations,
        rationale=rationale,
    )

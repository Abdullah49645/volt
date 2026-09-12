"""
Strands tools exposed to the VOLT agent.

Section 24/25 contract: the Strands agent NEVER touches devices or the
optimizer directly — it only calls these tools. Safety-critical limits
(SOC bounds, rate limits, reserve) are enforced in simulator/environment.py and
optimizer/engine.py, not here and not by the LLM.
"""
from __future__ import annotations
from datetime import datetime
from strands import tool

from simulator.environment import HouseholdSimulator
from optimizer.engine import run_optimizer as _run_optimizer
from domain.models import VerificationStatus
from agent.adapters import build_adapters


class VoltRuntime:
    """Holds the live household sim, its device adapters, and the
    activity/event logs the tools operate on."""

    def __init__(self, sim: HouseholdSimulator):
        self.sim = sim
        self.adapters = build_adapters(sim)   # device -> DeviceAdapter
        self.current_plan: dict | None = None
        self.activity_log: list[dict] = []
        self.escalations: list[dict] = []
        self.last_applied_actions: list[dict] = []

    def log(self, tool_name: str, input_: dict, result: dict, verification=VerificationStatus.PENDING):
        self.activity_log.append({
            "tool": tool_name, "input": input_, "result": result,
            "timestamp": self.sim.now().isoformat(), "verification_status": verification.value,
        })


def build_tools(rt: VoltRuntime):
    """Return the list of @tool-decorated functions bound to this runtime instance."""

    @tool
    def get_household_state() -> dict:
        """Return the current, real-time state of the household: solar generation,
        household load, electricity price, battery SOC/reserve, EV SOC/target/deadline,
        appliance states, and device/grid availability."""
        snap = rt.sim.snapshot()
        rt.log("get_household_state", {}, snap)
        return snap

    @tool
    def get_price_forecast(horizon_minutes: int = 900) -> dict:
        """Return the electricity price forecast ($/kWh) for the next N minutes."""
        from optimizer.engine import build_forecast
        pts = build_forecast(rt.sim, horizon_minutes)
        out = {"points": [{"minute_of_day": p.minute_of_day, "price_per_kwh": p.price_per_kwh} for p in pts]}
        rt.log("get_price_forecast", {"horizon_minutes": horizon_minutes}, {"n_points": len(pts)})
        return out

    @tool
    def get_solar_forecast(horizon_minutes: int = 900) -> dict:
        """Return the solar generation forecast (kW) for the next N minutes."""
        from optimizer.engine import build_forecast
        pts = build_forecast(rt.sim, horizon_minutes)
        out = {"points": [{"minute_of_day": p.minute_of_day, "solar_kw": p.solar_kw} for p in pts]}
        rt.log("get_solar_forecast", {"horizon_minutes": horizon_minutes}, {"n_points": len(pts)})
        return out

    @tool
    def get_current_plan() -> dict:
        """Return the currently active household operating plan, if any."""
        result = rt.current_plan or {"status": "no_active_plan"}
        rt.log("get_current_plan", {}, result)
        return result

    @tool
    def run_optimizer_tool(horizon_minutes: int = 900) -> dict:
        """Run the constrained optimization engine over the given planning horizon
        using current objectives (EV deadline/target, battery reserve, price and
        solar forecasts) and return a recommended schedule, expected cost, expected
        EV/battery SOC, and any constraint violations (e.g. infeasible deadline)."""
        result = _run_optimizer(rt.sim, horizon_minutes=horizon_minutes)
        out = {
            "feasible": result.feasible,
            "schedule": result.schedule,
            "expected_cost": result.expected_cost,
            "expected_ev_soc": result.expected_ev_soc,
            "expected_battery_soc": result.expected_battery_soc,
            "violations": result.violations,
            "rationale": result.rationale,
        }
        rt.log("run_optimizer", {"horizon_minutes": horizon_minutes}, out)
        return out

    @tool
    def update_plan(schedule: list, expected_cost: float, rationale: str) -> dict:
        """Persist a new active operating plan (from the optimizer's recommended
        schedule) as the household's authoritative plan, replacing any prior plan."""
        rt.current_plan = {
            "status": "active",
            "created_at": rt.sim.now().isoformat(),
            "schedule": schedule,
            "expected_cost": expected_cost,
            "rationale": rationale,
        }
        rt.log("update_plan", {"expected_cost": expected_cost}, rt.current_plan)
        return rt.current_plan

    @tool
    def apply_device_action(device: str, action: str, power_kw: float, duration_minutes: int = 15) -> dict:
        """Command a device (ev|battery) to charge/discharge/hold at a given power
        (kW) for the given duration. Goes through that device's adapter — the
        only thing in VOLT allowed to touch real or simulated hardware — which
        validates availability and safety limits before acting. Acceptance does
        NOT guarantee success; call verify_device_state afterward to confirm."""
        adapter = rt.adapters.get(device)
        if adapter is None:
            result = {"accepted": False, "reason": f"no adapter registered for device '{device}'"}
            rt.log("apply_device_action", {"device": device, "action": action, "power_kw": power_kw}, result, VerificationStatus.VERIFIED_FAILURE)
            return result
        result = adapter.apply_action(action, power_kw, duration_minutes)
        rt.last_applied_actions = [{"device": device, "power_kw": power_kw}]
        rt.log("apply_device_action", {"device": device, "action": action, "power_kw": power_kw}, result)
        return result

    @tool
    def verify_device_state(device: str) -> dict:
        """Re-read the ACTUAL state reported by the device's adapter (not the
        requested state). The agent must use this to confirm an action
        succeeded rather than assuming it did."""
        adapter = rt.adapters.get(device)
        if adapter is None:
            result = {"device": device, "error": "no adapter registered"}
            rt.log("verify_device_state", {"device": device}, result, VerificationStatus.VERIFIED_FAILURE)
            return result
        result = adapter.verify_state()
        status = VerificationStatus.VERIFIED_SUCCESS if "error" not in result else VerificationStatus.VERIFIED_FAILURE
        rt.log("verify_device_state", {"device": device}, result, status)
        return result

    @tool
    def record_event(event_type: str, payload: dict) -> dict:
        """Record a household event (price spike, solar forecast change, device
        failure, user preference change, etc.) to the authoritative event log."""
        evt = {"type": event_type, "payload": payload, "timestamp": rt.sim.now().isoformat()}
        rt.sim.events.append(evt)
        rt.log("record_event", evt, {"recorded": True})
        return evt

    @tool
    def request_human_decision(reason: str, options: list) -> dict:
        """Escalate a decision to the user. Use ONLY for consequential decisions:
        accepting real extra cost, violating a stated preference, disabling backup
        reserve, or a device outage with no safe automatic remedy. Do not use for
        routine operational decisions (those should be made autonomously)."""
        esc = {"reason": reason, "options": options, "timestamp": rt.sim.now().isoformat(), "user_decision": None}
        rt.escalations.append(esc)
        rt.log("request_human_decision", {"reason": reason, "options": options}, esc)
        return esc

    @tool
    def schedule_reassessment(in_minutes: int) -> dict:
        """Schedule the next automatic reassessment of the household plan."""
        result = {"next_reassessment_minute": rt.sim.minutes_elapsed + in_minutes}
        rt.log("schedule_reassessment", {"in_minutes": in_minutes}, result)
        return result

    return [
        get_household_state, get_price_forecast, get_solar_forecast, get_current_plan,
        run_optimizer_tool, update_plan, apply_device_action, verify_device_state,
        record_event, request_human_decision, schedule_reassessment,
    ]

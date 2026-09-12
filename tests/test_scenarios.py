"""
Runs scenarios A-J from the spec (Section 65, Step 8) against the real
simulator + optimizer + agent tool loop, and prints a readable trace so the
domain/optimizer/verification logic can be sanity-checked end to end.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from simulator.environment import HouseholdSimulator
from agent.tools import VoltRuntime
from agent.orchestrator import run_agent_cycle


def line(title):
    print(f"\n{'='*70}\n{title}\n{'='*70}")


def show(cycle):
    opt = cycle["optimizer_result"]
    print(f"  feasible={opt['feasible']}  expected_cost=${opt['expected_cost']}  "
          f"expected_ev_soc={opt['expected_ev_soc']}%  violations={opt['violations']}")
    print(f"  rationale: {opt['rationale']}")
    if cycle["escalation"]:
        print(f"  ESCALATED -> {cycle['escalation']['reason']}")
        print(f"    options: {cycle['escalation']['options']}")
    if cycle.get("applied"):
        a = cycle["applied"]
        print(f"  applied: device={a.get('device')} power_kw={a.get('power_kw')} accepted={a.get('accepted')}")
    if cycle.get("verification"):
        print(f"  verified: {cycle['verification']}")


def scenario_A():
    line("A — Normal operation")
    sim = HouseholdSimulator(sim_start=datetime(2026, 9, 9, 16, 0))
    rt = VoltRuntime(sim)
    show(run_agent_cycle(rt, "scheduled"))
    return sim, rt


def scenario_B():
    line("B — Price spike")
    sim, rt = scenario_A()
    sim.trigger_price_spike(0.47)
    print("  >> price spike to $0.47/kWh")
    show(run_agent_cycle(rt, "price_change"))


def scenario_C():
    line("C — Solar forecast collapse")
    sim, rt = scenario_A()
    sim.trigger_solar_forecast_collapse(0.4)
    print("  >> solar forecast collapses (cloud_factor=0.4)")
    show(run_agent_cycle(rt, "solar_forecast_change"))


def scenario_D():
    line("D — EV arrives early / lower SOC than expected")
    sim, rt = scenario_A()
    sim.trigger_ev_early_arrival(20.0)
    print("  >> EV arrives at 20% instead of expected")
    show(run_agent_cycle(rt, "ev_state_change"))


def scenario_E():
    line("E — EV deadline moves earlier")
    sim, rt = scenario_A()
    sim.trigger_deadline_change(sim.now() + timedelta(hours=2))
    print("  >> EV deadline moved to 2 hours from now")
    show(run_agent_cycle(rt, "deadline_change"))


def scenario_F():
    line("F — Battery reserve requirement changes")
    sim, rt = scenario_A()
    sim.trigger_reserve_change(50.0)
    print("  >> reserve requirement changed 30% -> 50%")
    show(run_agent_cycle(rt, "preference_change"))


def scenario_G():
    line("G — Device failure (EV charger offline)")
    sim, rt = scenario_A()
    sim.trigger_device_failure("ev_charger")
    print("  >> EV charger goes offline")
    show(run_agent_cycle(rt, "device_failure"))


def scenario_H():
    line("H — Impossible objective (20% -> 100% in 30 min)")
    sim = HouseholdSimulator(sim_start=datetime(2026, 9, 9, 16, 0))
    rt = VoltRuntime(sim)
    sim.ev.soc_pct = 20.0
    sim.ev.target_pct = 100.0
    sim.trigger_deadline_change(sim.now() + timedelta(minutes=30))
    print("  >> EV: 20% -> 100% target in 30 minutes (should be infeasible)")
    show(run_agent_cycle(rt, "objective_check"))


def scenario_I():
    line("I — User changes preference mid-run")
    sim, rt = scenario_A()
    print("  >> user: 'expecting a storm tonight, keep more backup' -> temp reserve 60%")
    sim.trigger_reserve_change(60.0)
    show(run_agent_cycle(rt, "preference_change"))


def scenario_J():
    line("J — Action succeeds but verification detects unexpected state")
    sim, rt = scenario_A()
    cycle1 = run_agent_cycle(rt, "scheduled")
    show(cycle1)
    print("  >> simulating an unexpected fault right after action was applied")
    sim.trigger_device_failure("ev_charger")
    cycle2 = run_agent_cycle(rt, "post_action_check")
    show(cycle2)


if __name__ == "__main__":
    scenario_B()
    scenario_C()
    scenario_D()
    scenario_E()
    scenario_F()
    scenario_G()
    scenario_H()
    scenario_I()
    scenario_J()
    print("\nAll scenarios executed.\n")

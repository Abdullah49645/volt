"""
VOLT agent orchestration layer.

`build_strands_agent()` wires a real strands.Agent to the tool set in
agent/tools.py and a Bedrock model. This is the production path: point it at
a Bedrock model ID and valid AWS credentials and it runs for real.

This sandbox has no AWS credentials and no network path to Bedrock, so it
can't make a live model call here. `run_agent_cycle()` below runs the exact
same OBSERVE -> REASON -> PLAN -> ACT -> VERIFY -> ADAPT loop through the
same tool functions, using a small deterministic policy in place of the LLM's
judgment call, so the rest of the system (tools, optimizer, sim, escalation,
logging) can be exercised and demoed end-to-end. Swap `run_agent_cycle` for
`strands_agent(...)` once deployed with real Bedrock access — no other code
changes needed, since both drive the same tools.
"""
from __future__ import annotations
from strands import Agent

from agent.tools import VoltRuntime, build_tools

SYSTEM_PROMPT = """You are VOLT, the policy/orchestration layer of an autonomous
household resource operator. You do not directly control electrical hardware —
you observe household state via tools, decide whether the current plan is still
valid, call the optimizer when re-planning is needed, apply device actions
through tools, and ALWAYS verify actions afterward rather than assuming success.
Escalate to the human only for consequential decisions (extra cost, preference
violations, disabling reserve, unresolvable device failures). Keep your
explanations concise operational statements, not chain-of-thought."""


def build_strands_agent(rt: VoltRuntime, model_id: str = "us.anthropic.claude-sonnet-4-6") -> Agent:
    """Production wiring: real Strands agent + Bedrock model. Requires AWS credentials
    and Bedrock access, which this sandbox does not have."""
    return Agent(model=model_id, tools=build_tools(rt), system_prompt=SYSTEM_PROMPT)


def run_agent_cycle(rt: VoltRuntime, trigger: str = "scheduled") -> dict:
    """
    Deterministic stand-in for one LLM-driven OBSERVE->REASON->PLAN->ACT->VERIFY
    cycle, calling the SAME tool functions a live Strands+Bedrock agent would
    call. Used here because this sandbox cannot reach Bedrock. The policy below
    intentionally mirrors what the system prompt instructs a real model to do:
    check state, re-optimize, apply the near-term action, verify it, and
    escalate only for consequential tradeoffs.
    """
    tools = {t.tool_name if hasattr(t, "tool_name") else t.__name__: t for t in build_tools(rt)}

    def call(name, **kwargs):
        return tools[name](**kwargs)

    state = call("get_household_state")
    opt = call("run_optimizer_tool", horizon_minutes=900)

    cycle_log = {"trigger": trigger, "state": state, "optimizer_result": opt, "escalation": None, "applied": None}

    if not opt["feasible"]:
        # Consequential — needs a human decision, not a silent violation.
        options = ["Allow backup/expedited charging option (adds cost)", "Keep current plan and accept missed target"]
        esc = call("request_human_decision", reason=opt["rationale"], options=options)
        cycle_log["escalation"] = esc
        return cycle_log

    call("update_plan", schedule=opt["schedule"], expected_cost=opt["expected_cost"], rationale=opt["rationale"])

    # Apply the next near-term scheduled action (first entry due now/soon), then verify.
    if opt["schedule"]:
        next_action = opt["schedule"][0]
        applied = call(
            "apply_device_action",
            device=next_action["device"],
            action=next_action["action"],
            power_kw=next_action["power_kw"],
            duration_minutes=15,
        )
        applied = {"device": next_action["device"], "power_kw": next_action["power_kw"], **applied}
        cycle_log["applied"] = applied
        if applied.get("accepted"):
            verification = call("verify_device_state", device=next_action["device"])
            cycle_log["verification"] = verification

    call("schedule_reassessment", in_minutes=15)
    return cycle_log

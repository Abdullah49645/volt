"""
VOLT domain model.

This is the authoritative, structured state of the system. The Strands agent
reads and mutates this state only through tools (see agent/tools.py) — never
through raw conversation text. The UI reads this same state.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
import uuid


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class DeviceType(str, Enum):
    EV = "ev"
    BATTERY = "battery"
    SOLAR = "solar"
    GRID = "grid"
    APPLIANCE = "appliance"


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class ObjectiveType(str, Enum):
    EV_TARGET_SOC = "ev_target_soc"
    BATTERY_RESERVE = "battery_reserve"
    COMFORT = "comfort"
    COST_MINIMIZE = "cost_minimize"
    SOLAR_PREFERENCE = "solar_preference"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    HARD_CONSTRAINT = "hard_constraint"  # must never be violated


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"


@dataclass
class Device:
    id: str
    household_id: str
    type: DeviceType
    name: str
    status: DeviceStatus = DeviceStatus.ONLINE
    # capabilities / constraints, e.g. max_rate_kw, capacity_kwh
    capabilities: dict = field(default_factory=dict)
    # live state, e.g. soc_pct, power_kw
    state: dict = field(default_factory=dict)


@dataclass
class EnergyState:
    timestamp: datetime
    solar_generation_kw: float
    household_load_kw: float
    grid_import_kw: float
    grid_export_kw: float
    electricity_price_per_kwh: float
    battery_soc_pct: float
    ev_soc_pct: float


@dataclass
class Objective:
    id: str
    household_id: str
    type: ObjectiveType
    target: Optional[float] = None          # e.g. 80 (percent)
    deadline: Optional[datetime] = None
    priority: Priority = Priority.MEDIUM
    active: bool = True


@dataclass
class Preference:
    id: str
    household_id: str
    type: str
    value: object
    priority: Priority = Priority.MEDIUM
    expiration: Optional[datetime] = None


@dataclass
class PlanAction:
    device_id: str
    action: str                 # e.g. "charge", "discharge", "hold", "defer"
    scheduled_start: datetime
    scheduled_end: datetime
    expected_result: dict = field(default_factory=dict)
    actual_result: Optional[dict] = None
    verification_status: VerificationStatus = VerificationStatus.PENDING


@dataclass
class Plan:
    id: str
    household_id: str
    status: str                 # "active" | "superseded" | "completed"
    created_at: datetime
    valid_until: datetime
    expected_cost: float
    expected_outcome: dict = field(default_factory=dict)
    actions: list[PlanAction] = field(default_factory=list)
    rationale: str = ""         # concise operational explanation, not chain-of-thought


@dataclass
class Event:
    id: str
    household_id: str
    type: str
    source: str
    payload: dict
    timestamp: datetime


@dataclass
class AgentAction:
    id: str
    household_id: str
    tool: str
    input: dict
    result: dict
    timestamp: datetime
    verification_status: VerificationStatus = VerificationStatus.PENDING


@dataclass
class Escalation:
    id: str
    household_id: str
    reason: str
    options: list[str]
    user_decision: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Household:
    id: str
    user_id: str
    timezone: str = "UTC"
    devices: dict[str, Device] = field(default_factory=dict)
    objectives: list[Objective] = field(default_factory=list)
    preferences: list[Preference] = field(default_factory=list)
    current_plan: Optional[Plan] = None
    activity_log: list[AgentAction] = field(default_factory=list)
    event_log: list[Event] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)

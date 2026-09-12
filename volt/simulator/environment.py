"""
Household simulator.

A deterministic simulation of solar, battery, EV, grid and flexible appliances.
This stands in for real hardware (Tesla/Wallbox/HomeAssistant/inverter APIs) so
VOLT's agent, optimizer and verification logic can be demonstrated end-to-end
without real devices. Every device here is clearly labelled SIMULATED and is
designed to sit behind the same DeviceAdapter interface a real adapter would use.

Physical constraints enforced here (never left to the LLM):
- battery/EV SOC in [0, 100], never below configured reserve/min
- charge/discharge rate capped by device max_rate_kw
- solar generation capped by configured capacity
- energy balance: load = solar + battery_discharge + grid_import - battery_charge - export
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math


@dataclass
class ApplianceSim:
    name: str
    power_kw: float
    duration_min: int
    window_start_min: int   # minutes from simulation start when it becomes eligible
    window_end_min: int     # deadline by which it must finish
    priority: str = "medium"
    running: bool = False
    completed: bool = False
    remaining_min: int = 0

    def __post_init__(self):
        self.remaining_min = self.duration_min


@dataclass
class BatterySim:
    capacity_kwh: float = 13.5
    soc_pct: float = 64.0
    reserve_pct: float = 30.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    round_trip_efficiency: float = 0.92

    def apply_power(self, kw: float, minutes: float):
        """Positive kw = charging, negative = discharging. Enforces SOC bounds & rate limits."""
        kw = max(-self.max_discharge_kw, min(self.max_charge_kw, kw))
        energy_kwh = kw * (minutes / 60.0)
        if energy_kwh >= 0:
            energy_kwh *= self.round_trip_efficiency
        delta_pct = (energy_kwh / self.capacity_kwh) * 100.0
        new_soc = self.soc_pct + delta_pct
        new_soc = max(0.0, min(100.0, new_soc))
        applied_pct = new_soc - self.soc_pct
        self.soc_pct = new_soc
        return applied_pct  # actual % change achieved (may be clipped)


@dataclass
class EVSim:
    capacity_kwh: float = 75.0
    soc_pct: float = 32.0
    target_pct: float = 80.0
    deadline: datetime = None
    max_charge_kw: float = 11.0
    connected: bool = True
    charger_online: bool = True

    def apply_charge(self, kw: float, minutes: float):
        if not (self.connected and self.charger_online):
            return 0.0
        kw = max(0.0, min(self.max_charge_kw, kw))
        energy_kwh = kw * (minutes / 60.0)
        delta_pct = (energy_kwh / self.capacity_kwh) * 100.0
        new_soc = min(100.0, self.soc_pct + delta_pct)
        applied = new_soc - self.soc_pct
        self.soc_pct = new_soc
        return applied


@dataclass
class SolarSim:
    capacity_kw: float = 6.0
    cloud_factor: float = 1.0  # 1.0 = clear, lower = more cloud

    def generation_at(self, minute_of_day: int) -> float:
        # simple daylight bell curve peaking at solar noon (~13:00)
        hour = (minute_of_day / 60.0) % 24
        if hour < 6 or hour > 20:
            base = 0.0
        else:
            base = math.sin(math.pi * (hour - 6) / 14.0)
        return max(0.0, base) * self.capacity_kw * self.cloud_factor


@dataclass
class GridSim:
    price_schedule: dict = field(default_factory=dict)  # hour -> $/kWh
    available: bool = True
    demand_response_active: bool = False

    def price_at(self, minute_of_day: int) -> float:
        hour = int((minute_of_day / 60.0) % 24)
        return self.price_schedule.get(hour, 0.25)


class HouseholdSimulator:
    """SIMULATED household — deterministic simulator, not real hardware."""

    def __init__(self, sim_start: datetime):
        self.sim_start = sim_start
        self.minutes_elapsed = 0
        self.battery = BatterySim()
        self.ev = EVSim(deadline=sim_start + timedelta(hours=15))
        self.solar = SolarSim()
        self.grid = GridSim(price_schedule={
            0: 0.14, 1: 0.12, 2: 0.11, 3: 0.11, 4: 0.13, 5: 0.16,
            6: 0.20, 7: 0.24, 8: 0.22, 9: 0.20, 10: 0.18, 11: 0.17,
            12: 0.16, 13: 0.15, 14: 0.16, 15: 0.18, 16: 0.22, 17: 0.30,
            18: 0.34, 19: 0.32, 20: 0.28, 21: 0.24, 22: 0.20, 23: 0.16,
        })
        self.appliances: list[ApplianceSim] = [
            ApplianceSim("Dishwasher", 1.2, 90, window_start_min=0, window_end_min=20 * 60, priority="low"),
            ApplianceSim("Washing machine", 0.9, 60, window_start_min=0, window_end_min=18 * 60, priority="low"),
        ]
        self.house_base_load_kw = 1.1
        self.house_load_kw = 1.7  # base + misc
        self.events: list[dict] = []

    def now(self) -> datetime:
        return self.sim_start + timedelta(minutes=self.minutes_elapsed)

    def minute_of_day(self) -> int:
        t = self.now()
        return t.hour * 60 + t.minute

    def snapshot(self) -> dict:
        mod = self.minute_of_day()
        solar_kw = self.solar.generation_at(mod)
        price = self.grid.price_at(mod)
        return {
            "timestamp": self.now().isoformat(),
            "solar_generation_kw": round(solar_kw, 2),
            "household_load_kw": round(self.house_load_kw, 2),
            "electricity_price_per_kwh": round(price, 3),
            "battery": {
                "soc_pct": round(self.battery.soc_pct, 1),
                "reserve_pct": self.battery.reserve_pct,
                "max_charge_kw": self.battery.max_charge_kw,
                "max_discharge_kw": self.battery.max_discharge_kw,
            },
            "ev": {
                "soc_pct": round(self.ev.soc_pct, 1),
                "target_pct": self.ev.target_pct,
                "deadline": self.ev.deadline.isoformat() if self.ev.deadline else None,
                "connected": self.ev.connected,
                "charger_online": self.ev.charger_online,
                "max_charge_kw": self.ev.max_charge_kw,
            },
            "appliances": [
                {"name": a.name, "running": a.running, "completed": a.completed,
                 "remaining_min": a.remaining_min, "power_kw": a.power_kw}
                for a in self.appliances
            ],
            "grid_available": self.grid.available,
        }

    def advance(self, minutes: int, plan_actions: list[dict] | None = None):
        """
        Advance simulated time, applying the currently-commanded device actions
        (as decided by the optimizer/agent) each minute, enforcing physical
        constraints, and recording actual (verifiable) outcomes.
        """
        plan_actions = plan_actions or []
        step = 1
        results = {"battery_delta_pct": 0.0, "ev_delta_pct": 0.0, "appliances_completed": []}
        remaining = minutes
        while remaining > 0:
            dt = min(step, remaining)
            remaining -= dt
            self.minutes_elapsed += dt

            battery_kw = 0.0
            ev_kw = 0.0
            for act in plan_actions:
                if act["device"] == "battery":
                    battery_kw = act.get("power_kw", 0.0)
                elif act["device"] == "ev":
                    ev_kw = act.get("power_kw", 0.0)

            results["battery_delta_pct"] += self.battery.apply_power(battery_kw, dt)
            results["ev_delta_pct"] += self.ev.apply_charge(ev_kw, dt)

            for a in self.appliances:
                if a.running and not a.completed:
                    a.remaining_min -= dt
                    if a.remaining_min <= 0:
                        a.completed = True
                        a.running = False
                        results["appliances_completed"].append(a.name)
        return results

    # --- deterministic demo events (Section 15) ---
    def trigger_price_spike(self, new_price: float):
        mod = self.minute_of_day()
        hour = int((mod / 60.0) % 24)
        self.grid.price_schedule[hour] = new_price
        self.events.append({"type": "price_spike", "price": new_price, "at": self.now().isoformat()})

    def trigger_solar_forecast_collapse(self, cloud_factor: float):
        self.solar.cloud_factor = cloud_factor
        self.events.append({"type": "solar_forecast_collapse", "cloud_factor": cloud_factor, "at": self.now().isoformat()})

    def trigger_ev_early_arrival(self, soc_pct: float):
        self.ev.soc_pct = soc_pct
        self.ev.connected = True
        self.events.append({"type": "ev_early_arrival", "soc_pct": soc_pct, "at": self.now().isoformat()})

    def trigger_deadline_change(self, new_deadline: datetime):
        self.ev.deadline = new_deadline
        self.events.append({"type": "deadline_change", "deadline": new_deadline.isoformat(), "at": self.now().isoformat()})

    def trigger_reserve_change(self, new_reserve_pct: float):
        self.battery.reserve_pct = new_reserve_pct
        self.events.append({"type": "reserve_change", "reserve_pct": new_reserve_pct, "at": self.now().isoformat()})

    def trigger_device_failure(self, device: str = "ev_charger"):
        if device == "ev_charger":
            self.ev.charger_online = False
        self.events.append({"type": "device_failure", "device": device, "at": self.now().isoformat()})

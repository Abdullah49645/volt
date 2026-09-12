"""
Device adapters — this is the layer that answers "how does VOLT actually see
and control the house?"

Nothing above this file (agent, optimizer) talks to real or simulated
hardware directly. Every device is accessed through a DeviceAdapter with
exactly three operations: get_state(), apply_action(), verify_state(). The
optimizer decides *what* should happen; an adapter is what actually reads or
changes device state, whether that's a local simulation or a real vendor API.

Swapping SimulatedDeviceAdapter for a real adapter below is the entire
integration step — nothing in agent/tools.py, optimizer/engine.py, or the
Strands tool contracts changes.

STATUS OF EACH ADAPTER IN THIS FILE:
  SimulatedDeviceAdapter    — real, working, used by the whole demo today.
  BYDAdapter                — real integration paths, NOT wired to a live
                               account. BYD has no official public API (unlike
                               Tesla) — see class docstring for the two paths
                               people actually use.
  EVSEMeteredChargingAdapter— real API shape, NOT wired to a live charger.
                               Brand-agnostic: meters charging power at the
                               charger itself rather than trusting the car's
                               own telemetry, so it works for ANY EV.
  TeslaAdapter              — real API shape, NOT wired to a live account.
  WallboxAdapter            — real API shape, NOT wired to a live account.
  HomeAssistantAdapter      — real, LOCAL API shape; this is the most
                               realistic near-term path for most households
                               (see notes below) and could be finished with a
                               live HA instance + long-lived token.
  SolarEdgeInverterAdapter  — real API shape, NOT wired to a live account.
  CircuitCTMonitorAdapter   — real API/local shape (Emporia Vue / Sense /
                               IoTaWatt style clamp-on monitor), NOT wired to
                               a live device. This is how VOLT would see a
                               "dumb" appliance like a basic AC that has no
                               network connection of its own.
  BasicApplianceControlAdapter — real shape for controlling a non-smart
                               appliance via a smart plug or IR blaster, NOT
                               wired to a live device.
  UtilityPriceAdapter       — an actually-public, no-auth API (Awattar/Octopus
                               Agile-style day-ahead pricing) — this one is
                               the closest to "would just work" without a
                               partnership or paid API key.

None of these vendor calls can run from this sandbox (no network path to
BYD/Wallbox/HA/SolarEdge/Emporia/utility endpoints — see the allowed-domains
list in the deployment environment), so they are written to the real,
documented API shapes but are unauthenticated stubs. They're meant to be
finished by plugging in credentials once deployed somewhere with real
network access.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


# --------------------------------------------------------------------------
# The adapter contract every device — simulated or real — must implement.
# --------------------------------------------------------------------------
class DeviceAdapter(ABC):
    """One adapter per physical device. This is the ONLY thing that touches
    hardware or a vendor API. Agent and optimizer code never talk to a
    device except through these three calls."""

    @abstractmethod
    def get_state(self) -> dict:
        """Read current device state (SOC, power draw, online/offline, etc.)."""

    @abstractmethod
    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        """Request the device do something (charge/discharge/hold at power_kw).
        Returns {'accepted': bool, ...} — acceptance is NOT proof of success."""

    @abstractmethod
    def verify_state(self) -> dict:
        """Re-read the device's ACTUAL state after an action, so the agent can
        confirm what really happened instead of assuming the request worked."""


# --------------------------------------------------------------------------
# REAL, WORKING TODAY — wraps the household simulator.
# --------------------------------------------------------------------------
class SimulatedDeviceAdapter(DeviceAdapter):
    """Wraps simulator/environment.py. This is what the whole current demo
    runs on. Clearly labeled SIMULATED everywhere it surfaces in the UI/README."""

    def __init__(self, sim, device: str):
        self.sim = sim          # HouseholdSimulator instance
        self.device = device    # "ev" | "battery"

    def get_state(self) -> dict:
        snap = self.sim.snapshot()
        sub = snap.get(self.device, snap)
        return {"device": self.device, **sub}

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        if self.device == "ev" and not self.sim.ev.charger_online:
            return {"accepted": False, "reason": "EV charger offline (simulated fault)"}
        before = self.sim.snapshot()
        self.sim.advance(duration_minutes, plan_actions=[{"device": self.device, "power_kw": power_kw}])
        after = self.sim.snapshot()
        return {"accepted": True, "before": before, "after": after}

    def verify_state(self) -> dict:
        return self.get_state()


# --------------------------------------------------------------------------
# REAL API SHAPES, NOT LIVE — the actual integration path for each vendor.
# Each class below documents exactly what's needed to make it real.
# --------------------------------------------------------------------------
@dataclass
class VendorCredentials:
    """Holds whatever auth a real adapter needs. Never populated with real
    secrets in this repo — see README 'Security' section. In production this
    is loaded from a secrets manager (e.g. AWS Secrets Manager), never from
    the frontend or agent conversation."""
    access_token: str | None = None
    refresh_token: str | None = None
    base_url: str | None = None
    device_id: str | None = None


class TeslaAdapter(DeviceAdapter):
    """
    Real integration path: Tesla Fleet API (fleet-api.prd.na.vn.cloud.tesla.com).
      1. Register a Tesla developer app, get client_id/secret.
      2. User completes Tesla's OAuth flow, granting `vehicle_device_data` and
         `vehicle_cmds` scopes; VOLT stores the resulting refresh token.
      3. Reads:  GET /api/1/vehicles/{id}/vehicle_data  -> charge_state.battery_level,
         charge_state.charging_state, charge_state.charge_limit_soc.
      4. Writes: POST /api/1/vehicles/{id}/command/charge_start (or charge_stop,
         set_charging_amps, set_charge_limit).
      5. Tesla vehicles sleep to save 12V battery power — a command may need to
         wake the car first (POST .../wake_up) before it responds, which adds
         real latency VOLT's event loop needs to tolerate (this is exactly the
         kind of "action accepted, verify separately" pattern apply_action/
         verify_state already model).
    Not connected here: needs a live OAuth-authorized account and network
    access this sandbox doesn't have.
    """
    def __init__(self, creds: VendorCredentials):
        self.creds = creds

    def get_state(self) -> dict:
        raise NotImplementedError(
            "Would call GET /api/1/vehicles/{id}/vehicle_data on "
            "fleet-api.prd.na.vn.cloud.tesla.com with creds.access_token — "
            "requires a live OAuth-connected Tesla account."
        )

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        raise NotImplementedError(
            "Would POST /api/1/vehicles/{id}/command/charge_start or "
            "set_charging_amps — requires a live OAuth-connected Tesla account."
        )

    def verify_state(self) -> dict:
        return self.get_state()


class BYDAdapter(DeviceAdapter):
    """
    Real integration path for a BYD Atto 3 (or any BYD EV) — UNLIKE TESLA,
    BYD DOES NOT PUBLISH AN OFFICIAL PUBLIC API. Two real paths people are
    actually using today:

      Path A — unofficial BYD app API integration (starting point)
        A community-maintained, reverse-engineered client for the private
        API behind the BYD companion app, installed into Home Assistant as
        a HACS custom integration. Needs: a BYD account (a *second* account
        sharing the car is recommended so the integration logging in doesn't
        keep kicking your own phone's app session out), that account's app
        password, and the car's 6-digit remote-control verification PIN
        (set up in the BYD app's authorisation settings first). Once
        configured it exposes ~130 entities in HA, including battery SOC
        and charge rate.

      Path B — OBD-II telematics dongle (e.g. WiCAN Pro)
        A small dongle plugged into the car's OBD-II port (may need a
        right-angle extension cable to tuck it under the dash), joins your
        home WiFi, and has its own HACS integration. Reads HV battery
        SOC/voltage/temperature directly off the CAN bus, independent of
        BYD's servers — keeps working even if the unofficial API breaks.

      Path C, and what this repo actually recommends by default — don't
        read the car at all, read the CHARGER. See
        EVSEMeteredChargingAdapter below. VOLT's real requirement is "how
        much power is flowing into the car right now", and metering that at
        the home charger sidesteps BYD's API instability entirely and works
        for any EV brand.

    Whichever path is used, the SOC/charge-rate values end up as Home
    Assistant entities either way — a real deployment can just point
    HomeAssistantAdapter at `sensor.<car>_battery_level` instead of writing
    BYD-specific code at all.
    """
    def __init__(self, creds: VendorCredentials, path: str = "unofficial_app_api"):
        self.creds = creds
        self.path = path  # "unofficial_app_api" | "obd_dongle"

    def get_state(self) -> dict:
        if self.path == "unofficial_app_api":
            raise NotImplementedError(
                "Would call the unofficial BYD app API (community-reverse-"
                "engineered, HACS-installed) for battery_level/charging_state "
                "— requires a live BYD account + control PIN, and is subject "
                "to breaking if BYD changes their private API."
            )
        raise NotImplementedError(
            "Would read HV battery SOC/voltage over the car's CAN bus via a "
            "WiCAN Pro OBD-II dongle's Home Assistant integration — requires "
            "the physical dongle installed and joined to the home network."
        )

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        return {
            "accepted": False,
            "reason": (
                "Neither the unofficial BYD API nor an OBD dongle can reliably "
                "start/stop charging on a BYD today — control belongs at the "
                "charger, not the car. Use EVSEMeteredChargingAdapter instead."
            ),
        }

    def verify_state(self) -> dict:
        return self.get_state()


class EVSEMeteredChargingAdapter(DeviceAdapter):
    """
    Real integration path for controlling AND metering EV charging that
    works regardless of car brand (BYD, Tesla, anything) — because it reads
    and controls the home charger (EVSE), not the vehicle. This is the most
    robust path for a non-Tesla EV like a BYD Atto 3, since it doesn't
    depend on any car-vendor API staying stable.

      Requires: a network-connected home charger — e.g. Wallbox, Zappi,
      Ohme, or any OCPP-compliant charger (OCPP — Open Charge Point
      Protocol — is an open standard many chargers support, and Home
      Assistant has a generic `ocpp` integration for it, so it isn't locked
      to one vendor the way car-side APIs are).

      Reads:  charger's local/cloud API or OCPP `MeterValues` message ->
              live charging power (kW), energy delivered this session
              (kWh), plug-connected/charging/idle status.
      Writes: OCPP `RemoteStartTransaction`/`RemoteStopTransaction`, or the
              charger vendor's own start/stop + set-max-current calls (this
              is what actually throttles delivered power — "charge at 3kW
              instead of 7kW" is a real, supported operation on most smart
              chargers, unlike most car APIs).

    This also sidesteps "does VOLT actually know the car reached target
    SOC" without needing car telemetry: integrate delivered kWh (from the
    charger) against the car's rated battery capacity to estimate SOC —
    less precise than reading the car directly, but doesn't depend on an
    unofficial API staying up.

    Not connected here: requires a live charger on the home network.
    """
    def __init__(self, creds: VendorCredentials):
        self.creds = creds

    def get_state(self) -> dict:
        raise NotImplementedError(
            "Would read live charging power / session energy via the "
            "charger's local API or OCPP MeterValues — requires a live "
            "network-connected EVSE."
        )

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        raise NotImplementedError(
            "Would send an OCPP RemoteStartTransaction/RemoteStopTransaction "
            "or the charger's set-max-current call — requires a live EVSE."
        )

    def verify_state(self) -> dict:
        return self.get_state()


class WallboxAdapter(DeviceAdapter):
    """
    Real integration path for a non-Tesla EV charger (Wallbox Pulsar/Commander):
    Wallbox's public REST API (api.wall-box.com) — authenticate with account
    email/password to get a bearer token, then:
      Reads:  GET /chargers/status/{charger_id} -> status_id, added_range,
              charging_power.
      Writes: POST /v2/charger/{charger_id} with {"maxChargingCurrent": ...}
              to throttle rate, or /charger/{id}/remote-action for start/stop.
    Not connected here: requires a live Wallbox account.
    """
    def __init__(self, creds: VendorCredentials):
        self.creds = creds

    def get_state(self) -> dict:
        raise NotImplementedError("Requires a live Wallbox account on api.wall-box.com.")

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        raise NotImplementedError("Requires a live Wallbox account on api.wall-box.com.")

    def verify_state(self) -> dict:
        return self.get_state()


class HomeAssistantAdapter(DeviceAdapter):
    """
    Real integration path for battery, HVAC, water heater, and flexible
    appliances (dishwasher/washer smart plugs) — via a self-hosted or
    HA Cloud instance running Home Assistant, which already normalizes most
    smart-home vendors (Tesla Powerwall, Enphase, smart plugs, thermostats)
    behind one local REST/WebSocket API. This is the MOST REALISTIC near-term
    integration for most households, since it doesn't require a separate
    OAuth partnership per vendor — the user just points VOLT at their
    existing HA instance.
      Auth:   long-lived access token generated in the user's HA profile.
      Reads:  GET {base_url}/api/states/{entity_id} -> state, attributes
              (e.g. sensor.powerwall_charge, switch.dishwasher).
      Writes: POST {base_url}/api/services/{domain}/{service} with
              {"entity_id": ...} — e.g. switch/turn_on, climate/set_temperature.
    Not connected here: requires the user's HA base_url and a live token.
    """
    def __init__(self, creds: VendorCredentials, entity_id: str):
        self.creds = creds
        self.entity_id = entity_id

    def get_state(self) -> dict:
        raise NotImplementedError(
            f"Would GET {self.creds.base_url}/api/states/{self.entity_id} "
            "with a Home Assistant long-lived access token."
        )

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        raise NotImplementedError(
            f"Would POST {self.creds.base_url}/api/services/... to control "
            f"{self.entity_id} — requires a live Home Assistant instance."
        )

    def verify_state(self) -> dict:
        return self.get_state()


class SolarEdgeInverterAdapter(DeviceAdapter):
    """
    Real integration path for solar generation data: SolarEdge Monitoring API
    (monitoringapi.solaredge.com) or Enphase Enlighten API, keyed by API key
    + site_id issued to the installer/owner.
      Reads:  GET /site/{site_id}/currentPowerFlow.json -> PV/load/grid/storage
              kW, refreshed roughly every 15 min (inverter reporting interval —
              a real constraint VOLT's forecast/re-plan cadence has to respect;
              it can't get truly real-time solar data faster than the inverter
              reports it).
      Writes: none — inverters are read-only for generation; VOLT never
              "controls" solar production, only reacts to it.
    Not connected here: requires a live SolarEdge/Enphase account + site_id.
    """
    def __init__(self, creds: VendorCredentials):
        self.creds = creds

    def get_state(self) -> dict:
        raise NotImplementedError("Requires a live SolarEdge/Enphase monitoring account.")

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        return {"accepted": False, "reason": "Solar generation is read-only — VOLT cannot control panel output."}

    def verify_state(self) -> dict:
        return self.get_state()


class CircuitCTMonitorAdapter(DeviceAdapter):
    """
    Real integration path for READING a "dumb" appliance that has no network
    connection of its own — a basic window/central AC unit, an electric
    water heater, an old dryer. VOLT doesn't talk to the appliance; it reads
    the CIRCUIT that feeds it, via a clamp-on current-transformer (CT) home
    energy monitor (Emporia Vue, Sense, IoTaWatt, etc.) installed in the
    breaker panel.

      How it physically works: a CT clamp goes around the single wire
      feeding the appliance's breaker — no rewiring, no cutting power, no
      modification to the appliance — and reports that circuit's live power
      draw. This is genuinely non-invasive and is how "basic" appliances
      become visible to VOLT at all.

      Reads:  the monitor's local or cloud API for a named circuit, e.g.
              Emporia's cloud API `GET /devices/{deviceGid}/usage` for the
              channel mapped to "AC" in the app, or (if using an ESPHome-
              flashed monitor) a Home Assistant sensor entity directly —
              live watts, and typically a running kWh total.
      Writes: NONE. This adapter is read-only by construction — a CT clamp
              can only observe current, it cannot switch anything. That's
              exactly why apply_action always refuses below: seeing the
              AC's load and controlling the AC are two separate integrations
              (see BasicApplianceControlAdapter).

    Not connected here: requires a live CT-clamp monitor installed in the
    home's panel.
    """
    def __init__(self, creds: VendorCredentials, circuit_name: str):
        self.creds = creds
        self.circuit_name = circuit_name  # e.g. "ac_compressor", "water_heater"

    def get_state(self) -> dict:
        raise NotImplementedError(
            f"Would read live watts for circuit '{self.circuit_name}' from a "
            "CT-clamp home energy monitor's API (e.g. Emporia Vue) or its "
            "Home Assistant entity — requires a live installed monitor."
        )

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        return {
            "accepted": False,
            "reason": (
                "A CT clamp is read-only — it can see the AC's load but "
                "cannot switch it. Use BasicApplianceControlAdapter (smart "
                "plug/IR) or a smart thermostat for actual control."
            ),
        }

    def verify_state(self) -> dict:
        return self.get_state()


class BasicApplianceControlAdapter(DeviceAdapter):
    """
    Real integration path for CONTROLLING a non-smart appliance — the other
    half of the AC story. Which mechanism applies depends on what kind of AC
    it actually is:

      Window/portable/split unit AC, plugged into a wall outlet
        A smart plug (e.g. a Home-Assistant-compatible Zigbee/WiFi plug)
        between the AC and the wall gives real on/off control, and many
        smart plugs also report the power draw themselves — sometimes
        making a separate CT clamp unnecessary for that specific circuit.

      Window/split unit AC controlled only by an infrared remote
        Fine-grained control (temperature, fan speed, mode) needs an IR
        blaster (e.g. Broadlink, SwitchBot Hub) that's taught the remote's
        codes once and then replays them on command — HA has integrations
        for the common IR blaster brands.

      Central/ducted AC (the compressor has no accessible plug at all)
        The real point of control is the THERMOSTAT, not the outdoor unit —
        a smart thermostat (Ecobee, Nest, or any Home-Assistant-`climate`-
        compatible model) is what actually commands the compressor to run.
        This is a materially different integration (climate.set_temperature)
        from a plug or IR blaster and is the right one for most central-air
        households.

      Reads:  the underlying device's own state (plug on/off + wattage,
              thermostat's `climate` entity mode/setpoint) via Home
              Assistant — so in practice this often reuses
              HomeAssistantAdapter rather than needing its own class.
      Writes: `switch.turn_on`/`turn_off` (plug), a taught IR code (IR
              blaster), or `climate.set_temperature`/`set_hvac_mode`
              (thermostat) — via Home Assistant service calls.

    Not connected here: requires the specific live device (plug, IR
    blaster, or thermostat) actually installed.
    """
    def __init__(self, creds: VendorCredentials, control_kind: str, entity_id: str):
        self.creds = creds
        self.control_kind = control_kind  # "smart_plug" | "ir_blaster" | "thermostat"
        self.entity_id = entity_id

    def get_state(self) -> dict:
        raise NotImplementedError(
            f"Would read {self.entity_id}'s state via Home Assistant — "
            f"requires a live {self.control_kind} device."
        )

    def apply_action(self, action: str, power_kw: float, duration_minutes: int) -> dict:
        raise NotImplementedError(
            f"Would send a {self.control_kind} command for {self.entity_id} "
            "(switch toggle, replayed IR code, or climate setpoint) via Home "
            f"Assistant — requires a live {self.control_kind} device."
        )

    def verify_state(self) -> dict:
        return self.get_state()


class UtilityPriceAdapter:
    """
    Real integration path for electricity price data — NOT a DeviceAdapter
    (there's nothing to control), just a read source for get_price_forecast.
    Two realistic options:
      1. A day-ahead/dynamic pricing API from the household's own utility or
         market operator (varies by region/utility — e.g. many US utilities
         publish TOU rate schedules; some markets have public day-ahead APIs
         like Octopus Energy's Agile tariff API or Awattar in Europe).
      2. If no live pricing API is available, fall back to the household's
         configured static time-of-use rate schedule (still real, just not
         dynamic) — which is what simulator/environment.py's GridSim.price_schedule
         represents today.
    This is the adapter most likely to actually work unauthenticated in a
    real deployment (many day-ahead tariff APIs are public), which is why
    it's worth building first once real network access is available.
    """
    def __init__(self, api_base_url: str | None = None):
        self.api_base_url = api_base_url

    def get_forecast(self, horizon_minutes: int) -> dict:
        if not self.api_base_url:
            raise NotImplementedError(
                "No live pricing API configured — falling back to the "
                "household's static TOU schedule (see GridSim.price_schedule)."
            )
        raise NotImplementedError(f"Would GET {self.api_base_url}/prices — requires a live pricing API endpoint.")


def build_adapters(sim) -> dict[str, DeviceAdapter]:
    """
    The registry the rest of VOLT actually uses today: every device routed
    to its SimulatedDeviceAdapter. Nothing else in the codebase needs to
    change to go live, since agent/tools.py only ever calls
    get_state/apply_action/verify_state — swap entries here only.

    Example for a real household with a BYD Atto 3, solar, and a basic
    (non-smart) AC — matching the caveats documented on each class above:

        creds = VendorCredentials(base_url="http://homeassistant.local:8123",
                                   access_token="<long-lived HA token>")
        return {
            "ev": EVSEMeteredChargingAdapter(creds),           # meter/control at the charger, not the car
            "ev_telemetry": BYDAdapter(creds, path="obd_dongle"),  # optional: extra SOC precision
            "battery": HomeAssistantAdapter(creds, entity_id="sensor.powerwall_soc"),
            "solar": SolarEdgeInverterAdapter(creds),
            "ac_load": CircuitCTMonitorAdapter(creds, circuit_name="ac_compressor"),   # see it
            "ac_control": BasicApplianceControlAdapter(creds, control_kind="thermostat",
                                                         entity_id="climate.living_room"),  # control it
        }
    """
    return {
        "ev": SimulatedDeviceAdapter(sim, "ev"),
        "battery": SimulatedDeviceAdapter(sim, "battery"),
    }

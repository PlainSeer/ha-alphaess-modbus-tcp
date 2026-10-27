"""Build explicit Lovelace views from the integration's entity registry.

Discovery happens in Python during setup, not in a browser strategy. The
result is ordinary saved configuration and works before custom JS registers.
"""
import re

from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN

FLOW_KEYS = {"solar": "current_pv_production", "grid": "power_grid",
             "battery": "power_battery", "house": "current_house_load",
             "soc": "soc_battery", "health": "soh_battery",
             "temperature": "battery_max_cell_temp", "mode": "inverter_work_mode"}
OVERVIEW = [
    ("Energy today", [("today_pv_generation", "Solar generated"),
                      ("house_load_energy_today", "House consumption"),
                      ("today_energy_from_grid", "Grid imported"),
                      ("today_energy_feed_to_grid", "Grid exported"),
                      ("today_battery_charged", "Battery charged"),
                      ("today_battery_discharged", "Battery discharged")]),
    ("Solar strings", [(f"pv{i}_power", f"PV string {i}") for i in range(1, 5)] +
     [("active_power_pv_meter", "AC PV meter"), ("inverter_temperature", "Inverter temperature")]),
    ("Battery details", [("battery_status", "Status"), ("battery_voltage", "Voltage"),
                         ("battery_current", "Current"), ("battery_min_cell_temp", "Minimum cell temperature"),
                         ("battery_max_cell_temp", "Maximum cell temperature"),
                         ("battery_remaining_time", "Remaining time")]),
    ("Filtered power", [("current_house_load_filtered", "Filtered house load"),
                        ("current_pv_production_filtered", "Filtered solar"), ("excess_power", "Excess power")]),
    ("House energy estimates", [("house_energy_today", "Daily balance"),
                               ("house_energy_since_tracking", "Since tracking")]),
]
DIAGNOSTICS = [
    ("Faults & warnings", r"fault|warning"),
    ("Energy", r"^(total_energy_|today_|house_energy_|house_load_energy_)"),
    ("Dispatch & operating", r"dispatch|^force_|^peak_shaving|^excess_export|^(charging|discharging)_"),
    ("Battery", r"battery|^bms_|^lmu_"), ("Grid", r"grid|^(ovp|uvp|ofp|ufp)"),
    ("Solar", r"^pv|_pv_|^active_power_pv"),
    ("Inverter & system", r"inverter|^system_|^ems_|^iso_|^modbus_|^ip_|^local_ip$|^subnet_mask$|^gateway$"),
    ("Other sensors", r".*"),
]


def _name(entity):
    return entity.name or entity.original_name or entity.entity_id


def _entities(title, entities):
    return {"type": "entities", "title": title, "show_header_toggle": False, "entities": entities}


def _view(title, path, cards, icon):
    # Two native sections give the phone a single logical reading order.
    return {"title": title, "path": path, "icon": icon, "type": "sections", "max_columns": 2,
            "sections": [{"type": "grid", "cards": cards[::2]},
                         {"type": "grid", "cards": cards[1::2]}]}


def build_dashboard(entities, devices):
    """Pure builder; omit disabled/hidden entries, preserve renamed IDs."""
    groups = {}
    for entity in sorted(entities, key=lambda e: e.entity_id):
        entry = entity.config_entry_id
        if (entity.platform != DOMAIN or not entry or not entity.device_id
                or entity.disabled_by or entity.hidden_by
                or not entity.unique_id.startswith(f"{entry}_")):
            continue
        group = groups.setdefault(entry, {"device_id": entity.device_id, "entities": {}})
        if entity.device_id != group["device_id"]:
            continue
        key = entity.unique_id[len(entry) + 1:]
        group["entities"][(entity.domain, key)] = entity
    views = []
    for entry, group in sorted(groups.items()):
        indexed = group["entities"]
        device = devices.get(group["device_id"])
        name = (device.name_by_user or device.name) if device else "AlphaESS"
        prefix = f"{name} · " if len(groups) > 1 else ""
        bindings = {alias: indexed[("sensor", key)].entity_id for alias, key in FLOW_KEYS.items()
                    if ("sensor", key) in indexed}
        flow = {"type": "custom:alphaess-power-flow", "entities": bindings}
        readings = []
        for title, specs in OVERVIEW:
            rows = [{"entity": indexed[("sensor", key)].entity_id, "name": label}
                    for key, label in specs if ("sensor", key) in indexed]
            if rows:
                readings.append(_entities(title, rows))
        controls, used = [], set()
        for key, title in [("force_charging", "Force charging"), ("force_discharging", "Force discharging"),
                           ("dispatch", "Dispatch"), ("force_import", "Force import"),
                           ("force_export", "Force export"), ("excess_export", "Excess export"),
                           ("peak_shaving", "Peak shaving")]:
            rows = []
            for (domain, stable), e in indexed.items():
                if stable != key and not stable.startswith(key + "_"):
                    continue
                if domain not in ("switch", "number", "select", "time") and not (
                        domain in ("sensor", "binary_sensor") and
                        (stable.endswith("countdown") or "pause" in stable)):
                    continue
                rows.append({"entity": e.entity_id, "name": _name(e)})
                used.add(e.entity_id)
            if rows:
                controls.append(_entities(title, rows))
        for domain, title in [("switch", "Other switches"), ("number", "Operating settings"),
                              ("select", "Operating modes"), ("time", "Scheduling")]:
            rows = [{"entity": e.entity_id, "name": _name(e)} for (d, _), e in indexed.items()
                    if d == domain and e.entity_id not in used]
            if rows:
                controls.append(_entities(title, rows))
        # Native tiles open more-info; no one-tap restart/reset actions.
        buttons = [{"type": "tile", "entity": e.entity_id, "name": _name(e),
                    "tap_action": {"action": "more-info"},
                    "icon_tap_action": {"action": "more-info"}}
                   for (domain, _), e in indexed.items() if domain == "button"]
        if buttons:
            controls.append({"type": "heading", "heading": "Maintenance", "icon": "mdi:tools"})
            controls.extend(buttons)
        diagnostics = []
        remaining = {k: e for k, e in indexed.items() if k[0] in ("sensor", "binary_sensor")}
        for title, pattern in DIAGNOSTICS:
            matches = [k for k in remaining if re.search(pattern, k[1])]
            rows = [{"entity": remaining[k].entity_id, "name": _name(remaining[k])} for k in matches]
            if rows:
                diagnostics.append(_entities(title, rows))
            for k in matches:
                del remaining[k]
        # Preserve the existing routes while moving the cards between saved views.
        views.extend([_view(prefix + "Main", f"{entry}-overview", [flow, *controls], "mdi:solar-panel"),
                      _view(prefix + "Readings", f"{entry}-controls", readings, "mdi:chart-box-outline"),
                      _view(prefix + "Diagnostics", f"{entry}-diagnostics", diagnostics, "mdi:information-outline")])
    return {"views": views} if views else None


def dashboard_from_registry(hass):
    """Read the registries only; never reads or writes inverter registers."""
    entities = [e for e in er.async_get(hass).entities.values()
                if not ((state := hass.states.get(e.entity_id)) and state.attributes.get("restored"))]
    registry = dr.async_get(hass)
    devices = {device_id: registry.async_get(device_id)
               for device_id in {e.device_id for e in entities if e.device_id and e.platform == DOMAIN}}
    return build_dashboard(entities, devices)

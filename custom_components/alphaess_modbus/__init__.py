from __future__ import annotations

import re

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .config_flow import CONF_SLAVE_ID
from .const import DOMAIN, PLATFORMS
from .coordinator import AlphaESSCoordinator
from .modbus_client import AlphaESSModbusClient

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_WRITE_REGISTER = "write_register"
SERVICE_WRITE_REGISTER_SCHEMA = vol.Schema({
    vol.Required("address"): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
    vol.Required("value"): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
})

_MODEL_PREFIXES: dict[str, str] = {
    "SMILE-T10": "SMILE-T10",
    "SMILE-M5": "SMILE-M5",
    "SMILE-B3": "SMILE-B3",
    "SMILE-S5": "SMILE-S5",
    "SMILE5": "SMILE5",
    "STORION": "STORION",
    "ALD": "Alphastore",
}


def _detect_model(raw_sn: str) -> str:
    if not raw_sn:
        return "SMILE series"
    sn_upper = raw_sn.upper()
    for prefix in sorted(_MODEL_PREFIXES, key=len, reverse=True):
        if sn_upper.startswith(prefix):
            return _MODEL_PREFIXES[prefix]
    _match = re.match(r"^(.+?)(?:20\d{2}|19\d{2})", raw_sn)
    if _match:
        return _match.group(1).rstrip("-_ ").strip() or "SMILE series"
    return raw_sn[:20].strip() or "SMILE series"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register optional frontend assets without changing integration entries."""
    from .dashboard import async_setup_dashboard

    await async_setup_dashboard(hass)
    try:
        from .dashboard_install import async_watch_dashboard
        async_watch_dashboard(hass)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not install optional AlphaESS dashboard")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.exceptions import ConfigEntryNotReady

    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    client = AlphaESSModbusClient(
        host=host,
        port=port,
        slave_id=entry.data[CONF_SLAVE_ID],
    )
    try:
        await client.connect()
    except Exception as err:
        raise ConfigEntryNotReady(f"Cannot connect to {host}:{port}: {err}") from err
    if not client.connected:
        raise ConfigEntryNotReady(f"Cannot connect to {host}:{port}")

    coordinator = AlphaESSCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await client.close()
        raise

    raw_sn = (coordinator.data.get("inverter_sn") or "").strip().rstrip("\x00").strip()
    _model = _detect_model(raw_sn)
    _serial = raw_sn or None

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if not hass.services.has_service(DOMAIN, SERVICE_WRITE_REGISTER):
        async def _handle_write_register(call: ServiceCall) -> None:
            address: int = call.data["address"]
            value: int = call.data["value"]
            # hass.data[DOMAIN] also holds non-coordinator entries (e.g. the
            # per-entry switches map), so only call coordinators.
            for obj in list(hass.data[DOMAIN].values()):
                if isinstance(obj, AlphaESSCoordinator):
                    await obj.async_write_raw(address, value)

        hass.services.async_register(
            DOMAIN,
            SERVICE_WRITE_REGISTER,
            _handle_write_register,
            schema=SERVICE_WRITE_REGISTER_SCHEMA,
        )

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="AlphaESS Inverter",
        manufacturer="AlphaESS",
        model=_model,
        serial_number=_serial,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        from .dashboard_install import async_install_dashboard
        await async_install_dashboard(hass)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not install optional saved AlphaESS dashboard")
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.options.get("model_variant", "standard") != coordinator._model_variant:
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        coordinator.apply_poll_options(entry.options)
        await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.components import persistent_notification

    coordinator = hass.data[DOMAIN][entry.entry_id]
    switches = hass.data[DOMAIN].get(f"{entry.entry_id}_switches", {})
    owner = coordinator.active_dispatch_key
    remaining = []
    for key, sw in switches.items():
        if key == owner or (owner is None and sw._uncertain is not None):
            expiry = sw._expected_expiry.isoformat() if sw._expected_expiry else "unknown"
            remaining.append(f"{sw._attr_name}: expected end {expiry}")
    if remaining:
        persistent_notification.async_create(
            hass, "Integration unloading; dispatch may still be running on the inverter. "
            + "; ".join(remaining) + ". Unacknowledged writes make expiry uncertain. "
            "After reload, Dispatch Reset explicitly stops dispatch. No mode is resumed automatically.",
            title="AlphaESS dispatch may remain active", notification_id=f"alphaess_unload_{entry.entry_id}",
        )
    coordinator._unloading = True
    # Stop all producers before awaiting even the first platform removal.
    from .switch import _MUTEX_SWITCHES
    for key, sw in switches.items():
        if key in _MUTEX_SWITCHES:
            sw.prepare_force_off()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        # Keep the connection usable for explicit recovery, but do not resume.
        coordinator._unloading = False
    if unloaded:
        coordinator: AlphaESSCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_switches", None)
        await coordinator.client.close()
        if not any(isinstance(o, AlphaESSCoordinator) for o in hass.data[DOMAIN].values()):
            hass.services.async_remove(DOMAIN, SERVICE_WRITE_REGISTER)
    return unloaded

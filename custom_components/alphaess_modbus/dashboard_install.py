"""Create the default AlphaESS dashboard using HA's Lovelace APIs.

Maintain the integration-owned default, including previously edited layouts.
Never replace an unrelated dashboard, change the HA landing page, or call a service.
"""
import logging
import asyncio
from inspect import unwrap

from homeassistant.components.lovelace.dashboard import DashboardsCollection, LovelaceStorage
from homeassistant.helpers.storage import Store
from homeassistant.core import callback
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from .dashboard_config import dashboard_from_registry

_LOGGER = logging.getLogger(__name__)
URL_PATH = "dashboard-alphaess-native"
STRATEGY = {"strategy": {"type": "custom:alphaess-modbus"}}
OWNER_STORE = "alphaess_modbus.saved_dashboard"


def is_beta_dashboard(config):
    """Only recognise the exact three-view beta card structure we installed."""
    views = config.get("views", [])
    return (
        set(config) == {"views"}
        and len(views) == 3
        and [v.get("path") for v in views] == ["overview", "controls", "diagnostics"]
        and all(v.get("type") == "panel" and len(v.get("cards", [])) == 1
                and v["cards"][0].get("type") == "custom:alphaess-beta-live"
                and set(v) <= {"title", "path", "type", "cards"}
                and set(v["cards"][0]) <= {"type", "view"}
                and v["cards"][0].get("view", v["path"]) == v["path"]
                for v in views)
    )


async def async_install_dashboard(hass):
    """Run once after Lovelace setup; persist registration through its collection."""
    data = hass.data.get("lovelace")
    dashboards = data.get("dashboards") if isinstance(data, dict) else getattr(data, "dashboards", None)
    if dashboards is None or hass.config.recovery_mode:
        return
    generated = dashboard_from_registry(hass)
    if generated is None:
        return  # Fresh installation: wait for the integration's entities.
    owner = Store(hass, 1, OWNER_STORE)
    saved = await owner.async_load() or {}
    current = dashboards.get(URL_PATH)
    if current is not None:
        if not isinstance(current, LovelaceStorage):
            return  # YAML dashboards remain user-managed.
        config = await current.async_load(False)
        # Ownership survives manual edits. A separate copy at another URL is
        # user-managed; a dashboard merely sharing our URL is not enough.
        if is_beta_dashboard(config) or config == STRATEGY or isinstance(saved.get("config"), dict):
            if config == generated:
                return
            # A targeted rollback record, saved before replacing the known beta.
            backup = Store(hass, 1, "alphaess_modbus.saved_dashboard_backup")
            if not await backup.async_load():
                await backup.async_save({"url_path": URL_PATH, "config": config})
            # Retain the original migration backup and the immediately previous
            # layout. A failed backup must prevent replacement.
            previous = Store(hass, 1, "alphaess_modbus.saved_dashboard_previous")
            await previous.async_save({"url_path": URL_PATH, "config": config})
            await current.async_save(generated)
            await owner.async_save({"config": generated})
        return

    # Reuse HA's live collection. A second collection would have a stale copy
    # of dashboard metadata and could lose changes made later through the UI.
    collection = _dashboard_collection(hass)
    if collection is None:
        _LOGGER.warning("Automatic AlphaESS dashboard creation unavailable; see the saved dashboard guide")
        return
    await collection.async_create_item({
        "url_path": URL_PATH, "title": "AlphaESS",
        "icon": "mdi:solar-panel", "show_in_sidebar": True, "require_admin": True,
    })
    # HA's own collection listener registers the panel and storage object.
    await dashboards[URL_PATH].async_save(generated)
    await owner.async_save({"config": generated})
    _LOGGER.info("Created default AlphaESS dashboard at /%s", URL_PATH)


def async_watch_dashboard(hass):
    """Debounce registry changes; refresh our owned default only when needed."""
    key = "alphaess_modbus_saved_dashboard_watcher"
    if key in hass.data:
        return
    pending = None
    lock = asyncio.Lock()

    async def update():
        async with lock:
            try:
                await async_install_dashboard(hass)
            except Exception:
                _LOGGER.exception("Could not update optional saved AlphaESS dashboard")

    @callback
    def changed(_event):
        nonlocal pending
        if pending:
            pending.cancel()
        pending = hass.loop.call_later(1, lambda: hass.async_create_task(update()))

    unsubscribers = [hass.bus.async_listen(event, changed) for event in
                    ("entity_registry_updated", "device_registry_updated")]
    unsubscribers.append(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, changed))
    hass.data[key] = unsubscribers


def _dashboard_collection(hass):
    """Resolve HA's registered collection, guarded for supported HA versions.

    The list command wraps a bound DashboardsCollectionWebSocket method with
    functools.wraps on HA 2024.12 and 2026.10. Never create a competing Store.
    """
    command = hass.data.get("websocket_api", {}).get("lovelace/dashboards/list")
    if not command:
        return None
    owner = getattr(unwrap(command[0]), "__self__", None)
    collection = getattr(owner, "storage_collection", None)
    return collection if isinstance(collection, DashboardsCollection) else None

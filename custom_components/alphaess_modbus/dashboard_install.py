"""Create the default AlphaESS dashboard using HA's Lovelace APIs.

Maintain the integration-owned default, including previously edited layouts.
Never replace an unrelated dashboard, change the HA landing page, or call a service.
"""
import logging
import asyncio
from inspect import unwrap

from homeassistant.components.lovelace.dashboard import ConfigNotFound, DashboardsCollection, LovelaceStorage
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.core import callback
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from .dashboard_config import dashboard_from_registry
from .dashboard_options import dashboard_management_enabled
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
URL_PATH = "dashboard-alphaess-native"
STRATEGY = {"strategy": {"type": "custom:alphaess-modbus"}}
OWNER_STORE = "alphaess_modbus.saved_dashboard"
LOCK_KEY = "alphaess_modbus_saved_dashboard_lock"


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
    """Serialize setup, preference changes and background updates together."""
    lock = hass.data.setdefault(LOCK_KEY, asyncio.Lock())
    async with lock:
        await _async_install_dashboard(hass)


async def _async_install_dashboard(hass):
    if not dashboard_management_enabled(hass):
        return
    data = hass.data.get("lovelace")
    dashboards = data.get("dashboards") if isinstance(data, dict) else getattr(data, "dashboards", None)
    if dashboards is None or hass.config.recovery_mode:
        return
    generated = dashboard_from_registry(hass)
    if generated is None:
        return  # Fresh installation: wait for the integration's entities.
    owner = Store(hass, 1, OWNER_STORE, atomic_writes=True)
    saved = await owner.async_load() or {}
    current = dashboards.get(URL_PATH)
    created = None
    collection = None
    if current is None:
        collection = _dashboard_collection(hass)
        if collection is None:
            _LOGGER.warning("Automatic AlphaESS dashboard creation unavailable; see the saved dashboard guide")
            return
        if not dashboard_management_enabled(hass):
            return
        created = await collection.async_create_item({
            "url_path": URL_PATH, "title": "AlphaESS",
            "icon": "mdi:solar-panel", "show_in_sidebar": True, "require_admin": True,
        })
        current = dashboards[URL_PATH]

    if current is not None:
        if not isinstance(current, LovelaceStorage):
            return  # YAML dashboards remain user-managed.
        metadata = getattr(current, "config", None)
        dashboard_id = metadata.get("id") if isinstance(metadata, dict) else None
        if not created and "dashboard_id" in saved and saved["dashboard_id"] != dashboard_id:
            return
        pending = saved.get("pending") if isinstance(saved.get("pending"), dict) else None
        try:
            config = await current.async_load(False)
        except ConfigNotFound:
            if not created and not pending:
                return
            config = None
        if not (created or pending or is_beta_dashboard(config or {}) or config == STRATEGY
                or isinstance(saved.get("config"), dict)):
            return
        record = {"config": generated}
        if dashboard_id is not None:
            record["dashboard_id"] = dashboard_id
        if config == generated and not pending:
            # Complete ownership metadata even if a previous layout save worked.
            if saved != record:
                await owner.async_save(record)
            return
        previous = pending.get("previous") if pending and (config is None or config == pending.get("target")) else config
        if previous is not None:
            backup = Store(hass, 1, "alphaess_modbus.saved_dashboard_backup", atomic_writes=True)
            if not await backup.async_load():
                await backup.async_save({"url_path": URL_PATH, "config": previous})
            # Retain the original migration backup and the immediately previous
            # layout. A failed backup must prevent replacement.
            previous_store = Store(hass, 1, "alphaess_modbus.saved_dashboard_previous", atomic_writes=True)
            await previous_store.async_save({"url_path": URL_PATH, "config": previous})
        if not dashboard_management_enabled(hass):
            return
        # HA mutates its layout cache before attempting the disk write. A durable
        # pending record prevents a failed write being mistaken for a later no-op.
        pending_record = {**record, "config": previous,
                          "pending": {"previous": previous, "target": generated}}
        try:
            await owner.async_save(pending_record)
        except BaseException:
            # No content was saved yet. Roll back only this newly-created blank
            # registration, never an existing or subsequently user-edited layout.
            if created and dashboards.get(URL_PATH) is current:
                try:
                    await current.async_load(False)
                except ConfigNotFound:
                    await collection.async_delete_item(created["id"])
            raise
        if not dashboard_management_enabled(hass):
            return
        if dashboards.get(URL_PATH) is not current:
            # A user replaced/removed the dashboard while storage was awaited.
            # Do not save into the detached object or leave ownership behind.
            await owner.async_save({})
            return
        try:
            latest = await current.async_load(False)
        except ConfigNotFound:
            latest = None
        if latest != config:
            # Leave the journal pending. A later refresh backs up this newer
            # user edit before applying the normal managed-layout policy.
            return
        await current.async_save(generated)
        await owner.async_save(record)
        if created:
            _LOGGER.info("Created default AlphaESS dashboard at /%s", URL_PATH)


def async_watch_dashboard(hass):
    """Debounce registry changes; refresh our owned default only when needed."""
    key = "alphaess_modbus_saved_dashboard_watcher"
    if key in hass.data:
        return
    pending = None
    registry = er.async_get(hass)
    known_entities = {e.entity_id for e in registry.entities.values() if e.platform == DOMAIN}
    known_devices = {e.device_id for e in registry.entities.values() if e.platform == DOMAIN and e.device_id}

    async def update():
        try:
            await async_install_dashboard(hass)
        except Exception:
            _LOGGER.exception("Could not update optional saved AlphaESS dashboard")

    @callback
    def changed(event):
        nonlocal pending
        if event.event_type == "entity_registry_updated":
            entity_id = event.data["entity_id"]
            old_id = event.data.get("old_entity_id")
            entry = registry.async_get(entity_id)
            relevant = (entity_id in known_entities or old_id in known_entities
                        or (entry is not None and entry.platform == DOMAIN))
            if not relevant:
                return
            known_entities.discard(old_id)
            known_entities.discard(entity_id)
            if entry is not None and entry.platform == DOMAIN:
                known_entities.add(entity_id)
                if entry.device_id:
                    known_devices.add(entry.device_id)
        elif event.event_type == "device_registry_updated":
            if event.data["device_id"] not in known_devices:
                return
            if event.data["action"] == "remove":
                known_devices.discard(event.data["device_id"])
        if not dashboard_management_enabled(hass):
            return
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

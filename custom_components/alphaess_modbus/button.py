from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DISPATCH_START_ADDR, RESET_MODE_ADDR
from .coordinator import AlphaESSCoordinator

_LOGGER = logging.getLogger(__name__)

_RESET_VERIFY_SECONDS = 5.0
_RESET_VERIFY_INTERVAL = 0.5
_RESET_VERIFY_ATTEMPTS = 11

BUTTON_DEFS = [
    {
        "key": "dispatch_reset",
        "name": "Dispatch Reset",
        "icon": "mdi:restart",
    },
    {
        "key": "synchronise_date_time",
        "name": "Synchronise Date & Time",
        "icon": "mdi:clock-check-outline",
    },
    {
        "key": "sync_dispatch_state",
        "name": "Sync Dispatch State",
        "icon": "mdi:sync",
    },
    {
        "key": "restart_pcs",
        "name": "Restart PCS",
        "icon": "mdi:restart",
    },
    {
        "key": "restart_ems",
        "name": "Restart EMS",
        "icon": "mdi:restart",
    },
    {
        "key": "reset_energy_totals",
        "name": "Reset Energy Totals",
        "icon": "mdi:counter",
    },
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AlphaESSCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AlphaESSButton(coordinator, entry, d) for d in BUTTON_DEFS
    )


class AlphaESSButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AlphaESSCoordinator,
        entry: ConfigEntry,
        definition: dict,
    ) -> None:
        self._coordinator = coordinator
        self._entry_id = entry.entry_id
        self._key = definition["key"]
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_name = definition["name"]
        self._attr_translation_key = self._key
        self._attr_icon = definition["icon"]
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self, **kwargs: Any) -> None:
        if self._key == "dispatch_reset":
            await self._reset_dispatch()
        elif self._key == "synchronise_date_time":
            await self._coordinator.async_sync_datetime()
        elif self._key == "sync_dispatch_state":
            await self._sync_dispatch_state()
        elif self._key == "restart_pcs":
            await self._coordinator.async_write_register(RESET_MODE_ADDR, 7)
        elif self._key == "restart_ems":
            await self._coordinator.async_write_register(RESET_MODE_ADDR, 8)
        elif self._key == "reset_energy_totals":
            await self._coordinator.async_write_register(RESET_MODE_ADDR, 1)

    async def _read_dispatch_state(self) -> int:
        # A recent poll may predate a newly issued dispatch command.
        value = await self._coordinator.client.read_register(DISPATCH_START_ADDR, "uint16")
        if value not in (0, 1):
            raise ValueError(f"Unknown dispatch state: {value!r}")
        return value

    async def _clear_dispatch_controls(self, *, preserve: set[str] | None = None) -> None:
        from .switch import _MUTEX_SWITCHES

        switches = self.hass.data[DOMAIN].get(f"{self._entry_id}_switches", {})
        controls = [switches[key] for key in _MUTEX_SWITCHES
                    if key in switches and key not in (preserve or set())]
        # Stop every producer before the first await can let queued work run.
        for sw in controls:
            sw.prepare_force_off()
        for sw in controls:
            await sw.async_force_off()

    async def _reset_dispatch(self) -> None:
        try:
            await self._clear_dispatch_controls()
            await self._coordinator.async_reset_dispatch()
            await self._verify_dispatch_stopped()
            persistent_notification.async_dismiss(
                self.hass, f"alphaess_dispatch_reset_{self._entry_id}"
            )
        except Exception as err:
            message = f"Dispatch Reset unresolved: {err}"
            _LOGGER.error(message)
            persistent_notification.async_create(
                self.hass, message, title="AlphaESS Dispatch Reset unresolved",
                notification_id=f"alphaess_dispatch_reset_{self._entry_id}",
            )
            # Fail the button action so recovery scripts cannot continue as if
            # reset succeeded. Never issue a second reset write automatically.
            raise HomeAssistantError(message) from err

    async def _verify_dispatch_stopped(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RESET_VERIFY_SECONDS
        last_error = "dispatch still active"
        for attempt in range(_RESET_VERIFY_ATTEMPTS):
            if attempt:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(_RESET_VERIFY_INTERVAL, remaining))
            try:
                if await self._read_dispatch_state() == 0:
                    return
                last_error = "dispatch still active"
            except Exception as err:
                last_error = str(err)
        # Bound retries without cancelling an in-flight shared Modbus request.
        raise HomeAssistantError(last_error)

    def _live_controller(self):
        """Return a locally owned autonomous controller with callbacks attached."""
        switches = self.hass.data[DOMAIN].get(f"{self._entry_id}_switches", {})
        key = self._coordinator.active_dispatch_key
        sw = switches.get(key)
        if sw is None or not sw.is_on:
            return None
        if key == "excess_export":
            live = sw._ee_listener_unsub is not None
        elif key == "peak_shaving":
            live = sw._listener_cancel is not None and sw._heartbeat_cancel is not None
        else:
            live = False
        return (key, sw, sw._control_generation) if live else None

    def _waiting_controller(self):
        """Keep paused controllers and their already-queued resume transitions."""
        controller = self._live_controller()
        if controller is None:
            return None
        key, sw, _ = controller
        if key == "excess_export":
            waiting = self._coordinator.ee_paused or any(not task.done() for task in sw._pending_tasks)
        else:
            waiting = sw.paused or sw._lock.locked()
        return controller if waiting else None

    async def _sync_dispatch_state(self) -> None:
        waiting_before_read = self._waiting_controller()
        try:
            dispatch_state = await self._read_dispatch_state()
        except Exception as err:
            _LOGGER.warning("sync_dispatch_state: live read failed; states unchanged: %s", err)
            return
        if dispatch_state == 1:
            _LOGGER.debug("sync_dispatch_state: dispatch active; ownership and switches unchanged")
            return
        retained = self._waiting_controller()
        if retained is None and waiting_before_read is not None:
            key, sw, generation = waiting_before_read
            # A resume may have begun while the direct read waited for Modbus.
            # Do not mistake that transition for a stopped controller.
            if self._live_controller() == (key, sw, generation):
                retained = waiting_before_read
        preserve = {retained[0]} if retained is not None else set()
        await self._clear_dispatch_controls(preserve=preserve)
        _LOGGER.debug("sync_dispatch_state: cleared stopped controls; preserved paused/resuming controllers: %s", preserve)

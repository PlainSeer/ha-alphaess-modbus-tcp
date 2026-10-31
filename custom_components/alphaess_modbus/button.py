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
        # A recent poll may predate a command (e.g. PV First Recovery).
        value = await self._coordinator.client.read_register(DISPATCH_START_ADDR, "uint16")
        if value not in (0, 1):
            raise ValueError(f"Unknown dispatch state: {value!r}")
        return value

    async def _clear_dispatch_controls(self) -> None:
        from .switch import _MUTEX_SWITCHES

        switches = self.hass.data[DOMAIN].get(f"{self._entry_id}_switches", {})
        controls = [switches[key] for key in _MUTEX_SWITCHES if key in switches]
        # Stop every producer before the first await can let queued work run.
        for sw in controls:
            sw.prepare_force_off()
        for sw in controls:
            await sw.async_force_off()

    async def _reset_dispatch(self) -> None:
        try:
            await self._clear_dispatch_controls()
            await self._coordinator.async_reset_dispatch()
            last_error = "dispatch still active"
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(0.5)
                try:
                    if await self._read_dispatch_state() == 0:
                        persistent_notification.async_dismiss(
                            self.hass, f"alphaess_dispatch_reset_{self._entry_id}"
                        )
                        return
                    last_error = "dispatch still active"
                except Exception as err:
                    last_error = str(err)
            raise HomeAssistantError(f"Dispatch Reset unresolved: {last_error}")
        except Exception as err:
            message = f"Dispatch Reset failed or could not be verified: {err}"
            _LOGGER.error(message)
            persistent_notification.async_create(
                self.hass, message, title="AlphaESS Dispatch Reset unresolved",
                notification_id=f"alphaess_dispatch_reset_{self._entry_id}",
            )
            # Fail the button action so recovery scripts cannot continue as if
            # reset succeeded. Never issue a second reset write automatically.
            raise HomeAssistantError(message) from err

    async def _sync_dispatch_state(self) -> None:
        try:
            dispatch_state = await self._read_dispatch_state()
        except Exception as err:
            _LOGGER.warning("sync_dispatch_state: live read failed; states unchanged: %s", err)
            return
        if dispatch_state == 1:
            # Power direction does not establish ownership. In particular, do
            # not turn an external PredBat command into a slider-controlled mode.
            return
        await self._clear_dispatch_controls()
        _LOGGER.info("sync_dispatch_state: cleared stopped dispatch controls")

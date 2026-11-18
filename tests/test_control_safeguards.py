import asyncio
from datetime import datetime, timezone
from inspect import signature
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import frame

from custom_components.alphaess_modbus import (
    SERVICE_WRITE_REGISTER_SCHEMA,
    _async_write_register_service,
)
from custom_components.alphaess_modbus.button import BUTTON_DEFS, AlphaESSButton
from custom_components.alphaess_modbus.const import DOMAIN, INVERTER_AC_LIMIT, NUMBER_REGISTERS, SELECT_REGISTERS
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator
from custom_components.alphaess_modbus.number import AlphaESSNumber
from custom_components.alphaess_modbus.select import AlphaESSSelect


def make_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    if hasattr(frame, "async_setup"):
        frame.async_setup(hass)
    return hass


def make_entry():
    extra = {"subentries_data": []} if "subentries_data" in signature(ConfigEntry).parameters else {}
    return ConfigEntry(
        domain=DOMAIN, title="AlphaESS", source="user", unique_id=None,
        version=1, minor_version=1, data={"host": "unused"}, options={},
        discovery_keys=MappingProxyType({}), **extra,
    )


def test_ac_limit_migrates_and_is_available_before_select_setup(tmp_path):
    async def scenario():
        hass = make_hass(tmp_path)
        hass.config_entries = ConfigEntries(hass, {})
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        client = SimpleNamespace(write_register=AsyncMock())
        coordinator = AlphaESSCoordinator(hass, entry, client)
        reg = next(reg for reg in SELECT_REGISTERS if reg.key == INVERTER_AC_LIMIT)
        select = AlphaESSSelect(coordinator, entry, reg)
        select.hass = hass
        with patch.object(select, "async_get_last_state", return_value=State("select.test", "8 kW")):
            await select.async_added_to_hass()
        assert entry.data == {"host": "unused", INVERTER_AC_LIMIT: "8 kW"}

        reloaded = AlphaESSCoordinator(hass, entry, client)
        power_reg = next(reg for reg in NUMBER_REGISTERS if reg.key == "dispatch_power")
        dispatch_power = AlphaESSNumber(reloaded, entry, power_reg)
        assert dispatch_power.native_min_value == -8
        assert dispatch_power.native_max_value == 8

        select = AlphaESSSelect(reloaded, entry, reg)
        select.hass = hass
        with (
            patch.object(select, "async_get_last_state") as restore,
            patch.object(select, "async_write_ha_state") as publish,
        ):
            await select.async_added_to_hass()
            restore.assert_not_called()
            assert select.current_option == "8 kW"
            await select.async_select_option("4.6 kW")
            publish.assert_called_once()
        assert entry.data[INVERTER_AC_LIMIT] == "4.6 kW"
        assert dispatch_power.native_min_value == -4.6
        client.write_register.assert_not_awaited()

    asyncio.run(scenario())


def test_clock_uses_ha_time_zone_and_packed_bcd(tmp_path):
    async def scenario():
        hass = make_hass(tmp_path)
        await hass.config.async_set_time_zone("Europe/London")
        client = SimpleNamespace(write_registers=AsyncMock())
        coordinator = AlphaESSCoordinator(hass, make_entry(), client)
        utc_time = datetime(2026, 10, 18, 17, 47, 56, tzinfo=timezone.utc)
        with patch(
            "custom_components.alphaess_modbus.coordinator.dt_util.now",
            side_effect=utc_time.astimezone,
        ), patch.object(coordinator, "async_request_refresh", new_callable=AsyncMock):
            await coordinator.async_sync_datetime()
        client.write_registers.assert_awaited_once_with(0x0740, [0x2610, 0x1818, 0x4756])

    asyncio.run(scenario())


def test_only_restart_and_energy_reset_buttons_default_to_disabled():
    disabled = {
        definition["key"] for definition in BUTTON_DEFS
        if not AlphaESSButton(None, make_entry(), definition).entity_registry_enabled_default
    }
    assert disabled == {"restart_pcs", "restart_ems", "reset_energy_totals"}


def test_raw_service_targets_one_inverter_and_refuses_disallowed_writes(tmp_path):
    async def scenario():
        hass = make_hass(tmp_path)
        first_entry, second_entry = make_entry(), make_entry()
        first = AlphaESSCoordinator(hass, first_entry, None)
        second = AlphaESSCoordinator(hass, second_entry, None)
        first.async_write_raw = AsyncMock()
        second.async_write_raw = AsyncMock()
        hass.data[DOMAIN] = {first_entry.entry_id: first, "unrelated_cache": {}}

        async def handle_write(call):
            await _async_write_register_service(hass, call)

        hass.services.async_register(DOMAIN, "write_register", handle_write, schema=SERVICE_WRITE_REGISTER_SCHEMA)

        async def write(address, **target):
            await hass.services.async_call(
                DOMAIN, "write_register", {"address": address, "value": 1, **target}, blocking=True
            )

        await write(0x0800)
        first.async_write_raw.assert_awaited_once_with(0x0800, 1)
        hass.data[DOMAIN][second_entry.entry_id] = second
        for address in (0x0740, 0x0804, 0x1100, 0xFFFF):
            with pytest.raises(ServiceValidationError):
                await write(address, config_entry_id=first_entry.entry_id)
        with pytest.raises(ServiceValidationError):
            await write(0x0885)
        with pytest.raises(ServiceValidationError):
            await write(0x0885, config_entry_id="missing")
        second._unloading = True
        with pytest.raises(ServiceValidationError):
            await write(0x0885, config_entry_id=second_entry.entry_id)
        await write(0x0885, config_entry_id=first_entry.entry_id)
        first.async_write_raw.assert_awaited_with(0x0885, 1)
        assert first.async_write_raw.await_count == 2
        second.async_write_raw.assert_not_awaited()

    asyncio.run(scenario())

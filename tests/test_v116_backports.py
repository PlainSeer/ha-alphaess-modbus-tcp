"""Read, diagnostics and dispatch checks for the v1.16.0 backports."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pymodbus.exceptions import ModbusException

from custom_components.alphaess_modbus.const import ModbusSensorDef, NUMBER_REGISTERS, SENSOR_REGISTERS
from custom_components.alphaess_modbus.house_load import HouseLoadSample, house_load_input_keys
from custom_components.alphaess_modbus.modbus_client import ModbusIllegalAddress
from custom_components.alphaess_modbus.number import AlphaESSNumber
from custom_components.alphaess_modbus.sensor import (
    AlphaESSModeCountdownSensor, AlphaESSSensor,
)

from .dispatch_simulation import DispatchSimulation
from .test_pv_meter_counter import coordinator, poll

REGISTERS = [
    ModbusSensorDef("first", "First", 10, "uint16", scan_interval=1),
    ModbusSensorDef("middle", "Middle", 12, "uint16", scan_interval=1),
    ModbusSensorDef("last", "Last", 14, "uint16", scan_interval=1),
]


@pytest.mark.parametrize("gap", [11, 12])
def test_rejected_address_splits_blocks_and_recovers_without_repeat_failures(gap):
    async def scenario():
        c = coordinator()
        rejects = True

        async def read(start, count):
            if rejects and start <= gap < start + count:
                raise ModbusIllegalAddress("unsupported")
            return list(range(start, start + count))

        c.client.read_block.side_effect = read
        with patch("custom_components.alphaess_modbus.coordinator.SENSOR_REGISTERS", REGISTERS):
            expected = {"first": 10, "last": 14}
            if gap == 11:
                expected["middle"] = 12
            assert await poll(c, 1000) == expected
            c.client.read_block.reset_mock()
            assert await poll(c, 1001) == expected
            assert await poll(c, 1002) == expected
            for call in c.client.read_block.await_args_list:
                start, count = call.args
                if start <= gap < start + count:
                    assert (start, count) == (gap, 1)
            c.client.read_block.reset_mock()
            assert await poll(c, 1003) == expected
            assert all(not (start <= gap < start + count)
                       for start, count in (call.args for call in c.client.read_block.await_args_list))
            rejects = False
            assert (await poll(c, 1602))["middle"] == 12
            assert "middle" not in c._unsupported_until

    asyncio.run(scenario())


def test_transport_failure_uses_the_existing_failure_path_without_splitting():
    async def scenario():
        c = coordinator()
        c.client.read_block.side_effect = ModbusException("offline")
        with patch("custom_components.alphaess_modbus.coordinator.SENSOR_REGISTERS", REGISTERS):
            with pytest.raises(UpdateFailed, match="All reads failed"):
                await poll(c, 1000)
        c.client.read_block.assert_awaited_once_with(10, 5)
        assert not c._block_splits and not c._unsupported_until

    asyncio.run(scenario())


def test_ups_load_uses_pv_and_battery_when_grid_is_absent():
    data = {f"pv{i}_power": 0 for i in range(1, 5)}
    data.update(inverter_work_mode=2, power_grid=1100, active_power_pv_meter=0,
                pv1_power=300, power_battery=500)
    sample = HouseLoadSample()
    sample.update(None, data, 100)
    assert sample.raw == 1100
    assert house_load_input_keys(data) == ("power_grid", "inverter_work_mode")
    data["power_grid"] = 0
    sample.update(None, data, 102)
    assert sample.raw == 800
    assert "power_battery" in house_load_input_keys(data)
    data["power_battery"] = None
    sample.update(None, data, 104)
    assert sample.value(104) is None


def test_system_mode_is_an_isolated_optional_read_and_battery_status_keeps_its_text():
    c = coordinator()
    regs = {reg.key: reg for reg in SENSOR_REGISTERS}
    reg = regs["system_mode"]
    assert (reg.address, reg.optional_read, reg.enabled_by_default) == (0x0805, True, False)
    c.data = {"system_mode": 3, "battery_status": 257}
    assert AlphaESSSensor(c, NS(entry_id="test"), reg).native_value == "Hybrid"
    battery = AlphaESSSensor(c, NS(entry_id="test"), regs["battery_status"])
    assert battery.native_value == "Charging + Discharging (257)"
    assert battery.extra_state_attributes == {"charge_state": 1, "discharge_state": 1}
    c.client.read_block.assert_not_awaited()


def test_excess_export_timer_can_be_changed_or_cleared_without_dispatch_rewrite():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other("excess_export")
        reg = next(r for r in NUMBER_REGISTERS if r.key == "excess_export_duration")
        number = AlphaESSNumber(s.c, s.entry, reg)
        number.hass = s.hass
        number.async_write_ha_state = lambda: None
        countdown = AlphaESSModeCountdownSensor(s.c, s.entry, "excess_export", "Countdown", "mdi:timer")
        await sw.async_turn_on()
        assert s.registers[8] == 300  # independent inverter watchdog
        assert countdown.native_value is None
        count = len(s.writes)
        await number.async_set_native_value(5)
        first = sw._duration_cancel
        callback = first._callback
        assert countdown.native_value in (299, 300)
        await number.async_set_native_value(10)
        assert first.cancelled() and countdown.native_value in (599, 600)
        callback()  # a callback already dispatched just before rescheduling
        await asyncio.gather(*sw._pending_tasks)
        assert sw.is_on and len(s.writes) == count
        await number.async_set_native_value(0)
        assert sw._duration_cancel is None and countdown.native_value is None
        await number.async_set_native_value(5)
        sw._duration_cancel._callback()
        await asyncio.gather(*sw._pending_tasks)
        assert not sw.is_on and s.registers[0] == 0 and countdown.native_value is None

    asyncio.run(scenario())


@pytest.mark.parametrize(("key", "grid", "lower_target"), [
    ("force_export", -4700, 4.7), ("force_import", 5300, 4.7),
])
def test_force_grid_target_edits_coalesce_and_cutoff_edit_preserves_power(key, grid, lower_target):
    async def scenario():
        s = DispatchSimulation()
        s.sample(house=1200, pv=340, soc=50)
        s.c.numbers[f"{key}_power"] = 5
        sw = s.other(key)
        await sw.async_turn_on()
        power = s.registers[2]
        deadline = sw._deadline
        writes_before = len(s.writes)
        power_number = next(r for r in NUMBER_REGISTERS if r.key == f"{key}_power")
        number = AlphaESSNumber(s.c, s.entry, power_number)
        number.hass = s.hass
        number.async_write_ha_state = lambda: None
        await number.async_set_native_value(4.9)
        await number.async_set_native_value(lower_target)
        assert len(s.writes) == writes_before
        s.now += 2
        s.sample(house=1200, pv=340, battery=860 - grid, soc=50)
        sw._observe_grid_target(s.c.data, exporting=key == "force_export")
        await asyncio.gather(*sw._pending_tasks)
        assert len(s.writes) == writes_before + 1
        assert s.registers[2] == power + (-300 if key == "force_export" else 300)
        assert sw._deadline == deadline
        power = s.registers[2]
        cutoff = f"{key}_cutoff_soc"
        s.c.numbers[cutoff] = 40 if key == "force_export" else 80
        await sw.async_apply_param_change(reset_timer=False, param_key=cutoff)
        assert s.registers[2] == power
        assert sw._deadline == deadline
        await sw.async_force_off()

    asyncio.run(scenario())

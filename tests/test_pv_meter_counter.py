"""Opt-in PV meter counter: isolated reads without changing existing consumers."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity, UpdateFailed
from pymodbus.exceptions import ModbusException

from custom_components.alphaess_modbus.const import DAILY_ENERGY_SENSORS, SENSOR_REGISTERS
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator, _decode_block
from custom_components.alphaess_modbus.sensor import AlphaESSSensor

KEY = "total_energy_pv_meter_0092"
REG = next(r for r in SENSOR_REGISTERS if r.key == KEY)
OLD = next(r for r in SENSOR_REGISTERS if r.key == "total_energy_feed_to_grid_pv")
CLOCK = "custom_components.alphaess_modbus.coordinator.time.monotonic"


def coordinator(enabled=False, multiplier=1, model="standard"):
    c = object.__new__(AlphaESSCoordinator)
    c.data = {}
    c.last_update_success = True
    c._last_polled = {}
    c._scan_multiplier = multiplier
    c._model_variant = model
    c.optional_poll_keys = {KEY} if enabled else set()
    c._optional_last_attempt = {}

    async def read(start, count):
        if start == REG.address:
            assert count == 2
            return [1, 2]
        return [0] * count

    c.client = NS(read_block=AsyncMock(side_effect=read),
                  read_register=AsyncMock(return_value="example"),
                  write_register=AsyncMock(), write_registers=AsyncMock())
    return c


async def poll(c, stamp):
    with patch(CLOCK, return_value=stamp):
        c.data = await c._async_update_data()
    return dict(c.data)


@pytest.mark.parametrize("words,value", [([0, 0], 0), ([0, 1], 0.01),
    ([1, 0], 655.36), ([1, 2], 655.38), ([65535, 65535], 42949672.95)])
def test_unsigned_32_bit_scaling_and_word_order(words, value):
    assert _decode_block(REG, [999, *words, 999], 1) == value


def test_sensor_has_separate_identity_and_existing_counter_is_preserved():
    c = coordinator()
    c.data = {KEY: 655.38, OLD.key: 0.05}
    sensor = AlphaESSSensor(c, NS(entry_id="test"), REG)
    old = AlphaESSSensor(c, NS(entry_id="test"), OLD)
    assert sensor.unique_id == f"test_{KEY}"
    assert sensor.unique_id != old.unique_id
    assert sensor.name == "PV Meter Energy (0x0092)"
    assert sensor.device_class == SensorDeviceClass.ENERGY
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
    assert sensor.native_unit_of_measurement == "kWh"
    assert sensor.native_value == 655.38
    assert not sensor.entity_registry_enabled_default
    assert REG.optional_read and REG.scan_interval == 60
    assert (OLD.address, OLD.scale, OLD.name) == (
        0x0090, 0.01, "Total Energy Feed to Grid (PV)")
    assert old.unique_id == "test_total_energy_feed_to_grid_pv"
    assert old.native_value == 0.05
    assert old.entity_registry_enabled_default and not OLD.optional_read
    assert all(KEY not in definition for definition in DAILY_ENERGY_SENSORS)
    c.data.pop(KEY)
    assert sensor.native_value is None  # An unsupported address is never a zero.


def test_entity_lifecycle_gates_optional_polling_only():
    async def scenario():
        c = coordinator()
        sensor = AlphaESSSensor(c, NS(entry_id="test"), REG)
        callbacks = []
        with patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()), \
                patch.object(sensor, "async_on_remove", side_effect=callbacks.append):
            await sensor.async_added_to_hass()
        assert c.optional_poll_keys == {KEY}
        assert len(callbacks) == 1
        callbacks[0]()
        assert not c.optional_poll_keys
        with patch(CLOCK, return_value=1000):
            assert not c._is_due(REG)
            assert c._is_due(OLD)
        c.client.read_block.assert_not_called()
        c.client.write_register.assert_not_called()
        c.client.write_registers.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("multiplier,step", [(0.5, 0.5), (1, 2), (3, 2)])
@pytest.mark.parametrize("model", ["standard", "b3"])
@pytest.mark.parametrize("enabled", [False, True])
def test_existing_poll_sequence_and_values_remain_identical(multiplier, step, model, enabled):
    async def simulate(registers):
        c = coordinator(enabled, multiplier, model)
        samples = []
        with patch("custom_components.alphaess_modbus.coordinator.SENSOR_REGISTERS", registers):
            for tick in range(int(180 / step) + 1):
                samples.append(await poll(c, 1000 + tick * step))
        c.client.write_register.assert_not_called()
        c.client.write_registers.assert_not_called()
        return c, samples

    before, before_samples = asyncio.run(simulate([r for r in SENSOR_REGISTERS if r != REG]))
    after, after_samples = asyncio.run(simulate(SENSOR_REGISTERS))
    calls = after.client.read_block.await_args_list
    optional = [call for call in calls if call.args[0] == REG.address]
    assert len(optional) == (int(180 / (60 * multiplier)) + 1 if enabled else 0)
    assert all(call.args == (REG.address, 2) for call in optional)
    assert [call for call in calls if call.args[0] != REG.address] == before.client.read_block.await_args_list
    assert before.client.read_register.await_args_list == after.client.read_register.await_args_list
    assert before_samples == [{key: value for key, value in sample.items() if key != KEY}
                              for sample in after_samples]
    assert all(sample[KEY] == 655.38 for sample in after_samples) if enabled else all(
        KEY not in sample for sample in after_samples)


@pytest.mark.parametrize("bad_result", [ModbusException("Illegal data address"),
    ModbusException("Transport timeout"), [], [0], [0, "invalid"]])
@pytest.mark.parametrize("multiplier", [0.5, 1, 3])
def test_failed_optional_read_preserves_other_sensors_and_waits_before_retry(bad_result, multiplier):
    async def scenario():
        c = coordinator(enabled=True, multiplier=multiplier)
        await poll(c, 1000)
        previous = dict(c.data)
        successful_read = c.client.read_block.side_effect

        async def read(start, count):
            if start == REG.address:
                if isinstance(bad_result, Exception):
                    raise bad_result
                return bad_result
            return await successful_read(start, count)

        c.client.read_block.side_effect = read
        stamp = 1000 + 60 * multiplier
        result = await poll(c, stamp)
        assert KEY not in result
        assert result == {key: value for key, value in previous.items() if key != KEY}
        assert result[OLD.key] == 0
        assert c.house_load_sample.raw == 0
        assert c.house_load_sample.reason == "valid"
        assert c._last_polled[KEY] == 1000
        attempts = sum(call.args[0] == REG.address for call in c.client.read_block.await_args_list)
        await poll(c, stamp + 60 * multiplier - 0.1)
        assert sum(call.args[0] == REG.address for call in c.client.read_block.await_args_list) == attempts
        c.client.read_block.side_effect = successful_read
        result = await poll(c, stamp + 60 * multiplier)
        assert result[KEY] == 655.38
        assert c._last_polled[KEY] == stamp + 60 * multiplier
        c.optional_poll_keys.clear()
        await poll(c, stamp + 180 * multiplier)
        assert sum(call.args[0] == REG.address for call in c.client.read_block.await_args_list) == attempts + 1
        c.client.write_register.assert_not_called()
        c.client.write_registers.assert_not_called()
    asyncio.run(scenario())


def test_optional_counter_cannot_mask_failure_of_all_existing_reads():
    async def scenario():
        c = coordinator(enabled=True)
        c.client.read_block.side_effect = ModbusException("offline")
        with patch("custom_components.alphaess_modbus.coordinator.SENSOR_REGISTERS", [OLD, REG]):
            with pytest.raises(UpdateFailed, match="All reads failed"):
                await poll(c, 1000)
        assert [call.args for call in c.client.read_block.await_args_list] == [(OLD.address, 2)]
    asyncio.run(scenario())

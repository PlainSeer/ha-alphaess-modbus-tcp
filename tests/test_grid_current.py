"""Grid-current entities and real coordinator polling with a simulated client."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.alphaess_modbus.const import DOMAIN, SENSOR_REGISTERS
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator, _decode_block
from custom_components.alphaess_modbus.sensor import AlphaESSSensor, async_setup_entry

KEYS = tuple(f"current_phase_{phase}_grid" for phase in "abc")
REGS = [next(r for r in SENSOR_REGISTERS if r.key == key) for key in KEYS]


@pytest.mark.parametrize("reg,raw,expected", list(zip(REGS, (29, 64, 25), (2.9, 6.4, 2.5))))
def test_phase_current_decode_at_block_offset(reg, raw, expected):
    assert _decode_block(reg, [999, raw, 999], 1) == expected
    assert _decode_block(reg, [0], 0) == 0.0


def test_setup_creates_current_entities_with_stable_identity_and_missing_data():
    coordinator = NS(data=dict(zip(KEYS, (2.9, 6.4, 2.5))), last_update_success=True)
    entry = NS(entry_id="test-entry")
    hass = NS(data={DOMAIN: {entry.entry_id: coordinator}})
    entities = []
    asyncio.run(async_setup_entry(hass, entry, entities.extend))
    currents = [e for e in entities if isinstance(e, AlphaESSSensor) and e._reg.key in KEYS]
    assert len(currents) == 3
    assert len({e.unique_id for e in entities}) == len(entities)
    for sensor in currents:
        assert sensor.unique_id == f"test-entry_{sensor._reg.key}"
        assert sensor.device_info["identifiers"] == {(DOMAIN, entry.entry_id)}
        assert sensor.device_class == SensorDeviceClass.CURRENT
        assert sensor.state_class == SensorStateClass.MEASUREMENT
        assert sensor.native_unit_of_measurement == "A"
        assert sensor.entity_registry_enabled_default
        assert sensor.native_value == coordinator.data[sensor._reg.key]
    coordinator.data.clear()
    assert all(e.native_value is None for e in currents)
    coordinator.last_update_success = False
    assert all(not e.available for e in currents)


@pytest.mark.parametrize("multiplier,step", [(0.5, 0.5), (1, 2), (3, 2)])
def test_successful_polling_preserves_reads_existing_values_and_never_writes(multiplier, step):
    async def simulate(registers):
        # Only exercise the coordinator's update path; no HA startup or sockets.
        coordinator = object.__new__(AlphaESSCoordinator)
        coordinator.data = {}
        coordinator._last_polled = {}
        coordinator._scan_multiplier = multiplier
        coordinator._model_variant = "standard"
        words = dict(zip((0x17, 0x18, 0x19), (29, 64, 25)))
        client = NS(read_block=AsyncMock(side_effect=lambda start, count:
                    [words.get(address, 0) for address in range(start, start + count)]),
                    read_register=AsyncMock(return_value="example"),
                    write_register=AsyncMock(), write_registers=AsyncMock())
        coordinator.client = client
        samples = []
        with patch("custom_components.alphaess_modbus.coordinator.SENSOR_REGISTERS", registers):
            for tick in range(int(180 / step) + 1):
                with patch("custom_components.alphaess_modbus.coordinator.time.monotonic",
                           return_value=1000 + tick * step):
                    coordinator.data = await coordinator._async_update_data()
                samples.append(dict(coordinator.data))
        client.write_register.assert_not_called()
        client.write_registers.assert_not_called()
        return client.read_block.call_args_list, client.read_register.call_args_list, samples

    original = [r for r in SENSOR_REGISTERS if r.key not in KEYS]
    before = asyncio.run(simulate(original))
    after = asyncio.run(simulate(SENSOR_REGISTERS))
    assert before[:2] == after[:2]
    assert before[2] == [{k: v for k, v in sample.items() if k not in KEYS}
                         for sample in after[2]]
    assert all([sample[key] for key in KEYS] == [2.9, 6.4, 2.5] for sample in after[2])

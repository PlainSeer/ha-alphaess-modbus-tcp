"""Cache expiry must respect the configured poll cadence, including failures."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pymodbus.exceptions import ModbusException

from custom_components.alphaess_modbus.const import SENSOR_REGISTERS
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator


@pytest.mark.parametrize('multiplier,age,retained', [
    (10, 31, True), (10, 249, True), (10, 251, False),
    (3, 26, True), (3, 76, False),
    (1, 24, True), (1, 26, False),
    (0.5, 24, True), (0.5, 26, False),
])
def test_expiry_respects_slow_polling_but_failed_values_still_expire(multiplier, age, retained):
    async def scenario():
        grid = next(r for r in SENSOR_REGISTERS if r.key == 'power_grid')
        dispatch = next(r for r in SENSOR_REGISTERS if r.key == 'dispatch_start')
        c = SimpleNamespace(data={'power_grid': 10, 'dispatch_start': 1},
                            _last_polled={'power_grid': 100.0, 'dispatch_start': 100.0},
                            _scan_multiplier=multiplier, _model_variant='standard')
        async def read(address, count):
            if address == dispatch.address:
                raise ModbusException('dispatch block offline')
            return [0] * count
        c.client = SimpleNamespace(read_block=AsyncMock(side_effect=read))
        c._is_due = lambda reg: AlphaESSCoordinator._is_due(c, reg)
        with patch('custom_components.alphaess_modbus.coordinator.SENSOR_REGISTERS', [grid, dispatch]), patch(
            'custom_components.alphaess_modbus.coordinator.time.monotonic', return_value=100 + age
        ):
            result = await AlphaESSCoordinator._async_update_data(c)
        assert ('dispatch_start' in result) == retained
        assert c._last_polled['dispatch_start'] == 100.0
        assert c._last_polled['power_grid'] == 100 + age
        if multiplier == 10 and age == 31:
            # It is healthy to have no new dispatch sample before its 50s poll.
            assert all(call.args[0] != dispatch.address for call in c.client.read_block.await_args_list)
    asyncio.run(scenario())

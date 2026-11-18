import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import State

from custom_components.alphaess_modbus.config_flow import AlphaESSConfigFlow
from custom_components.alphaess_modbus.const import DOMAIN
from custom_components.alphaess_modbus.modbus_client import _SLAVE_KWARG

from .dispatch_simulation import DispatchSimulation


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("force_charging_hold", True),
        ("force_discharging_hold", True),
        ("force_export_hold", True),
        ("force_import_hold", True),
        ("force_charging", False),
        ("force_discharging", False),
        ("dispatch", False),
    ],
)
def test_restore_hold_without_resuming_dispatch(key, expected):
    async def scenario():
        sim = DispatchSimulation()
        switch = sim.other(key)
        switch.async_get_last_state = AsyncMock(return_value=State(f"switch.{key}", "on"))
        await switch.async_added_to_hass()
        assert switch.is_on is expected
        assert not sim.writes and not sim.single_writes

    asyncio.run(scenario())


@pytest.mark.parametrize(("key", "idle_soc", "cutoff"), [
    ("force_export", 60, 30),
    ("force_import", 40, 70),
])
def test_force_servo_zero_power_is_allowed_until_cutoff(key, idle_soc, cutoff):
    async def scenario():
        sim = DispatchSimulation()
        sw = sim.other(key)
        hold = sim.other(f"{key}_hold")
        sim.c.numbers[f"{key}_cutoff_soc"] = cutoff
        sim.sample(house=7000 if key == "force_import" else 1000,
                   pv=7000 if key == "force_export" else 0, battery=0, soc=idle_soc)
        await sw.async_turn_on()
        for _ in range(3):
            sim.sample(battery=0, soc=idle_soc)
            for listener in tuple(sim.listeners):
                listener()
            await asyncio.sleep(0)
        assert sw.is_on and sw._soc_unsub is None

        await hold.async_turn_on()
        sim.sample(soc=cutoff)
        for listener in tuple(sim.listeners):
            listener()
        assert sw.is_on and sw._is_on
        await hold.async_turn_off()
        for listener in tuple(sim.listeners):
            listener()
        await asyncio.gather(*sw._pending_tasks)
        assert not sw.is_on

    asyncio.run(scenario())


def test_reconfigure_reuses_live_client_and_preserves_existing_data():
    async def scenario():
        sim = DispatchSimulation()
        flow = AlphaESSConfigFlow()
        entry = SimpleNamespace(
            entry_id="test", title="My inverter",
            data={"host": "test.local", "port": 502, "slave_id": 1, "inverter_ac_limit": "8 kW"},
        )
        flow.hass = SimpleNamespace(
            data={DOMAIN: {entry.entry_id: sim.c}},
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        )
        flow._get_reconfigure_entry = lambda: entry
        flow.async_abort = Mock()
        flow.async_update_reload_and_abort = Mock()
        await flow.async_step_reconfigure({"host": "test.local", "port": 502, "slave_id": 1})
        flow.async_abort.assert_called_once_with(reason="reconfigure_successful")
        sim.client._client.read_holding_registers.assert_not_awaited()

        with patch("custom_components.alphaess_modbus.config_flow.AlphaESSModbusClient") as factory:
            await flow.async_step_reconfigure({"host": "test.local", "port": 502, "slave_id": 2})
        factory.assert_not_called()
        assert sim.client._slave_id == 1
        sim.client._client.read_holding_registers.assert_awaited_once_with(0x0102, count=1, **{_SLAVE_KWARG: 2})
        flow.async_update_reload_and_abort.assert_called_once_with(
            entry, data_updates={"host": "test.local", "port": 502, "slave_id": 2},
            title="My inverter", unique_id="test.local:502", reload_even_if_entry_is_unchanged=False,
        )

    asyncio.run(scenario())

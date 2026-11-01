"""An off action for one switch must not reset another local controller."""
import asyncio
from itertools import permutations
from unittest.mock import Mock

import pytest

from custom_components.alphaess_modbus.const import NUMBER_REGISTERS
from custom_components.alphaess_modbus.number import AlphaESSNumber

from .dispatch_simulation import DispatchSimulation


CONTROLS = ['force_charging', 'force_discharging', 'force_export',
            'force_import', 'dispatch', 'excess_export', 'peak_shaving']


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 5))


async def setup(owner):
    s = DispatchSimulation()
    for key in CONTROLS[:-1]:
        s.other(key)
    if owner == 'peak_shaving':
        await s.start()
    else:
        await s.switches[owner].async_turn_on()
    assert s.c.active_dispatch_key == owner and s.switches[owner].is_on
    return s


async def set_param(s, key, value):
    number = AlphaESSNumber(s.c, s.entry, next(reg for reg in NUMBER_REGISTERS if reg.key == key))
    number.hass = s.hass
    number.async_write_ha_state = Mock()
    await number.async_set_native_value(value)


@pytest.mark.parametrize('owner,other', list(permutations(CONTROLS, 2)))
def test_off_of_another_switch_preserves_local_owner_and_registers(owner, other):
    async def scenario():
        s = await setup(owner)
        sw = s.switches[owner]
        command = list(s.registers)
        generation, deadline = sw._control_generation, sw._deadline
        writes = len(s.writes)
        try:
            assert not s.switches[other].is_on
            await s.switches[other].async_turn_off()
            assert not s.switches[other].is_on and sw.is_on
            assert s.c.active_dispatch_key == owner
            assert sw._control_generation == generation and sw._deadline == deadline
            assert s.registers == command and len(s.writes) == writes
        finally:
            await sw.async_force_off()
    run(scenario())


@pytest.mark.parametrize('owner', CONTROLS)
def test_the_active_owner_still_stops_normally(owner):
    async def scenario():
        s = await setup(owner)
        sw = s.switches[owner]
        writes = len(s.writes)
        await sw.async_turn_off()
        assert not sw.is_on and s.c.active_dispatch_key is None
        assert len(s.writes) == writes + 1 and s.registers[0] == 0
    run(scenario())


def test_explicit_stop_without_a_local_owner_keeps_legacy_reset_behavior():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        s.registers = [1, 0, 39500, 0, 32000, 2, 51, 0, 300, 255, 0]
        s.state = 1
        await sw.async_turn_off()
        assert s.registers[0] == 0 and len(s.writes) == 1
    run(scenario())


@pytest.mark.parametrize('owner', ['excess_export', 'peak_shaving'])
def test_off_of_another_switch_preserves_a_paused_controller(owner):
    async def scenario():
        s = await setup(owner)
        sw = s.switches[owner]
        try:
            if owner == 'excess_export':
                s.c.ee_paused = True
            else:
                sw.paused = True
            await sw._pause_dispatch()
            assert s.registers[0] == 0 and sw.is_on
            writes = len(s.writes)
            await s.switches['force_charging'].async_turn_off()
            assert sw.is_on and s.c.active_dispatch_key == owner
            assert s.c.ee_paused if owner == 'excess_export' else sw.paused
            assert len(s.writes) == writes
        finally:
            await sw.async_force_off()
    run(scenario())


def test_repeated_predbat_discharge_start_keeps_soc_and_rate_updates_working():
    async def scenario():
        s = await setup('force_discharging')
        sw = s.switches['force_discharging']
        try:
            for power, soc in ((7.5, 20), (3.0, 30)):
                writes = len(s.writes)
                for key in ('dispatch', 'force_charging', 'force_export', 'force_import'):
                    await s.switches[key].async_turn_off()
                await set_param(s, 'force_discharging_cutoff_soc', soc)
                await set_param(s, 'force_discharging_duration', 480)
                await sw.async_turn_on()
                await set_param(s, 'force_discharging_power', power)
                assert sw.is_on and not sw._suspended
                assert s.registers[2] == 32000 + power * 1000
                assert all(command[0] == 1 for _, command in s.writes[writes:])
        finally:
            await sw.async_force_off()
    run(scenario())


def test_repeated_predbat_charge_start_and_export_stop_do_not_reset_charging():
    async def scenario():
        s = await setup('force_charging')
        charging = s.switches['force_charging']
        hold = s.other('force_charging_hold')
        await hold.async_turn_on()
        try:
            for power in (2.2, 1.8):
                writes = len(s.writes)
                # Representative charge-start cleanup: callers can repeat the
                # full sequence while charging is already on.
                for key in ('dispatch', 'force_export', 'force_discharging', 'force_import'):
                    await s.switches[key].async_turn_off()
                assert len(s.writes) == writes
                await set_param(s, 'force_charging_power', power)
                # Repeated stop-SoC and duration settings, then explicit start.
                await set_param(s, 'force_charging_cutoff_soc', 100)
                await set_param(s, 'force_charging_duration', 480)
                await hold.async_turn_on()
                await charging.async_turn_on()
                assert not charging._suspended and s.registers[2] == 32000 - power * 1000
                assert all(command[0] == 1 for _, command in s.writes[writes:])
                # PredBat's discharge-stop cleanup is also harmless during charge.
                for key in ('force_export', 'force_discharging', 'dispatch'):
                    await s.switches[key].async_turn_off()
                assert s.registers[0] == 1 and charging.is_on
        finally:
            await charging.async_force_off()
    run(scenario())


@pytest.mark.parametrize('mode,power', [('Battery only Charges from PV (1)', -8), ('No Battery Charge (19)', 0)])
def test_off_actions_preserve_dispatch_holds_and_mode_1_power_updates(mode, power):
    async def scenario():
        s = DispatchSimulation()
        for key in CONTROLS[:-1]:
            s.other(key)
        dispatch = s.switches['dispatch']
        s.c.get_select = lambda _: mode
        s.c.numbers.update(dispatch_duration=480, dispatch_power=power)
        await dispatch.async_turn_on()
        try:
            command = list(s.registers)
            for key in ('force_charging', 'force_export', 'force_discharging', 'force_import'):
                await s.switches[key].async_turn_off()
            assert s.registers == command and len(s.writes) == 1
            if power == -8:
                deadline = dispatch._deadline
                for setpoint in (0, -8):
                    await set_param(s, 'dispatch_power', setpoint)
                    assert not dispatch._suspended and dispatch._deadline == deadline
            await dispatch.async_turn_off()  # Explicit return to Demand still stops.
            assert s.registers[0] == 0 and not dispatch.is_on
        finally:
            await dispatch.async_force_off()
    run(scenario())


def test_off_queued_during_another_controller_start_does_not_reset_the_new_owner():
    async def scenario():
        s = await setup('force_charging')
        original = s.client._client.write_registers.side_effect
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_start(address, command, **kwargs):
            if command[0] == 1:
                entered.set()
                await asyncio.wait_for(release.wait(), 2)
            return await original(address, command, **kwargs)

        s.client._client.write_registers.side_effect = slow_start
        start = asyncio.create_task(s.switches['force_discharging'].async_turn_on())
        await entered.wait()
        stop_old = asyncio.create_task(s.switches['force_charging'].async_turn_off())
        await asyncio.sleep(0)
        release.set()
        try:
            await asyncio.gather(start, stop_old)
            assert s.c.active_dispatch_key == 'force_discharging'
            assert s.switches['force_discharging'].is_on
            assert [command[0] for _, command in s.writes] == [1, 0, 1]
        finally:
            await s.switches['force_discharging'].async_force_off()
    run(scenario())

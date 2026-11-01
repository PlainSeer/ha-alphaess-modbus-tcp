"""Capture failed consistency reads without changing inverter control policy."""
import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.alphaess_modbus.dispatch import DispatchConflict, WriteIndeterminate

from .dispatch_simulation import DispatchSimulation


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 5))


async def start_charge():
    s = DispatchSimulation()
    sw = s.other('force_charging')
    s.c.numbers.update(force_charging_duration=480, force_charging_power=2.1,
                       force_charging_cutoff_soc=100)
    await s.other('force_charging_hold').async_turn_on()
    await sw.async_turn_on()
    s.c.numbers['force_charging_power'] = 2.2
    await sw.async_apply_param_change(reset_timer=False)
    return s, sw


@pytest.mark.parametrize('index,value', [(2, 29900), (5, 19), (6, 254), (8, 28800), (9, 0)])
def test_soc_reapply_conflict_preserves_exact_read_and_blocks_further_updates(index, value, caplog):
    async def scenario():
        s, sw = await start_charge()
        acknowledged = list(sw._acknowledged)
        s.registers[index] = value
        observed = list(s.registers)
        transport = s.client._client
        writes, reads = len(s.writes), transport.read_holding_registers.await_count
        try:
            # Replay the failure stage: PredBat repeats 100% while charging is on.
            with pytest.raises(DispatchConflict) as raised:
                await sw.async_apply_param_change(reset_timer=False)
            message = str(raised.value)
            assert 'block=0x0880..0x088a' in message
            assert f'observed={observed}' in message
            assert f'expected={[acknowledged]}' in message
            assert message in caplog.text
            assert sw.extra_state_attributes['dispatch_error'] == message
            assert sw.is_on and sw._suspended
            assert sw._acknowledged == acknowledged and sw._uncertain is None
            assert len(s.writes) == writes
            assert transport.read_holding_registers.await_count == reads + 1
            assert transport.retries == 3
            # The next PredBat action still reports suspension, not success.
            with pytest.raises(HomeAssistantError, match='Session suspended'):
                await sw.async_apply_param_change(reset_timer=True)
            assert len(s.writes) == writes
        finally:
            await sw.async_force_off()
    run(scenario())


def test_conflict_reports_both_acknowledged_and_uncertain_candidates():
    async def scenario():
        s, sw = await start_charge()
        acknowledged = list(sw._acknowledged)
        transport = s.client._client
        original = transport.write_registers.side_effect

        async def applied_without_reply(*args, **kwargs):
            await original(*args, **kwargs)
            raise OSError('reply lost after application')

        try:
            transport.write_registers.side_effect = applied_without_reply
            s.c.numbers['force_charging_power'] = 1.8
            with pytest.raises(WriteIndeterminate):
                await sw.async_apply_param_change(reset_timer=False)
            uncertain = list(sw._uncertain)
            assert uncertain[2] == 30200 and acknowledged[2] == 29800
            s.client._client = transport  # Simulate reconnect after the lost reply.
            transport.write_registers.side_effect = original
            s.registers[5] = 19
            observed = list(s.registers)
            writes = len(s.writes)
            with pytest.raises(DispatchConflict) as raised:
                await sw.async_apply_param_change(reset_timer=False)
            assert f'observed={observed}' in str(raised.value)
            assert f'expected={[acknowledged, uncertain]}' in str(raised.value)
            assert sw._acknowledged == acknowledged and sw._uncertain == uncertain
            assert len(s.writes) == writes and sw._suspended
        finally:
            await sw.async_force_off()
    run(scenario())


def test_predbat_start_bridge_soc_duration_sequence_with_consistent_reads(caplog):
    async def scenario():
        s, sw = await start_charge()
        deadline = sw._deadline
        try:
            # Existing allowances remain: PV=0 leaves PV unchanged, and the
            # inverter may expose a decrementing rather than configured time.
            s.registers[10] = 1
            s.registers[8] -= 1
            await sw.async_apply_param_change(reset_timer=False)
            assert sw._deadline == deadline and not sw._suspended
            await sw.async_apply_param_change(reset_timer=True)
            assert sw.is_on and not sw._suspended
            assert sw._dispatch_status == 'acknowledged'
            assert len(s.writes) == 4
            assert s.client._client.read_holding_registers.await_count == 2
            assert not caplog.records
        finally:
            await sw.async_force_off()
    run(scenario())

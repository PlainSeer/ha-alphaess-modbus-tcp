"""Dispatch correctness simulations: no HA instance or inverter is contacted."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from .dispatch_simulation import DispatchSimulation


@pytest.fixture(autouse=True)
def notifications():
    with patch('custom_components.alphaess_modbus.button.persistent_notification.async_create') as create, patch(
        'custom_components.alphaess_modbus.button.persistent_notification.async_dismiss'
    ) as dismiss:
        yield create, dismiss


@pytest.mark.parametrize('power', [-3000, 0, 3000])
def test_sync_never_claims_external_dispatch_or_enables_slider_writes(power):
    async def run():
        s = DispatchSimulation()
        s.state = 1
        s.c.data.update(dispatch_start=0, dispatch_active_power=power)
        controls = [s.other(key) for key in ('force_charging', 'force_export', 'dispatch')]
        await s.button('sync_dispatch_state').async_press()
        for sw in controls:
            assert not sw.is_on
            await sw.async_apply_param_change(reset_timer=False)
            await sw.async_apply_param_change(reset_timer=True)
        assert s.c.active_dispatch_key is None
        assert not s.writes
        s.client.read_register.assert_awaited_once_with(0x0880, 'uint16')
    asyncio.run(run())


def test_sync_clears_stale_pause_flags_with_different_owner_without_writes():
    async def run():
        s = DispatchSimulation()
        for key in ('force_export', 'force_import', 'excess_export', 'dispatch_pv', 'force_export_hold'):
            s.other(key)._is_on = True
        s.c.ee_paused = s.c.fi_paused = True
        s.c.active_dispatch_key = 'force_export'
        s.c.data['dispatch_start'] = 1  # cached state is irrelevant
        await s.button('sync_dispatch_state').async_press()
        assert all(not s.switches[k].is_on for k in ('force_export', 'force_import', 'excess_export'))
        assert s.switches['dispatch_pv'].is_on and s.switches['force_export_hold'].is_on
        assert not s.c.ee_paused and not s.c.fi_paused
        assert s.c.active_dispatch_key is None and not s.writes
    asyncio.run(run())


@pytest.mark.parametrize('result', [None, 2, OSError('offline')])
def test_unknown_sync_leaves_controller_and_preferences_unchanged(result):
    async def run():
        s = DispatchSimulation()
        sw = s.other('force_export')
        sw._is_on = True
        s.c.active_dispatch_key = 'force_export'
        s.client.read_register.side_effect = result if isinstance(result, Exception) else None
        s.client.read_register.return_value = result
        await s.button('sync_dispatch_state').async_press()
        assert sw.is_on and s.c.active_dispatch_key == 'force_export'
        assert not s.writes
    asyncio.run(run())


@pytest.mark.parametrize('key', ['force_export', 'force_import', 'excess_export'])
def test_reset_invalidates_created_but_not_started_servo_tasks(key):
    async def run():
        s = DispatchSimulation()
        sw = s.other(key)
        await sw.async_turn_on()
        s.writes.clear()
        if key == 'excess_export':
            coro = sw._start_excess_export()
        else:
            writer = sw._write_export_dispatch if key == 'force_export' else sw._write_import_dispatch
            coro = writer(33000, 7200, reset_timer=False)
        task = sw._create_control_task(coro, name='delayed-servo')
        sw._pending_tasks.add(task)
        # No yield: task exists but has not executed when Reset starts.
        await s.button('dispatch_reset').async_press()
        await task
        assert not task.cancelled()
        assert s.writes == [(0x0880, [0, 0, 32000, 0, 32000, 0, 0, 0, 90, 255, 0])]
        assert not sw.is_on and not s.listeners
        assert sw._timer_cancel is None and sw._duration_cancel is None
        for callback in list(s.listeners):
            callback()
        await sw.async_apply_param_change(reset_timer=False)
        assert len(s.writes) == 1
    asyncio.run(run())


def test_reset_waits_for_inflight_write_without_cancelling_shared_request():
    async def run():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        s.writes.clear()
        entered, release = asyncio.Event(), asyncio.Event()
        original = s.client._client.write_registers.side_effect

        async def delayed(address, values, **kwargs):
            if values[0]:
                entered.set()
                await release.wait()
            return await original(address, values, **kwargs)

        s.client._client.write_registers.side_effect = delayed
        task = sw._create_control_task(sw._write_export_dispatch(35000, 7200, reset_timer=False), name='servo')
        sw._pending_tasks.add(task)
        await entered.wait()
        reset = asyncio.create_task(s.button('dispatch_reset').async_press())
        await asyncio.sleep(0)
        assert not sw.is_on and not reset.done()
        release.set()
        await asyncio.gather(task, reset)
        assert [values[0] for _, values in s.writes] == [1, 0]
        assert not task.cancelled() and s.state == 0
    asyncio.run(run())


def test_post_lock_guard_blocks_slider_write_waiting_behind_reset():
    async def run():
        s = DispatchSimulation()
        sw = s.other('force_export')
        sw._is_on = True
        await s.client._lock.acquire()
        task = asyncio.create_task(sw._write_export_dispatch(35000, 7200, reset_timer=False))
        await asyncio.sleep(0)  # task now waits inside the real client lock
        await sw.async_force_off()
        s.client._lock.release()
        await task
        assert not s.writes and s.c.dispatch_started_at is None
    asyncio.run(run())


def test_guard_rechecked_after_reconnect_await():
    async def run():
        s = DispatchSimulation()
        sw = s.other('force_export')
        sw._is_on = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def connect():
            entered.set()
            await release.wait()

        s.client._ensure_connected.side_effect = connect
        task = asyncio.create_task(sw._write_export_dispatch(35000, 7200, reset_timer=False))
        await entered.wait()
        await sw.async_force_off()
        release.set()
        await task
        assert not s.writes
    asyncio.run(run())


@pytest.mark.parametrize('button', ['dispatch_reset', 'sync_dispatch_state'])
def test_all_controllers_stop_before_any_drain_await(button):
    async def run():
        s = DispatchSimulation()
        first, second = s.other('force_charging'), s.other('force_export')
        first._is_on = second._is_on = True
        first._pending_tasks.add(asyncio.create_task(asyncio.sleep(0)))
        task = second._create_control_task(
            second._write_export_dispatch(35000, 7200, reset_timer=False), name='queued-servo'
        )
        second._pending_tasks.add(task)
        await s.button(button).async_press()
        await task
        assert not first.is_on and not second.is_on
        assert [values[0] for _, values in s.writes] == ([0] if button == 'dispatch_reset' else [])
    asyncio.run(run())


def test_queued_duration_stop_cannot_reset_a_later_external_dispatch():
    async def run():
        s = DispatchSimulation()
        sw = s.other('force_export')
        sw._is_on = True
        task = sw._create_control_task(sw._async_turn_off_silent(), name='expired-duration')
        sw._pending_tasks.add(task)
        await s.button('dispatch_reset').async_press()
        s.state = 1  # another controller starts after the successful reset
        await task
        await asyncio.sleep(0)
        assert s.state == 1 and len(s.writes) == 1
    asyncio.run(run())


@pytest.mark.parametrize('readings', [[1, 1, 0], [OSError('offline'), 0]])
def test_reset_verification_retries_reads_only(readings):
    async def run():
        s = DispatchSimulation()
        s.client.read_register.side_effect = readings
        with patch('custom_components.alphaess_modbus.button.asyncio.sleep', new=AsyncMock()):
            await s.button('dispatch_reset').async_press()
        assert len(s.writes) == 1
        assert s.client.read_register.await_count == len(readings)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['active', 'read', 'write'])
@pytest.mark.parametrize('recovery', ['clear_discharge_export', 'pv_first_recovery'])
def test_recovery_sequences_abort_before_followup_actions(failure, recovery, notifications, tmp_path):
    async def run():
        s = DispatchSimulation()
        if failure == 'write':
            s.client._client.write_registers.side_effect = OSError('write failed')
        elif failure == 'read':
            s.client.read_register.side_effect = OSError('read failed')
        else:
            s.client.read_register.side_effect = None
            s.client.read_register.return_value = 1
        write_mock = s.client._client.write_registers
        followup = AsyncMock()

        # Exercise HA's actual script engine with the post-reset steps used by
        # the two live recovery scripts, without invoking their live services.
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import config_validation as cv
        from homeassistant.helpers.script import Script
        hass = HomeAssistant(str(tmp_path))

        async def press(call):
            key = 'dispatch_reset' if call.data['entity_id'][0].endswith('dispatch_reset') else 'sync_dispatch_state'
            await s.button(key).async_press()

        hass.services.async_register('button', 'press', press)
        hass.services.async_register('switch', 'turn_on', followup)
        hass.services.async_register('logbook', 'log', followup)
        sequence = [
            {'action': 'button.press', 'target': {'entity_id': 'button.alphaess_inverter_dispatch_reset'}},
            {'delay': {'seconds': 0}},
        ]
        if recovery == 'pv_first_recovery':
            sequence.append({'action': 'switch.turn_on', 'target': {'entity_id': 'switch.alphaess_inverter_excess_export'}})
        sequence += [
            {'action': 'button.press', 'target': {'entity_id': 'button.alphaess_inverter_sync_dispatch_state'}},
            {'action': 'logbook.log', 'data': {'name': recovery, 'message': 'completed'}},
        ]
        script = Script(hass, cv.SCRIPT_SCHEMA(sequence), recovery, 'script')
        with patch('custom_components.alphaess_modbus.button.asyncio.sleep', new=AsyncMock()):
            with pytest.raises(HomeAssistantError, match='Dispatch Reset unresolved:'):
                await script.async_run()
        await hass.async_stop()
        followup.assert_not_awaited()
        assert write_mock.await_count == 1
        assert s.client.read_register.await_count == (0 if failure == 'write' else 11)
        notifications[0].assert_called_once()
    asyncio.run(run())


def test_pv_recovery_sync_uses_read_after_new_excess_export_command():
    async def run():
        s = DispatchSimulation()
        await s.button('dispatch_reset').async_press()
        sw = s.other('excess_export')
        await sw.async_turn_on()
        assert s.c.data['dispatch_start'] == 0  # pre-command coordinator poll
        await s.button('sync_dispatch_state').async_press()
        assert sw.is_on and s.c.active_dispatch_key == 'excess_export'
        assert [values[0] for _, values in s.writes] == [0, 1]
        await sw.async_force_off()
    asyncio.run(run())

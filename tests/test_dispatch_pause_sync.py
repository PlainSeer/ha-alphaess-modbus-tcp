"""Exercise real pause/resume transitions, with simulated Modbus only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from .dispatch_simulation import DispatchSimulation


async def paused_controller(s, key):
    if key == 'peak_shaving':
        await s.start()
        await s.step(mode=2)
        assert s.sw.is_on and s.sw.paused and s.state == 0
        return s.sw
    sw = s.other('excess_export')
    await sw.async_turn_on()
    # A normal-mode grid import triggers the actual EE pause callback.
    for callback in list(s.listeners):
        callback()
    await asyncio.gather(*sw._pending_tasks)
    assert sw.is_on and s.c.ee_paused and s.state == 0
    return sw


async def resume_controller(s, sw):
    if sw.switch_key == 'peak_shaving':
        await s.step()
        await s.step(seconds=600)
        assert not sw.paused
    else:
        sw._ee_last_pause_time = -10000
        sw._ee_work_mode_1_since = -10000
        s.sample(house=1000, pv=2000)
        for callback in list(s.listeners):
            callback()
        await asyncio.gather(*sw._pending_tasks)
        assert not s.c.ee_paused
    assert sw.is_on and s.state == 1


@pytest.mark.parametrize('key', ['excess_export', 'peak_shaving'])
def test_sync_preserves_actual_paused_controller_and_it_resumes(key):
    async def run():
        s = DispatchSimulation()
        sw = await paused_controller(s, key)
        listeners, writes = list(s.listeners), list(s.writes)
        for _ in range(3):
            await s.button('sync_dispatch_state').async_press()
        assert s.listeners == listeners and s.writes == writes
        assert sw.is_on and s.c.active_dispatch_key == key
        await resume_controller(s, sw)
        await sw.async_force_off()
    asyncio.run(run())


@pytest.mark.parametrize('key', ['excess_export', 'peak_shaving'])
@pytest.mark.parametrize("unresolved", [False, True])
def test_reset_stops_paused_controller_and_prevents_resume(key, unresolved):
    async def run():
        s = DispatchSimulation()
        sw = await paused_controller(s, key)
        s.writes.clear()
        if unresolved:
            s.client.read_register.side_effect = None
            s.client.read_register.return_value = 1
        with patch('custom_components.alphaess_modbus.button.persistent_notification.async_dismiss'), patch(
            'custom_components.alphaess_modbus.button.persistent_notification.async_create'
        ), patch('custom_components.alphaess_modbus.button.asyncio.sleep', new=AsyncMock()):
            if unresolved:
                with pytest.raises(HomeAssistantError):
                    await s.button('dispatch_reset').async_press()
            else:
                await s.button('dispatch_reset').async_press()
        assert not sw.is_on and not s.listeners and s.c.active_dispatch_key is None
        assert not s.c.ee_paused and not s.sw.paused
        await s.step(seconds=1000)
        assert [values[0] for _, values in s.writes] == [0]
    asyncio.run(run())


@pytest.mark.parametrize('key', ['excess_export', 'peak_shaving'])
@pytest.mark.parametrize('defect', ['off', 'unowned', 'no_callbacks'])
def test_stale_pause_flags_do_not_protect_dead_controller(key, defect):
    async def run():
        s = DispatchSimulation()
        sw = await paused_controller(s, key)
        if defect == 'off':
            sw._is_on = False
        elif defect == 'unowned':
            s.c.active_dispatch_key = None
        elif key == 'peak_shaving':
            sw._stop_callbacks()
        else:
            sw._ee_listener_unsub()
            sw._ee_listener_unsub = None
        s.writes.clear()
        await s.button('sync_dispatch_state').async_press()
        assert not sw.is_on and not s.listeners
        assert not s.c.ee_paused and not s.sw.paused and not s.writes
    asyncio.run(run())


@pytest.mark.parametrize('key', ['excess_export', 'peak_shaving'])
def test_sync_does_not_stop_resume_that_begins_during_read(key):
    async def run():
        s = DispatchSimulation()
        sw = await paused_controller(s, key)

        async def read(*_):
            await resume_controller(s, sw)
            return 0  # sample preceded the resume write

        s.client.read_register.side_effect = read
        await s.button('sync_dispatch_state').async_press()
        assert sw.is_on and s.c.active_dispatch_key == key and s.state == 1
        await sw.async_force_off()
    asyncio.run(run())


def test_pv_recovery_sync_after_real_import_pause_retains_auto_resume():
    async def run():
        s = DispatchSimulation()
        with patch('custom_components.alphaess_modbus.button.persistent_notification.async_dismiss'):
            await s.button('dispatch_reset').async_press()
        sw = await paused_controller(s, 'excess_export')
        await s.button('sync_dispatch_state').async_press()
        await resume_controller(s, sw)
        await sw.async_force_off()
    asyncio.run(run())


def test_sync_retains_ee_when_resume_is_queued_before_read_starts():
    async def run():
        s = DispatchSimulation()
        sw = await paused_controller(s, 'excess_export')
        sw._ee_last_pause_time = sw._ee_work_mode_1_since = -10000
        s.sample(house=1000, pv=2000)
        for callback in list(s.listeners):
            callback()
        assert not s.c.ee_paused and s.state == 0
        # The real callback clears the pause flag before its resume task runs.
        await s.button('sync_dispatch_state').async_press()
        await asyncio.gather(*sw._pending_tasks)
        assert sw.is_on and s.state == 1
        await sw.async_force_off()
    asyncio.run(run())


@pytest.mark.parametrize('key', ['excess_export', 'peak_shaving'])
@pytest.mark.parametrize('transition', ['stop', 'lost_callbacks'])
def test_pause_snapshot_does_not_preserve_controller_that_stopped_during_read(key, transition):
    async def run():
        s = DispatchSimulation()
        sw = await paused_controller(s, key)

        async def read(*_):
            if transition == 'stop':
                await sw.async_force_off()
            elif key == 'peak_shaving':
                sw._stop_callbacks()
            else:
                sw._ee_listener_unsub()
                sw._ee_listener_unsub = None
            return 0

        s.client.read_register.side_effect = read
        s.writes.clear()
        await s.button('sync_dispatch_state').async_press()
        assert not sw.is_on and not s.listeners and not s.writes
        assert not s.c.ee_paused and not s.sw.paused
    asyncio.run(run())


@pytest.mark.parametrize('lag', [0, 1.5, 4.5, 5.0])
def test_reset_allows_slow_confirmation_with_one_write(lag):
    async def run():
        s = DispatchSimulation()
        now = [0.0]
        s.client.read_register.side_effect = lambda *_: 0 if now[0] >= lag else 1

        async def sleep(seconds):
            now[0] += seconds

        with patch('custom_components.alphaess_modbus.button.asyncio.get_running_loop',
                   return_value=SimpleNamespace(time=lambda: now[0])), patch(
            'custom_components.alphaess_modbus.button.asyncio.sleep', side_effect=sleep
        ), patch('custom_components.alphaess_modbus.button.persistent_notification.async_dismiss'):
            await s.button('dispatch_reset').async_press()
        assert now[0] == lag and len(s.writes) == 1
    asyncio.run(run())


def test_slow_failed_read_uses_deadline_without_cancelling_request():
    async def run():
        s = DispatchSimulation()
        now = [0.0]

        async def read(*_):
            now[0] += 6
            raise OSError('read timed out')

        s.client.read_register.side_effect = read
        with patch('custom_components.alphaess_modbus.button.asyncio.get_running_loop',
                   return_value=SimpleNamespace(time=lambda: now[0])), patch(
            'custom_components.alphaess_modbus.button.persistent_notification.async_create'
        ) as notify:
            with pytest.raises(HomeAssistantError) as error:
                await s.button('dispatch_reset').async_press()
        assert str(error.value) == 'Dispatch Reset unresolved: read timed out'
        notify.assert_called_once()
        assert len(s.writes) == 1 and s.client.read_register.await_count == 1
    asyncio.run(run())

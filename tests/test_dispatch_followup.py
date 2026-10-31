"""Control ordering and transient failures through the real client, without IO."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.alphaess_modbus.dispatch import WriteNotSent

from .dispatch_simulation import DispatchSimulation


FIXED = ['force_charging', 'force_discharging', 'force_export', 'force_import', 'dispatch']


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 5))


@pytest.fixture(autouse=True)
def notifications():
    with patch('custom_components.alphaess_modbus.button.persistent_notification.async_create'), patch(
        'custom_components.alphaess_modbus.button.persistent_notification.async_dismiss'
    ):
        yield


@pytest.mark.parametrize('key', FIXED)
def test_reset_invalidates_a_start_before_waiting_for_its_control_lock(key):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other(key)
        transport = s.client._client
        original = transport.write_registers.side_effect
        entered, release = asyncio.Event(), asyncio.Event()
        async def write(*args, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            return await original(*args, **kwargs)
        transport.write_registers.side_effect = write
        start = asyncio.create_task(sw.async_turn_on())
        await entered.wait()
        generation = sw._control_generation
        reset = asyncio.create_task(s.button('dispatch_reset').async_press())
        await asyncio.sleep(0)
        try:
            assert not sw.is_on and sw._control_generation != generation
            assert sw._duration_cancel is None and not s.listeners
            assert not reset.done()
        finally:
            release.set()
            await asyncio.gather(start, reset)
        assert [command[0] for _, command in s.writes] == [1, 0]
        assert not sw.is_on and not s.listeners and s.state == 0
        assert not start.cancelled()
    run(scenario())


def test_reset_also_blocks_a_restart_already_queued_behind_the_start():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        entered, release = asyncio.Event(), asyncio.Event()
        original = s.client._client.write_registers.side_effect
        async def write(*args, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            return await original(*args, **kwargs)
        s.client._client.write_registers.side_effect = write
        start = asyncio.create_task(sw.async_turn_on())
        await entered.wait()
        restart = asyncio.create_task(sw.async_turn_on())
        await asyncio.sleep(0)
        reset = asyncio.create_task(s.button('dispatch_reset').async_press())
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(start, restart, reset, return_exceptions=True)
        assert isinstance(results[1], HomeAssistantError)
        assert results[0] is None and results[2] is None
        assert [command[0] for _, command in s.writes] == [1, 0]
        assert not sw.is_on and not s.listeners
        # Reset must release its gate on completion.
        await sw.async_turn_on()
        assert sw.is_on
        await sw.async_force_off()
    run(scenario())


@pytest.mark.parametrize('key', FIXED + ['excess_export'])
@pytest.mark.parametrize('duration_change', [False, True])
def test_successful_restart_does_not_emit_a_false_switch_off(key, duration_change):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other(key)
        await sw.async_turn_on()
        generation = sw._control_generation
        old_timer = sw._duration_cancel
        old_callback = old_timer._callback if old_timer else None
        states = []
        sw.async_write_ha_state = Mock(side_effect=lambda: states.append(sw.is_on))
        if duration_change:
            await sw.async_apply_param_change(reset_timer=True)
        else:
            await sw.async_turn_on()
        assert sw.is_on and sw._control_generation > generation
        assert states and all(states)
        if old_timer:
            assert old_timer.cancelled()
            count = len(s.writes)
            old_callback()
            await asyncio.gather(*sw._pending_tasks)
            assert len(s.writes) == count
        await sw.async_force_off()
    run(scenario())


@pytest.mark.parametrize('servo', [False, True])
@pytest.mark.parametrize('busy_count', [1, 2, 3])
def test_busy_has_one_bounded_budget_and_preserves_acknowledged_state(servo, busy_count):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export' if servo else 'force_charging')
        if servo:
            await sw.async_turn_on()
            sw._fe_last_power_raw = s.registers[2] = sw._acknowledged[2] = 33000
            s.c.data['power_grid'] = 0
        transport = s.client._client
        original = transport.write_registers.side_effect
        attempts = 0
        async def write(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            assert transport.retries == 0
            if attempts <= busy_count:
                return SimpleNamespace(isError=lambda: True, exception_code=6)
            return await original(*args, **kwargs)
        transport.write_registers.side_effect = write
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', new=AsyncMock()):
            if servo:
                sw._queue_servo()
                await asyncio.gather(*sw._pending_tasks)
            elif busy_count == 3:
                with pytest.raises(HomeAssistantError):
                    await sw.async_turn_on()
            else:
                await sw.async_turn_on()
        assert attempts == min(busy_count + 1, 3)
        assert sw._uncertain is None and transport.retries == 3
        assert sw._suspended == (busy_count == 3)
        assert sw.is_on == (servo or busy_count < 3)
        await sw.async_force_off()
    run(scenario())


@pytest.mark.parametrize('pause', [False, True])
@pytest.mark.parametrize('applied', [False, True])
def test_transient_background_stop_recovers_with_readback_and_no_duplicate_if_applied(pause, applied):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('excess_export' if pause else 'force_charging')
        await sw.async_turn_on()
        transport = s.client._client
        original = transport.write_registers.side_effect
        attempts = 0
        async def write(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                if applied:
                    await original(*args, **kwargs)
                raise OSError('stop acknowledgement lost')
            return await original(*args, **kwargs)
        transport.write_registers.side_effect = write
        async def backoff(_):
            s.client._client = transport
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=backoff):
            if pause:
                s.c.ee_paused = True
                await sw._create_control_task(sw._pause_dispatch(), name='pause')
            else:
                await sw._automatic_stop()
        assert not sw._suspended and sw._uncertain is None and s.state == 0
        assert attempts == (1 if applied else 2)
        assert sw.is_on == pause
        if pause:
            assert s.c.ee_paused and s.listeners
            # A confirmed pause still permits this controller's normal resume.
            s.c.ee_paused = False
            await sw._start_excess_export()
            assert s.state == 1 and not sw._suspended
        await sw.async_force_off()
    run(scenario())


@pytest.mark.parametrize('pause', [False, True])
@pytest.mark.parametrize('interruption', ['external', 'new_session', 'reset', 'raw'])
def test_stop_retry_cannot_overwrite_external_or_newer_control(pause, interruption):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('excess_export' if pause else 'force_charging')
        await sw.async_turn_on()
        transport = s.client._client
        original = transport.write_registers.side_effect
        attempts = 0
        async def write(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise OSError('stop reply lost')
        transport.write_registers.side_effect = write
        async def backoff(_):
            s.client._client = transport
            if interruption == 'external':
                s.registers[2] = 39999
            elif interruption == 'new_session':
                transport.write_registers.side_effect = original
                await s.other('dispatch').async_turn_on()
            elif interruption == 'raw':
                from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator
                await AlphaESSCoordinator.async_write_raw(s.c, 0x0885, 19)
            else:
                transport.write_registers.side_effect = original
                await s.button('dispatch_reset').async_press()
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=backoff):
            if pause:
                s.c.ee_paused = True
                await sw._create_control_task(sw._pause_dispatch(), name='pause')
            else:
                try:
                    await sw._automatic_stop()
                except HomeAssistantError:
                    pass
        assert attempts == 1
        if interruption == 'external':
            assert s.registers[2] == 39999 and sw._suspended
            assert sw._dispatch_status == 'conflict'
        elif interruption == 'new_session':
            assert s.c.active_dispatch_key == 'dispatch' and s.state == 1
        elif interruption == 'raw':
            assert s.single_writes == [(0x0885, 19)] and s.registers[5] == 19
            assert not sw.is_on
        else:
            assert s.state == 0 and not sw.is_on
        for item in s.switches.values():
            await item.async_force_off()
    run(scenario())


@pytest.mark.parametrize('pause', [False, True])
@pytest.mark.parametrize('failure', ['busy', 'permanent', 'timeout'])
def test_terminal_background_failures_are_bounded_and_exposed(pause, failure):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('excess_export' if pause else 'force_charging')
        await sw.async_turn_on()
        transport = s.client._client
        before = transport.write_registers.await_count
        def write(*args, **kwargs):
            if failure == 'timeout':
                raise OSError('reply lost')
            return SimpleNamespace(isError=lambda: True, exception_code=6 if failure == 'busy' else 2)
        transport.write_registers.side_effect = write
        async def backoff(_):
            s.client._client = transport
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=backoff):
            if pause:
                s.c.ee_paused = True
                await sw._create_control_task(sw._pause_dispatch(), name='pause')
            else:
                with pytest.raises(HomeAssistantError):
                    await sw._automatic_stop()
        assert transport.write_registers.await_count - before == (1 if failure == 'permanent' else 3)
        assert sw.is_on == pause
        assert sw.extra_state_attributes['dispatch_suspended'] is True
        assert sw._dispatch_status == {'busy': 'busy', 'permanent': 'rejected', 'timeout': 'indeterminate'}[failure]
        assert (sw._uncertain is not None) == (failure == 'timeout')
        await sw.async_force_off()
    run(scenario())


def test_busy_start_does_not_retry_past_its_deadline():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        transport = s.client._client
        transport.write_registers.side_effect = lambda *a, **kw: SimpleNamespace(isError=lambda: True, exception_code=6)
        async def backoff(_):
            sw._deadline = 0
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=backoff):
            with pytest.raises(WriteNotSent):
                await sw.async_turn_on()
        assert transport.write_registers.await_count == 1
        assert not sw.is_on and sw._duration_cancel is None and sw._uncertain is None
    run(scenario())


def test_busy_servo_uses_latest_target_and_remaining_duration():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        s.c.numbers['force_export_duration'] = 480
        await sw.async_turn_on()
        sw._fe_last_power_raw = s.registers[2] = sw._acknowledged[2] = 33000
        s.c.data['power_grid'] = 0
        transport = s.client._client
        original = transport.write_registers.side_effect
        attempts = []
        async def write(address, values, **kwargs):
            attempts.append(list(values))
            if len(attempts) == 1:
                return SimpleNamespace(isError=lambda: True, exception_code=6)
            return await original(address, values, **kwargs)
        transport.write_registers.side_effect = write
        async def backoff(_):
            s.c.numbers['force_export_power'] = 1
            sw._deadline -= 3600
            sw._queue_servo()
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=backoff):
            sw._queue_servo()
            await asyncio.gather(*sw._pending_tasks)
        assert len(attempts) == 2 and attempts[1][2] < attempts[0][2]
        assert 25198 <= attempts[1][8] <= 25200
        assert not sw._suspended
        await sw.async_force_off()
    run(scenario())


def test_failed_mode_handover_cleans_up_the_requested_start():
    async def scenario():
        s = DispatchSimulation()
        old, new = s.other('force_charging'), s.other('force_discharging')
        await old.async_turn_on()
        s.client._client.write_registers.side_effect = OSError('handover stop failed')
        with pytest.raises(HomeAssistantError):
            await new.async_turn_on()
        assert not old.is_on and not new.is_on
        assert new._dispatch_status == 'indeterminate' and new._suspended
        assert s.c.active_dispatch_key is None and not s.listeners
    run(scenario())


@pytest.mark.parametrize('key', ['force_charging', 'force_discharging', 'dispatch'])
def test_restart_does_not_retrigger_ha_switch_state_bridges(key, tmp_path):
    from homeassistant.core import HomeAssistant, callback
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        s = DispatchSimulation()
        sw = s.other(key)
        entity = 'switch.test_dispatch_control'
        transitions = []
        @callback
        def changed(event):
            if event.data['entity_id'] != entity:
                return
            old, new = event.data['old_state'], event.data['new_state']
            if old and new and old.state != new.state:
                transitions.append((old.state, new.state))
        hass.bus.async_listen('state_changed', changed)
        sw.async_write_ha_state = lambda: hass.states.async_set(entity, 'on' if sw.is_on else 'off', sw.extra_state_attributes)
        await sw.async_turn_on()
        await hass.async_block_till_done()
        await sw.async_apply_param_change(reset_timer=True)
        await hass.async_block_till_done()
        assert hass.states.get(entity).state == 'on' and transitions == []
        await sw.async_force_off()
        await hass.async_stop()
    run(scenario())


def test_busy_after_confirming_a_lost_reply_does_not_lose_the_newest_servo_target():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        sw._fe_last_power_raw = s.registers[2] = sw._acknowledged[2] = 33000
        s.c.data['power_grid'] = 0
        transport = s.client._client
        original = transport.write_registers.side_effect
        attempts = []
        async def write(address, values, **kwargs):
            attempts.append(list(values))
            if len(attempts) == 1:
                await original(address, values, **kwargs)
                raise OSError('applied but reply lost')
            if len(attempts) == 2:
                return SimpleNamespace(isError=lambda: True, exception_code=6)
            return await original(address, values, **kwargs)
        transport.write_registers.side_effect = write
        async def backoff(_):
            s.client._client = transport
            if len(attempts) == 1:
                s.c.numbers['force_export_power'] = 1
                sw._queue_servo()
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=backoff):
            sw._queue_servo()
            await asyncio.gather(*sw._pending_tasks)
        assert len(attempts) == 3
        # The confirmed 2500 W command plus the newest 1000 W grid-error
        # correction gives 2800 W; the obsolete 5000 W target gives 4000 W.
        assert s.registers == attempts[-1] and s.registers[2] == 34800
        assert sw._uncertain is None and not sw._suspended
        await sw.async_force_off()
    run(scenario())


def test_peak_shaving_retries_a_busy_reply_without_a_recovery_reset():
    async def scenario():
        s = DispatchSimulation()
        transport = s.client._client
        original = transport.write_registers.side_effect
        attempts = 0
        async def write(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return SimpleNamespace(isError=lambda: True, exception_code=6)
            return await original(*args, **kwargs)
        transport.write_registers.side_effect = write
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', new=AsyncMock()):
            await s.start()
        assert s.sw.is_on and attempts == 2
        assert [command[0] for _, command in s.writes] == [1]
        await s.sw.async_force_off()
    run(scenario())


def test_verified_reset_clears_a_terminal_fault_and_its_suspension():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        sw._record_fault(HomeAssistantError('test fault'), suspend=True)
        count = len(s.writes)
        await s.button('dispatch_reset').async_press()
        assert not sw.is_on and not sw.extra_state_attributes['dispatch_suspended']
        assert sw._dispatch_status == 'stopped' and sw._last_error is None
        assert len(s.writes) == count + 1 and s.state == 0
    run(scenario())

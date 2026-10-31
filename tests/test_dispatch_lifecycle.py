"""Fault injection through the real dispatch/client path; never uses a network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.alphaess_modbus import _async_update_options, async_unload_entry
from custom_components.alphaess_modbus.const import DOMAIN
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator
from custom_components.alphaess_modbus.dispatch import WriteIndeterminate, WriteNotSent, WriteRejected, command_matches
from .dispatch_simulation import DispatchSimulation


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 5))


@pytest.mark.parametrize('key', ['force_charging', 'force_discharging', 'force_export', 'force_import', 'dispatch'])
def test_eight_hour_session_uses_remaining_time_and_duration_change_restarts(key):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other(key)
        s.c.numbers[f'{key}_duration'] = 480
        await sw.async_turn_on()
        deadline, started = sw._deadline, s.c.dispatch_started_at
        assert 28798 <= s.writes[-1][1][8] <= 28800
        sw._deadline -= 3600
        await sw.async_apply_param_change(reset_timer=False)
        assert 25198 <= s.writes[-1][1][8] <= 25200
        assert s.c.dispatch_started_at == started
        assert sw._deadline == deadline - 3600
        await sw.async_apply_param_change(reset_timer=True)
        assert sw._deadline > deadline - 1
        assert 28798 <= s.writes[-1][1][8] <= 28800
        await sw.async_force_off()
    run(scenario())


def test_deferred_seed_has_deadline_and_metadata_before_first_write():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        s.c.data['pv1_power'] = None
        await sw.async_turn_on()
        assert not s.writes and sw._deadline is not None
        assert s.c.dispatch_started_at is not None and sw._duration_cancel is not None
        sw._deadline -= 300
        s.sample()
        for callback in list(s.listeners):
            callback()
        await asyncio.gather(*sw._pending_tasks)
        assert 6898 <= s.writes[-1][1][8] <= 6900
        await sw.async_force_off()
    run(scenario())


def test_failed_restart_removes_old_timer_and_old_callback_cannot_stop_next_session():
    async def scenario():
        s = DispatchSimulation()
        old = s.other('force_charging')
        await old.async_turn_on()
        timer = old._duration_cancel
        callback = timer._callback
        transport = s.client._client
        original = transport.write_registers.side_effect
        transport.write_registers.side_effect = OSError('reply lost')
        with pytest.raises(WriteIndeterminate):
            await old.async_turn_on()
        assert not old.is_on and timer.cancelled() and s.c.active_dispatch_key is None
        transport.write_registers.side_effect = original
        s.client._client = transport
        new = s.other('force_discharging')
        await new.async_turn_on()
        count = len(s.writes)
        callback()
        await asyncio.gather(*old._pending_tasks)
        assert len(s.writes) == count and s.state == 1 and new.is_on
        await new.async_force_off()
    run(scenario())


def test_remaining_time_computed_after_reconnect_and_expired_command_not_sent():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        async def reconnect():
            sw._deadline -= 120
        s.client._ensure_connected.side_effect = reconnect
        await sw.async_apply_param_change(reset_timer=False)
        assert 7078 <= s.writes[-1][1][8] <= 7080
        count = len(s.writes)
        async def expire():
            sw._deadline = 0
        s.client._ensure_connected.side_effect = expire
        with pytest.raises(WriteNotSent):
            await sw.async_apply_param_change(reset_timer=False)
        assert len(s.writes) == count
        await sw.async_force_off()
    run(scenario())


@pytest.mark.parametrize('layer', ['client', 'transaction', 'ctx'])
def test_dispatch_disables_transport_retries_only_under_lock_and_restores_policy(layer):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        transport = s.client._client
        if layer != 'client':
            del transport.retries
            setattr(transport, layer, SimpleNamespace(retries=3))
        owner = transport if layer == 'client' else getattr(transport, layer)
        original = transport.write_registers.side_effect
        async def write(*a, **kw):
            assert owner.retries == 0 and s.client._lock.locked()
            return await original(*a, **kw)
        transport.write_registers.side_effect = write
        await sw.async_turn_on()
        assert owner.retries == 3
        await sw.async_force_off()
    run(scenario())


def test_timeout_that_applied_is_recognized_on_retry_not_mistaken_for_external_writer():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        s.c.data['power_grid'] = 0
        sw._fe_last_power_raw = s.registers[2] = sw._acknowledged[2] = 33000
        transport = s.client._client
        original = transport.write_registers.side_effect
        first = True
        async def write(*a, **kw):
            nonlocal first
            result = await original(*a, **kw)
            if first:
                first = False
                raise OSError('applied but acknowledgement lost')
            return result
        transport.write_registers.side_effect = write
        async def reconnect(_):
            assert sw._uncertain == s.registers
            assert sw._acknowledged[2] == 33000
            s.client._client = transport
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=reconnect):
            sw._queue_servo()
            await asyncio.gather(*sw._pending_tasks)
        assert not sw._suspended and sw._uncertain is None
        assert sw._acknowledged == s.registers and s.registers[2] > 33000
        assert transport.retries == 3
        await sw.async_force_off()
    run(scenario())


@pytest.mark.parametrize('code,outcome', [(2, WriteRejected), (0, WriteIndeterminate), (5, WriteIndeterminate)])
def test_explicit_rejection_vs_library_timeout_and_processing_response(code, outcome):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        transport = s.client._client
        transport.write_registers.side_effect = None
        transport.write_registers.return_value = SimpleNamespace(isError=lambda: True, exception_code=code)
        with pytest.raises(outcome):
            await sw.async_turn_on()
        assert not sw.is_on and sw._duration_cancel is None
        assert transport.retries == 3
        assert (sw._uncertain is None) == (code == 2)
    run(scenario())


def test_readback_mismatch_suspends_worker_and_deadline_does_not_reset_external_command():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        timer = sw._duration_cancel
        s.registers[2] = 39000
        count = len(s.writes)
        sw._queue_servo()
        await asyncio.gather(*sw._pending_tasks)
        assert sw._suspended and sw._dispatch_status == 'conflict'
        timer._callback()
        await asyncio.gather(*sw._pending_tasks)
        assert len(s.writes) == count and s.registers[2] == 39000
    run(scenario())


def test_single_worker_coalesces_slow_updates_and_reads_newest_target():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        transport = s.client._client
        original = transport.write_registers.side_effect
        sw._fe_last_power_raw = s.registers[2] = sw._acknowledged[2] = 33000
        s.c.data['power_grid'] = 0
        entered, release = asyncio.Event(), asyncio.Event()
        async def write(*a, **kw):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            return await original(*a, **kw)
        transport.write_registers.side_effect = write
        sw._queue_servo()
        await entered.wait()
        worker = sw._servo_worker
        for target in (1, 2, 3, 4):
            s.c.numbers['force_export_power'] = target
            sw._queue_servo()
        assert sw._servo_worker is worker
        assert sum(not task.done() for task in sw._pending_tasks) == 1
        count = len(s.writes)
        release.set()
        await asyncio.gather(*sw._pending_tasks)
        assert len(s.writes) == count + 2  # in-flight + one newest evaluation
        await sw.async_force_off()
    run(scenario())


def test_deadline_during_retry_prevents_another_packet():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        transport = s.client._client
        count = transport.write_registers.await_count
        sw._fe_last_write_time = 0
        transport.write_registers.side_effect = OSError('reply lost')
        async def expire(_):
            sw._deadline = 0
        with patch('custom_components.alphaess_modbus.switch.asyncio.sleep', side_effect=expire):
            sw._queue_servo()
            await asyncio.gather(*sw._pending_tasks)
        assert transport.write_registers.await_count == count + 1
        await sw.async_force_off()
    run(scenario())


def test_unload_drains_untracked_service_write_before_closing_and_notifies():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        transport = s.client._client
        original = transport.write_registers.side_effect
        entered, release = asyncio.Event(), asyncio.Event()
        async def write(*a, **kw):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            assert not transport.close.called
            return await original(*a, **kw)
        transport.write_registers.side_effect = write
        start = asyncio.create_task(sw.async_turn_on())
        await entered.wait()
        async def unload(*_):
            for entity in s.switches.values():
                await entity.async_will_remove_from_hass()
            return True
        s.hass.config_entries = SimpleNamespace(async_unload_platforms=unload)
        s.hass.services = SimpleNamespace(async_remove=Mock())
        s.hass.data[DOMAIN]['test'] = s.c
        with patch('homeassistant.components.persistent_notification.async_create') as notice:
            unloading = asyncio.create_task(async_unload_entry(s.hass, s.entry))
            await asyncio.sleep(0)
            assert not sw.is_on and not unloading.done() and not start.cancelled()
            assert not transport.close.called
            release.set()
            assert await unloading
            await start
            notice.assert_called_once()
        transport.close.assert_called_once()
        assert not start.cancelled() and not s.listeners
    run(scenario())


@pytest.mark.parametrize('key', ['dispatch_pv','force_charging_hold','force_export_hold'])
def test_unload_does_not_write_preferences_off(key):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other(key)
        sw._is_on = True
        sw.async_write_ha_state.reset_mock()
        await sw.async_will_remove_from_hass()
        assert sw.is_on
        sw.async_write_ha_state.assert_not_called()
        assert not s.writes
    run(scenario())


def test_poll_options_apply_without_reload_or_controller_restart():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        await sw.async_turn_on()
        deadline = sw._deadline
        s.c._model_variant = 'standard'
        s.c.apply_poll_options = lambda options: AlphaESSCoordinator.apply_poll_options(s.c, options)
        s.c.async_request_refresh = AsyncMock()
        s.hass.data[DOMAIN]['test'] = s.c
        s.hass.config_entries = SimpleNamespace(async_reload=AsyncMock())
        s.entry.options = dict(model_variant='standard',poll_mode='fast',fast_multiplier=0.25)
        await _async_update_options(s.hass, s.entry)
        s.hass.config_entries.async_reload.assert_not_called()
        assert s.c._scan_multiplier == 0.25 and s.c.update_interval.total_seconds() == 0.25
        assert sw.is_on and sw._deadline == deadline and len(s.writes) == 1
        s.entry.options['model_variant'] = 'b3'
        await _async_update_options(s.hass, s.entry)
        s.hass.config_entries.async_reload.assert_awaited_once_with('test')
        await sw.async_force_off()
    run(scenario())


def test_command_comparison_handles_countdown_and_unchanged_pv_but_not_other_modes():
    command = [1,0,33000,0,32000,2,50,0,300,255,0]
    observed = list(command)
    observed[8], observed[10] = 200, 1
    assert command_matches(observed, command)
    observed[5] = 19
    assert not command_matches(observed, command)


@pytest.mark.parametrize('address', [0x0880, 0x0885, 0x088A, 0x0740])
def test_raw_dispatch_write_relinquishes_sessions_but_unrelated_register_does_not(address):
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        callback = sw._duration_cancel._callback
        await AlphaESSCoordinator.async_write_raw(s.c, address, 1)
        count = len(s.writes)
        if address != 0x0740:
            assert not sw.is_on and s.c.active_dispatch_key is None
            callback()
            await asyncio.gather(*sw._pending_tasks)
            assert len(s.writes) == count
        else:
            assert sw.is_on
        assert s.single_writes == [(address, 1)]
        await sw.async_force_off()
    run(scenario())


def test_connect_failure_is_not_sent_and_does_not_claim_an_uncertain_command():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        s.client._ensure_connected.side_effect = OSError('not connected')
        with pytest.raises(WriteNotSent):
            await sw.async_turn_on()
        assert not s.writes and sw._uncertain is None and not sw.is_on
        assert sw._dispatch_status == 'not_sent' and sw._duration_cancel is None
    run(scenario())


def test_short_readback_is_not_falsely_reported_as_an_external_writer():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        await sw.async_turn_on()
        s.client._client.read_holding_registers.side_effect = None
        s.client._client.read_holding_registers.return_value = SimpleNamespace(isError=lambda: False, registers=[1])
        with pytest.raises(WriteNotSent):
            await sw.async_apply_param_change(reset_timer=False)
        assert sw._dispatch_status == 'not_sent' and len(s.writes) == 1
        assert s.client._client.retries == 3
        await sw.async_force_off()
    run(scenario())


def test_automatic_deadline_after_natural_expiry_does_not_write_another_reset():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        callback = sw._duration_cancel._callback
        count = len(s.writes)
        s.registers[0] = s.state = 0
        callback()
        await asyncio.gather(*sw._pending_tasks)
        assert not sw.is_on and len(s.writes) == count
    run(scenario())


def test_failed_setup_first_refresh_closes_connected_client():
    from custom_components.alphaess_modbus import async_setup_entry
    async def scenario():
        client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), connected=True)
        coordinator = SimpleNamespace(async_config_entry_first_refresh=AsyncMock(side_effect=OSError('poll failed')))
        entry = SimpleNamespace(data={'host':'unused','port':502,'slave_id':1})
        with patch('custom_components.alphaess_modbus.AlphaESSModbusClient', return_value=client), patch(
            'custom_components.alphaess_modbus.AlphaESSCoordinator', return_value=coordinator
        ):
            with pytest.raises(OSError):
                await async_setup_entry(SimpleNamespace(), entry)
        client.close.assert_awaited_once()
    run(scenario())


def test_actual_pymodbus_client_retry_owner_and_keyword_signature():
    import inspect
    from pymodbus.client import AsyncModbusTcpClient
    from custom_components.alphaess_modbus.modbus_client import _SLAVE_KWARG
    async def scenario():
        s = DispatchSimulation()
        transport = AsyncModbusTcpClient('unused', timeout=5, reconnect_delay=0)
        inspect.signature(transport.write_registers).bind(0x0880, [0]*11, **{_SLAVE_KWARG:1})
        owner = next(obj for obj in (getattr(transport,'ctx',None),getattr(transport,'transaction',None),transport)
                     if obj is not None and hasattr(obj,'retries'))
        retries = owner.retries
        async def write(*args, **kwargs):
            assert owner.retries == 0
            return SimpleNamespace(isError=lambda: False)
        transport.write_registers = AsyncMock(side_effect=write)
        s.client._client = transport
        sw = s.other('force_charging')
        await sw.async_turn_on()
        assert owner.retries == retries
        await sw.async_force_off()
        await s.client.close()
    run(scenario())


def test_known_pv_preference_change_does_not_look_like_external_takeover():
    async def scenario():
        s = DispatchSimulation()
        s.c.async_write_register = lambda *a: AlphaESSCoordinator.async_write_register(s.c, *a)
        sw = s.other('dispatch')
        await sw.async_turn_on()
        pv = s.other('dispatch_pv')
        await pv.async_turn_off()
        await sw.async_apply_param_change(reset_timer=False)
        assert not sw._suspended and s.registers[10] == 2
        await sw.async_force_off()
    run(scenario())


def test_old_servo_failure_cannot_suspend_new_session():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_export')
        await sw.async_turn_on()
        sw._fe_last_write_time = 0
        transport = s.client._client
        original = transport.write_registers.side_effect
        entered, release = asyncio.Event(), asyncio.Event()
        first = True
        async def write(*a, **kw):
            nonlocal first
            if first:
                first = False
                entered.set()
                await asyncio.wait_for(release.wait(), 2)
                raise OSError('old session failed')
            return await original(*a, **kw)
        async def connect():
            if s.client._client is None:
                s.client._client = transport
        s.client._ensure_connected.side_effect = connect
        transport.write_registers.side_effect = write
        sw._queue_servo()
        await entered.wait()
        old_worker = sw._servo_worker
        restart = asyncio.create_task(sw.async_turn_on())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(old_worker, restart)
        assert sw.is_on and not sw._suspended
        assert sw._dispatch_status == 'acknowledged'
        await sw.async_force_off()
    run(scenario())


def test_predbat_hold_freeze_power_updates_and_explicit_demand_release():
    async def scenario():
        s = DispatchSimulation()
        charging = s.other('force_charging')
        hold = s.other('force_charging_hold')
        s.c.numbers['force_charging_duration'] = 480
        await hold.async_turn_on()
        await charging.async_turn_on()
        charging._bp_near_zero_since = -10000
        s.c.data['power_battery'] = 0
        for callback in list(s.listeners):
            callback()
        assert charging.is_on and not charging._pending_tasks
        await charging.async_turn_off()
        await hold.async_turn_off()
        dispatch = s.other('dispatch')
        s.c.numbers.update(dispatch_duration=480, dispatch_power=-8)
        s.c.get_select = lambda _: 'Battery only Charges from PV (1)'
        await dispatch.async_turn_on()
        assert s.registers[5] == 1 and s.registers[2] == 24000
        dispatch._deadline -= 1800
        deadline = dispatch._deadline
        for power in (0,-8):
            s.c.numbers['dispatch_power'] = power
            await dispatch.async_apply_param_change(reset_timer=False)
            assert s.registers[2] == 32000 + power*1000
            assert dispatch._deadline == deadline and 26998 <= s.registers[8] <= 27000
        await dispatch.async_turn_off()  # explicit return from Hold/Freeze
        assert s.state == 0 and s.c.active_dispatch_key is None
        s.c.get_select = lambda _: 'No Battery Charge (19)'
        s.c.numbers['dispatch_power'] = 0
        await dispatch.async_turn_on()
        assert s.registers[5] == 19 and s.registers[2] == 32000
        await dispatch.async_turn_off()
        assert s.state == 0 and not s.listeners
    run(scenario())


def test_service_cancellation_does_not_cancel_wire_or_close_connection_under_request():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        transport = s.client._client
        original = transport.write_registers.side_effect
        entered, release = asyncio.Event(), asyncio.Event()
        wire = []
        async def write(*a, **kw):
            wire.append(asyncio.current_task())
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            assert not transport.close.called
            return await original(*a, **kw)
        transport.write_registers.side_effect = write
        start = asyncio.create_task(sw.async_turn_on())
        await entered.wait()
        start.cancel()  # emulate an HA mode:restart caller, not integration policy
        await asyncio.sleep(0)
        close = asyncio.create_task(s.client.close())
        await asyncio.sleep(0)
        assert not close.done() and not wire[0].cancelled()
        release.set()
        result = await asyncio.gather(start, return_exceptions=True)
        await close
        assert isinstance(result[0], asyncio.CancelledError)
        assert not wire[0].cancelled() and not sw.is_on and not s.listeners
        assert s.state == 1 and sw._uncertain is not None
    run(scenario())


def test_failed_stop_is_uncertain_and_reported_without_a_second_reset():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        await sw.async_turn_on()
        transport = s.client._client
        count = transport.write_registers.await_count
        transport.write_registers.side_effect = OSError('stop reply lost')
        with pytest.raises(WriteIndeterminate):
            await sw.async_turn_off()
        assert not sw.is_on and sw._dispatch_status == 'indeterminate'
        assert sw._uncertain[0] == 0 and sw._expected_expiry is not None
        assert transport.write_registers.await_count == count + 1
    run(scenario())


def test_countdown_uses_monotonic_deadline_despite_wall_clock_metadata_change():
    from datetime import datetime, timezone, timedelta
    from custom_components.alphaess_modbus.sensor import AlphaESSModeCountdownSensor
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('force_charging')
        await sw.async_turn_on()
        sensor = AlphaESSModeCountdownSensor(s.c, s.entry, sw.switch_key, 'countdown', 'mdi:timer')
        s.c.dispatch_started_at = datetime.now(timezone.utc) - timedelta(days=1)
        assert 7198 <= sensor.native_value <= 7200
        await sw.async_force_off()
        assert sensor.native_value is None
    run(scenario())


def test_sync_does_not_preserve_a_paused_controller_that_is_suspended():
    async def scenario():
        s = DispatchSimulation()
        sw = s.other('excess_export')
        await sw.async_turn_on()
        for callback in list(s.listeners):
            callback()
        await asyncio.gather(*sw._pending_tasks)
        assert s.c.ee_paused and s.state == 0
        sw._suspended = True
        count = len(s.writes)
        await s.button('sync_dispatch_state').async_press()
        assert not sw.is_on and not s.c.ee_paused and not s.listeners
        assert len(s.writes) == count and sw._dispatch_status == 'stopped'
    run(scenario())


def test_raw_takeover_also_drains_peak_and_clears_its_ownership():
    async def scenario():
        s = DispatchSimulation()
        await s.start()
        assert s.c.active_dispatch_key == 'peak_shaving'
        await AlphaESSCoordinator.async_write_raw(s.c, 0x0885, 1)
        count = len(s.writes)
        assert not s.sw.is_on and s.c.active_dispatch_key is None
        assert not s.listeners and s.single_writes == [(0x0885,1)]
        await s.step()
        assert len(s.writes) == count
    run(scenario())

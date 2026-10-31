"""Simulated controller and dispatch writes; never connects to an inverter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.alphaess_modbus.const import DOMAIN
from custom_components.alphaess_modbus.peak_shaving import (
    AlphaESSPeakShavingSwitch, PEAK_NUMBERS, _REQUIRED,
)
from custom_components.alphaess_modbus.switch import AlphaESSSwitch, SWITCH_DEFS, _MUTEX_SWITCHES
from custom_components.alphaess_modbus.number import AlphaESSNumber
from custom_components.alphaess_modbus.button import AlphaESSButton


class Simulation:
    def __init__(self):
        self.now = 1000.0
        self.listeners = []
        self.c = SimpleNamespace(
            data={}, _last_polled={}, _scan_multiplier=1, last_update_success=True,
            ac_limit_w=8000, active_dispatch_key=None,
            numbers={'peak_shaving_power': 4, 'peak_shaving_cutoff_soc': 100},
            async_write_dispatch=AsyncMock(), async_reset_dispatch=AsyncMock(),
            async_write_register=AsyncMock(), ee_paused=False, fi_paused=False,
            client=SimpleNamespace(read_register=AsyncMock(return_value=0)),
        )
        self.c.get_number = self.c.numbers.get
        self.c.get_select = lambda key: None
        self.c.async_add_listener = self.add_listener
        self.entry = SimpleNamespace(entry_id='test')
        self.sw = AlphaESSPeakShavingSwitch(self.c, self.entry)
        self.switches = {self.sw.switch_key: self.sw}
        self.hass = SimpleNamespace(data={DOMAIN: {'test_switches': self.switches}},
                                    async_create_task=lambda coro, **kw: asyncio.create_task(coro))
        self.sw.hass = self.hass
        self.sw.async_write_ha_state = Mock()
        self.sample()

    def add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def sample(self, house=7000, pv=1000, battery=0, soc=50, mode=1, lower=10):
        self.c.data.update(power_grid=house-pv-battery, power_battery=battery,
                           pv1_power=pv, pv2_power=0, pv3_power=0, pv4_power=0,
                           active_power_pv_meter=0, soc_battery=soc,
                           inverter_work_mode=mode, discharging_cutoff_soc=lower,
                           dispatch_start=0)
        self.c._last_polled = {key: self.now for key in (*_REQUIRED, "dispatch_start")}

    async def step(self, seconds=5, **sample):
        self.now += seconds
        self.sample(**sample)
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=self.now):
            await self.sw._update()

    async def start(self):
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=self.now), patch(
            'custom_components.alphaess_modbus.peak_shaving.async_track_time_interval', return_value=Mock()
        ):
            await self.sw.async_turn_on()

    def last(self):
        return self.c.async_write_dispatch.call_args.args[0]

    def other(self, key):
        sw = AlphaESSSwitch(self.c, self.entry, next(d for d in SWITCH_DEFS if d['key'] == key))
        sw.hass = self.hass
        sw.async_write_ha_state = Mock()
        self.switches[key] = sw
        return sw


def test_disabled_setup_restore_and_updates_never_write():
    async def run():
        s = Simulation()
        s.sw.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state='on'))
        s.sw.async_on_remove = Mock()
        await s.sw.async_added_to_hass()
        assert not s.sw.is_on
        for _ in range(5):
            await s.step()
            s.sw._queue_update()
        await s.sw.async_turn_off()
        await s.sw.async_will_remove_from_hass()
        s.c.async_write_dispatch.assert_not_called()
        s.c.async_reset_dispatch.assert_not_called()
        assert s.sw._worker is None and s.sw._heartbeat_cancel is None
    asyncio.run(run())


def test_example_dispatch_block_and_power_bounds():
    async def run():
        s = Simulation()
        await s.start()
        assert s.last() == [1, 0, 34000, 0, 32000, 2, int(10/.392), 0, 300, 255, 0]
        await s.step(house=100000)
        assert 34000 < s.last()[2] <= 40000  # ramp toward the 8 kW AC cap
        assert s.c.async_write_register.call_count == 0
        s.c.numbers['peak_shaving_power'] = 0
        await s.step(house=100000)
        assert s.last()[2] == 40000
    asyncio.run(run())


def test_below_limit_holds_zero_not_grid_charge_or_export():
    async def run():
        s = Simulation()
        s.sample(house=2000, pv=0)
        await s.start()
        assert s.last() == [1, 0, 32000, 0, 32000, 2, 255, 0, 300, 255, 0]
        assert s.sw.reason == 'below_import_limit'
    asyncio.run(run())


def test_constant_load_renews_lease_without_repeated_writes():
    async def run():
        s = Simulation()
        await s.start()
        for _ in range(239):
            await s.step(seconds=1)
        assert s.c.async_write_dispatch.call_count == 1
        await s.step(seconds=1)
        assert s.c.async_write_dispatch.call_count == 2
        assert s.last()[8] == 300
    asyncio.run(run())


def test_write_rate_limit_and_deadband():
    async def run():
        s = Simulation()
        await s.start()
        await s.step(seconds=1, house=7100)
        assert s.c.async_write_dispatch.call_count == 1
        await s.step(seconds=4, house=7100)  # remains inside the write deadband
        assert s.c.async_write_dispatch.call_count == 1
        await s.step(seconds=1, house=10000)
        assert s.c.async_write_dispatch.call_count == 2
        assert 34050 <= s.last()[2] <= 37000
    asyncio.run(run())


@pytest.mark.parametrize('key', _REQUIRED)
def test_each_missing_or_stale_input_pauses_once_and_stops_refresh(key):
    async def run():
        s = Simulation()
        await s.start()
        s.c._last_polled[key] = -10000
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=s.now):
            await s.sw._update()
            await s.sw._update()
        assert s.sw.paused
        s.c.async_reset_dispatch.assert_awaited_once()
        assert s.c.async_write_dispatch.call_count == 1
        s.c.data.pop(key)
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=s.now):
            await s.sw._update()
        assert s.c.async_reset_dispatch.call_count == 1
    asyncio.run(run())


@pytest.mark.parametrize('bad', [None, 'bad', float('nan'), float('inf')])
def test_bad_input_never_converted_to_zero(bad):
    async def run():
        s = Simulation()
        s.c.data['power_grid'] = bad
        await s.start()
        assert s.sw.paused
        s.c.async_write_dispatch.assert_not_called()
        s.c.async_reset_dispatch.assert_not_called()
    asyncio.run(run())


def test_failure_recovery_waits_for_normal_mode_ten_minutes():
    async def run():
        s = Simulation()
        await s.start()
        await s.step(mode=2)
        assert s.sw.paused and s.sw.reason == 'inverter_not_normal'
        await s.step()
        await s.step(seconds=599)
        assert s.sw.paused
        await s.step(seconds=1)
        assert not s.sw.paused
        assert s.c.async_write_dispatch.call_count == 2
    asyncio.run(run())


def test_soc_upper_threshold_lower_cutoff_and_resume():
    async def run():
        s = Simulation()
        s.c.numbers['peak_shaving_cutoff_soc'] = 70
        s.sample(soc=70)
        await s.start()
        assert s.sw.paused
        s.c.async_write_dispatch.assert_not_called()
        await s.step(seconds=600, soc=69)
        assert not s.sw.paused
        await s.step(soc=10)
        assert s.sw.paused and s.sw.reason == 'discharge_cutoff'
        await s.step(seconds=15, soc=10)
        assert s.sw.paused
        await s.step(soc=11)
        assert not s.sw.paused
    asyncio.run(run())


def test_solar_surplus_pause_and_debounce():
    async def run():
        s = Simulation()
        s.sample(house=1000, pv=2000)
        await s.start()
        assert s.sw.paused and s.sw.reason == 'solar_surplus'
        await s.step(seconds=600, house=2000, pv=0)
        assert not s.sw.paused
        await s.step(seconds=1, house=100, pv=10000)
        assert not s.sw.paused  # grace following resume
        await s.step(seconds=14, house=100, pv=10000)
        assert s.sw.paused
    asyncio.run(run())


@pytest.mark.parametrize('key', ['force_charging', 'force_discharging', 'force_import', 'force_export', 'dispatch', 'excess_export'])
def test_peak_refuses_to_take_over_existing_modes(key):
    async def run():
        s = Simulation()
        other = s.other(key)
        other._is_on = True
        s.c.active_dispatch_key = key
        with pytest.raises(Exception, match='Stop the active dispatch'):
            await s.start()
        assert other.is_on and not s.sw.is_on
        s.c.async_reset_dispatch.assert_not_called()
        s.c.async_write_dispatch.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('key, starter', [
    ('force_charging', '_start_force_charging'),
    ('force_discharging', '_start_force_discharging'),
    ('force_import', '_start_force_import'),
    ('force_export', '_start_force_export'),
    ('dispatch', '_start_dispatch'),
    ('excess_export', '_start_excess_export'),
])
def test_existing_modes_stop_peak_before_starting(key, starter):
    async def run():
        s = Simulation()
        await s.start()
        other = s.other(key)
        async def start(*args, **kwargs):
            assert not s.sw.is_on
            assert s.sw._heartbeat_cancel is None
            s.c.async_reset_dispatch.assert_awaited_once()
        setattr(other, starter, AsyncMock(side_effect=start))
        other._schedule_duration_off = Mock()
        other._start_battery_power_watcher = Mock()
        await other.async_turn_on()
        assert other.is_on and s.c.active_dispatch_key == key
        await s.step()
        assert s.c.async_write_dispatch.call_count == 1
    asyncio.run(run())


def test_write_failure_stops_session_and_attempts_single_reset():
    async def run():
        s = Simulation()
        s.c.async_write_dispatch.side_effect = OSError('connection lost')
        s.c.async_reset_dispatch.side_effect = OSError('still disconnected')
        await s.start()
        assert not s.sw.is_on and s.sw.reason == 'write_failed'
        assert s.sw._heartbeat_cancel is None
        await s.step(seconds=600)
        s.c.async_write_dispatch.assert_awaited_once()
        s.c.async_reset_dispatch.assert_awaited_once()
    asyncio.run(run())


def test_stop_waits_for_inflight_write_and_no_stale_commands_follow():
    async def run():
        s = Simulation()
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def delayed(values):
            entered.set()
            await release.wait()
            order.append('write')
        async def reset():
            order.append('reset')
        s.c.async_write_dispatch.side_effect = delayed
        s.c.async_reset_dispatch.side_effect = reset
        start = asyncio.create_task(s.start())
        await entered.wait()
        stop = asyncio.create_task(s.sw.async_turn_off())
        await asyncio.sleep(0)
        assert not s.sw.is_on
        release.set()
        await asyncio.gather(start, stop)
        await s.step()
        assert order == ['write', 'reset']
    asyncio.run(run())


def test_reset_button_stops_peak_controller_before_reset():
    async def run():
        s = Simulation()
        await s.start()
        button = AlphaESSButton(s.c, s.entry, {'key':'dispatch_reset', 'name':'Reset', 'icon':'mdi:restart'})
        button.hass = s.hass
        with patch("custom_components.alphaess_modbus.button.persistent_notification.async_dismiss"):
            await button.async_press()
        await s.step()
        assert not s.sw.is_on
        s.c.async_reset_dispatch.assert_awaited_once()
        assert s.c.async_write_dispatch.call_count == 1
    asyncio.run(run())


def test_settings_are_local_and_disabled_changes_do_not_write():
    async def run():
        s = Simulation()
        for definition in PEAK_NUMBERS:
            assert definition.address is None
            number = AlphaESSNumber(s.c, s.entry, definition)
            number.hass = s.hass
            number.async_write_ha_state = Mock()
            await number.async_set_native_value(definition.default_value)
        s.c.async_write_dispatch.assert_not_called()
        s.c.async_write_register.assert_not_called()
        s.c.async_reset_dispatch.assert_not_called()
    asyncio.run(run())


def test_parameter_change_updates_active_setpoint_without_extra_polling():
    async def run():
        s = Simulation()
        await s.start()
        s.now += 5
        number = AlphaESSNumber(s.c, s.entry, PEAK_NUMBERS[0])
        number.hass = s.hass
        number.async_write_ha_state = Mock()
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=s.now):
            await number.async_set_native_value(3)
        assert s.last()[2] == 35000
        s.c.async_write_register.assert_not_called()
    asyncio.run(run())


def test_invalid_settings_pause_without_command():
    async def run():
        for key, value in [('peak_shaving_power', -1), ('peak_shaving_power', 9),
                           ('peak_shaving_cutoff_soc', 10), ('peak_shaving_cutoff_soc', 101)]:
            s = Simulation()
            s.c.numbers[key] = value
            await s.start()
            assert s.sw.paused
            s.c.async_write_dispatch.assert_not_called()
    asyncio.run(run())


def test_external_dispatch_not_overwritten():
    async def run():
        s = Simulation()
        s.c.data['dispatch_start'] = 1
        with pytest.raises(Exception, match='Reset the existing'):
            await s.start()
        s.c.async_write_dispatch.assert_not_called()
    asyncio.run(run())


def test_closed_loop_constant_demand_does_not_feed_battery_back_into_target():
    async def run():
        s = Simulation()
        await s.start()
        for _ in range(60):
            battery = s.last()[2] - 32000
            await s.step(house=7000, pv=1000, battery=battery)
            assert s.last()[2] == 34000
        assert s.c.async_write_dispatch.call_count == 2  # renewal only
    asyncio.run(run())


def test_other_mode_waits_when_peak_is_already_stopping():
    async def run():
        s = Simulation()
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def delayed(values):
            entered.set()
            await release.wait()
            order.append('peak_write')
        async def reset():
            order.append('reset')
        s.c.async_write_dispatch.side_effect = delayed
        s.c.async_reset_dispatch.side_effect = reset
        start = asyncio.create_task(s.start())
        await entered.wait()
        stop = asyncio.create_task(s.sw.async_turn_off())
        await asyncio.sleep(0)
        other = s.other('force_charging')
        other._start_battery_power_watcher = Mock()
        async def other_start():
            order.append('other_start')
        other._start_force_charging = AsyncMock(side_effect=other_start)
        takeover = asyncio.create_task(other.async_turn_on())
        await asyncio.sleep(0)
        assert 'other_start' not in order
        release.set()
        await asyncio.gather(start, stop, takeover)
        assert order == ['peak_write', 'reset', 'other_start']
        await s.step()
        assert s.c.active_dispatch_key == 'force_charging'
    asyncio.run(run())


def test_repeated_enable_and_off_are_idempotent():
    async def run():
        s = Simulation()
        await s.start()
        await s.start()
        s.c.async_write_dispatch.assert_awaited_once()
        assert len(s.listeners) == 1
        await s.sw.async_turn_off()
        await s.sw.async_turn_off()
        s.c.async_reset_dispatch.assert_awaited_once()
        assert not s.listeners
    asyncio.run(run())


def test_coalesces_callbacks_while_command_is_in_flight():
    async def run():
        s = Simulation()
        await s.start()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(values):
            entered.set()
            await release.wait()
        s.c.async_write_dispatch.side_effect = delayed
        s.now += 240
        s.sample()
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=s.now):
            s.sw._queue_update()
            await entered.wait()
            worker = s.sw._worker
            for _ in range(20):
                s.sw._queue_update()
            assert s.sw._worker is worker
            release.set()
            await worker
        assert s.c.async_write_dispatch.call_count == 2
    asyncio.run(run())


@pytest.mark.parametrize('dispatch', [None, 'bad', float('nan'), 2])
def test_unknown_dispatch_state_cannot_be_taken_over(dispatch):
    async def run():
        s = Simulation()
        s.c.data['dispatch_start'] = dispatch
        with pytest.raises(Exception, match='fresh inactive'):
            await s.start()
        assert not s.sw.is_on
        s.c.async_write_dispatch.assert_not_called()
    asyncio.run(run())


def test_failed_poll_immediately_resets_active_dispatch():
    async def run():
        s = Simulation()
        await s.start()
        s.c.last_update_success = False
        await s.step()
        assert s.sw.paused
        s.c.async_reset_dispatch.assert_awaited_once()
    asyncio.run(run())


def test_active_unload_drains_and_resets_once():
    async def run():
        s = Simulation()
        await s.start()
        await s.sw.async_will_remove_from_hass()
        assert not s.sw.is_on and not s.listeners
        s.c.async_reset_dispatch.assert_awaited_once()
        await s.step()
        assert s.c.async_write_dispatch.call_count == 1
    asyncio.run(run())


@pytest.mark.parametrize('key', ['force_charging_hold', 'force_discharging_hold', 'force_export_hold', 'force_import_hold', 'dispatch_pv'])
def test_preferences_do_not_take_over_peak_dispatch(key):
    async def run():
        s = Simulation()
        await s.start()
        pref = s.other(key)
        await pref.async_turn_on()
        assert s.sw.is_on and s.c.active_dispatch_key == 'peak_shaving'
        s.c.async_reset_dispatch.assert_not_called()
    asyncio.run(run())


def test_ac_coupled_pv_counted_once():
    async def run():
        s = Simulation()
        s.c.data.update(pv1_power=0, active_power_pv_meter=1000)
        await s.start()
        assert s.last()[2] == 34000
    asyncio.run(run())


def test_demand_steps_with_delayed_battery_response_settle_without_limit_cycle():
    async def run():
        s = Simulation()
        await s.start()
        battery = 0
        for demand, expected in [(9000,4000), (3000,0), (6000,1000), (18000,8000)]:
            for _ in range(100):
                await s.step(house=demand, pv=1000, battery=battery)
                requested = s.last()[2] - 32000
                assert 0 <= requested <= 8000
                battery += .5 * (requested - battery)
            # Fresh samples must converge even after the demand becomes steady.
            assert abs(requested - expected) <= 100
    asyncio.run(run())


def test_cached_timer_ticks_do_not_advance_private_filter():
    async def run():
        s = Simulation()
        await s.start()
        await s.step(house=9000)
        filtered = dict(s.sw._filtered)
        for _ in range(20):
            with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=s.now):
                await s.sw._update()
        assert s.sw._filtered == filtered
    asyncio.run(run())


@pytest.mark.parametrize('lag', [1, 3, 10])
def test_asynchronous_grid_and_battery_samples_settle(lag):
    async def run():
        s = Simulation()
        await s.start()
        history = [0.] * (lag + 1)
        outputs = []
        for n in range(600):
            s.now += 2
            request = s.last()[2] - 32000
            battery = history[-1] + .4 * (request - history[-1])
            history.append(battery)
            s.sample(house=9000, pv=1000, battery=battery)
            # A delayed grid sample is deliberately inconsistent with the most
            # recent battery sample: the control must not amplify this forever.
            s.c.data['power_grid'] = 8000 - history[-lag-1]
            with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=s.now):
                await s.sw._update()
            outputs.append(s.last()[2] - 32000)
        assert all(abs(value - 4000) < 150 for value in outputs[-60:])
    asyncio.run(run())


def test_peak_refuses_pending_command_from_previous_mode():
    async def run():
        s = Simulation()
        other = s.other('force_export')
        pending = asyncio.create_task(asyncio.sleep(100))
        other._pending_tasks.add(pending)
        try:
            with pytest.raises(Exception, match='previous dispatch command'):
                await s.start()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        s.c.async_write_dispatch.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('key', ['force_charging', 'force_discharging', 'force_import', 'force_export', 'dispatch', 'excess_export'])
def test_existing_mode_commands_identical_with_peak_off_or_absent(key):
    async def capture(include_peak):
        s = Simulation()
        if not include_peak:
            s.switches.pop('peak_shaving')
        other = s.other(key)
        # Leave actual existing dispatch construction intact. Stub only scheduling
        # so these comparisons cannot leave background tasks after the test.
        for name in ('_schedule_duration_off', '_schedule_auto_off', '_start_battery_power_watcher',
                     '_start_force_export_watcher', '_start_force_import_watcher', '_start_ee_watcher'):
            setattr(other, name, Mock())
        await other.async_turn_on()
        commands = []
        for call in s.c.async_write_dispatch.call_args_list:
            kwargs = dict(call.kwargs)
            assert kwargs.pop("guard")()
            commands.append((call.args, kwargs))
        await other.async_turn_off()
        return commands, list(s.c.async_reset_dispatch.call_args_list)
    async def run():
        assert await capture(True) == await capture(False)
    asyncio.run(run())


def test_pause_sensor_and_settings_are_registered_without_changing_old_entities():
    from custom_components.alphaess_modbus.binary_sensor import (
        AlphaESSPeakShavingPauseSensor, async_setup_entry as setup_binary,
    )
    from custom_components.alphaess_modbus.switch import async_setup_entry as setup_switch
    from custom_components.alphaess_modbus.number import async_setup_entry as setup_number
    from custom_components.alphaess_modbus.const import NUMBER_REGISTERS
    async def run():
        s = Simulation()
        s.hass.data[DOMAIN]['test'] = s.c
        switches, numbers, binaries = [], [], []
        await setup_switch(s.hass, s.entry, switches.extend)
        await setup_number(s.hass, s.entry, numbers.extend)
        await setup_binary(s.hass, s.entry, binaries.extend)
        assert len(switches) == len(SWITCH_DEFS) + 1
        assert len(numbers) == len(NUMBER_REGISTERS) + 2
        assert all(not sw.is_on for sw in switches)
        pause = next(e for e in binaries if isinstance(e, AlphaESSPeakShavingPauseSensor))
        pause.hass = s.hass
        assert pause.is_on is False
        peak = s.hass.data[DOMAIN]['test_switches']['peak_shaving']
        peak._is_on = True
        peak.paused = True
        assert pause.is_on is True
        s.c.async_write_dispatch.assert_not_called()
        s.c.async_reset_dispatch.assert_not_called()
    asyncio.run(run())


def test_failed_manual_stop_clears_state_and_propagates_error():
    async def run():
        s = Simulation()
        await s.start()
        s.c.async_reset_dispatch.side_effect = RuntimeError("offline")
        with pytest.raises(RuntimeError, match="offline"):
            await s.sw.async_turn_off()
        assert not s.sw.is_on and not s.sw.paused
        assert s.sw.reason == "off" and s.sw._command is None
        assert s.c.active_dispatch_key is None
        assert s.sw._heartbeat_cancel is None and s.sw._listener_cancel is None
        writes = s.c.async_write_dispatch.call_count
        await s.step()
        assert s.c.async_write_dispatch.call_count == writes
    asyncio.run(run())

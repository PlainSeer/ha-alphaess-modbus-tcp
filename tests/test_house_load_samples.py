"""Inconsistent power snapshots must not become zero consumption or control commands."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pymodbus.exceptions import ModbusException

from custom_components.alphaess_modbus.const import SENSOR_REGISTERS
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator
from custom_components.alphaess_modbus.house_load import (
    HouseLoadSample, house_load, house_load_sample,
)
from custom_components.alphaess_modbus.sensor import AlphaESSCalculatedSensor, CALCULATED_SENSORS
from custom_components.alphaess_modbus.house_load_daily import AlphaESSHouseLoadDailySensor
from .dispatch_simulation import Simulation

MODULE = "custom_components.alphaess_modbus."


@contextmanager
def clock(now):
    # Do not replace asyncio's process-wide monotonic clock.
    fake = SimpleNamespace(monotonic=lambda: now)
    with patch(MODULE + "house_load.time", fake), patch(MODULE + "house_load_daily.time", fake):
        yield


def coordinator():
    return SimpleNamespace(data=dict(inverter_work_mode=1, power_grid=500, power_battery=0,
        pv1_power=0, pv2_power=0, pv3_power=0, pv4_power=0, active_power_pv_meter=0),
        last_update_success=True, _last_polled={})


def inconsistent(c):
    # Public issue #10's example: 1455 - 1932 - 1151 = -1628, not zero load.
    c.data.update(pv1_power=1455, power_battery=-1932, power_grid=-1151)


def test_reported_negative_balance_is_invalid_but_genuine_zero_is_valid():
    c = coordinator()
    inconsistent(c)
    assert house_load(c.data) is None
    c.data.update(pv1_power=1000, power_battery=-500, power_grid=-500)
    assert house_load(c.data) == 0


def test_shared_hold_expires_without_another_poll_or_reader_extending_it():
    c = coordinator()
    with clock(100):
        sample = house_load_sample(c)
        assert sample.value() == 500
    inconsistent(c)
    with clock(102):
        assert house_load_sample(c) is sample
        assert sample.value() == 500
        assert sample.raw is None and sample.status == "estimated"
    for now in (104, 108, 109.9):
        with clock(now):
            assert house_load_sample(c).value() == 500
            assert sample.hold_until == 110
    with clock(110):
        assert house_load_sample(c).value() is None
        assert sample.status == "inconsistent"
    with clock(112):
        c.data.update(pv1_power=0, power_battery=0, power_grid=700)
        assert house_load_sample(c).value() == 700
        assert sample.status == "valid"


@pytest.mark.parametrize("bad", [None, "bad", float("nan"), float("inf")])
def test_missing_and_nonfinite_values_never_hold_or_raise(bad):
    c = coordinator()
    with clock(100):
        house_load_sample(c)
    c.data["power_grid"] = bad
    with clock(102):
        assert house_load_sample(c).value() is None
        assert not house_load_sample(c).inconsistent
        assert house_load(c.data) is None


def test_failure_and_mode_change_clear_the_previous_estimate():
    c = coordinator()
    with clock(100):
        house_load_sample(c)
    with clock(102):
        c.last_update_success = False
        assert house_load_sample(c).value() is None
        c.last_update_success = True
        assert house_load_sample(c).value() == 500
        c.data.update(inverter_work_mode=2, power_grid=100)
        assert house_load_sample(c).value() == 100
        c.data.update(inverter_work_mode=1)
        inconsistent(c)
        assert house_load_sample(c).value() is None


def test_sensor_metadata_and_estimate_expiry():
    c = coordinator()
    entry = SimpleNamespace(entry_id="test")
    sensor = AlphaESSCalculatedSensor(c, entry, next(d for d in CALCULATED_SENSORS if d.key == "current_house_load"))
    with clock(100):
        assert sensor.native_value == 500
        assert sensor.extra_state_attributes == {"sample_status": "valid", "estimated": False, "sample_reason": "valid"}
    inconsistent(c)
    with clock(102):
        assert sensor.native_value == 500
        assert sensor.extra_state_attributes == {"sample_status": "estimated", "estimated": True, "sample_reason": "negative_balance"}
    with clock(111):
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {"sample_status": "inconsistent", "estimated": False, "sample_reason": "negative_balance"}


@pytest.mark.parametrize("recover_at", [6, 20])
def test_daily_energy_bridges_only_the_bounded_estimate(recover_at):
    c = coordinator()
    now = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
    entry = SimpleNamespace(entry_id="test")
    source = AlphaESSCalculatedSensor(c, entry, next(d for d in CALCULATED_SENSORS if d.key == "current_house_load"))
    daily = AlphaESSHouseLoadDailySensor(c, entry, source)
    daily._day = now.date()
    daily._gap = False

    def sample(seconds):
        c._last_polled = {k: 100 + seconds for k in c.data}
        with clock(100 + seconds):
            daily._sample(now + timedelta(seconds=seconds))

    sample(0)
    inconsistent(c)
    sample(2)
    c.data.update(pv1_power=0, power_battery=0, power_grid=500)
    sample(recover_at)
    assert daily._energy == pytest.approx(500 * min(recover_at, 10) / 3_600_000)
    assert daily.extra_state_attributes["incomplete_day"] == (recover_at > 10)


def test_daily_expiry_followed_by_outage_does_not_extrapolate():
    c = coordinator()
    now = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
    entry = SimpleNamespace(entry_id="test")
    source = AlphaESSCalculatedSensor(c, entry, next(d for d in CALCULATED_SENSORS if d.key == "current_house_load"))
    daily = AlphaESSHouseLoadDailySensor(c, entry, source)
    daily._day = now.date()
    for seconds in (0, 2, 12):
        c._last_polled = {k: 100 + seconds for k in c.data}
        if seconds:
            inconsistent(c)
        with clock(100 + seconds):
            daily._sample(now + timedelta(seconds=seconds))
    assert daily._energy == pytest.approx(500 * 10 / 3_600_000)
    assert daily.available
    assert daily.extra_state_attributes["accumulation_paused"]
    with clock(130):
        c.last_update_success = False
        daily._sample(now + timedelta(seconds=30))
    assert daily._energy == pytest.approx(500 * 10 / 3_600_000)


def test_partial_block_failure_invalidates_the_sample_despite_cached_values():
    async def run():
        c = coordinator()
        c._scan_multiplier, c._model_variant = 1, "standard"
        grid = next(r for r in SENSOR_REGISTERS if r.key == "power_grid")
        c._is_due = lambda reg: True
        c.client = SimpleNamespace(read_block=AsyncMock(side_effect=ModbusException("offline")))
        c.house_load_sample = HouseLoadSample()
        with clock(100):
            c.house_load_sample.update(c, c.data, 100)
            with patch(MODULE + "coordinator.SENSOR_REGISTERS", [grid]):
                data = await AlphaESSCoordinator._async_update_data(c)
            c.data = data
            assert c.data["power_grid"] == 500
            assert house_load_sample(c).value() is None
    asyncio.run(run())


@pytest.mark.parametrize("key", ["force_export", "force_import"])
def test_force_seed_and_servo_wait_for_consistent_power(key):
    async def run():
        s = Simulation()
        sw = s.other(key)
        sw._is_on = True
        with clock(100):
            house_load_sample(s.c)
        inconsistent(s.c)
        with clock(102):
            compute = getattr(sw, "_compute_" + key + "_power_raw")
            assert compute(s.c.data) is None
            assert sw._servo_word() is None  # no target-only seed fallback
            setattr(sw, "_fe_last_power_raw" if key == "force_export" else "_fi_last_power_raw", 35000)
            assert sw._servo_word() is None
            await getattr(sw, "_start_" + key)(300)
            for listener in list(s.listeners):
                listener()
            await asyncio.sleep(0)
        s.c.async_write_dispatch.assert_not_called()
        s.c.async_reset_dispatch.assert_not_called()
        s.sample()
        assert sw._servo_word() is not None
        sw.prepare_force_off()
    asyncio.run(run())


def test_excess_export_does_not_resume_using_an_estimated_house_load():
    async def run():
        s = Simulation()
        sw = s.other("excess_export")
        sw._is_on, s.c.ee_paused = True, True
        sw._start_ee_watcher()
        with clock(100):
            house_load_sample(s.c)
        inconsistent(s.c)
        with clock(102):
            for listener in list(s.listeners):
                listener()
        assert s.c.ee_paused
        s.c.async_write_dispatch.assert_not_called()
        s.c.async_reset_dispatch.assert_not_called()
        sw.prepare_force_off()
    asyncio.run(run())


def test_peak_skips_inconsistent_samples_without_pausing_or_resetting():
    async def run():
        s = Simulation()
        await s.start()
        count = s.c.async_write_dispatch.call_count
        old_filters = dict(s.sw._filtered)
        inconsistent(s.c)
        with patch(MODULE + "peak_shaving.time", SimpleNamespace(monotonic=lambda: s.now)):
            await s.sw._update()
        assert s.sw.is_on and not s.sw.paused and not s.sw._suspended
        assert s.sw.reason == "waiting_for_consistent_power"
        assert s.sw._filtered == old_filters
        assert s.c.async_write_dispatch.call_count == count
        s.c.async_reset_dispatch.assert_not_called()
        await s.step(house=8000)
        assert s.sw.reason != "waiting_for_consistent_power"
    asyncio.run(run())


def test_expiry_notifies_only_dependents_and_unload_cancels_timer():
    c = coordinator()
    entity, other_entity = Mock(), Mock()
    loop = Mock()
    with clock(100):
        sample = house_load_sample(c)
        remove = sample.async_add_listener(entity, loop)
        remove_other = sample.async_add_listener(other_entity, loop)
    inconsistent(c)
    with clock(102):
        house_load_sample(c)
    assert loop.call_later.call_args.args[0] == 8
    with clock(110):
        loop.call_later.call_args.args[1]()
        assert sample.value() is None
    entity.assert_called_once_with()
    other_entity.assert_called_once_with()
    with clock(112):
        c.data.update(power_grid=500, power_battery=0, pv1_power=0)
        house_load_sample(c)
    inconsistent(c)
    with clock(114):
        house_load_sample(c)
    handle = sample._expiry
    remove()
    remove_other()
    handle.cancel.assert_called_once()
    assert sample._expiry is None


def test_partial_poll_timestamps_cannot_renew_an_old_published_sample():
    c = coordinator()
    with clock(100):
        sample = house_load_sample(c)
    # The coordinator has begun its next poll, but hasn't published new data.
    c._last_polled["power_grid"] = 108
    with clock(108):
        assert house_load_sample(c).hold_until == 110
    inconsistent(c)
    with clock(111):
        assert house_load_sample(c).value() is None


def test_fresh_unchanged_completed_polls_refresh_the_valid_baseline():
    c = coordinator()
    with clock(100):
        sample = house_load_sample(c)
    with clock(120):
        sample.update(c, c.data, 120)
        assert house_load_sample(c).hold_until == 130
    inconsistent(c)
    with clock(122):
        assert house_load_sample(c).value() == 500


@pytest.mark.parametrize("kind", ["mode", "soc", "upper_soc"])
def test_peak_safety_pause_still_takes_priority_over_inconsistent_power(kind):
    async def run():
        s = Simulation()
        await s.start()
        inconsistent(s.c)
        s.c.data["inverter_work_mode" if kind == "mode" else "soc_battery"] = 100 if kind == "upper_soc" else 0
        with patch(MODULE + "peak_shaving.time", SimpleNamespace(monotonic=lambda: s.now)):
            await s.sw._update()
        assert s.sw.paused
        assert s.sw.reason == {"mode": "inverter_not_normal", "soc": "discharge_cutoff", "upper_soc": "above_soc_threshold"}[kind]
        s.c.async_reset_dispatch.assert_awaited_once()
    asyncio.run(run())


def test_force_export_soc_stop_is_not_blocked_by_bad_power():
    async def run():
        s = Simulation()
        sw = s.other("force_export")
        sw._is_on = True
        sw._start_force_export_watcher(300)
        inconsistent(s.c)
        s.c.data["soc_battery"] = 0
        sw._automatic_stop = AsyncMock()
        for listener in list(s.listeners):
            listener()
        await asyncio.sleep(0)
        sw._automatic_stop.assert_awaited_once()
        sw.prepare_force_off()
    asyncio.run(run())


@pytest.mark.parametrize("key", ["force_charging", "force_discharging", "dispatch"])
def test_direct_power_modes_preserve_commands_with_inconsistent_house_balance(key):
    async def run():
        s = Simulation()
        sw = s.other(key)
        sw._is_on = True
        s.c.active_dispatch_key = key
        s.c.numbers[key + "_power"] = 2.5
        inconsistent(s.c)
        if key == "force_discharging":
            await sw._start_force_discharging(300)
        else:
            await getattr(sw, "_start_" + key)()
        command = s.c.async_write_dispatch.call_args.args[0]
        assert command[2] == (29500 if key == "force_charging" else 34500)
        s.c.async_reset_dispatch.assert_not_called()
        sw.prepare_force_off()
    asyncio.run(run())

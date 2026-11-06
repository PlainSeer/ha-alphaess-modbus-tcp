"""Replay public issue #10 snapshots and distinguish sustained loads from spikes."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.alphaess_modbus.house_load import HouseLoadSample, POWER_KEYS
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator
from custom_components.alphaess_modbus.const import SENSOR_REGISTERS
from custom_components.alphaess_modbus.sensor import AlphaESSCalculatedSensor, CALCULATED_SENSORS
from custom_components.alphaess_modbus.house_load_daily import AlphaESSHouseLoadDailySensor
from .dispatch_simulation import Simulation
from .test_house_load_samples import coordinator, clock, MODULE


def observe(sample, c, when, *, load=None, meter=0, pv2=0, pv3=0, battery=0, grid=0):
    c.data.update(pv1_power=0, pv2_power=pv2, pv3_power=pv3, pv4_power=0,
                  active_power_pv_meter=meter, power_battery=battery,
                  power_grid=grid if load is None else load, inverter_work_mode=1)
    c._last_polled = {key: when for key in (*POWER_KEYS, "inverter_work_mode")}
    sample.update(c, c.data, when)
    return sample.value(when)


def test_public_full_battery_sequence_holds_only_trusted_readings():
    c, sample = coordinator(), HouseLoadSample()
    rows = [
        (5.733, 896, 126, 383, -1653, 941, 693),
        (7.823, 891, 122, 382, -1936, 982, 441),
        (9.723, 887, 126, 381, -1614, -608, 441),
        (11.824, 883, 127, 376, -1695, -255, 441),
        (13.858, 881, 126, 371, -1696, 820, 502),
        (19.805, 875, 121, 374, -1655, 915, 630),
        (21.733, 875, 123, 371, -1656, -579, 630),
        (23.735, 875, 123, 373, -1575, 900, 696),
    ]
    for when, meter, pv2, pv3, battery, grid, expected in rows:
        assert observe(sample, c, when, meter=meter, pv2=pv2, pv3=pv3,
                       battery=battery, grid=grid) == expected


def test_public_dispatch_end_sequence_never_promotes_the_positive_spikes():
    c, sample = coordinator(), HouseLoadSample()
    rows = [(42.492, 2, 7554, -6663, 893),
            (44.499, 3, 7533, -3545, 893),
            (46.579, 5, 7540, 40, 893),
            (48.489, 5, 7580, 1724, 893),
            (50.536, 5, 7602, 1054, 893),
            (52.569, 4, -1370, -1185, None),
            (56.490, 5, 549, 189, 743),
            (64.567, 5, 627, -74, 558)]
    for when, meter, battery, grid, expected in rows:
        assert observe(sample, c, when, meter=meter, battery=battery, grid=grid) == expected
        if 44 < when < 52:
            assert sample.raw is None
            assert sample.reason == "unconfirmed_step"
    assert sample.raw == 558


@pytest.mark.parametrize("load", [2000, 8000, 20000])
def test_sustained_real_loads_are_confirmed_without_a_wattage_cap(load):
    c, sample = coordinator(), HouseLoadSample()
    assert observe(sample, c, 100, load=500) == 500
    for when in (102, 104, 106, 108):
        assert observe(sample, c, when, load=load) == 500
        assert sample.raw is None
    assert observe(sample, c, 110, load=load) is None
    assert observe(sample, c, 112, load=load) == load
    assert sample.raw == load and sample.reason == "valid"


def test_eight_second_flat_spike_then_negative_cannot_be_held_as_trusted():
    c, sample = coordinator(), HouseLoadSample()
    observe(sample, c, 100, load=500)
    for when in (102, 104, 106, 108):
        assert observe(sample, c, when, load=7500) == 500
    assert observe(sample, c, 110, battery=-2000) is None
    assert sample._last_good == 500
    assert observe(sample, c, 112, load=600) == 600


def test_cached_samples_cannot_confirm_a_candidate_even_after_ten_seconds():
    c, sample = coordinator(), HouseLoadSample()
    observe(sample, c, 100, load=500)
    observe(sample, c, 102, load=3000)
    observe(sample, c, 104, load=3000)
    observe(sample, c, 106, load=3000)
    sample.update(c, c.data, 114)  # no newly polled power registers
    assert sample.raw is None and sample.value(114) is None
    assert observe(sample, c, 116, load=3000) == 3000


def test_missing_read_clears_the_pending_step_and_estimate():
    c, sample = coordinator(), HouseLoadSample()
    observe(sample, c, 100, load=500)
    observe(sample, c, 102, load=5000)
    sample.update(c, c.data, 104, usable=False)
    assert sample.raw is None and sample.value(104) is None
    assert sample._candidate is None and sample.reason == "missing"


def test_ordinary_steps_and_genuine_zero_remain_immediate():
    c, sample = coordinator(), HouseLoadSample()
    for when, load in ((100, 500), (102, 1400), (104, 0), (106, 900)):
        assert observe(sample, c, when, load=load) == load
        assert sample.raw == load


def test_peak_waits_for_confirmation_then_controls_the_sustained_load():
    async def run():
        s = Simulation()
        await s.start()
        old = s.last()
        await s.step(house=20000)
        assert s.last() == old
        assert s.sw.reason == "waiting_for_consistent_power"
        assert not s.sw.paused and not s.sw._suspended
        s.c.async_reset_dispatch.assert_not_called()
        await s.step(house=20000)
        await s.step(house=20000)
        assert s.last()[2] > old[2]
    asyncio.run(run())


def test_completed_coordinator_polls_confirm_unchanged_power_values():
    async def run():
        c = coordinator()
        c._scan_multiplier, c._model_variant = 1, "standard"
        c._is_due = lambda reg: True
        c.client = SimpleNamespace(read_block=AsyncMock(return_value=[3000]))
        c.house_load_sample = HouseLoadSample()
        c.house_load_sample.update(c, c.data, 100)
        grid = next(r for r in SENSOR_REGISTERS if r.key == "power_grid")
        for when in (102, 107, 112):
            with clock(when), patch(MODULE + "coordinator.time", SimpleNamespace(monotonic=lambda: when)), patch(
                MODULE + "coordinator.SENSOR_REGISTERS", [grid]
            ), patch(MODULE + "coordinator._decode_block", return_value=3000):
                c.data = await AlphaESSCoordinator._async_update_data(c)
            assert c.house_load_sample.raw == (3000 if when == 112 else None)
    asyncio.run(run())


def test_daily_energy_does_not_integrate_spikes_or_backfill_confirmation_gap():
    c, sample = coordinator(), HouseLoadSample()
    c.house_load_sample = sample
    entry = SimpleNamespace(entry_id="test")
    source = AlphaESSCalculatedSensor(c, entry, next(d for d in CALCULATED_SENSORS if d.key == "current_house_load"))
    daily = AlphaESSHouseLoadDailySensor(c, entry, source)
    start = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    daily._day, daily._gap = start.date(), False
    for seconds, load in ((0, 500), (2, 8000), (7, 8000), (12, 8000), (14, 8000)):
        with clock(100 + seconds):
            observe(sample, c, 100 + seconds, load=load)
            daily._sample(start + timedelta(seconds=seconds))
    assert daily._energy == pytest.approx((500 * 10 + 8000 * 2) / 3_600_000)
    assert daily.extra_state_attributes["incomplete_day"] is True

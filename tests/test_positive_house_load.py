"""Do not manufacture missing load data by rejecting positive changes."""
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


def test_public_dispatch_end_peaks_are_an_explicit_remaining_limitation():
    c, sample = coordinator(), HouseLoadSample()
    rows = [(42.492, 2, 7554, -6663, 893),
            (44.499, 3, 7533, -3545, 3991),
            (46.579, 5, 7540, 40, 7585),
            (48.489, 5, 7580, 1724, 9309),
            (50.536, 5, 7602, 1054, 8661),
            (52.569, 4, -1370, -1185, 8661),
            (56.490, 5, 549, 189, 743),
            (64.567, 5, 627, -74, 558)]
    for when, meter, battery, grid, expected in rows:
        assert observe(sample, c, when, meter=meter, battery=battery, grid=grid) == expected
        # A positive amplitude test cannot prove that these are false peaks.
        # Keep this replay honest about the remaining positive-spike limitation.
        if when == 52.569:
            assert sample.raw is None
            assert sample.reason == "negative_balance"
        else:
            assert sample.raw == expected
    assert sample.raw == 558


@pytest.mark.parametrize("load", [2000, 8000, 20000])
def test_large_load_steps_are_immediate_without_a_wattage_cap(load):
    c, sample = coordinator(), HouseLoadSample()
    assert observe(sample, c, 100, load=500) == 500
    for when in (102, 104, 106, 108, 110, 112):
        assert observe(sample, c, when, load=load) == load
    assert sample.raw == load and sample.reason == "valid"


def test_eight_second_real_load_burst_is_not_discarded():
    c, sample = coordinator(), HouseLoadSample()
    observe(sample, c, 100, load=500)
    for when in (102, 104, 106, 108):
        assert observe(sample, c, when, load=7500) == 7500
    assert observe(sample, c, 110, load=500) == 500
    assert observe(sample, c, 112, load=600) == 600


def test_ramping_and_cycling_loads_cannot_be_starved_by_a_stability_band():
    c, sample = coordinator(), HouseLoadSample()
    observe(sample, c, 100, load=500)
    for when in range(102, 222, 2):
        load = 2000 + (when - 102) * 150
        assert observe(sample, c, when, load=load) == load
    for when in range(222, 342, 2):
        load = 3000 if when % 4 else 8000
        assert observe(sample, c, when, load=load) == load


def test_missing_read_clears_the_power_estimate_after_a_large_step():
    c, sample = coordinator(), HouseLoadSample()
    observe(sample, c, 100, load=500)
    observe(sample, c, 102, load=5000)
    sample.update(c, c.data, 104, usable=False)
    assert sample.raw is None and sample.value(104) is None
    assert sample.reason == "missing"


def test_ordinary_steps_and_genuine_zero_remain_immediate():
    c, sample = coordinator(), HouseLoadSample()
    for when, load in ((100, 500), (102, 1400), (104, 0), (106, 900)):
        assert observe(sample, c, when, load=load) == load
        assert sample.raw == load


def test_peak_reacts_to_a_large_load_step_without_false_waiting_or_reset():
    async def run():
        s = Simulation()
        await s.start()
        old = s.last()
        await s.step(house=20000)
        assert s.last()[2] > old[2]
        assert s.sw.reason != "waiting_for_consistent_power"
        assert not s.sw.paused and not s.sw._suspended
        s.c.async_reset_dispatch.assert_not_called()
        await s.step(house=20000)
        await s.step(house=20000)
        assert s.last()[2] > old[2]
    asyncio.run(run())


def test_completed_coordinator_polls_accept_large_and_unchanged_power_values():
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
            assert c.house_load_sample.raw == 3000
    asyncio.run(run())


def test_daily_energy_counts_a_real_load_increase_without_a_confirmation_gap():
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
    assert daily._energy == pytest.approx((500 * 2 + 8000 * 12) / 3_600_000)
    assert daily.extra_state_attributes["incomplete_day"] is False
    assert daily.extra_state_attributes["accumulation_paused"] is False

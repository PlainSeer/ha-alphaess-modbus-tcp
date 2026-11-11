"""Comparison sensors are passive, bounded experiments beside unchanged sources."""
import asyncio
import csv
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.alphaess_modbus.const import DOMAIN
from custom_components.alphaess_modbus.house_load import HouseLoadSample, POWER_KEYS
from custom_components.alphaess_modbus.house_load_comparison import (
    AlphaESSHouseLoadComparisonSensor, COMPARISONS, ComparisonFilter,
)
from custom_components.alphaess_modbus.house_load_daily import AlphaESSHouseLoadDailySensor
from custom_components.alphaess_modbus.sensor import (
    AlphaESSCalculatedSensor, CALCULATED_SENSORS, async_setup_entry,
)

MODULE = "custom_components.alphaess_modbus."
METHODS = [d[2] for d in COMPARISONS]


def make(method):
    c = NS(data={}, _last_polled={}, last_update_success=True,
           house_load_sample=HouseLoadSample(), async_add_listener=Mock(),
           async_request_refresh=AsyncMock(), async_write_dispatch=AsyncMock(),
           async_reset_dispatch=AsyncMock(), client=NS(read_block=AsyncMock(),
               write_register=AsyncMock(), write_registers=AsyncMock()))
    definition = next(d for d in COMPARISONS if d[2] == method)
    s = AlphaESSHouseLoadComparisonSensor(c, NS(entry_id="test"), *definition)
    s.hass = NS(loop=Mock())
    s.async_write_ha_state = Mock()
    return c, s


def source(c, stamp, house=500, battery=0, mode=1):
    c.data = dict(inverter_work_mode=mode, power_grid=house if mode == 2 else house - battery,
                  power_battery=battery, pv1_power=0, pv2_power=0,
                  pv3_power=0, pv4_power=0, active_power_pv_meter=0)
    if mode == 2:
        c.data["power_grid"] = house
    c._last_polled = {key: stamp for key in c.data}
    c.house_load_sample.update(c, c.data, stamp)


def sample(s, now):
    with patch(MODULE + "house_load_comparison.time", NS(monotonic=lambda: now)):
        s._sample()
        return s.native_value


def value(s, now):
    with patch(MODULE + "house_load_comparison.time", NS(monotonic=lambda: now)):
        return s.native_value


def no_requests(c):
    c.async_request_refresh.assert_not_called()
    c.async_write_dispatch.assert_not_called()
    c.async_reset_dispatch.assert_not_called()
    c.client.read_block.assert_not_called()
    c.client.write_register.assert_not_called()
    c.client.write_registers.assert_not_called()


def test_setup_registers_distinct_disabled_diagnostic_sensors_without_subscribing():
    c, _ = make("median_5")
    entities = []
    asyncio.run(async_setup_entry(NS(data={DOMAIN: {"test": c}}), NS(entry_id="test"), entities.extend))
    experiments = [e for e in entities if isinstance(e, AlphaESSHouseLoadComparisonSensor)]
    assert len(experiments) == 2
    assert len({e.unique_id for e in entities}) == len(entities)
    for sensor in experiments:
        assert not sensor.entity_registry_enabled_default
        assert sensor.entity_category == EntityCategory.DIAGNOSTIC
        assert sensor.state_class == SensorStateClass.MEASUREMENT
        assert sensor.native_unit_of_measurement == "W"
        assert sensor._expiry is None
    c.async_add_listener.assert_not_called()
    no_requests(c)


def test_median_filters_isolated_spike_but_sustained_load_arrives_after_two_polls():
    c, s = make("median_5")
    readings = [500] * 5 + [12000] + [500] * 5 + [3500] * 3
    for i, watts in enumerate(readings):
        source(c, 100 + i * 2, watts)
        output = sample(s, 100 + i * 2)
        assert output == (3500 if i == len(readings) - 1 else 500)
    assert s._filter.window_samples == 5
    no_requests(c)


@pytest.mark.parametrize("step", [0.5, 2, 4])
def test_median_warmup_and_slow_polling_return_observed_values_with_bounded_history(step):
    c, s = make("median_5")
    seen = []
    for i in range(12):
        stamp = 100 + i * step
        watts = 400 + i * 101
        source(c, stamp, watts)
        seen.append((stamp, watts))
        output = sample(s, stamp)
        assert s._filter.window_samples in (1, 3, 5)
        assert output in [v for t, v in seen if stamp - t < 10]
    assert s._filter.window_samples == (3 if step == 4 else 5)


@pytest.mark.parametrize("method", METHODS)
def test_fresh_unchanged_polls_advance_but_cached_callbacks_and_partial_polls_do_not(method):
    c, s = make(method)
    for stamp in (100, 102, 104, 106, 108):
        source(c, stamp)
        assert sample(s, stamp) == 500
    assert s._filter.expires_at == 118
    old_window = list(s._filter._window)
    for stamp in (109, 110, 111):
        assert sample(s, stamp) == 500
        assert s._filter.expires_at == 118
    c._last_polled["power_grid"] = 112
    assert sample(s, 112) == 500
    assert s._filter.expires_at == 118
    assert list(s._filter._window) == old_window
    assert value(s, 118) is None


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("bad", [None, "bad", float("nan"), float("inf")])
def test_missing_or_nonfinite_input_discards_history_immediately(method, bad):
    c, s = make(method)
    source(c, 100)
    sample(s, 100)
    source(c, 102)
    c.data["power_grid"] = bad
    c.house_load_sample.update(c, c.data, 102)
    assert sample(s, 102) is None
    assert not s._filter._window
    assert s._filter.expires_at is None
    source(c, 104, 5000)
    assert sample(s, 104) == 5000
    no_requests(c)


@pytest.mark.parametrize("method", METHODS)
def test_partial_read_failure_and_stale_mode_clear_history_even_with_cached_values(method):
    c, s = make(method)
    source(c, 100)
    sample(s, 100)
    source(c, 102)
    c.house_load_sample.update(c, c.data, 102, usable=False)
    assert sample(s, 102) is None
    source(c, 104, 5000)
    c._last_polled["inverter_work_mode"] = 40
    assert sample(s, 104) is None
    source(c, 106, 6000)
    assert sample(s, 106) == 6000


@pytest.mark.parametrize("method", METHODS)
def test_negative_source_estimate_cannot_seed_or_extend_a_comparison(method):
    c, s = make(method)
    source(c, 100, -100)
    assert sample(s, 100) is None
    source(c, 102, 500)
    assert sample(s, 102) == 500
    for stamp in (104, 106, 110):
        source(c, stamp, -100)
        assert sample(s, stamp) == 500
        assert s._filter.expires_at == 112
        assert s._filter.reason == "source_negative_balance"
    assert value(s, 112) is None
    source(c, 114, 5000)
    assert sample(s, 114) == 5000


@pytest.mark.parametrize("method", METHODS)
def test_failure_gap_mode_change_and_restart_do_not_carry_old_filter_state(method):
    c, s = make(method)
    source(c, 100)
    sample(s, 100)
    c.last_update_success = False
    assert sample(s, 102) is None
    c.last_update_success = True
    source(c, 104, 3000)
    assert sample(s, 104) == 3000
    source(c, 200, 7000)
    assert sample(s, 200) == 7000
    source(c, 202, 2000, battery=None, mode=2)
    assert sample(s, 202) == 2000  # Legacy EPS source needs only grid power.
    source(c, 204, 0, mode=1)
    assert sample(s, 204) == 0
    restarted = AlphaESSHouseLoadComparisonSensor(c, NS(entry_id="test"),
                                                 *next(d for d in COMPARISONS if d[2] == method))
    restarted.hass = s.hass
    assert sample(restarted, 204) == 0
    assert restarted._filter.window_samples <= 1
    source(c, 90, 8000)
    assert sample(s, 90) == 8000  # Monotonic rollback resets consumed timestamps.


@pytest.mark.parametrize("step", [0.5, 2, 3])
def test_battery_hold_is_at_most_two_fresh_samples_and_four_elapsed_seconds(step):
    c, s = make("battery_step")
    source(c, 100, 500, battery=500)
    sample(s, 100)
    first = 100 + step
    for offset in (0, step):
        stamp = first + offset
        source(c, stamp, 3500, battery=3500)
        assert sample(s, stamp) == 500
        assert s._filter.expires_at == first + 4
    assert value(s, first + 4) is None
    stamp = first + 2 * step
    source(c, stamp, 3500, battery=3500)
    assert sample(s, stamp) == 3500
    no_requests(c)


def test_battery_hold_expiry_is_unknown_until_a_fresh_sample_not_a_cached_acceptance():
    c, s = make("battery_step")
    source(c, 100, 500, battery=500)
    sample(s, 100)
    source(c, 102, 3500, battery=3500)
    sample(s, 102)
    with patch(MODULE + "house_load_comparison.time", NS(monotonic=lambda: 106)):
        s._expire()
    s.async_write_ha_state.assert_called_once()
    assert sample(s, 106) is None
    source(c, 108, 3500, battery=3500)
    assert sample(s, 108) == 3500
    no_requests(c)


@pytest.mark.parametrize("method", METHODS)
def test_genuine_four_second_battery_supplied_load_can_be_removed_a_documented_tradeoff(method):
    c, s = make(method)
    outputs = []
    for i, watts in enumerate([500] * 5 + [3500, 3500, 500, 500]):
        source(c, 100 + i * 2, watts, battery=watts)
        outputs.append(sample(s, 100 + i * 2))
    assert outputs == [500] * 9
    assert c.house_load_sample.raw == 500
    no_requests(c)


def test_battery_filter_preserves_short_grid_supplied_rise_without_battery_step():
    c, s = make("battery_step")
    for i, watts in enumerate([500, 500, 3500, 3500, 500]):
        source(c, 100 + i * 2, watts, battery=0)
        assert sample(s, 100 + i * 2) == watts


@pytest.mark.parametrize("method", METHODS)
def test_timer_only_updates_its_entity_and_remove_cancels_it(method):
    async def scenario():
        c, s = make(method)
        source(c, 100)
        removers = []
        before = (deepcopy(c.data), deepcopy(c._last_polled), c.house_load_sample.signature,
                  c.house_load_sample.raw, c.house_load_sample.hold_until)
        with patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()), \
                patch.object(s, "async_on_remove", side_effect=removers.append), \
                patch(MODULE + "house_load_comparison.time", NS(monotonic=lambda: 100)):
            await s.async_added_to_hass()
        assert s.hass.loop.call_later.call_args.args[0] == 10
        with patch(MODULE + "house_load_comparison.time", NS(monotonic=lambda: 110)):
            s._expire()
        s.async_write_ha_state.assert_called_once()
        assert value(s, 110) is None
        assert before == (c.data, c._last_polled, c.house_load_sample.signature,
                          c.house_load_sample.raw, c.house_load_sample.hold_until)
        source(c, 112)
        sample(s, 112)
        handle = s._expiry
        removers[0]()
        handle.cancel.assert_called_once()
        assert s._expiry is None
        no_requests(c)
    asyncio.run(scenario())


def test_comparisons_leave_original_power_and_daily_energy_identical_across_midnight():
    def run(enabled):
        c, median = make("median_5")
        battery = AlphaESSHouseLoadComparisonSensor(c, NS(entry_id="test"), *COMPARISONS[1])
        battery.hass = median.hass
        entry = NS(entry_id="test")
        raw = AlphaESSCalculatedSensor(c, entry, next(d for d in CALCULATED_SENSORS
                                                     if d.key == "current_house_load"))
        daily = AlphaESSHouseLoadDailySensor(c, entry, raw)
        wall = datetime(2026, 11, 11, 23, 59, 56, tzinfo=timezone.utc)
        daily._day = wall.date()
        results = []
        for i, watts in enumerate([500, 500, 3500, 3500, 500, -500, 500]):
            stamp = 100 + 2 * i
            source(c, stamp, watts, battery=watts)
            if enabled:
                sample(median, stamp)
                sample(battery, stamp)
            clock = NS(monotonic=lambda: stamp)
            with patch(MODULE + "house_load.time", clock), patch(MODULE + "house_load_daily.time", clock):
                daily._sample(wall + timedelta(seconds=2 * i))
                results.append((raw.native_value, deepcopy(raw.extra_state_attributes),
                                daily.native_value, deepcopy(daily.extra_state_attributes)))
        no_requests(c)
        return results
    assert run(False) == run(True)


@pytest.mark.parametrize("method", METHODS)
def test_public_sample_three_reduces_the_reported_peak_without_changing_source(method):
    # Public recorder reconstruction, not a synchronised hardware snapshot:
    # https://github.com/PlainSeer/ha-alphaess-modbus-tcp/issues/10#issuecomment-6806550730
    with (Path(__file__).parent / "fixtures/issue10_sample3.csv").open() as f:
        rows = list(csv.DictReader(f))
    c, s = make(method)
    first = datetime.fromisoformat(rows[0]["time_utc"].replace("Z", "+00:00"))
    for row in rows:
        wall = datetime.fromisoformat(row["time_utc"].replace("Z", "+00:00"))
        stamp = 100 + (wall - first).total_seconds()
        c.data = dict(inverter_work_mode=1, power_grid=float(row["grid"]),
                      power_battery=float(row["battery"]),
                      active_power_pv_meter=float(row["pv_meter"]),
                      **{f"pv{i}_power": float(row[f"pv_string{i}"]) for i in range(1, 5)})
        c._last_polled = {key: stamp for key in c.data}
        c.house_load_sample.update(c, c.data, stamp)
        published = int(row["house_load"])
        assert c.house_load_sample.raw == published
        original = deepcopy(c.data)
        output = sample(s, stamp)
        if published == 12374:
            assert output < 2500
            assert c.house_load_sample.raw == 12374
        assert original == c.data
    no_requests(c)


@pytest.mark.parametrize("method", METHODS)
def test_expired_and_partial_cached_source_cannot_restart_filter_without_all_fresh_inputs(method):
    c, s = make(method)
    source(c, 100)
    sample(s, 100)
    c.house_load_sample.update(c, c.data, 102, usable=False)
    assert sample(s, 102) is None
    c._last_polled["power_grid"] = 104
    c.house_load_sample.update(c, c.data, 104)
    assert sample(s, 104) is None
    source(c, 106, 7000)
    assert sample(s, 106) == 7000

"""Retained daily amounts and live power availability are different contracts."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity, EntityPlatformState
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.alphaess_modbus.house_load import HouseLoadSample
from .test_house_load_daily import make, MODULE
from .test_house_load_samples import clock

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def fixture():
    coordinator, daily = make(NOW)
    coordinator.house_load_sample = HouseLoadSample()
    return coordinator, daily


def tick(coordinator, daily, seconds, *, stale=False, start=NOW):
    stamp = 100 + seconds
    coordinator._last_polled = {key: stamp for key in coordinator.data}
    if stale:
        coordinator._last_polled["power_grid"] = stamp - 61
    with clock(stamp):
        # A real completed coordinator poll updates the shared sample even when
        # its power values are identical to the previous poll.
        coordinator.house_load_sample.update(
            coordinator, coordinator.data, stamp,
            usable=coordinator.last_update_success,
        )
        daily._sample(start + timedelta(seconds=seconds))


@pytest.mark.parametrize("kind", ["missing", "nonfinite", "stale", "failed"])
def test_known_total_stays_numeric_without_accumulating_or_backfilling_an_outage(kind):
    c, daily = fixture()
    tick(c, daily, 0)
    tick(c, daily, 36)
    assert daily.native_value == .01

    if kind == "missing":
        del c.data["power_grid"]
    elif kind == "nonfinite":
        c.data["power_grid"] = float("nan")
    elif kind == "failed":
        c.last_update_success = False
    for seconds in (40, 80, 140):
        tick(c, daily, seconds, stale=kind == "stale")
        assert daily.available and daily.native_value == .01
        assert daily.extra_state_attributes["incomplete_day"]
        assert daily.extra_state_attributes["accumulation_paused"]

    c.last_update_success, c.data["power_grid"] = True, 1000
    tick(c, daily, 150)
    assert daily.native_value == .01  # no energy invented for the missing interval
    assert not daily.extra_state_attributes["accumulation_paused"]
    tick(c, daily, 186)
    assert daily.native_value == .02


def test_negative_balance_estimate_expires_but_the_accumulated_amount_remains():
    c, daily = fixture()
    tick(c, daily, 0)
    tick(c, daily, 36)
    c.data["power_grid"] = -1000
    tick(c, daily, 38)
    with clock(138):
        assert daily._source.native_value is not None  # bounded display estimate
    tick(c, daily, 46)
    with clock(146):
        assert daily._source.native_value is None
    assert daily._energy == pytest.approx(1000 * 46 / 3_600_000)
    assert daily.available and daily.extra_state_attributes["accumulation_paused"]
    frozen = daily._energy
    tick(c, daily, 100)
    assert daily._energy == frozen
    c.data["power_grid"] = 1000
    tick(c, daily, 110)
    assert daily._energy == frozen
    tick(c, daily, 146)
    assert daily._energy == pytest.approx(frozen + .01)


def test_no_restored_amount_or_valid_power_does_not_manufacture_a_zero_total():
    c, daily = fixture()
    c.data["power_grid"] = None
    tick(c, daily, 0)
    assert not daily.available and daily.native_value is None
    assert daily.extra_state_attributes["accumulation_paused"]
    c.data["power_grid"] = 0
    tick(c, daily, 10)
    assert daily.available and daily.native_value == 0  # genuine zero is valid
    assert not daily.extra_state_attributes["accumulation_paused"]


def test_known_total_resets_at_midnight_during_an_outage_without_backfill():
    midnight = datetime(2026, 11, 7, tzinfo=timezone.utc)
    c, daily = fixture()
    start = midnight - timedelta(seconds=72)
    daily._day = start.date()
    tick(c, daily, 0, start=start)
    tick(c, daily, 36, start=start)
    assert daily.native_value == .01
    c.last_update_success = False
    tick(c, daily, 72, start=start)
    assert daily.available and daily.native_value == 0
    assert daily.extra_state_attributes["tracking_date"] == "2026-11-07"
    assert daily.extra_state_attributes["accumulation_paused"]
    assert daily.extra_state_attributes["incomplete_day"]
    c.last_update_success = True
    tick(c, daily, 106, start=start)
    assert daily.native_value == 0
    tick(c, daily, 142, start=start)
    assert daily.native_value == .01


def test_backwards_clock_during_an_outage_cannot_publish_or_recount_a_future_total():
    c, daily = fixture()
    tick(c, daily, 0)
    tick(c, daily, 36)
    c.last_update_success = False
    tick(c, daily, 50)
    tick(c, daily, 40)
    assert not daily.available
    assert daily._energy == .01
    tick(c, daily, 50)
    assert daily.available and daily.native_value == .01
    c.last_update_success = True
    tick(c, daily, 60)
    tick(c, daily, 96)
    assert daily.native_value == .02


def test_same_day_restored_total_is_visible_before_power_recovers():
    c, daily = fixture()
    c.last_update_success = False
    daily.async_get_last_state = AsyncMock(return_value=SimpleNamespace(attributes={
        "tracking_date": daily._day.isoformat(), "accumulated_kwh": 1.23456789,
    }))
    daily.async_on_remove, daily.hass = Mock(), Mock()
    with patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()), patch(
        MODULE + ".async_track_time_change", return_value=Mock()
    ), patch(MODULE + ".dt_util.utcnow", return_value=NOW), clock(100):
        asyncio.run(daily.async_added_to_hass())
    assert daily.available and daily.native_value == 1.2346
    assert daily.extra_state_attributes["accumulation_paused"]
    assert daily.extra_state_attributes["incomplete_day"]
    c.last_update_success = True
    tick(c, daily, 20)
    assert daily._energy == 1.23456789
    tick(c, daily, 56)
    assert daily._energy == pytest.approx(1.24456789)


def test_home_assistant_publishes_a_numeric_counter_when_power_is_unknown(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        c, daily = fixture()
        daily.hass, daily.entity_id = hass, "sensor.test_daily"
        source = daily._source
        source.hass, source.entity_id = hass, "sensor.test_power"
        # HA requires a platform for state publication. Only polling is stubbed;
        # the entity availability, value and HA state serialization remain real.
        source.platform = daily.platform = SimpleNamespace(
            platform_name="alphaess_modbus", domain="sensor", config_entry=None,
            component_translations={}, platform_translations={},
        )
        source._platform_state = daily._platform_state = EntityPlatformState.ADDED
        # make() mocks the state writer for other unit tests. Use HA's real
        # writer here to verify the downstream state/attribute contract.
        daily.async_write_ha_state = Entity.async_write_ha_state.__get__(daily)
        try:
            tick(c, daily, 0)
            tick(c, daily, 36)
            c.data["power_grid"] = -1000
            tick(c, daily, 38)
            tick(c, daily, 46)
            with clock(146):
                source.async_write_ha_state()
                daily.async_write_ha_state()
            power_state = hass.states.get(source.entity_id)
            total_state = hass.states.get(daily.entity_id)
            assert power_state.state == "unknown"
            assert float(total_state.state) == pytest.approx(round(1000 * 46 / 3_600_000, 4))
            assert total_state.attributes["incomplete_day"]
            assert total_state.attributes["accumulation_paused"]
            assert "accumulated_kwh" in total_state.attributes
            frozen = total_state.state
            c.last_update_success = False
            tick(c, daily, 60)
            with clock(160):
                source.async_write_ha_state()
                daily.async_write_ha_state()
            assert hass.states.get(source.entity_id).state == "unavailable"
            assert hass.states.get(daily.entity_id).state == frozen
            assert hass.states.get(daily.entity_id).attributes["accumulation_paused"]
        finally:
            await hass.async_stop()
    asyncio.run(run())

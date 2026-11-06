"""Daily integration boundaries, restore and invalid-source regressions."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from custom_components.alphaess_modbus.house_load_daily import AlphaESSHouseLoadDailySensor
from custom_components.alphaess_modbus.sensor import AlphaESSCalculatedSensor, CALCULATED_SENSORS

MODULE = 'custom_components.alphaess_modbus.house_load_daily'


@pytest.fixture(autouse=True)
def london():
    old = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo('Europe/London'))
    yield
    dt_util.set_default_time_zone(old)


def make(now):
    data = dict(inverter_work_mode=0, power_grid=1000, power_battery=0,
                pv1_power=0, pv2_power=0, pv3_power=0, pv4_power=0, active_power_pv_meter=0)
    c = SimpleNamespace(data=data, last_update_success=True,
                        _last_polled={k: 100 for k in data})
    entry = SimpleNamespace(entry_id='test')
    source = AlphaESSCalculatedSensor(c, entry, next(d for d in CALCULATED_SENSORS if d.key == 'current_house_load'))
    s = AlphaESSHouseLoadDailySensor(c, entry, source)
    s._day = dt_util.as_local(now).date()
    s.async_write_ha_state = Mock()
    return c, s


def sample(s, now):
    with patch(MODULE + '.time.monotonic', return_value=100):
        s._sample(now)


def test_constant_load_accumulates_without_value_changes():
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    c, s = make(now)
    for minute in range(61):
        sample(s, now + timedelta(minutes=minute))
    assert s.native_value == 1
    c.data['power_grid'] = 0
    sample(s, now + timedelta(minutes=61))
    value = s.native_value
    sample(s, now + timedelta(minutes=62))
    assert s.native_value == value
    assert s.state_class == 'total_increasing'


@pytest.mark.parametrize('day', ['2026-03-29', '2026-10-25', '2026-10-10'])
def test_midnight_local_and_dst_days(day):
    midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Europe/London')).astimezone(timezone.utc)
    c, s = make(midnight - timedelta(seconds=10))
    s._energy = 5
    sample(s, midnight - timedelta(seconds=10))
    with patch(MODULE + '.time.monotonic', return_value=100):
        s._midnight(midnight)
    assert s.native_value == 0
    sample(s, midnight + timedelta(seconds=36))
    assert s.native_value == .01
    s.async_write_ha_state.assert_called_once()


def test_delayed_midnight_counts_only_new_day_portion():
    midnight = datetime(2026, 10, 9, 23, tzinfo=timezone.utc)
    _, s = make(midnight - timedelta(seconds=10))
    s._energy = 9
    sample(s, midnight - timedelta(seconds=10))
    sample(s, midnight + timedelta(seconds=36))
    assert s.native_value == .01


@pytest.mark.parametrize('bad', [None, 'bad', float('nan'), float('inf')])
def test_invalid_reading_skips_gap(bad):
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    c, s = make(now)
    sample(s, now)
    c.data['power_grid'] = bad
    sample(s, now + timedelta(seconds=10))
    assert s.native_value is None
    c.data['power_grid'] = 1000
    sample(s, now + timedelta(seconds=40))
    assert s.native_value == 0
    sample(s, now + timedelta(seconds=76))
    assert s.native_value == .01
    assert s.extra_state_attributes['incomplete_day']


def test_stale_failure_negative_and_long_gap():
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    c, s = make(now)
    sample(s, now)
    c._last_polled['power_grid'] = 0
    sample(s, now + timedelta(seconds=10))
    assert not s.available
    c._last_polled['power_grid'] = 100
    c.last_update_success = False
    sample(s, now + timedelta(seconds=20))
    assert not s.available
    c.last_update_success = True
    c.data.update(inverter_work_mode=2, power_grid=-1000)
    sample(s, now + timedelta(seconds=30))
    sample(s, now + timedelta(seconds=60))
    assert s.native_value == 0
    c.data['power_grid'] = 1000
    sample(s, now + timedelta(seconds=90))
    sample(s, now + timedelta(seconds=200))
    assert s.native_value == 0


def test_backwards_clock_does_not_double_count():
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    _, s = make(now)
    sample(s, now)
    sample(s, now + timedelta(seconds=36))
    sample(s, now + timedelta(seconds=10))
    assert not s.available
    sample(s, now + timedelta(seconds=36))
    assert s.native_value == .01


@pytest.mark.parametrize('same_day', [True, False])
def test_restore_precision_without_integrating_downtime(same_day):
    now = dt_util.utcnow()
    _, s = make(now)
    saved_day = s._day if same_day else s._day - timedelta(days=1)
    s.async_get_last_state = AsyncMock(return_value=SimpleNamespace(attributes={
        'tracking_date': saved_day.isoformat(), 'accumulated_kwh': 1.23456789}))
    s.async_on_remove = Mock()
    s.hass = Mock()
    with patch.object(CoordinatorEntity, 'async_added_to_hass', new=AsyncMock()), patch(MODULE + '.async_track_time_change', return_value=Mock()), patch(MODULE + '.time.monotonic', return_value=100):
        asyncio.run(s.async_added_to_hass())
    assert s._energy == (1.23456789 if same_day else 0)
    assert s._last is not None
    assert s.async_on_remove.call_count == 2

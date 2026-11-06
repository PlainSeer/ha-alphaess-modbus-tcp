"""Daily consumption integrated from the existing house-load calculation."""
from datetime import date, timedelta
import time

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .house_energy import finite
from .house_load import house_load_sample


class AlphaESSHouseLoadDailySensor(CoordinatorEntity, RestoreSensor):
    """Accumulate positive observed load; never extrapolate through an outage."""

    _attr_has_entity_name = True
    _attr_name = "House Load Energy Today (Accumulated)"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, entry, source):
        super().__init__(coordinator)
        self._source = source
        self._attr_unique_id = f"{entry.entry_id}_house_load_energy_today"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        self._day = dt_util.now().date()
        self._energy = 0.0
        self._last = None
        self._last_hold_until = None
        self._ready = False
        self._gap = True  # First day begins when enabled, not retrospectively.

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last:
            attrs = last.attributes
            value = finite(attrs.get("accumulated_kwh"))
            try:
                saved_day = date.fromisoformat(attrs.get("tracking_date", ""))
            except (TypeError, ValueError):
                saved_day = None
            if saved_day == self._day and value is not None and value >= 0:
                self._energy = value
                self._gap = True  # Never invent consumption during downtime.
        self.async_on_remove(async_track_time_change(
            self.hass, self._midnight, hour=0, minute=0, second=0
        ))
        self._sample()
        self.async_on_remove(house_load_sample(self.coordinator).async_add_listener(
            self._handle_coordinator_update, self.hass.loop
        ))

    def _power(self):
        if not self.coordinator.last_update_success:
            return None
        data = self.coordinator.data or {}
        mode = finite(data.get("inverter_work_mode"))
        if mode is None:
            return None
        keys = ["power_grid"]
        if mode != 2:
            keys += ["power_battery", "pv1_power", "pv2_power", "pv3_power",
                     "pv4_power", "active_power_pv_meter"]
        # Cached readings must not keep accumulating after polling stops.
        now = time.monotonic()
        for key in keys + ["inverter_work_mode"]:
            stamp = self.coordinator._last_polled.get(key)
            if (finite(data.get(key)) is None or stamp is None
                    or not 0 <= now - stamp <= 60):
                return None
        try:
            value = finite(self._source.native_value)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, value) if value is not None else None

    def _sample(self, now=None):
        now = now or dt_util.utcnow()
        today = dt_util.as_local(now).date()
        if today < self._day:
            # A backwards clock adjustment must not reset or double-count a day.
            self._ready = False
            self._gap = True
            return
        if today > self._day:
            self._day = today
            self._energy = 0.0
            self._gap = False
        power = self._power()
        self._ready = power is not None
        sample = house_load_sample(self.coordinator)
        hold_until = None
        if sample.inconsistent and sample.hold_until is not None:
            hold_until = now + timedelta(seconds=sample.hold_until - time.monotonic())
        if power is None and hold_until is None:
            self._last = None
            self._last_hold_until = None
            self._gap = True
            return
        if self._last is not None:
            previous_time, previous_power = self._last
            elapsed = (now - previous_time).total_seconds()
            if elapsed < 0:
                self._ready = False
                self._gap = True
                return
            if 0 < elapsed <= 60:
                midnight = dt_util.as_utc(dt_util.start_of_local_day(dt_util.as_local(now)))
                start = max(previous_time, midnight)
                end = now
                for expiry in (self._last_hold_until, hold_until):
                    if expiry is not None:
                        end = min(end, expiry)
                self._energy += previous_power * max(0, (end - start).total_seconds()) / 3_600_000
                if end < now:
                    self._gap = True
            elif elapsed != 0:
                self._gap = True
        self._last = (now, power) if power is not None else None
        self._last_hold_until = hold_until if power is not None else None
        if power is None:
            self._gap = True

    def _handle_coordinator_update(self):
        self._sample()
        super()._handle_coordinator_update()

    @callback
    def _midnight(self, now):
        self._sample(dt_util.as_utc(now))
        self.async_write_ha_state()

    @property
    def available(self):
        return super().available and self._ready

    @property
    def native_value(self):
        return round(self._energy, 4) if self.available else None

    @property
    def extra_state_attributes(self):
        return {"tracking_date": self._day.isoformat(),
                "accumulated_kwh": self._energy, "incomplete_day": self._gap,
                "method": "left", "source": "current_house_load"}

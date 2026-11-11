"""Opt-in house-load experiments; no reads, writes or control consumers."""
from collections import deque
import statistics
import time

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .house_energy import finite
from .house_load import POWER_KEYS

MAX_AGE = 10.0
HOLD_SECONDS = 4.0
STEP_W = 2000

COMPARISONS = (
    ("house_load_median_5_comparison", "House Load Median 5 (Comparison)", "median_5"),
    ("house_load_battery_step_comparison", "House Load Battery Step (Comparison)", "battery_step"),
)


class ComparisonFilter:
    """Causal experiments with bounded history and no persisted state."""

    def __init__(self, method):
        self.method = method
        self.clear("waiting_for_sample")

    def clear(self, reason):
        self._window = deque(maxlen=5)
        self._accepted = self._accepted_at = self._previous_battery = None
        self._last_stamp = self._mode = self._value = None
        self._hold_until = None
        self._held = self.window_samples = 0
        self.expires_at = None
        self.reason = reason

    def value(self, now):
        if (self.expires_at is not None and self._last_stamp is not None
                and self._last_stamp <= now < self.expires_at):
            return self._value
        return None

    def expire(self, now):
        if self.expires_at is not None and now >= self.expires_at:
            self.clear("hold_expired" if self._hold_until is not None else "stale")

    def estimate(self, until, now):
        # Never seed a filter from an estimate or renew its original deadline.
        if self.value(now) is None or until is None or until <= now:
            self.clear("source_unavailable")
            return
        self.expires_at = min(self.expires_at, until)
        self.reason = "source_negative_balance"

    def feed(self, value, battery, stamp, mode, now):
        self.expire(now)
        if (mode != self._mode or (self._last_stamp is not None
                and (stamp <= self._last_stamp or stamp - self._last_stamp > MAX_AGE))):
            self.clear("reset")
        self._mode = mode
        if self.method == "median_5":
            self._window.append((stamp, value))
            while self._window and stamp - self._window[0][0] >= MAX_AGE:
                self._window.popleft()
            # During warm-up/slow polling use the newest odd-sized window, so
            # even sample counts never produce an invented midpoint value.
            count = len(self._window)
            self.window_samples = count if count % 2 else count - 1
            self._value = statistics.median([v for _, v in self._window][-self.window_samples:])
            self.reason = "median"
            self.expires_at = stamp + MAX_AGE
        else:
            step = (battery is not None and self._previous_battery is not None
                    and abs(battery - self._previous_battery) > STEP_W)
            jump = self._accepted is not None and abs(value - self._accepted) > STEP_W
            pending = self._hold_until is not None and stamp < self._hold_until
            if mode != 2 and jump and (step or pending) and self._held < 2:
                if not pending:
                    self._hold_until = min(stamp + HOLD_SECONDS, self._accepted_at + MAX_AGE)
                self._value = self._accepted
                self._held += 1
                self.expires_at = self._hold_until
                self.reason = "battery_step_hold"
            else:
                self._accepted, self._accepted_at = value, stamp
                self._value = value
                self._hold_until = None
                self._held = 0
                self.expires_at = stamp + MAX_AGE
                self.reason = "unfiltered"
            self._previous_battery = battery
        self._last_stamp = stamp


class AlphaESSHouseLoadComparisonSensor(CoordinatorEntity, SensorEntity):
    """Observe completed polls only; an expiry updates this entity alone."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry, key, name, method):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        self._filter = ComparisonFilter(method)
        self._last_stamps = None
        self._last_clock = None
        self._mode = None
        self._expiry = None

    def _sample(self):
        now = time.monotonic()
        if self._last_clock is not None and now < self._last_clock:
            self._last_stamps = None
            self._filter.clear("clock_changed")
        self._last_clock = now
        self._filter.expire(now)
        c = self.coordinator
        data = c.data or {}
        sample = getattr(c, "house_load_sample", None)
        mode = finite(data.get("inverter_work_mode"))
        if mode != self._mode:
            self._filter.clear("mode_changed")
            self._mode = mode
        polled = getattr(c, "_last_polled", {})
        keys = ("power_grid",) if mode == 2 else POWER_KEYS
        stamps = {key: finite(polled.get(key)) for key in keys}
        mode_stamp = finite(polled.get("inverter_work_mode"))
        if (not c.last_update_success or sample is None or sample.reason == "missing"
                or mode is None or mode_stamp is None or not 0 <= now - mode_stamp < 60
                or any(stamp is None or not 0 <= now - stamp < MAX_AGE
                       or finite(data.get(key)) is None for key, stamp in stamps.items())):
            self._filter.clear("source_unavailable")
        elif sample.raw is None:
            self._filter.estimate(sample.hold_until, now)
        elif (self._last_stamps is None or all(stamp > self._last_stamps.get(key, -1)
                                             for key, stamp in stamps.items())):
            # Cached/partial polls and attribute-only callbacks cannot fill a
            # median window, confirm a step or renew its estimate.
            self._last_stamps = stamps
            self._filter.feed(sample.raw, finite(data.get("power_battery")),
                              min(stamps.values()), mode, now)
        self._schedule_expiry(now)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_expiry)
        self._sample()

    def _handle_coordinator_update(self):
        self._sample()
        super()._handle_coordinator_update()

    def _cancel_expiry(self):
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None

    def _schedule_expiry(self, now):
        self._cancel_expiry()
        if self._filter.value(now) is not None:
            self._expiry = self.hass.loop.call_later(self._filter.expires_at - now, self._expire)

    def _expire(self):
        self._expiry = None
        now = time.monotonic()
        self._filter.expire(now)
        self._schedule_expiry(now)
        self.async_write_ha_state()

    @property
    def native_value(self):
        if not self.coordinator.last_update_success:
            return None
        return self._filter.value(time.monotonic())

    @property
    def extra_state_attributes(self):
        value = self.native_value
        return {"experimental": True, "comparison_only": True,
                "source": "current_house_load", "method": self._filter.method,
                "estimated": value is not None, "sample_reason": self._filter.reason,
                "window_samples": self._filter.window_samples,
                "max_sample_age_seconds": MAX_AGE,
                "max_step_hold_seconds": HOLD_SECONDS, "max_step_hold_samples": 2}

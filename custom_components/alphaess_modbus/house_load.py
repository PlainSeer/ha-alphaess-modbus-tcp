"""Shared power balance and bounded handling of inconsistent house-load samples."""
import time

from .house_energy import finite

HOUSE_LOAD_HOLD_SECONDS = 10.0
PV_KEYS = ("pv1_power", "pv2_power", "pv3_power", "pv4_power", "active_power_pv_meter")
POWER_KEYS = (*PV_KEYS, "power_battery", "power_grid")
SAMPLE_KEYS = (*POWER_KEYS, "inverter_work_mode")


def pv_production(data):
    values = [finite(data.get(key)) for key in PV_KEYS]
    if any(value is None for value in values):
        return None
    return max(0, sum(int(value) for value in values))


def house_load_balance(data):
    """Return the signed balance; EPS retains its existing grid-only calculation."""
    grid = finite(data.get("power_grid"))
    mode = finite(data.get("inverter_work_mode", 0))
    if grid is None or mode is None:
        return None
    if mode == 2:
        return round(grid)
    pv, battery = pv_production(data), finite(data.get("power_battery"))
    if pv is None or battery is None:
        return None
    return pv + int(battery) + int(grid)


def house_load(data):
    balance = house_load_balance(data)
    if balance is None or (finite(data.get("inverter_work_mode", 0)) != 2 and balance < 0):
        return None
    return balance


def _signature(data):
    # Poll timestamps change while a block read is still in flight. Readers must
    # not reclassify the previous published data using those partial timestamps.
    return tuple(data.get(key) for key in SAMPLE_KEYS)


class HouseLoadSample:
    """One shared result per completed poll, with estimates excluded from control."""

    def __init__(self):
        self.signature = None
        self.raw = None
        self.inconsistent = False
        self.hold_until = None
        self._last_good = None
        self._mode = None
        self._listeners = set()
        self._loop = None
        self._expiry = None

    def update(self, coordinator, data, now, *, usable=True):
        self.signature = _signature(data)
        mode = finite(data.get("inverter_work_mode", 0))
        if mode != self._mode:
            self._last_good = self.hold_until = None
        self._mode = mode
        balance = house_load_balance(data) if usable else None
        self.inconsistent = balance is not None and mode != 2 and balance < 0
        self.raw = None if self.inconsistent else balance
        if balance is None:
            # Failed/missing reads are outages, not permission to estimate.
            self._last_good = self.hold_until = None
        elif not self.inconsistent:
            self._last_good = balance
            self.hold_until = now + HOUSE_LOAD_HOLD_SECONDS
        self._schedule_expiry(now)

    def async_add_listener(self, callback, loop):
        """Notify only dependent entities on expiry, never controller listeners."""
        self._listeners.add(callback)
        self._loop = loop
        self._schedule_expiry(time.monotonic())

        def remove():
            self._listeners.discard(callback)
            if not self._listeners and self._expiry is not None:
                self._expiry.cancel()
                self._expiry = None
        return remove

    def _schedule_expiry(self, now):
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        if (self._listeners and self.inconsistent and self.hold_until is not None
                and now < self.hold_until):
            self._expiry = self._loop.call_later(self.hold_until - now, self._expire)

    def _expire(self):
        self._expiry = None
        for listener in tuple(self._listeners):
            listener()

    def value(self, now=None):
        if self.raw is not None:
            return self.raw
        now = time.monotonic() if now is None else now
        if (self.inconsistent and self.hold_until is not None
                and now < self.hold_until):
            return self._last_good
        return None

    @property
    def status(self):
        if self.raw is not None:
            return "valid"
        if self.inconsistent:
            return "estimated" if self.value() is not None else "inconsistent"
        return "missing"


def house_load_sample(coordinator):
    """Read the coordinator's sample; also supports manually supplied coordinator data."""
    sample = getattr(coordinator, "house_load_sample", None)
    if sample is None:
        sample = coordinator.house_load_sample = HouseLoadSample()
    data = coordinator.data or {}
    if not coordinator.last_update_success:
        sample.update(coordinator, data, time.monotonic(), usable=False)
        sample.signature = None
    elif sample.signature != _signature(data):
        sample.update(coordinator, data, time.monotonic())
    return sample

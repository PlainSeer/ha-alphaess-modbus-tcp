"""Optional Hillview-inspired peak shaving through the existing dispatch API.

No register definitions, polling, or direct Modbus calls live here. The controller
only runs after explicit enable, and never restores an active session on startup.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DISPATCH_FLOW_DIRECTION, DISPATCH_MODE_SOC_CONTROL, DISPATCH_PV_UNCHANGED,
    DISPATCH_SOC_SCALE, DOMAIN, ModbusNumberDef, SENSOR_REGISTERS,
)
from .house_energy import finite
from .switch import AlphaESSSwitch, _MUTEX_SWITCHES, _calc_house_load, _calc_pv_production

_LOGGER = logging.getLogger(__name__)

# Local HA settings, deliberately outside NUMBER_REGISTERS. No register backing.
PEAK_NUMBERS = (
    ModbusNumberDef("peak_shaving_power", "Peak Shaving Grid Import Limit", None,
                    min_value=0, max_value=20, step=0.1, unit="kW",
                    icon="mdi:transmission-tower-import", ac_limit_scaled=True,
                    default_value=5),
    ModbusNumberDef("peak_shaving_cutoff_soc", "Peak Shaving Below SoC", None,
                    min_value=4, max_value=100, unit="%", icon="mdi:battery",
                    default_value=100),
)
_REQUIRED = ("power_grid", "power_battery", "pv1_power", "pv2_power", "pv3_power",
             "pv4_power", "active_power_pv_meter", "inverter_work_mode",
             "soc_battery", "discharging_cutoff_soc")
_INTERVALS = {r.key: r.scan_interval for r in SENSOR_REGISTERS if r.key in _REQUIRED}
MIN_WRITE_SECONDS = 5
RENEW_SECONDS = 240
LEASE_SECONDS = 300
DEADBAND_W = 50
FILTER_GAIN = 0.05  # conservative smoothing for asynchronous meter/battery samples
PAUSE_SECONDS = 15
NORMAL_SECONDS = 600


class AlphaESSPeakShavingSwitch(AlphaESSSwitch):
    """One owner, one in-flight update, and no worker while disabled."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, {
            "key": "peak_shaving", "name": "Peak Shaving", "icon": "mdi:chart-bell-curve",
        })
        self._lock = asyncio.Lock()
        self._worker = None
        self._heartbeat_cancel = None
        self._listener_cancel = None
        self._command = None
        self._last_write = None
        self._normal_since = None
        self._paused_since = None
        self._resumed_at = None
        self._previous = {}
        self._filtered = {}
        self.paused = False
        self.reason = "off"

    @property
    def extra_state_attributes(self):
        return {**super().extra_state_attributes, "paused": self.paused, "reason": self.reason,
                "requested_battery_power_w": self._command[0] if self._command else None}

    @property
    def stopping(self):
        return not self._is_on and self._owns_dispatch()

    def _owns_dispatch(self):
        return self._coordinator.active_dispatch_key == self.switch_key

    def _stop_callbacks(self):
        for attr in ("_listener_cancel", "_heartbeat_cancel"):
            cancel = getattr(self, attr)
            if cancel:
                cancel()
                setattr(self, attr, None)

    def _queue_update(self, *_):
        if not self._is_on or not self._owns_dispatch():
            return
        if self._worker is None or self._worker.done():
            self._worker = self.hass.async_create_task(self._update(), name="alphaess_peak_shaving")

    async def _turn_on(self, **kwargs):
        async with self._lock:
            if self._is_on:
                return
            switches = self.hass.data[DOMAIN].get(f"{self._entry.entry_id}_switches", {})
            active = self._coordinator.active_dispatch_key
            if (active is not None and active != self.switch_key) or any(
                key != self.switch_key and key in _MUTEX_SWITCHES and sw.is_on
                for key, sw in switches.items()
            ):
                raise HomeAssistantError("Stop the active dispatch mode before enabling Peak Shaving")
            if any(not task.done() for sw in switches.values() if sw is not self
                   for task in sw._pending_tasks):
                raise HomeAssistantError("Wait for the previous dispatch command to finish")
            # Also avoid silently taking over an inverter dispatch not owned by HA.
            dispatch = finite((self._coordinator.data or {}).get("dispatch_start"))
            stamp = finite(self._coordinator._last_polled.get("dispatch_start"))
            if dispatch != 0 or stamp is None or not 0 <= time.monotonic() - stamp <= 30:
                raise HomeAssistantError("Reset the existing inverter dispatch or wait for a fresh inactive dispatch reading before enabling Peak Shaving")
            if getattr(self._coordinator, "_unloading", False):
                raise HomeAssistantError("Integration is unloading")
            self._control_generation += 1
            self._suspended = False
            self._acknowledged = self._uncertain = self._requested = None
            self._is_on = True
            self._coordinator.active_dispatch_key = self.switch_key
            self._command = None
            self._last_write = None
            self._normal_since = None
            self._paused_since = None
            self._resumed_at = None
            self._previous.clear()
            self._filtered.clear()
            self.paused = False
            self.reason = "starting"
            self._listener_cancel = self._coordinator.async_add_listener(self._queue_update)
            self._heartbeat_cancel = async_track_time_interval(
                self.hass, self._queue_update, timedelta(seconds=1)
            )
        await self._update()

    async def async_apply_param_change(self, *, reset_timer=False):
        await self._update()

    async def _stop(self, *, reset):
        # Disable first: queued updates must recheck after acquiring the lock.
        self._is_on = False
        self._stop_callbacks()
        async with self._lock:
            try:
                if self._owns_dispatch():
                    try:
                        if reset and self._command is not None:
                            await self._coordinator.async_reset_dispatch()
                    finally:
                        self._coordinator.active_dispatch_key = None
            finally:
                self._command = None
                self.paused = False
                self.reason = "off"
                self.async_write_ha_state()

    async def _async_turn_off_silent(self):
        await self._stop(reset=True)

    def prepare_force_off(self):
        self._control_generation += 1
        self._is_on = False
        self._stop_callbacks()

    async def async_force_off(self):
        await self._stop(reset=False)

    async def async_force_on(self):
        # State synchronisation must never activate an unattended control loop.
        return

    async def async_will_remove_from_hass(self):
        # A renewable lease expires naturally; unloading must not reset a
        # command that another controller may already have replaced.
        await self._stop(reset=False)
        if self._worker and not self._worker.done():
            await self._worker
        await super().async_will_remove_from_hass()

    def _inputs(self, now):
        c = self._coordinator
        d = c.data or {}
        if not c.last_update_success:
            return None
        multiplier = max(1, c._scan_multiplier)
        for key in _REQUIRED:
            value = finite(d.get(key))
            stamp = finite(c._last_polled.get(key))
            if value is None or stamp is None or not 0 <= now - stamp <= max(15, 3 * _INTERVALS[key] * multiplier):
                return None
        soc, lower = float(d["soc_battery"]), float(d["discharging_cutoff_soc"])
        limit = finite(c.get_number("peak_shaving_power"))
        upper = finite(c.get_number("peak_shaving_cutoff_soc"))
        ac_limit = finite(c.ac_limit_w)
        if (limit is None or upper is None or ac_limit is None or ac_limit <= 0
                or not 0 <= limit <= ac_limit / 1000
                or not 0 <= lower < upper <= 100 or not 0 <= soc <= 100):
            return None
        # Use the existing raw calculations. Advance the private lowpass on fresh
        # power samples, even at constant power, so control converges after a step.
        # Repeated timer callbacks on cached samples must not advance the filter.
        pv, house = _calc_pv_production(d), _calc_house_load(d)
        stamps = tuple(c._last_polled[key] for key in _REQUIRED[:7])
        for key, value in (("pv", pv), ("house", house)):
            if self._previous.get(key) != stamps:
                self._filtered[key] = round((1 - FILTER_GAIN) * self._filtered.get(key, value) + FILTER_GAIN * value, 2)
                self._previous[key] = stamps
        return d, soc, lower, upper, limit * 1000, ac_limit, pv

    async def _pause(self, reason, now):
        if not self.paused:
            self._paused_since = now
        self.paused = True
        self.reason = reason
        if self._command is not None:
            await self._pause_dispatch()
            self._command = None
            self._last_write = now

    async def _update(self):
        async with self._lock:
            if not self._is_on or not self._owns_dispatch() or self._suspended:
                return
            try:
                await self._evaluate(time.monotonic())
            except Exception:
                # A failed write may have reached the inverter. Stop local
                # control without issuing an unexplained recovery reset.
                self._is_on = False
                self._stop_callbacks()
                self.reason = "write_failed"
                self.paused = False
                self._coordinator.active_dispatch_key = None
                self._command = None
                _LOGGER.exception("Peak Shaving stopped after command failure")
            self.async_write_ha_state()

    async def _evaluate(self, now):
        inputs = self._inputs(now)
        if inputs is None:
            self._normal_since = None
            self._previous.clear()
            self._filtered.clear()
            await self._pause("invalid_or_stale_data", now)
            return
        d, soc, lower, upper, limit, ac_limit, raw_pv = inputs
        if float(d["inverter_work_mode"]) != 1:
            self._normal_since = None
            await self._pause("inverter_not_normal", now)
            return
        if self._normal_since is None:
            self._normal_since = now
        if soc <= lower:
            await self._pause("discharge_cutoff", now)
            return
        house, pv = self._filtered["house"], self._filtered["pv"]
        if self.paused:
            if (now - self._paused_since < PAUSE_SECONDS
                    or now - self._normal_since < NORMAL_SECONDS
                    or raw_pv > house or soc >= upper):
                return
            self.paused = False
            self._resumed_at = now
        elif (pv > house or soc >= upper) and (
            self._resumed_at is None or now - self._resumed_at >= PAUSE_SECONDS
        ):
            await self._pause("solar_surplus" if pv > house else "above_soc_threshold", now)
            return
        requested = min(max(0, int(house) - int(pv) - int(limit)), int(ac_limit), 32767)
        cutoff = int(lower / DISPATCH_SOC_SCALE) if requested > 0 else 255
        command = (requested, cutoff)
        elapsed = now - self._last_write if self._last_write is not None else float("inf")
        changed = (self._command is None or abs(requested - self._command[0]) >= DEADBAND_W
                   or cutoff != self._command[1])
        if elapsed >= MIN_WRITE_SECONDS and (changed or elapsed >= RENEW_SECONDS):
            await self._write_dispatch([
                1, 0, 32000 + requested, 0, 32000, DISPATCH_MODE_SOC_CONTROL,
                cutoff, 0, LEASE_SECONDS, DISPATCH_FLOW_DIRECTION, DISPATCH_PV_UNCHANGED,
            ])
            self._command = command
            self._last_write = now
        self.reason = "limiting_import" if requested else "below_import_limit"

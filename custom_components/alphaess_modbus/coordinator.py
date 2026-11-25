from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from pymodbus.exceptions import ModbusException

from .const import (
    DOMAIN,
    SENSOR_REGISTERS,
    INVERTER_AC_LIMIT,
    INVERTER_AC_LIMIT_OPTIONS,
    ModbusSensorDef,
    B3_SCALE_OVERRIDES,
    DISPATCH_FLOW_DIRECTION,
    DISPATCH_PV_UNCHANGED,
)
from .modbus_client import AlphaESSModbusClient, ModbusIllegalAddress
from .house_load import HouseLoadSample, house_load_input_keys

_LOGGER = logging.getLogger(__name__)
_UNSUPPORTED_AFTER = 3
_UNSUPPORTED_RETRY_S = 600.0

# Per-register scan intervals — used for stale-entry expiry.
_SCAN_INTERVAL_MAP: dict[str, float] = {r.key: r.scan_interval for r in SENSOR_REGISTERS}


def _reg_width(reg: ModbusSensorDef) -> int:
    return 2 if reg.data_type in ("int32", "uint32") else 1


def _group_registers(
    regs: list[ModbusSensorDef], split_before: set[int] | None = None,
) -> list[tuple[int, int, list[ModbusSensorDef]]]:
    """Group numeric (non-string) registers into (start, count, members) block tuples.

    Adjacent registers (gap <= 4) are merged so one read_block call covers the span.
    Each group is capped at 100 registers to stay within inverter limits.
    """
    if not regs:
        return []
    sorted_regs = sorted(regs, key=lambda r: r.address)
    groups: list[tuple[int, int, list[ModbusSensorDef]]] = []
    g_start = sorted_regs[0].address
    g_end = g_start + _reg_width(sorted_regs[0]) - 1
    g_regs: list[ModbusSensorDef] = [sorted_regs[0]]

    cuts = sorted(split_before or ())
    for reg in sorted_regs[1:]:
        r_end = reg.address + _reg_width(reg) - 1
        # Merge if gap <= 4 registers and total span stays within 100
        crosses_cut = any(g_start < cut <= reg.address for cut in cuts)
        if not crosses_cut and reg.address <= g_end + 5 and r_end - g_start + 1 <= 100:
            g_regs.append(reg)
            g_end = max(g_end, r_end)
        else:
            groups.append((g_start, g_end - g_start + 1, g_regs))
            g_start = reg.address
            g_end = r_end
            g_regs = [reg]

    groups.append((g_start, g_end - g_start + 1, g_regs))
    return groups


def _decode_block(
    reg: ModbusSensorDef, raw: list[int], offset: int, *, scale: float | None = None,
) -> Any:
    """Decode a register value from a raw uint16 word slice (block read result)."""
    dt = reg.data_type
    if dt == "int16":
        v: int = raw[offset]
        if v > 32767:
            v -= 65536
    elif dt == "uint16":
        v = raw[offset]
    elif dt == "int32":
        combined = (raw[offset] << 16) | raw[offset + 1]
        if combined > 2147483647:
            combined -= 4294967296
        v = combined
    elif dt == "uint32":
        v = (raw[offset] << 16) | raw[offset + 1]
    else:
        raise ValueError(f"Block decode: unsupported type {dt!r}")
    value = (v + reg.offset) * (reg.scale if scale is None else scale)
    if reg.precision is not None:
        value = round(value, reg.precision)
        if reg.precision == 0:
            value = int(value)
    return value


class AlphaESSCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, client: AlphaESSModbusClient) -> None:
        options = config_entry.options
        poll_mode = options.get("poll_mode", "normal")
        if poll_mode == "slow":
            self._scan_multiplier: float = float(options.get("slow_multiplier", 3.0))
            loop_interval = 2
        elif poll_mode == "fast":
            self._scan_multiplier = float(options.get("fast_multiplier", 0.5))
            loop_interval = self._scan_multiplier
        else:
            self._scan_multiplier = 1.0
            loop_interval = 2
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=loop_interval),
        )
        self._model_variant: str = options.get("model_variant", "standard")
        self._entry_id = config_entry.entry_id
        self.client = client
        self._last_polled: dict[str, float] = {}
        self._block_splits: set[int] = set()
        self._illegal_replies: dict[str, int] = {}
        self._unsupported_until: dict[str, float] = {}
        self.optional_poll_keys: set[str] = set()
        self._optional_last_attempt: dict[str, float] = {}
        self.house_load_sample = HouseLoadSample()
        # Caches for number/select entity values — populated by those entities
        # so switch.py can read them without going through hass.states.
        self.numbers: dict[str, float] = {}
        self.selects: dict[str, str] = {}
        saved_ac_limit = config_entry.data.get(INVERTER_AC_LIMIT)
        if saved_ac_limit not in INVERTER_AC_LIMIT_OPTIONS:
            saved_ac_limit = next(iter(INVERTER_AC_LIMIT_OPTIONS))
        self.set_ac_limit(saved_ac_limit)
        self.ee_paused: bool = False
        self.excess_export_deadline: float | None = None
        self.fi_paused: bool = False
        self.dispatch_started_at: datetime | None = None
        self.dispatch_duration_s: int = 0
        self.dispatch_deadline: float | None = None
        self.active_dispatch_key: str | None = None
        self._unloading = False

    def apply_poll_options(self, options) -> None:
        mode = options.get("poll_mode", "normal")
        self._scan_multiplier = float(options.get(
            "slow_multiplier" if mode == "slow" else "fast_multiplier", 3.0 if mode == "slow" else 0.5
        )) if mode != "normal" else 1.0
        self.update_interval = timedelta(seconds=self._scan_multiplier if mode == "fast" else 2)

    def set_ac_limit(self, option: str) -> None:
        self.selects[INVERTER_AC_LIMIT] = option
        self.ac_limit_w = INVERTER_AC_LIMIT_OPTIONS[option]

    def get_number(self, key: str) -> float | None:
        return self.numbers.get(key)

    def get_select(self, key: str) -> str | None:
        return self.selects.get(key)

    def _is_due(self, reg: ModbusSensorDef) -> bool:
        if reg.optional_read:
            if reg.key not in getattr(self, "optional_poll_keys", set()):
                return False
            last = self._optional_last_attempt.get(reg.key, 0.0)
        else:
            retry_at = self._unsupported_until.get(reg.key)
            if retry_at is not None:
                return time.monotonic() >= retry_at
            last = self._last_polled.get(reg.key, 0.0)
        interval = reg.scan_interval * self._scan_multiplier
        return (time.monotonic() - last) >= interval

    def _note_illegal_address(self, reg: ModbusSensorDef, now: float) -> None:
        count = self._illegal_replies.get(reg.key, 0) + 1
        self._illegal_replies[reg.key] = count
        if count >= _UNSUPPORTED_AFTER:
            self._unsupported_until[reg.key] = now + _UNSUPPORTED_RETRY_S
            if count == _UNSUPPORTED_AFTER:
                _LOGGER.debug("Register %s does not answer on this model; retrying in 10 minutes", reg.key)

    def _mark_polled(self, reg: ModbusSensorDef, now: float) -> None:
        self._last_polled[reg.key] = now
        self._illegal_replies.pop(reg.key, None)
        self._unsupported_until.pop(reg.key, None)

    async def _async_update_data(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(self.data or {})
        now = time.monotonic()

        due = [r for r in SENSOR_REGISTERS if self._is_due(r)]
        if not due:
            return data

        string_regs = [r for r in due if r.data_type == "string"]
        numeric_regs = [r for r in due if r.data_type != "string" and not r.optional_read]
        optional_regs = [r for r in due if r.optional_read]
        errors: list[str] = []
        errors_this_cycle = 0
        failed_keys: set[str] = set()

        def record_error(reg: ModbusSensorDef, err: Exception) -> None:
            failed_keys.add(reg.key)
            errors.append(f"{reg.key}: {err}")

        def record_illegal(reg: ModbusSensorDef, err: ModbusIllegalAddress) -> None:
            record_error(reg, err)
            data.pop(reg.key, None)
            self._note_illegal_address(reg, now)

        async def read_group(start: int, count: int, members: list[ModbusSensorDef]) -> None:
            nonlocal errors_this_cycle
            if errors_this_cycle > 5:
                raise UpdateFailed(
                    f"Too many read errors this cycle ({errors_this_cycle}), aborting"
                )
            try:
                raw = await self.client.read_block(start, count)
            except ModbusIllegalAddress as err:
                if len(members) == 1:
                    record_illegal(members[0], err)
                    return
                # Remember a boundary even when all individual registers work:
                # the rejected address can be an unused gap between them.
                middle = len(members) // 2
                self._block_splits.add(members[middle].address)
                for part in (members[:middle], members[middle:]):
                    part_start = part[0].address
                    part_end = max(reg.address + _reg_width(reg) for reg in part)
                    await read_group(part_start, part_end - part_start, part)
                return
            except ModbusException as err:
                errors_this_cycle += 1
                for reg in members:
                    record_error(reg, err)
                _LOGGER.debug("Block read error at %#06x+%d: %s", start, count, err)
                return

            for reg in members:
                scale = B3_SCALE_OVERRIDES.get(reg.key) if self._model_variant == "b3" else None
                try:
                    data[reg.key] = _decode_block(reg, raw, reg.address - start, scale=scale)
                    self._mark_polled(reg, now)
                except (IndexError, ValueError, TypeError) as err:
                    errors_this_cycle += 1
                    record_error(reg, err)
                    _LOGGER.debug("Decode error for %s: %s", reg.key, err)

        normal_regs = [r for r in numeric_regs if r.key not in self._unsupported_until]
        retry_regs = [r for r in numeric_regs if r.key in self._unsupported_until]
        for start, count, members in _group_registers(normal_regs, self._block_splits):
            await read_group(start, count, members)
        for reg in retry_regs:
            await read_group(reg.address, _reg_width(reg), [reg])

        for reg in string_regs:
            if errors_this_cycle > 5:
                raise UpdateFailed(
                    f"Too many read errors this cycle ({errors_this_cycle}), aborting"
                )
            try:
                data[reg.key] = await self.client.read_register(reg.address, reg.data_type, reg.count)
                self._mark_polled(reg, now)
            except ModbusIllegalAddress as err:
                record_illegal(reg, err)
            except ModbusException as err:
                errors_this_cycle += 1
                record_error(reg, err)
                _LOGGER.debug("Modbus read error for %s: %s", reg.key, err)

        if errors and not data:
            raise UpdateFailed(f"All reads failed: {errors}")

        # Opt-in counters must not extend an existing block or consume its error
        # budget. A rejected optional address leaves all existing reads intact.
        for reg in optional_regs:
            if reg.key not in self.optional_poll_keys:
                continue
            try:
                raw = await self.client.read_block(reg.address, _reg_width(reg))
                data[reg.key] = _decode_block(reg, raw, 0)
                self._last_polled[reg.key] = time.monotonic()
            except (ModbusException, IndexError, ValueError, TypeError) as err:
                data.pop(reg.key, None)
                _LOGGER.debug("Optional read error for %s: %s", reg.key, err)
            finally:
                # Failed reads also wait a full configured interval before retry.
                self._optional_last_attempt[reg.key] = time.monotonic()

        # Allow five configured poll intervals, preserving the existing normal /
        # fast-mode tolerance. Slow polling must not expire healthy cached data
        # before that register is due to be read again.
        # Sensors whose register permanently fails will report unknown instead of
        # keeping the last good value indefinitely.
        for key in list(data.keys()):
            last = self._last_polled.get(key, 0.0)
            if last == 0.0:
                continue
            si = _SCAN_INTERVAL_MAP.get(key)
            if si and (now - last) > 5 * si * max(1.0, self._scan_multiplier):
                del data[key]

        needed = house_load_input_keys(data)
        if not hasattr(self, "house_load_sample"):
            self.house_load_sample = HouseLoadSample()
        self.house_load_sample.update(
            self, data, time.monotonic(), usable=not failed_keys.intersection(needed)
        )
        return data

    async def async_write_dispatch(
        self, values: list[int], *, reset_timer: bool = True,
        guard: Callable[[], bool] | None = None,
        prepare: Callable[[], list[int] | None] | None = None,
        expected=None, attempted=None, acknowledged=None, confirmed=None,
    ) -> bool:
        from .const import DISPATCH_START_ADDR
        if getattr(self, "_unloading", False):
            raise RuntimeError("Integration is unloading")
        valid = lambda: not getattr(self, "_unloading", False) and (guard is None or guard())
        accepted = await self.client.write_dispatch(
            DISPATCH_START_ADDR, guard=valid, prepare=prepare or (lambda: list(values)),
            expected=expected or (lambda: []), attempted=attempted or (lambda command: None),
            acknowledged=acknowledged or (lambda command: None), confirmed=confirmed,
        )
        if not accepted:
            return False
        if not valid():
            return True  # acknowledged, but bookkeeping belongs to a newer session
        if values and values[0] == 1 and len(values) >= 9:
            if reset_timer:
                self.dispatch_started_at = datetime.now(timezone.utc)
                self.dispatch_duration_s = (int(values[7]) << 16) | int(values[8])
                self.dispatch_deadline = time.monotonic() + self.dispatch_duration_s
        else:
            self.dispatch_started_at = None
            self.dispatch_duration_s = 0
            self.dispatch_deadline = None
        return True

    async def async_reset_dispatch(self, *, guard=None, **kwargs) -> None:
        await self.async_write_dispatch([
            0,      # Dispatch Start: stop
            0, 32000,  # Active Power (32000 offset, hi+lo word)
            0, 32000,  # Reactive Power (32000 offset, hi+lo word)
            0,      # Dispatch Mode
            0,      # Dispatch SoC
            0, 90,  # Dispatch Time (90 seconds)
            DISPATCH_FLOW_DIRECTION,  # Flow Direction (0x0889)
            DISPATCH_PV_UNCHANGED,    # PV Switch (0x088A): leave unchanged on stop
        ], guard=guard, **kwargs)

    async def async_write_register(self, address: int, value: int) -> None:
        await self.client.write_register(address, value)

    async def async_write_raw(self, address: int, value: int) -> None:
        _LOGGER.debug("write_raw: address=%#06x value=%d", address, value)
        from .switch import _control_lock, _MUTEX_SWITCHES
        async with _control_lock(self):
            if 0x0880 <= address <= 0x088A:
                switches = self.hass.data[DOMAIN].get(f"{self._entry_id}_switches", {})
                controls = [sw for key, sw in switches.items() if key in _MUTEX_SWITCHES]
                for sw in controls:
                    sw.prepare_force_off()
                for sw in controls:
                    await sw.async_force_off()
            await self.client.write_register(address, value)

    async def async_write_registers(self, address: int, values: list[int]) -> None:
        await self.client.write_registers(address, values)

    async def async_sync_datetime(self) -> None:
        now = dt_util.now(dt_util.get_time_zone(self.hass.config.time_zone))
        if not 2000 <= now.year <= 2099:
            raise ServiceValidationError("The AlphaESS clock supports years 2000 to 2099.")
        # Interpret the decimal digits as hexadecimal nibbles to pack BCD.
        yymm = int(f"{now.year % 100:02d}{now.month:02d}", 16)
        ddhh = int(f"{now.day:02d}{now.hour:02d}", 16)
        mmss = int(f"{now.minute:02d}{now.second:02d}", 16)
        await self.client.write_registers(0x0740, [yymm, ddhh, mmss])
        await self.async_request_refresh()

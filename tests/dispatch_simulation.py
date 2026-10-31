"""Shared in-memory dispatch controller simulation; no network access."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.alphaess_modbus.button import AlphaESSButton
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator
from custom_components.alphaess_modbus.modbus_client import AlphaESSModbusClient
from custom_components.alphaess_modbus.const import DOMAIN
from custom_components.alphaess_modbus.peak_shaving import AlphaESSPeakShavingSwitch, _REQUIRED
from custom_components.alphaess_modbus.switch import AlphaESSSwitch, SWITCH_DEFS


class Simulation:
    def __init__(self):
        self.now = 1000.0
        self.listeners = []
        self.c = SimpleNamespace(
            data={}, _last_polled={}, _scan_multiplier=1, last_update_success=True,
            ac_limit_w=8000, active_dispatch_key=None,
            numbers={'peak_shaving_power': 4, 'peak_shaving_cutoff_soc': 100},
            async_write_dispatch=AsyncMock(), async_reset_dispatch=AsyncMock(),
            async_write_register=AsyncMock(), ee_paused=False, fi_paused=False,
            client=SimpleNamespace(read_register=AsyncMock(return_value=0)),
        )
        self.c.get_number = self.c.numbers.get
        self.c.get_select = lambda key: None
        self.c.async_add_listener = self.add_listener
        self.entry = SimpleNamespace(entry_id='test')
        self.sw = AlphaESSPeakShavingSwitch(self.c, self.entry)
        self.switches = {self.sw.switch_key: self.sw}
        self.hass = SimpleNamespace(loop=asyncio.get_running_loop(), data={DOMAIN: {'test_switches': self.switches}},
                                    async_create_task=lambda coro, **kw: asyncio.create_task(coro))
        self.sw.hass = self.hass
        self.sw.async_write_ha_state = Mock()
        self.sample()

    def add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def sample(self, house=7000, pv=1000, battery=0, soc=50, mode=1, lower=10):
        self.c.data.update(power_grid=house-pv-battery, power_battery=battery,
                           pv1_power=pv, pv2_power=0, pv3_power=0, pv4_power=0,
                           active_power_pv_meter=0, soc_battery=soc,
                           inverter_work_mode=mode, discharging_cutoff_soc=lower,
                           dispatch_start=0)
        self.c._last_polled = {key: self.now for key in (*_REQUIRED, "dispatch_start")}

    async def step(self, seconds=5, **sample):
        self.now += seconds
        self.sample(**sample)
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=self.now):
            await self.sw._update()

    async def start(self):
        with patch('custom_components.alphaess_modbus.peak_shaving.time.monotonic', return_value=self.now), patch(
            'custom_components.alphaess_modbus.peak_shaving.async_track_time_interval', return_value=Mock()
        ):
            await self.sw.async_turn_on()

    def last(self):
        return self.c.async_write_dispatch.call_args.args[0]

    def other(self, key):
        sw = AlphaESSSwitch(self.c, self.entry, next(d for d in SWITCH_DEFS if d['key'] == key))
        sw.hass = self.hass
        sw.async_write_ha_state = Mock()
        self.switches[key] = sw
        return sw


class DispatchSimulation(Simulation):
    def __init__(self):
        super().__init__()
        self.hass.loop = asyncio.get_running_loop()
        self.client = AlphaESSModbusClient('unused', 502, 1)
        self.client._ensure_connected = AsyncMock()
        self.writes = []
        self.state = 0
        self.single_writes = []
        self.c.hass = self.hass
        self.c._entry_id = self.entry.entry_id

        async def write(address, values, **kwargs):
            self.writes.append((address, list(values)))
            self.state = values[0]
            self.registers = list(values)
            return SimpleNamespace(isError=lambda: False)

        self.registers = [0] * 11
        async def read(*args, **kwargs):
            return SimpleNamespace(isError=lambda: False, registers=list(self.registers))
        async def write_single(address, value, **kwargs):
            self.single_writes.append((address, value))
            if 0x0880 <= address <= 0x088A:
                self.registers[address - 0x0880] = value
            return SimpleNamespace(isError=lambda: False)
        self.client._client = SimpleNamespace(retries=3, connected=True, close=Mock(),
            write_registers=AsyncMock(side_effect=write), write_register=AsyncMock(side_effect=write_single), read_holding_registers=AsyncMock(side_effect=read))
        self.client.read_register = AsyncMock(side_effect=lambda *args: self.state)
        self.c.client = self.client
        self.c.dispatch_started_at = None
        self.c.dispatch_duration_s = 0
        self.c.async_write_dispatch = lambda *a, **kw: AlphaESSCoordinator.async_write_dispatch(self.c, *a, **kw)
        self.c.async_reset_dispatch = lambda **kw: AlphaESSCoordinator.async_reset_dispatch(self.c, **kw)

    def button(self, key):
        button = AlphaESSButton(self.c, self.entry, {'key': key, 'name': key, 'icon': 'mdi:sync'})
        button.hass = self.hass
        return button



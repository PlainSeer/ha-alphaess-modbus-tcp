"""Polling options must not tear down an active controller."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.alphaess_modbus import _async_update_options
from custom_components.alphaess_modbus.const import DOMAIN
from custom_components.alphaess_modbus.coordinator import AlphaESSCoordinator


@pytest.mark.parametrize('mode,multiplier,seconds', [('normal',1,2),('fast',0.25,0.25),('slow',4,2)])
def test_poll_options_preserve_active_session_and_cached_samples(mode,multiplier,seconds):
    async def scenario():
        samples = {'soc_battery':50}
        stamps = {'soc_battery':1000}
        c = SimpleNamespace(_model_variant='standard', data=samples, _last_polled=stamps,
                            active_dispatch_key='force_charging', async_request_refresh=AsyncMock())
        c.apply_poll_options = lambda options: AlphaESSCoordinator.apply_poll_options(c,options)
        hass = SimpleNamespace(data={DOMAIN:{'test':c}}, config_entries=SimpleNamespace(async_reload=AsyncMock()))
        entry = SimpleNamespace(entry_id='test',options=dict(model_variant='standard',poll_mode=mode,
                                                           fast_multiplier=0.25,slow_multiplier=4))
        await _async_update_options(hass,entry)
        assert c._scan_multiplier == multiplier and c.update_interval.total_seconds() == seconds
        assert c.active_dispatch_key == 'force_charging' and c.data is samples and c._last_polled is stamps
        c.async_request_refresh.assert_awaited_once()
        hass.config_entries.async_reload.assert_not_awaited()
        entry.options['model_variant'] = 'b3'
        await _async_update_options(hass,entry)
        hass.config_entries.async_reload.assert_awaited_once_with('test')
    asyncio.run(scenario())

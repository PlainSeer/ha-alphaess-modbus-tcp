"""Dashboard management faults and events; no inverter or live HA access."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries
from homeassistant.components.lovelace.dashboard import (
    DashboardsCollection, DashboardsCollectionWebSocket, LovelaceStorage,
)
from homeassistant.components.lovelace.const import (
    STORAGE_DASHBOARD_CREATE_FIELDS, STORAGE_DASHBOARD_UPDATE_FIELDS,
)
from homeassistant.helpers import entity_registry as er, device_registry as dr

from custom_components.alphaess_modbus import _async_update_options
from custom_components.alphaess_modbus import dashboard_install as install
from custom_components.alphaess_modbus.const import DOMAIN


OLD = {'views': [{'title': 'Previous working layout'}]}
NEW = {'views': [{'title': 'Generated layout'}]}


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 10))


class Environment:
    def __init__(self, directory):
        self.hass = HomeAssistant(str(directory))
        self.hass.data['lovelace'] = NS(dashboards={})
        self.hass.data['frontend_panels'] = {}
        self.entry = NS(options={}, domain=DOMAIN, entry_id='test')
        self.entries = [self.entry]
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.hass.config_entries.async_entries = Mock(side_effect=lambda *a, **kw: self.entries)
        self.collection = DashboardsCollection(self.hass)

    @property
    def dashboards(self):
        return self.hass.data['lovelace'].dashboards

    async def setup(self):
        if hasattr(dr, 'async_setup'):
            dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        await self.collection.async_load()
        async def changed(kind, item_id, item):
            if kind == 'removed':
                self.dashboards.pop(item['url_path'], None)
            elif kind == 'added':
                self.dashboards[item['url_path']] = LovelaceStorage(self.hass, item)
        self.collection.async_add_listener(changed)
        DashboardsCollectionWebSocket(self.collection, 'lovelace/dashboards', 'dashboard',
            STORAGE_DASHBOARD_CREATE_FIELDS, STORAGE_DASHBOARD_UPDATE_FIELDS).async_setup(self.hass)

    async def install(self):
        with patch.object(install, 'dashboard_from_registry', return_value=deepcopy(NEW)):
            await install.async_install_dashboard(self.hass)

    async def seed(self):
        await self.install()
        await self.dashboards[install.URL_PATH].async_save(deepcopy(OLD))

    async def close(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop()


@pytest.mark.parametrize('existing', [False, True])
def test_opt_out_preserves_existing_and_does_not_create_a_dashboard(tmp_path, existing):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        if existing:
            await e.seed()
        e.entry.options['manage_dashboard'] = False
        with patch.object(install, 'dashboard_from_registry', return_value=deepcopy(NEW)) as generate:
            await install.async_install_dashboard(e.hass)
        generate.assert_not_called()
        assert len(e.collection.async_items()) == int(existing)
        if existing:
            assert await e.dashboards[install.URL_PATH].async_load(False) == OLD
        await e.close()
    run(scenario())


def test_one_opted_out_inverter_pauses_management_of_the_shared_dashboard(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        e.entries.append(NS(options={'manage_dashboard': False}, domain=DOMAIN, entry_id='second'))
        await e.install()
        assert not e.collection.async_items()
        e.entries[-1].options['manage_dashboard'] = True
        await e.install()
        assert len(e.collection.async_items()) == 1
        await e.close()
    run(scenario())


def test_dashboard_option_does_not_reload_refresh_or_touch_active_control():
    async def scenario():
        previous = dict(model_variant='standard', poll_mode='normal', manage_dashboard=True)
        coordinator = NS(_model_variant='standard', _entry_options=previous,
                         active_dispatch_key='force_charging', dispatch_deadline=9999,
                         apply_poll_options=Mock(), async_request_refresh=AsyncMock())
        hass = NS(data={DOMAIN: {'test': coordinator}}, config_entries=NS(async_reload=AsyncMock()))
        entry = NS(entry_id='test', options={**previous, 'manage_dashboard': False})
        with patch.object(install, 'async_install_dashboard', new=AsyncMock()) as update:
            await _async_update_options(hass, entry)
            update.assert_awaited_once_with(hass)
        coordinator.apply_poll_options.assert_not_called()
        coordinator.async_request_refresh.assert_not_awaited()
        hass.config_entries.async_reload.assert_not_awaited()
        assert coordinator.active_dispatch_key == 'force_charging' and coordinator.dispatch_deadline == 9999
    run(scenario())


def test_concurrent_setup_and_refresh_create_only_one_dashboard(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        original = e.collection.async_create_item
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(data):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            return await original(data)
        with patch.object(e.collection, 'async_create_item', side_effect=delayed) as create, patch.object(
            install, 'dashboard_from_registry', return_value=deepcopy(NEW)
        ):
            first = asyncio.create_task(install.async_install_dashboard(e.hass))
            await entered.wait()
            second = asyncio.create_task(install.async_install_dashboard(e.hass))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
        assert create.await_count == 1 and len(e.collection.async_items()) == 1
        await e.close()
    run(scenario())


@pytest.mark.parametrize('stage', ['layout', 'owner'])
def test_failed_save_retries_even_when_ha_cache_already_has_generated_layout(tmp_path, stage):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        await e.seed()
        current = e.dashboards[install.URL_PATH]
        if stage == 'layout':
            context = patch.object(current._store, 'async_save', new=AsyncMock(side_effect=OSError('disk full')))
        else:
            original = install.Store.async_save
            async def save(store, data):
                if store.key == install.OWNER_STORE and data.get('config') == NEW and not data.get('pending'):
                    raise OSError('ownership completion failed')
                return await original(store, data)
            context = patch.object(install.Store, 'async_save', new=save)
        with context, pytest.raises(OSError):
            await e.install()
        assert await current.async_load(False) == NEW
        with patch.object(current, 'async_save', wraps=current.async_save) as save:
            await e.install()
        save.assert_awaited_once_with(NEW)
        persisted = LovelaceStorage(e.hass, current.config)
        assert await persisted.async_load(False) == NEW
        previous = await install.Store(e.hass, 1, 'alphaess_modbus.saved_dashboard_previous').async_load()
        assert previous['config'] == OLD
        await e.close()
    run(scenario())


def test_created_dashboard_with_failed_first_layout_save_recovers_after_restart(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        with patch.object(LovelaceStorage, 'async_save', new=AsyncMock(side_effect=OSError('disk full'))):
            with pytest.raises(OSError):
                await e.install()
        # A new instance has no in-memory layout from the failed write.
        old = e.dashboards[install.URL_PATH]
        e.dashboards[install.URL_PATH] = LovelaceStorage(e.hass, old.config)
        await e.install()
        assert len(e.collection.async_items()) == 1
        assert await e.dashboards[install.URL_PATH].async_load(False) == NEW
        await e.close()
    run(scenario())


def test_failed_creation_ownership_save_does_not_leave_an_unowned_blank_dashboard(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        original = install.Store.async_save
        async def save(store, data):
            if store.key == install.OWNER_STORE:
                raise OSError('ownership unavailable')
            return await original(store, data)
        with patch.object(install.Store, 'async_save', new=save), pytest.raises(OSError):
            await e.install()
        assert not e.collection.async_items() and install.URL_PATH not in e.dashboards
        await e.install()
        assert await e.dashboards[install.URL_PATH].async_load(False) == NEW
        await e.close()
    run(scenario())


def test_pending_recovery_does_not_adopt_a_different_dashboard(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        await e.seed()
        current = e.dashboards[install.URL_PATH]
        await install.Store(e.hass, 1, install.OWNER_STORE).async_save({
            'dashboard_id': 'different-id', 'config': OLD,
            'pending': {'previous': OLD, 'target': NEW},
        })
        await e.install()
        assert await current.async_load(False) == OLD
        await e.close()
    run(scenario())


def test_registry_watcher_ignores_unrelated_events_and_handles_rename_and_remove(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        registry, devices = er.async_get(e.hass), dr.async_get(e.hass)
        alpha = registry.async_get_or_create('sensor', DOMAIN, 'test_power_grid')
        unrelated = registry.async_get_or_create('light', 'test_light', 'unrelated')
        await e.hass.async_block_till_done()
        timers = []
        def later(delay, callback, *args, **kwargs):
            timer = Mock(callback=callback, delay=delay)
            timers.append(timer)
            return timer
        with patch.object(e.hass.loop, 'call_later', side_effect=later):
            install.async_watch_dashboard(e.hass)
            registry.async_update_entity(unrelated.entity_id, name='Unrelated rename')
            await e.hass.async_block_till_done()
            assert not timers
            registry.async_update_entity(alpha.entity_id, new_entity_id='sensor.renamed_alphaess')
            await e.hass.async_block_till_done()
            assert len(timers) == 1
            registry.async_remove('sensor.renamed_alphaess')
            await e.hass.async_block_till_done()
            assert len(timers) == 2 and timers[0].cancel.called
            registry.async_remove(unrelated.entity_id)
            await e.hass.async_block_till_done()
            assert len(timers) == 2
            added = registry.async_get_or_create('sensor', DOMAIN, 'test_added_sensor')
            await e.hass.async_block_till_done()
            assert len(timers) == 3
            registry.async_update_entity(added.entity_id, hidden_by=er.RegistryEntryHider.USER)
            await e.hass.async_block_till_done()
            assert len(timers) == 4
        await e.close()
    run(scenario())


def test_device_events_are_filtered_and_debounced_and_opt_out_stops_scheduling(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        alpha = NS(entity_id='sensor.alpha', platform=DOMAIN, device_id='alpha-device')
        registry = NS(entities={'sensor.alpha': alpha})
        timers = []
        def later(delay, callback, *args, **kwargs):
            timer = Mock(callback=callback)
            timers.append(timer)
            return timer
        with patch.object(install.er, 'async_get', return_value=registry), patch.object(
            e.hass.loop, 'call_later', side_effect=later
        ), patch.object(install, 'async_install_dashboard', new=AsyncMock()) as update:
            install.async_watch_dashboard(e.hass)
            for device in ['other-device', 'alpha-device', 'alpha-device']:
                e.hass.bus.async_fire('device_registry_updated', {'action': 'update', 'device_id': device})
                await e.hass.async_block_till_done()
            assert len(timers) == 2 and timers[0].cancel.called
            timers[-1].callback()
            await e.hass.async_block_till_done()
            update.assert_awaited_once()
            e.entry.options['manage_dashboard'] = False
            e.hass.bus.async_fire('device_registry_updated', {'action': 'update', 'device_id': 'alpha-device'})
            await e.hass.async_block_till_done()
            assert len(timers) == 2
            e.entry.options['manage_dashboard'] = True
            e.hass.bus.async_fire('device_registry_updated', {'action': 'remove', 'device_id': 'alpha-device'})
            await e.hass.async_block_till_done()
            assert len(timers) == 3
            e.hass.bus.async_fire('device_registry_updated', {'action': 'update', 'device_id': 'alpha-device'})
            await e.hass.async_block_till_done()
            assert len(timers) == 3
        await e.close()
    run(scenario())


def test_user_edit_during_failed_creation_is_not_rolled_back(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        original = install.Store.async_save
        custom = {'views': [{'title': 'User edit during creation'}]}
        async def save(store, data):
            if store.key == install.OWNER_STORE:
                await e.dashboards[install.URL_PATH].async_save(custom)
                raise OSError('ownership unavailable')
            return await original(store, data)
        with patch.object(install.Store, 'async_save', new=save), pytest.raises(OSError):
            await e.install()
        assert len(e.collection.async_items()) == 1
        await e.install()
        assert await e.dashboards[install.URL_PATH].async_load(False) == custom
        await e.close()
    run(scenario())


def test_disabling_management_waits_for_inflight_save_without_refreshing_control(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        await e.seed()
        coordinator = NS(_model_variant='standard', _entry_options={'manage_dashboard': True},
                         active_dispatch_key='dispatch', dispatch_deadline=9999,
                         apply_poll_options=Mock(), async_request_refresh=AsyncMock())
        e.hass.data[DOMAIN] = {'test': coordinator}
        current = e.dashboards[install.URL_PATH]
        original = current._store.async_save
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(data):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            return await original(data)
        with patch.object(current._store, 'async_save', side_effect=delayed) as save:
            writer = asyncio.create_task(e.install())
            await entered.wait()
            e.entry.options['manage_dashboard'] = False
            option_change = asyncio.create_task(_async_update_options(e.hass, e.entry))
            await asyncio.sleep(0)
            assert not option_change.done()
            release.set()
            await asyncio.gather(writer, option_change)
            assert save.await_count == 1
            await e.install()
            assert save.await_count == 1
        coordinator.async_request_refresh.assert_not_awaited()
        coordinator.apply_poll_options.assert_not_called()
        assert coordinator.active_dispatch_key == 'dispatch' and coordinator.dispatch_deadline == 9999
        await e.close()
    run(scenario())


def test_optional_frontend_import_failure_does_not_abort_integration_setup():
    import builtins
    from custom_components.alphaess_modbus import async_setup
    original = builtins.__import__
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'dashboard' and 'async_setup_dashboard' in fromlist:
            raise ImportError('optional frontend unavailable')
        return original(name, globals, locals, fromlist, level)
    async def scenario():
        hass = NS(data={})
        with patch('builtins.__import__', side_effect=guarded), patch.object(install, 'async_watch_dashboard'):
            assert await async_setup(hass, {})
    run(scenario())


@pytest.mark.parametrize('value', [False, True])
def test_options_form_exposes_and_preserves_dashboard_preference(value):
    from custom_components.alphaess_modbus.config_flow import AlphaESSOptionsFlowHandler
    async def scenario():
        entry = NS(options={'manage_dashboard': value})
        flow = AlphaESSOptionsFlowHandler(entry)
        form = await flow.async_step_init()
        schema = form['data_schema']
        result = schema({})
        assert result['manage_dashboard'] is value
        submitted = {**result, 'manage_dashboard': not value}
        saved = await flow.async_step_init(submitted)
        assert saved['data']['manage_dashboard'] is not value
    run(scenario())


@pytest.mark.parametrize('replace', [False, True])
def test_user_change_during_journal_save_is_not_overwritten_without_a_backup(tmp_path, replace):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        await e.seed()
        old = e.dashboards[install.URL_PATH]
        custom = {'views': [{'title': 'Concurrent user edit'}]}
        original = install.Store.async_save
        async def save(store, data):
            await original(store, data)
            if store.key == install.OWNER_STORE and data.get('pending'):
                if replace:
                    e.dashboards[install.URL_PATH] = LovelaceStorage(e.hass, old.config)
                await e.dashboards[install.URL_PATH].async_save(custom)
        with patch.object(install.Store, 'async_save', new=save):
            await e.install()
        current = e.dashboards[install.URL_PATH]
        assert await current.async_load(False) == custom
        await e.install()
        if replace:
            assert await current.async_load(False) == custom
        else:
            assert await current.async_load(False) == NEW
            previous = await install.Store(e.hass, 1, 'alphaess_modbus.saved_dashboard_previous').async_load()
            assert previous['config'] == custom
        await e.close()
    run(scenario())


def test_cancelled_layout_save_leaves_a_recoverable_journal(tmp_path):
    async def scenario():
        e = Environment(tmp_path)
        await e.setup()
        await e.seed()
        current = e.dashboards[install.URL_PATH]
        with patch.object(current._store, 'async_save', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with pytest.raises(asyncio.CancelledError):
                await e.install()
        await e.install()
        persisted = LovelaceStorage(e.hass, current.config)
        assert await persisted.async_load(False) == NEW
        previous = await install.Store(e.hass, 1, 'alphaess_modbus.saved_dashboard_previous').async_load()
        assert previous['config'] == OLD
        await e.close()
    run(scenario())

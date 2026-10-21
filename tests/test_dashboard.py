"""Frontend publishing is optional and never sets up inverter controls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from hashlib import sha256

from custom_components.alphaess_modbus import dashboard


def fake(frontend=True):
    async def execute(fn):
        return fn()
    return SimpleNamespace(config=SimpleNamespace(components={'frontend'} if frontend else set()),
                           data={},
                           http=SimpleNamespace(async_register_static_paths=AsyncMock()),
                           async_add_executor_job=AsyncMock(side_effect=execute))


def test_headless_setup_is_noop():
    h = fake(False)
    asyncio.run(dashboard.async_setup_dashboard(h))
    h.async_add_executor_job.assert_not_called()
    h.http.async_register_static_paths.assert_not_called()


def test_module_registration_and_content_hash():
    h = fake()
    with patch.object(dashboard, 'add_extra_js_url') as add:
        asyncio.run(dashboard.async_setup_dashboard(h))
    path = h.http.async_register_static_paths.call_args.args[0][0]
    assert path.url_path == dashboard.MODULE_URL
    assert not path.cache_headers
    content_path = h.http.async_register_static_paths.call_args.args[0][1]
    assert content_path.url_path == dashboard.CONTENT_URL
    assert not content_path.cache_headers
    digest = sha256(dashboard.MODULE_PATH.read_bytes() + dashboard.CONTENT_PATH.read_bytes()).hexdigest()[:16]
    add.assert_called_once_with(h, f'{dashboard.MODULE_URL}?v={digest}')


def test_frontend_failure_does_not_block_integration():
    h = fake()
    h.http.async_register_static_paths.side_effect = RuntimeError('frontend unavailable')
    with patch.object(dashboard, 'add_extra_js_url') as add:
        asyncio.run(dashboard.async_setup_dashboard(h))
    add.assert_not_called()


def test_setup_hook_delegates_only_to_frontend():
    from custom_components.alphaess_modbus import async_setup
    with patch.object(dashboard, 'async_setup_dashboard', new_callable=AsyncMock) as setup:
        h = fake()
        assert asyncio.run(async_setup(h, {}))
        setup.assert_awaited_once_with(h)


def test_resource_create_update_and_noop():
    """Use the actual HA collection schema and ID handling on both versions."""
    from homeassistant.core import HomeAssistant
    from homeassistant.components.lovelace.resources import ResourceStorageCollection
    import tempfile

    async def run():
        with tempfile.TemporaryDirectory() as directory:
            h = HomeAssistant(directory)
            resources = ResourceStorageCollection(h, Mock())
            resources._async_schedule_save = Mock()
            resources.async_load = AsyncMock()
            h.data['lovelace'] = {'resources': resources}
            url = dashboard.MODULE_URL + '?v=first'
            await dashboard.async_register_dashboard_resource(h, url)
            resources.async_load.assert_awaited_once()
            items = resources.async_items()
            assert len(items) == 1 and items[0]['type'] == 'module'
            original_id = items[0]['id']
            await dashboard.async_register_dashboard_resource(h, url)
            assert len(resources.async_items()) == 1
            other = await resources.async_create_item({'url':'/other.js','res_type':'module'})
            external = await resources.async_create_item({'url':'https://example.org'+dashboard.MODULE_URL,'res_type':'module'})
            duplicate = await resources.async_create_item({'url':url+'old','res_type':'js'})
            h.data['lovelace'] = SimpleNamespace(resources=resources)
            await dashboard.async_register_dashboard_resource(h, url + '2')
            assert resources.data[original_id]['url'] == url + '2'
            assert resources.data[other['id']] == other
            assert resources.data[external['id']] == external
            assert resources.data[duplicate['id']]['url'] == url + '2'
            assert resources.data[duplicate['id']]['type'] == 'module'
            assert len(resources.async_items()) == 4
            with patch.object(resources, 'async_update_item', new_callable=AsyncMock) as update:
                await dashboard.async_register_dashboard_resource(h, url + '2')
                update.assert_not_called()
    asyncio.run(run())


def test_yaml_and_missing_resources_are_untouched():
    from homeassistant.components.lovelace.resources import ResourceYAMLCollection
    h = fake()
    items = [{'url':'/mine.js','type':'module'}]
    h.data['lovelace'] = SimpleNamespace(resources=ResourceYAMLCollection(items))
    asyncio.run(dashboard.async_register_dashboard_resource(h, dashboard.MODULE_URL))
    assert items == [{'url':'/mine.js','type':'module'}]


def test_persisted_resource_is_updated_without_duplicate():
    from homeassistant.core import HomeAssistant
    from homeassistant.components.lovelace.resources import ResourceStorageCollection
    import tempfile

    async def run():
        with tempfile.TemporaryDirectory() as directory:
            h = HomeAssistant(directory)
            resources = ResourceStorageCollection(h, Mock())
            resources._async_schedule_save = Mock()
            old = {'id': 'persisted', 'url': dashboard.MODULE_URL + '?v=old', 'type': 'module'}
            with patch.object(resources.store, 'async_load', new_callable=AsyncMock,
                              return_value={'items': [old]}) as read:
                h.data['lovelace'] = SimpleNamespace(resources=resources)
                url = dashboard.MODULE_URL + '?v=new'
                await dashboard.async_register_dashboard_resource(h, url)
                read.assert_awaited_once()
                assert resources.async_items() == [{'id': 'persisted', 'url': url, 'type': 'module'}]
    asyncio.run(run())


def test_resource_failure_keeps_extra_module():
    h = fake()
    with patch.object(dashboard, 'add_extra_js_url') as add, patch.object(
        dashboard, 'async_register_dashboard_resource', new_callable=AsyncMock,
        side_effect=RuntimeError('storage unavailable')
    ):
        asyncio.run(dashboard.async_setup_dashboard(h))
    add.assert_called_once()

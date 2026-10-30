"""Saved views, guarded migrations and entity coverage."""
import asyncio
import json
import re
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant, is_callback
from custom_components.alphaess_modbus import dashboard_install as install
from custom_components.alphaess_modbus.dashboard_config import build_dashboard, dashboard_from_registry
from custom_components.alphaess_modbus.house_load_daily import AlphaESSHouseLoadDailySensor


def entity(key, domain="sensor", entry="entry", **kwargs):
    return NS(entity_id=f"{domain}.renamed_{entry}_{key}", domain=domain,
              unique_id=f"{entry}_{key}", config_entry_id=entry, device_id=entry,
              platform="alphaess_modbus", disabled_by=kwargs.get("disabled_by"),
              hidden_by=kwargs.get("hidden_by"), name=None, original_name=key)


def visible_cards(view, width):
    cards = []
    for section in view["sections"]:
        visible = True
        for condition in section.get("visibility", []):
            assert condition["condition"] == "screen"
            match = re.fullmatch(r"\((min|max)-width: (\d+)px\)", condition["media_query"])
            assert match
            boundary = int(match[2])
            visible &= width >= boundary if match[1] == "min" else width <= boundary
        if visible:
            cards.extend(section["cards"])
    return cards


def generated():
    return build_dashboard([entity(k) for k in ("soc_battery", "power_grid", "power_battery",
        "current_house_load", "current_pv_production", "house_load_energy_today", "battery_fault")]
        + [entity("force_export", "switch"), entity("restart_ems", "button")], {})


def test_builder_no_strategies_renames_and_complete_entities():
    entities = [entity("power_grid"), entity("battery_fault"), entity("dispatch", "switch"),
                entity("restart_ems", "button"), entity("battery_warning", disabled_by="user"),
                entity("pv1_power", hidden_by="user")]
    config = build_dashboard(entities, {})
    text = json.dumps(config)
    assert "strategy" not in text
    assert "sensor.renamed_entry_power_grid" in text
    assert "battery_warning" not in text and "pv1_power" not in text
    assert text.count('"entity": "sensor.renamed_entry_battery_fault"') == 1
    buttons = [c for v in config["views"] for s in v["sections"] for c in s["cards"]
               if c.get("entity", "").startswith("button.")]
    assert buttons[0]["tap_action"] == buttons[0]["icon_tap_action"] == {"action": "more-info"}
    assert len(build_dashboard([entity("power_grid"), entity("power_grid", entry="second")], {})["views"]) == 6
    assert build_dashboard([], {}) is None


def test_midnight_registered_as_event_loop_callback():
    assert is_callback(AlphaESSHouseLoadDailySensor._midnight)


def test_registry_uses_supported_device_lookup_and_preserves_device_names():
    entities = [entity("power_grid"), entity("power_grid", entry="second")]
    devices = {key: NS(name=key, name_by_user=f"Named {key}") for key in ("entry", "second")}
    registry = Mock(spec=["async_get"])
    registry.async_get.side_effect = devices.get
    hass = NS(states=NS(get=lambda _: None))
    with patch("custom_components.alphaess_modbus.dashboard_config.er.async_get",
               return_value=NS(entities={e.entity_id: e for e in entities})), \
         patch("custom_components.alphaess_modbus.dashboard_config.dr.async_get", return_value=registry):
        result = dashboard_from_registry(hass)
    assert result == build_dashboard(entities, devices)
    assert [view["title"] for view in result["views"]][::3] == ["Named entry · Main", "Named second · Main"]
    assert registry.async_get.call_count == 2


def test_main_keeps_flow_summary_and_all_controls_readings_move_to_second_view():
    entities = [entity(k) for k in ("soc_battery", "soh_battery", "battery_max_cell_temp",
                "inverter_work_mode", "power_grid", "today_pv_generation", "battery_fault")]
    entities += [entity("force_charging", "switch"), entity("force_export_power", "number"),
                 entity("restart_ems", "button")]
    config = build_dashboard(entities, {})
    main, readings, diagnostics = config["views"]
    assert [v["title"] for v in config["views"]] == ["Main", "Readings", "Diagnostics"]
    assert [v["path"] for v in config["views"]] == ["entry-overview", "entry-controls", "entry-diagnostics"]
    flow = main["sections"][0]["cards"][0]
    assert flow["type"] == "custom:alphaess-power-flow"
    assert set(flow["entities"]) >= {"soc", "health", "temperature", "mode", "grid"}
    reading_text = json.dumps(readings)
    for width in (320, 390, 767, 768, 1024):
        main_text = json.dumps(visible_cards(main, width))
        for e in entities:
            if e.domain in ("switch", "number", "button"):
                assert main_text.count(e.entity_id) == 1
                assert e.entity_id not in reading_text
        assert "today_pv_generation" not in main_text
    assert "today_pv_generation" in reading_text
    assert json.dumps(diagnostics).count('"entity": "sensor.renamed_entry_battery_fault"') == 1


def test_main_mobile_order_preserves_each_card_and_the_desktop_columns():
    from collections import Counter

    entities = [entity("power_grid"), entity("today_pv_generation"), entity("battery_fault")]
    entities += [entity(key, "switch") for key in ("force_charging", "force_discharging",
                 "force_import", "force_export", "dispatch", "excess_export", "peak_shaving")]
    entities += [entity("max_feed_to_grid", "number"), entity("restart_ems", "button")]
    for entries in (entities, [e for e in entities if "force_charging" not in e.entity_id]):
        main, readings, diagnostics = build_dashboard(entries, {})["views"]
        desktop = visible_cards(main, 1024)
        labels = lambda cards: [c.get("title", c["type"]) for c in cards]
        expected = ["custom:alphaess-power-flow", "Force charging", "Force discharging", "Dispatch"]
        expected = [name for name in expected if name in labels(desktop)]
        remaining = [c for c in desktop if c.get("title", c["type"]) not in expected]
        for width in (320, 390, 767):
            mobile = visible_cards(main, width)
            assert labels(mobile)[:len(expected)] == expected
            assert mobile[len(expected):] == remaining
            assert Counter(json.dumps(c, sort_keys=True) for c in mobile) == Counter(
                json.dumps(c, sort_keys=True) for c in desktop)
        assert visible_cards(main, 768) == desktop
        assert len(main["sections"]) == 3
        assert all("visibility" not in section for v in (readings, diagnostics) for section in v["sections"])
        assert json.dumps(main).count('"strategy"') == 0


def test_recognition_rejects_modified_beta():
    config = {"views": [{"path": page, "type": "panel", "cards": [
        {"type": "custom:alphaess-beta-live"}]} for page in ("overview", "controls", "diagnostics")]}
    assert install.is_beta_dashboard(config)
    config["views"][0]["cards"].append({"type": "markdown"})
    assert not install.is_beta_dashboard(config)


def test_owned_default_refreshes_after_manual_edits_and_adds_current_sensors():
    async def run():
        with tempfile.TemporaryDirectory() as directory:
            h = HomeAssistant(directory)
            current = Mock(spec=install.LovelaceStorage)
            h.data["lovelace"] = NS(dashboards={install.URL_PATH: current})
            current.async_load = AsyncMock(return_value=install.STRATEGY)
            current.async_save = AsyncMock()
            with patch.object(install, "dashboard_from_registry", return_value=generated()):
                await install.async_install_dashboard(h)
                current.async_save.assert_awaited_once_with(generated())
                backup = await install.Store(h, 1, "alphaess_modbus.saved_dashboard_backup").async_load()
                assert backup["config"] == install.STRATEGY
                current.async_load.return_value = generated()
                current.async_save.reset_mock()
                await install.async_install_dashboard(h)
                current.async_save.assert_not_called()
                renamed = generated()
                renamed["views"][0]["title"] = "Updated registry"
                with patch.object(install, "dashboard_from_registry", return_value=renamed):
                    await install.async_install_dashboard(h)
                current.async_save.assert_awaited_once_with(renamed)
                edited = {"views": [{"title": "My own view"}]}
                current.async_load.return_value = edited
                current.async_save.reset_mock()
                updated = build_dashboard([entity("power_grid"),
                    *[entity(f"current_phase_{p}_grid") for p in "abc"]], {})
                with patch.object(install, "dashboard_from_registry", return_value=updated):
                    await install.async_install_dashboard(h)
                current.async_save.assert_awaited_once_with(updated)
                previous = await install.Store(h, 1, "alphaess_modbus.saved_dashboard_previous").async_load()
                assert previous["config"] == edited
                original = await install.Store(h, 1, "alphaess_modbus.saved_dashboard_backup").async_load()
                assert original["config"] == install.STRATEGY
                for p in "abc":
                    assert json.dumps(updated["views"][2]).count(
                        f'"entity": "sensor.renamed_entry_current_phase_{p}_grid"') == 1
                current.async_load.return_value = updated
                current.async_save.reset_mock()
                with patch.object(install, "dashboard_from_registry", return_value=updated):
                    await install.async_install_dashboard(h)
                current.async_save.assert_not_called()
            await h.async_block_till_done()
    asyncio.run(run())


def test_unowned_same_url_yaml_and_separate_custom_copy_remain_untouched():
    async def run():
        with tempfile.TemporaryDirectory() as directory:
            h = HomeAssistant(directory)
            custom = Mock(spec=install.LovelaceStorage)
            custom.async_load = AsyncMock(return_value={"views": [{"title": "Custom copy"}]})
            custom.async_save = AsyncMock()
            unowned = Mock(spec=install.LovelaceStorage)
            unowned.async_load = AsyncMock(return_value={"views": [{"title": "Unrelated"}]})
            unowned.async_save = AsyncMock()
            h.data["lovelace"] = NS(dashboards={install.URL_PATH: unowned, "my-alphaess-copy": custom})
            with patch.object(install, "dashboard_from_registry", return_value=generated()):
                await install.async_install_dashboard(h)
                unowned.async_save.assert_not_called()
                # Even a saved ownership record must not permit writing YAML.
                await install.Store(h, 1, install.OWNER_STORE).async_save({"config": generated()})
                yaml = NS(async_load=AsyncMock(), async_save=AsyncMock())
                h.data["lovelace"].dashboards[install.URL_PATH] = yaml
                await install.async_install_dashboard(h)
                yaml.async_load.assert_not_called()
                yaml.async_save.assert_not_called()
                h.data["lovelace"].dashboards[install.URL_PATH] = unowned
                await install.async_install_dashboard(h)
                unowned.async_save.assert_awaited_once_with(generated())
                custom.async_load.assert_not_called()
                custom.async_save.assert_not_called()
            await h.async_block_till_done()
    asyncio.run(run())


def test_previous_backup_failure_preserves_owned_edited_default():
    current = Mock(spec=install.LovelaceStorage)
    current.async_load = AsyncMock(return_value={"views": [{"title": "Edited"}]})
    current.async_save = AsyncMock()
    h = NS(data={"lovelace": NS(dashboards={install.URL_PATH: current})}, config=NS(recovery_mode=False))
    owner = NS(async_load=AsyncMock(return_value={"config": generated()}), async_save=AsyncMock())
    original = NS(async_load=AsyncMock(return_value={"config": install.STRATEGY}))
    previous = NS(async_save=AsyncMock(side_effect=RuntimeError("disk full")))
    stores = {install.OWNER_STORE: owner, "alphaess_modbus.saved_dashboard_backup": original,
              "alphaess_modbus.saved_dashboard_previous": previous}
    async def run():
        with patch.object(install, "dashboard_from_registry", return_value=generated()), patch.object(
            install, "Store", side_effect=lambda hass, version, key: stores[key]
        ):
            try:
                await install.async_install_dashboard(h)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Expected failed previous-layout backup")
        current.async_save.assert_not_called()
        owner.async_save.assert_not_called()
    asyncio.run(run())


def test_backup_failure_does_not_change_dashboard():
    current = Mock(spec=install.LovelaceStorage)
    current.async_load = AsyncMock(return_value=install.STRATEGY)
    current.async_save = AsyncMock()
    h = NS(data={"lovelace": NS(dashboards={install.URL_PATH: current})}, config=NS(recovery_mode=False))
    store = NS(async_load=AsyncMock(return_value=None), async_save=AsyncMock(side_effect=RuntimeError("disk full")))
    async def run():
        with patch.object(install, "dashboard_from_registry", return_value=generated()), patch.object(install, "Store", return_value=store):
            try:
                await install.async_install_dashboard(h)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Expected failed backup")
        current.async_save.assert_not_called()
    asyncio.run(run())


def test_real_collection_persists_and_second_setup_is_noop():
    from homeassistant.components.lovelace.dashboard import DashboardsCollectionWebSocket
    from homeassistant.components.lovelace.const import STORAGE_DASHBOARD_CREATE_FIELDS, STORAGE_DASHBOARD_UPDATE_FIELDS
    async def run():
        with tempfile.TemporaryDirectory() as directory:
            h = HomeAssistant(directory)
            dashboards = {}
            h.data["lovelace"] = NS(dashboards=dashboards)
            h.data["frontend_panels"] = {}
            collection = install.DashboardsCollection(h)
            await collection.async_load()
            async def listener(kind, item_id, item):
                dashboards[item["url_path"]] = install.LovelaceStorage(h, item)
            collection.async_add_listener(listener)
            DashboardsCollectionWebSocket(collection, "lovelace/dashboards", "dashboard",
                STORAGE_DASHBOARD_CREATE_FIELDS, STORAGE_DASHBOARD_UPDATE_FIELDS).async_setup(h)
            with patch.object(install, "dashboard_from_registry", return_value=generated()):
                assert install._dashboard_collection(h) is collection
                await install.async_install_dashboard(h)
                assert await dashboards[install.URL_PATH].async_load(False) == generated()
                await install.async_install_dashboard(h)
                assert len(collection.async_items()) == 1
                await collection.async_update_item(collection.async_items()[0]["id"], {"title": "User title"})
                assert collection.async_items()[0]["title"] == "User title"
            await h.async_block_till_done()
    asyncio.run(run())

"""Entity tests against Home Assistant classes; no network or inverter calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.alphaess_modbus.const import DOMAIN
from custom_components.alphaess_modbus.sensor import (
    AlphaESSCalculatedSensor, AlphaESSFilteredPowerSensor, CALCULATED_SENSORS,
    async_setup_entry,
)


def make_sensors():
    coordinator = SimpleNamespace(
        data={"pv1_power": 1000, "pv2_power": 0, "pv3_power": 0,
              "pv4_power": 0, "active_power_pv_meter": 200,
              "power_battery": -500, "power_grid": 0, "inverter_work_mode": 1},
        last_update_success=True,
    )
    entry = SimpleNamespace(entry_id="test-entry")
    sources = {d.key: AlphaESSCalculatedSensor(coordinator, entry, d)
               for d in CALCULATED_SENSORS}
    sensors = {key: AlphaESSFilteredPowerSensor(coordinator, entry, key, key, sources)
               for key in ("current_house_load_filtered",
                           "current_pv_production_filtered", "excess_power")}
    for sensor in sensors.values():
        sensor.async_write_ha_state = Mock()
        sensor._handle_coordinator_update()
    return coordinator, sources, sensors


def update(sensors):
    for sensor in reversed(list(sensors.values())):
        sensor._handle_coordinator_update()


def test_initial_values_include_ac_pv_and_have_stable_identity():
    _, _, sensors = make_sensors()
    assert sensors["current_pv_production_filtered"].native_value == 1200
    assert sensors["current_house_load_filtered"].native_value == 700
    assert sensors["excess_power"].native_value == 500
    for key, sensor in sensors.items():
        assert sensor.unique_id == f"test-entry_{key}"
        assert sensor.device_info["identifiers"] == {(DOMAIN, "test-entry")}
        assert sensor.native_unit_of_measurement == "W"


def test_lowpass_changes_once_per_source_change_without_changing_raw():
    coordinator, sources, sensors = make_sensors()
    coordinator.data["pv1_power"] = 2000
    update(sensors)
    assert sources["current_pv_production"].native_value == 2200
    assert sources["current_house_load"].native_value == 1700
    assert sensors["current_pv_production_filtered"].native_value == 1400
    assert sensors["current_house_load_filtered"].native_value == 900
    for _ in range(5):
        update(sensors)  # Unrelated or repeated coordinator notification.
        assert sensors["current_pv_production_filtered"].native_value == 1400
    coordinator.data["pv1_power"] = 3000
    update(sensors)
    assert sensors["current_pv_production_filtered"].native_value == 1760


def test_excess_clamps_and_does_not_depend_on_other_entities_updating():
    coordinator, _, sensors = make_sensors()
    coordinator.data["power_battery"] = 5000
    sensors["excess_power"]._handle_coordinator_update()
    assert sensors["excess_power"].native_value == 0


def test_missing_input_is_unavailable_then_reseeds():
    coordinator, _, sensors = make_sensors()
    del coordinator.data["pv4_power"]
    update(sensors)
    assert all(not sensor.available for sensor in sensors.values())
    assert all(sensor.native_value is None for sensor in sensors.values())
    coordinator.data["pv4_power"] = 1000
    update(sensors)
    assert sensors["current_pv_production_filtered"].native_value == 2200


def test_coordinator_failure_resets_filter_history():
    coordinator, _, sensors = make_sensors()
    coordinator.last_update_success = False
    update(sensors)
    assert all(sensor.native_value is None for sensor in sensors.values())
    coordinator.last_update_success = True
    coordinator.data["pv1_power"] = 2000
    update(sensors)
    assert sensors["current_pv_production_filtered"].native_value == 2200


def test_missing_grid_only_invalidates_house_and_excess():
    coordinator, _, sensors = make_sensors()
    del coordinator.data["power_grid"]
    update(sensors)
    assert sensors["current_pv_production_filtered"].available
    assert not sensors["current_house_load_filtered"].available
    assert not sensors["excess_power"].available


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -float("inf")])
def test_nonfinite_derived_input_is_unavailable(bad):
    _, _, sensors = make_sensors()
    sensor = sensors["current_pv_production_filtered"]
    sensor._sources["current_pv_production"] = SimpleNamespace(native_value=bad)
    sensor._handle_coordinator_update()
    assert not sensor.available
    assert sensor.native_value is None


def test_work_mode_two_preserves_existing_house_calculation():
    coordinator, sources, sensors = make_sensors()
    coordinator.data["inverter_work_mode"] = 2
    coordinator.data["power_grid"] = 100
    update(sensors)
    assert sources["current_house_load"].native_value == 100
    assert sensors["current_house_load_filtered"].native_value == 580


def test_setup_registers_new_entities_once():
    coordinator, _, _ = make_sensors()
    entry = SimpleNamespace(entry_id="test-entry")
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: coordinator}})
    entities = []
    asyncio.run(async_setup_entry(hass, entry, entities.extend))
    ids = [e.unique_id for e in entities]
    assert len(ids) == len(set(ids))
    assert len([e for e in entities if isinstance(e, AlphaESSFilteredPowerSensor)]) == 3
    assert "test-entry_current_house_load" in ids
    assert "test-entry_current_pv_production" in ids

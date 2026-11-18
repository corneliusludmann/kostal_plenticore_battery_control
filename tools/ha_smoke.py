"""Real HA smoke tests, isolated from tests/conftest.py.

Validated with Home Assistant 2026.10.4 / 2026.11.1 and CPython 3.14.10.
Run in a disposable venv: pip install homeassistant==2026.11.1 pymodbus
Then: python tools/ha_smoke.py

Uses real HA forms, serializers, restore storage, entity platform and removal.
Modbus is mocked; device association is omitted for this standalone entity
platform. This is not complete integration setup/reload or hardware coverage.
"""

import asyncio
from datetime import UTC, datetime
import logging
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import AsyncMock, PropertyMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import probatio
from homeassistant import const
from homeassistant.components.number import NumberExtraStoredData
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    area_registry, device_registry, entity_registry, floor_registry,
    label_registry, restore_state,
)
from homeassistant.helpers.config_validation import custom_serializer
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.kostal_modbus_control import KostalData, number, sensor
from custom_components.kostal_modbus_control.config_flow import ConfigFlow, KostalOptionsFlowHandler


async def smoke(config_dir: str) -> None:
    hass = HomeAssistant(config_dir)
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    result = await flow.async_step_user()
    fields = probatio.to_field_list(
        result["data_schema"], custom_serializer=custom_serializer
    )
    timeout = next(field for field in fields if field["name"] == "modbus_timeout")
    assert timeout["type"] == "integer"
    assert (timeout["valueMin"], timeout["valueMax"], timeout["default"]) == (10, 3600, 30)
    assert len(fields) == 3
    print(f"PASS: real HA {const.__version__} configuration form serialization")

    options_flow = KostalOptionsFlowHandler()
    options_flow.hass = hass
    options_flow.context = {"source": "init"}
    with patch.object(
        KostalOptionsFlowHandler, "config_entry", new_callable=PropertyMock,
        return_value=SimpleNamespace(entry_id="smoke", options={"max_control_power_w": 8500}),
    ):
        option_form = await options_flow.async_step_init()
        option_fields = probatio.to_field_list(
            option_form["data_schema"], custom_serializer=custom_serializer
        )
        ceiling = next(field for field in option_fields if field["name"] == "max_control_power_w")
        assert ceiling["selector"]["number"]["unit_of_measurement"] == "W"
        assert ceiling["selector"]["number"]["min"] == 100
        assert ceiling["selector"]["number"]["max"] == 50000
        assert "max_control_power_w" not in option_form["data_schema"]({})
        result = await options_flow.async_step_init(option_form["data_schema"]({}))
        assert "max_control_power_w" not in result["data"]
    print("PASS: real HA optional power ceiling serialization and clear")

    hass.config_entries = ConfigEntries(hass, {})
    hass.config_entries._store.set_load_empty()
    await hass.config_entries.async_initialize()
    for registry in (floor_registry, area_registry, label_registry):
        await registry.async_load(hass, load_empty=True)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
    await restore_state.async_load(hass, load_empty=True)

    handler = AsyncMock()
    data = KostalData(handler=handler, inverter_timeout=30)
    data.last_control_update = time.monotonic()
    data.control_values_valid = True
    component = EntityComponent(logging.getLogger(__name__), "number", hass)
    entities = [
        number.KostalMinSocLimitNumber(data, "smoke"),
        number.KostalMaxSocLimitNumber(data, "smoke"),
    ]
    data._soc_limit_entities = entities
    data._soc_restore_pending = {entity._key for entity in entities}
    data.soc_limits_ready = False
    restore_data = restore_state.async_get(hass)
    # HA 2026.11 split the restore cache by entity and registry ID.
    restored_states = (
        restore_data.last_states_by_entity_id
        if hasattr(restore_data, "last_states_by_entity_id")
        else restore_data.last_states
    )
    for entity, value in zip(entities, (20, 80), strict=True):
        entity.entity_id = f"number.{entity._key}"
        # Standalone platform: exercise restore/lifecycle without device binding.
        entity._attr_unique_id = None
        restored_states[entity.entity_id] = (
            restore_state.StoredState(
                State(entity.entity_id, str(value)),
                NumberExtraStoredData(100, 0, 1, "%", value),
                datetime.now(UTC),
            )
        )
    with patch.object(
        number.KostalSocLimitNumber, "device_info",
        new_callable=PropertyMock, return_value=None,
    ):
        await component.async_add_entities(entities)
    await hass.async_block_till_done()
    assert (data.min_soc, data.max_soc) == (20, 80)
    assert data.soc_limits_ready
    assert not handler.write_float.await_count
    print("PASS: real Number restore and startup write suppression")

    data.last_stop_time = 0  # Simulate expiration without sleeping.
    for entity in entities:
        await entity._async_write_limit()
    assert any(call.args == (1042, 20) for call in handler.write_float.await_args_list)
    assert any(call.args == (1044, 80) for call in handler.write_float.await_args_list)
    for entity in entities:
        await entity.async_remove()
    await hass.async_block_till_done()
    assert all(entity._removed and entity._remove_timer is None for entity in entities)
    assert all(not entity._active_write_tasks for entity in entities)
    print("PASS: post-settle limit writes and real entity removal/timer cleanup")

    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), name="hold smoke", config_entry=None,
    )
    coordinator.async_set_updated_data({1040: 6000, 1042: 20})
    data.coordinator = coordinator
    diagnostic = sensor.KostalDischargeHoldModeSensor(coordinator, data, "smoke")
    diagnostic.entity_id = "sensor.discharge_hold_mode"
    diagnostic._attr_unique_id = None
    sensors = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    with patch.object(
        sensor.KostalBaseSensor, "device_info",
        new_callable=PropertyMock, return_value=None,
    ):
        await sensors.async_add_entities([diagnostic])
    assert coordinator._listeners
    state = hass.states.get(diagnostic.entity_id)
    assert state.state == "inactive"
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == ["inactive", "minimum_soc", "discharge_limit"]
    assert data.hold_use_discharge_limit  # Upstream 4.2.0 default.
    data.hold_use_discharge_limit = False  # Exercise the explicit legacy-mode fallback.
    data.discharge_hold_min_soc = 90
    coordinator.async_set_updated_data({1040: 0, 1042: 20})
    state = hass.states.get(diagnostic.entity_id)
    assert state.state == "discharge_limit"
    assert state.attributes["automatic_fallback"]
    assert state.attributes["reason"] == "above_requested_max_soc"
    assert state.attributes["observed_discharge_limit_w"] == 0
    data.hold_use_discharge_limit = True
    data.discharge_limit_guard_soc = 80
    for current_soc in (79, 76, 10, 78):
        coordinator.async_set_updated_data({210: current_soc, 1040: 0, 1042: 20})
        state = hass.states.get(diagnostic.entity_id)
        assert state.attributes["minimum_soc_guard_active"]
        assert state.attributes["guard_min_soc"] == max(data.min_soc, current_soc)
    data.communication_ok = False
    coordinator.async_set_update_error(ConnectionError("smoke outage"))
    state = hass.states.get(diagnostic.entity_id)
    assert state.state == "discharge_limit"
    assert state.attributes["observed_discharge_limit_w"] is None
    assert not state.attributes["control_ready"]
    await diagnostic.async_remove()
    assert not coordinator._listeners
    print("PASS: real enum sensor updates, outage diagnostics and listener cleanup")
    await hass.async_stop(force=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="kostal-ha-smoke-") as config_dir:
        asyncio.run(asyncio.wait_for(smoke(config_dir), timeout=30))

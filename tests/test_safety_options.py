"""Safety policy regressions beyond transport/individual entity tests."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.kostal_modbus_control import (
    KostalCoordinator, _apply_safety_options, _async_options_update_listener,
)
from custom_components.kostal_modbus_control.const import (
    DOMAIN, REG_BATTERY_SOC, REG_BATTERY_POWER,
    REG_CURRENT_PHASE1, REG_CURRENT_PHASE2, REG_CURRENT_PHASE3,
)
from custom_components.kostal_modbus_control.switch import KostalChargeStartSwitch, KostalEMSSwitch


def pair(data, hass):
    data.last_control_update = time.monotonic()
    data.control_values_valid = True
    data.coordinator = SimpleNamespace(data={REG_BATTERY_SOC: 50, REG_BATTERY_POWER: 0,
        REG_CURRENT_PHASE1: 5, REG_CURRENT_PHASE2: 5, REG_CURRENT_PHASE3: 5})
    data.charge_power_fixed_w = 1000
    data.inverter_max_power_w = 2000
    charge = KostalChargeStartSwitch(data, 'test'); charge.hass = hass; charge._attr_is_on = True
    ems = KostalEMSSwitch(data, 'test', charge); ems.hass = hass; ems._attr_is_on = True
    data.ems_charge_limit_w = 800
    return charge, ems


@pytest.mark.parametrize('missing', [None, float('nan'), float('inf')])
async def test_enabled_ems_revokes_old_positive_limit_on_invalid_phase(data, hass, missing):
    charge, ems = pair(data, hass)
    data.coordinator.data[REG_CURRENT_PHASE2] = missing
    assert charge._charge_target_watts() == 0  # even before the EMS timer ticks
    await ems._loop_action()
    assert data.ems_charge_limit_w == 0
    assert data.ems_status == 'Blocked'
    data.coordinator.data[REG_CURRENT_PHASE2] = 5
    await ems._loop_action()
    assert 0 < charge._charge_target_watts() <= 1000


async def test_http_firmware_fallback_is_off_by_default(data, hass):
    coordinator = KostalCoordinator(hass, data.handler, data, 'example.invalid')
    assert await coordinator._async_rest_software_version() is None


@pytest.mark.parametrize('options', [
    {'auto_resume_on_recovery': 'false'}, {'control_max_data_age': float('nan')},
    {'charge_max_duration_minutes': 0}, {'charge_max_duration_minutes': 1.5},
])
def test_runtime_safety_options_reject_invalid_stored_values(data, options):
    with pytest.raises(ValueError):
        _apply_safety_options(options, data)


def test_runtime_safety_defaults_and_explicit_options(data):
    _apply_safety_options({}, data)
    assert not data.auto_resume_on_recovery
    assert data.charge_max_duration_minutes == 60
    _apply_safety_options({'auto_resume_on_recovery': True, 'control_max_data_age': 30,
                          'charge_max_duration_minutes': 10}, data)
    assert data.auto_resume_on_recovery
    assert data.control_max_data_age == 30
    assert data.charge_max_duration_minutes == 10


async def test_invalid_options_do_not_mutate_runtime_state(data, hass):
    hass.data[DOMAIN] = {'test': data}
    entry = SimpleNamespace(entry_id='test', options={'grid_target_w': float('nan')})
    with pytest.raises(ValueError):
        await _async_options_update_listener(hass, entry)
    assert data.grid_target_w == 0

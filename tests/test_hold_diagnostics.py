"""Hold diagnostics follow actual control transitions without claiming hardware ACKs."""
import time
from types import SimpleNamespace

import pytest

from custom_components.kostal_modbus_control.const import (
    REG_BATTERY_SOC, REG_BATTERY_MIN_SOC, REG_BATTERY_MAX_DISCHARGE_POWER_W,
    REG_BATTERY_MAX_DISCHARGE_LIMIT,
)
from custom_components.kostal_modbus_control.number import KostalMaxSocLimitNumber
from custom_components.kostal_modbus_control.sensor import KostalDischargeHoldModeSensor
from custom_components.kostal_modbus_control.switch import KostalBlockDischargeSwitch


def setup(data, hass):
    data.control_values_valid = True
    data.last_control_update = time.monotonic()
    data.coordinator = SimpleNamespace(data={REG_BATTERY_SOC: 80,
        REG_BATTERY_MIN_SOC: 10, REG_BATTERY_MAX_DISCHARGE_POWER_W: 6000,
        REG_BATTERY_MAX_DISCHARGE_LIMIT: 6000}, last_update_success=True)
    hold = KostalBlockDischargeSwitch(data, 'test')
    hold.hass = hass
    hold._attr_is_on = True
    return hold, KostalDischargeHoldModeSensor(data.coordinator, data, 'test')


async def test_hold_diagnostic_follows_limit_transitions_and_stop(data, hass):
    hold, sensor = setup(data, hass)
    assert sensor.native_value == 'inactive'
    await hold._run_guarded_action(hold._hold_discharge_at, 'test', 80)
    assert sensor.native_value == 'minimum_soc'
    assert sensor.extra_state_attributes['hold_owner'] == hold._key
    maximum = KostalMaxSocLimitNumber(data, 'test'); maximum.hass = hass
    await maximum.async_set_native_value(50)
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == 'discharge_limit'
    assert attrs['automatic_fallback'] and attrs['standby_discharge_possible']
    assert attrs['reason'] == 'above_requested_max_soc'
    assert attrs['hold_target_soc'] == 80
    await maximum.async_set_native_value(90)
    assert sensor.native_value == 'minimum_soc'
    assert not sensor.extra_state_attributes['automatic_fallback']
    await hold.async_turn_off()
    assert sensor.native_value == 'inactive'
    assert sensor.extra_state_attributes['reason'] == 'no_hold'
    assert not sensor.extra_state_attributes['control_ready']
    await maximum.async_will_remove_from_hass()


@pytest.mark.parametrize('auto_resume', [False, True])
async def test_failed_max_raise_explains_acknowledgement_fallback(data, hass, auto_resume):
    data.auto_resume_on_recovery = auto_resume
    hold, sensor = setup(data, hass)
    data.max_soc = data.applied_max_soc = 50
    await hold._run_guarded_action(hold._hold_discharge_at, 'test', 80)
    maximum = KostalMaxSocLimitNumber(data, 'test'); maximum.hass = hass
    data.handler.write_float.side_effect = ConnectionError('test')
    await maximum.async_set_native_value(90)
    assert hold.is_on is auto_resume
    assert hold._faulted
    assert not sensor.extra_state_attributes['control_ready']
    assert sensor.native_value == ('discharge_limit' if auto_resume else 'inactive')
    assert sensor.extra_state_attributes['reason'] == (
        'above_acknowledged_max_soc' if auto_resume else 'no_hold'
    )
    assert sensor.extra_state_attributes['acknowledged_max_soc'] == 50
    await maximum.async_will_remove_from_hass()


async def test_configured_limit_and_stale_observations_are_distinct(data, hass):
    hold, sensor = setup(data, hass)
    data.hold_use_discharge_limit = True
    await hold._run_guarded_action(hold._hold_discharge_at, 'test', 80)
    attrs = sensor.extra_state_attributes
    assert attrs['reason'] == 'configured_discharge_limit'
    assert not attrs['automatic_fallback']
    # Last poll still says 6000: local intent must not masquerade as read-back.
    assert attrs['observed_discharge_limit_w'] == 6000
    data.last_control_update -= 1000
    assert sensor.available
    assert not sensor.extra_state_attributes['control_ready']
    assert sensor.extra_state_attributes['observed_discharge_limit_w'] is None
    assert sensor.extra_state_attributes['observed_min_soc'] is None

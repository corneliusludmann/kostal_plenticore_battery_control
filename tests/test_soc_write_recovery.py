"""SoC write failures must revoke control before a healthy poll can recover."""
from unittest.mock import AsyncMock, Mock, call

import pytest

from custom_components.kostal_modbus_control import KostalCoordinator
from custom_components.kostal_modbus_control.const import (
    REG_BATTERY_SOC, REG_BATTERY_POWER, REG_BATTERY_MGMT_MODE,
    REG_BATTERY_MIN_SOC, REG_BATTERY_MAX_SOC,
)
from custom_components.kostal_modbus_control.number import (
    KostalMinSocLimitNumber, KostalMaxSocLimitNumber,
)
from custom_components.kostal_modbus_control.switch import KostalChargeStartSwitch


@pytest.mark.parametrize('auto_resume', [False, True])
@pytest.mark.parametrize('number_type,register,target', [
    (KostalMinSocLimitNumber, REG_BATTERY_MIN_SOC, 20),
    (KostalMaxSocLimitNumber, REG_BATTERY_MAX_SOC, 80),
])
async def test_soc_write_failure_revokes_charge_before_successful_poll(
    data, hass, auto_resume, number_type, register, target,
):
    data.auto_resume_on_recovery = auto_resume
    data.inverter_max_power_w = 6000
    data.charge_power_fixed_w = 1000
    sample = {
        REG_BATTERY_SOC: 79, REG_BATTERY_POWER: -1000,
        REG_BATTERY_MGMT_MODE: 2,
        REG_BATTERY_MIN_SOC: 5, REG_BATTERY_MAX_SOC: 100,
    }
    data.handler.read_uint16_scale_pair = AsyncMock(return_value=(6000, 0))
    for name in ('read_float', 'read_int16', 'read_uint8', 'read_uint16',
                 'read_uint32', 'read_uint32_big_endian', 'read_string'):
        setattr(data.handler, name, AsyncMock(
            side_effect=lambda address, *args: sample.get(address)
        ))
    coordinator = KostalCoordinator(hass, data.handler, data, 'unit-test.invalid')
    data.coordinator = coordinator
    coordinator.data = await coordinator._async_update_data()
    charge = KostalChargeStartSwitch(data, 'test')
    charge.hass = hass
    charge._attr_is_on = True
    # Keep timer scheduling at the HA boundary; execute production ticks below.
    charge._schedule_start_loop = Mock()
    charge._remove_timer = cancel_loop = Mock()
    charge._deadline_timer = cancel_deadline = Mock()
    limit = number_type(data, 'test')
    limit.hass = hass

    async def fail_limit(address, value):
        if address == register:
            raise OSError('SoC limit write failed')

    data.handler.write_float.side_effect = fail_limit
    try:
        await limit.async_set_native_value(target)
        data.handler.write_float.assert_any_await(register, target)
        if register == REG_BATTERY_MAX_SOC:
            assert data.handler.write_float.await_args_list == [
                call(REG_BATTERY_MIN_SOC, 5), call(REG_BATTERY_MAX_SOC, 80),
            ]
        assert not data.communication_ok
        assert charge._faulted
        assert charge.is_on is auto_resume
        assert charge._resume_pending is auto_resume
        assert data.control_settling()
        cancel_loop.assert_called_once()
        if auto_resume:
            cancel_deadline.assert_not_called()
        else:
            cancel_deadline.assert_called_once()

        data.handler.write_float.reset_mock(side_effect=True)
        # A complete, healthy poll arrives before the next control timer tick.
        coordinator.data = await coordinator._async_update_data()
        assert data.communication_ok
        assert coordinator.data[REG_BATTERY_MAX_SOC] == 100
        assert data.applied_max_soc == 100
        await charge._async_handle_loop_tick()
        await limit._async_write_tick()
        data.handler.write_float.assert_not_awaited()

        data.last_stop_time = 0  # Advance beyond the full settling interval.
        await limit._async_write_tick()
        data.handler.write_float.assert_any_await(register, target)
        data.handler.write_float.reset_mock()
        await charge._async_handle_loop_tick()
        if auto_resume:
            charge._schedule_start_loop.assert_called_once_with('resume')
            data.handler.write_float.assert_any_await(data.charge_discharge_reg, -1000)
        else:
            charge._schedule_start_loop.assert_not_called()
            data.handler.write_float.assert_not_awaited()
            assert not charge.is_on
    finally:
        await limit.async_will_remove_from_hass()
        await charge.async_will_remove_from_hass()

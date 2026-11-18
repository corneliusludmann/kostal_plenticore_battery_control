"""Replay hypothetical Modbus startup samples based on an HA REST observation.

The observed 0/Off -> 82/GridPreCheck sequence is not a Modbus capture. Tests
exercise real coordinator/actions; only transport, scheduler and HA are doubles.
No inverter-state whitelist or arbitrary SoC jump threshold is assumed.
"""
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.kostal_modbus_control import KostalCoordinator, KostalData
from custom_components.kostal_modbus_control.const import (
    REG_BATTERY_SOC, REG_BATTERY_POWER, REG_BATTERY_MGMT_MODE,
    REG_BATTERY_MIN_SOC, REG_INVERTER_STATE,
)
from custom_components.kostal_modbus_control.number import KostalMinSocLimitNumber
from custom_components.kostal_modbus_control.switch import (
    KostalBlockDischargeSwitch, KostalChargeStartSwitch,
)


def replay(data, hass):
    sample = {REG_BATTERY_SOC: 82, REG_BATTERY_POWER: 0,
              REG_BATTERY_MGMT_MODE: 2, REG_INVERTER_STATE: 6}
    data.handler.read_uint16_scale_pair = AsyncMock(return_value=(6000, 0))
    for name in ('read_float', 'read_int16', 'read_uint8', 'read_uint16',
                 'read_uint32', 'read_uint32_big_endian', 'read_string'):
        setattr(data.handler, name, AsyncMock(side_effect=lambda address, *args: sample.get(address)))
    coordinator = KostalCoordinator(hass, data.handler, data, 'unit-test.invalid')
    data.coordinator = coordinator

    async def poll(soc, inverter_state):
        sample.update({REG_BATTERY_SOC: soc, REG_INVERTER_STATE: inverter_state})
        # HA publishes a snapshot only after a successful complete poll.
        coordinator.data = await coordinator._async_update_data()

    return sample, poll


async def test_startup_zero_then_real_soc_keeps_restored_limit_silent(data, hass):
    runtime = KostalData(handler=data.handler, soc_limits_ready=True)
    _sample, poll = replay(runtime, hass)
    await poll(0, 0)
    minimum = KostalMinSocLimitNumber(runtime, 'test')
    minimum.hass = hass
    minimum.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=10))
    await minimum.async_added_to_hass()
    await minimum._write_task
    try:
        await poll(82, 12)
        await minimum._async_write_tick()
        assert runtime.control_settling()
        data.handler.write_float.assert_not_awaited()
        runtime.last_stop_time = 0  # Advance past the entire quiet interval.
        await minimum._async_write_tick()
        data.handler.write_float.assert_awaited_once_with(REG_BATTERY_MIN_SOC, 10)
    finally:
        await minimum.async_will_remove_from_hass()


@pytest.mark.parametrize('switch_type', [KostalChargeStartSwitch, KostalBlockDischargeSwitch])
@pytest.mark.parametrize('auto_resume', [False, True])
async def test_reconnect_zero_does_not_replay_control_during_quiet_period(
    data, hass, switch_type, auto_resume,
):
    data.auto_resume_on_recovery = auto_resume
    data.inverter_max_power_w = 6000
    data.charge_power_fixed_w = 1000
    _sample, poll = replay(data, hass)
    await poll(82, 6)
    switch = switch_type(data, 'test')
    switch.hass = hass
    switch._attr_is_on = True
    # Replace task scheduling but exercise production tick entry points at each
    # sample, including ticks arriving before the quiet interval has expired.
    switch._schedule_start_loop = Mock()
    data.register_runtime_switch(switch)
    await switch._async_handle_loop_tick()
    assert data.handler.write_float.await_count > 0
    data.handler.write_float.reset_mock()
    reader = data.handler.read_float.side_effect
    data.handler.read_float.side_effect = OSError('inverter restarting')
    with pytest.raises(UpdateFailed):
        await poll(0, 0)
    assert not data.control_data_fresh()
    assert switch.is_on is auto_resume
    data.handler.read_float.side_effect = reader
    await poll(0, 0)
    assert data.communication_ok
    assert data.control_settling()
    await switch._async_handle_loop_tick()
    data.handler.write_float.assert_not_awaited()
    await poll(82, 12)
    await switch._async_handle_loop_tick()
    data.handler.write_float.assert_not_awaited()
    data.last_stop_time = 0
    await poll(82, 6)
    await switch._async_handle_loop_tick()
    if auto_resume:
        switch._schedule_start_loop.assert_called_once_with('resume')
        if switch_type is KostalBlockDischargeSwitch:
            assert data.discharge_hold_min_soc == 82
            data.handler.write_float.assert_any_await(REG_BATTERY_MIN_SOC, 82)
        else:
            data.handler.write_float.assert_any_await(data.charge_discharge_reg, -1000)
    else:
        switch._schedule_start_loop.assert_not_called()
        data.handler.write_float.assert_not_awaited()
    await switch.async_will_remove_from_hass()


@pytest.mark.parametrize('state', [6, 10])  # FeedIn and nighttime Standby.
async def test_genuine_zero_remains_chargeable_after_quiet_period(data, hass, state):
    data.inverter_max_power_w = 6000
    data.charge_power_fixed_w = 1000
    _sample, poll = replay(data, hass)
    await poll(0, state)
    assert data.control_data_fresh()
    switch = KostalChargeStartSwitch(data, 'test')
    switch.hass = hass
    switch._attr_is_on = True
    await switch._async_handle_loop_tick()
    data.handler.write_float.assert_any_await(data.charge_discharge_reg, -1000)
    await switch.async_will_remove_from_hass()


async def test_partial_reconnect_poll_never_reauthorizes_cached_measurements(data, hass):
    sample, poll = replay(data, hass)
    await poll(82, 6)
    previous = data.coordinator.data
    data.auto_resume_on_recovery = True
    data.inverter_max_power_w = 6000
    data.charge_power_fixed_w = 1000
    switch = KostalChargeStartSwitch(data, 'test')
    switch.hass = hass
    switch._attr_is_on = True
    data.handler.read_uint8.side_effect = OSError('connection dropped mid-poll')
    with pytest.raises(UpdateFailed):
        await poll(0, 0)
    assert sample[REG_BATTERY_SOC] == 0
    assert data.coordinator.data is previous
    assert previous[REG_BATTERY_SOC] == 82
    assert not data.control_data_fresh()
    assert switch.is_on  # Opt-in resume preserves intent, never cached permission.
    await switch._async_handle_loop_tick()
    data.handler.write_float.assert_not_awaited()
    await switch.async_will_remove_from_hass()

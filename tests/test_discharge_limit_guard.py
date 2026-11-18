"""Residual-discharge holds: register traces, limits, lifecycle and timer ordering."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.kostal_modbus_control import (
    _async_switch_hold_register, _apply_hold_options,
)
from custom_components.kostal_modbus_control.const import (
    REG_BATTERY_SOC, REG_BATTERY_POWER, REG_BATTERY_MIN_SOC,
    REG_BATTERY_MAX_DISCHARGE_POWER_W, REG_BATTERY_MAX_DISCHARGE_LIMIT,
)
from custom_components.kostal_modbus_control.number import (
    KostalMinSocLimitNumber, KostalMaxSocLimitNumber,
)
from custom_components.kostal_modbus_control.sensor import KostalDischargeHoldModeSensor
from custom_components.kostal_modbus_control.switch import (
    KostalBlockDischargeSwitch, KostalChargeStartSwitch,
)


@pytest.fixture
def hold(data, hass, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('custom_components.kostal_modbus_control.time.monotonic', lambda: clock[0])
    data.control_values_valid = True
    data.last_control_update = clock[0]
    data.inverter_max_power_w = 6000
    data.coordinator = SimpleNamespace(data={
        REG_BATTERY_SOC: 80, REG_BATTERY_POWER: 212,
        REG_BATTERY_MAX_DISCHARGE_LIMIT: 6000,
        REG_BATTERY_MAX_DISCHARGE_POWER_W: 6000,
    })
    switch = KostalBlockDischargeSwitch(data, 'test')
    switch.hass = hass
    switch._attr_is_on = True
    switch._schedule_start_loop = Mock()
    data.register_runtime_switch(switch)

    async def tick(seconds=0, soc=80, power=212):
        clock[0] += seconds
        data.last_control_update = clock[0]
        data.coordinator.data.update({REG_BATTERY_SOC: soc, REG_BATTERY_POWER: power})
        await switch._async_handle_loop_tick()

    return switch, tick


def writes(data):
    return [call.args for call in data.handler.write_float.await_args_list]


async def activate(hold):
    _, tick = hold
    await tick()
    await tick(30)


async def test_default_1040_only_until_sustained_discharge(data, hold):
    _, tick = hold
    _apply_hold_options({}, data)
    assert data.hold_use_discharge_limit
    await tick(power=0)
    await tick(30, power=0)
    assert data.discharge_limit_guard_soc is None
    await tick(power=212)
    await tick(29, power=212)
    assert data.discharge_limit_guard_soc is None
    await tick(1, power=50)  # At threshold breaks the consecutive interval.
    await tick(1, power=212)
    await tick(29, power=212)
    assert data.discharge_limit_guard_soc is None
    await tick(1, power=212)
    assert data.discharge_limit_guard_soc == 80
    assert writes(data)[-2:] == [(1040, 0.0), (1042, 80)]


@pytest.mark.parametrize('regular_min', [5, 12])
async def test_guard_tracks_down_and_up_without_refill_target(data, hold, regular_min):
    data.min_soc = regular_min
    data.hold_soc_offset = 2  # A guard must not add the hold offset above actual SoC.
    await activate(hold)
    _, tick = hold
    for soc in [79, 75, regular_min, regular_min - 1, 0, 85]:
        data.handler.write_float.reset_mock()
        await tick(15, soc=soc, power=0)
        assert writes(data) == [(1040, 0.0), (1042, max(regular_min, soc))]
        assert data.discharge_limit_guard_soc is not None  # Zero W does not unlatch it.
    sensor = KostalDischargeHoldModeSensor(data.coordinator, data, 'test')
    assert sensor.extra_state_attributes['minimum_soc_guard_active']
    assert sensor.extra_state_attributes['guard_min_soc'] == 85


async def test_number_keepalive_tracks_new_soc_before_hold_tick(data, hass, hold):
    await activate(hold)
    minimum = KostalMinSocLimitNumber(data, 'test')
    minimum.hass = hass
    minimum._attr_native_value = data.min_soc = 10
    try:
        for soc in [79, 7, 86]:
            data.coordinator.data[REG_BATTERY_SOC] = soc
            data.handler.write_float.reset_mock()
            await minimum._async_write_tick()
            assert writes(data) == [(1042, max(10, soc))]
    finally:
        await minimum.async_will_remove_from_hass()


@pytest.mark.parametrize('invalid', [None, float('nan'), float('inf'), -1, 101, 'unknown', True])
async def test_invalid_soc_does_not_lower_guard_or_start_one(data, hold, invalid):
    await activate(hold)
    data.coordinator.data[REG_BATTERY_SOC] = invalid
    data._update_limit_guard(invalid, 212)
    assert data.effective_min_soc() == 80
    data.clear_discharge_limit_guard()
    data._update_limit_guard(invalid, 212)
    assert data.discharge_limit_guard_soc is None
    assert data.discharge_limit_delivering_since is None


async def test_stale_data_does_not_lower_guard_or_write(data, hold):
    await activate(hold)
    switch, _ = hold
    data.auto_resume_on_recovery = True
    data.last_control_update -= 1000
    data.coordinator.data[REG_BATTERY_SOC] = 0
    assert data.effective_min_soc() == 80
    data.handler.write_float.reset_mock()
    await switch._async_handle_loop_tick()
    assert writes(data) == []
    assert switch._faulted


@pytest.mark.parametrize('power', [None, float('nan'), float('inf'), True])
async def test_missing_power_breaks_detection_interval(data, hold, power):
    _, tick = hold
    await tick()
    await tick(30, power=power)
    assert data.discharge_limit_guard_soc is None
    await tick(1)
    assert data.discharge_limit_guard_soc is None
    await tick(30)
    assert data.discharge_limit_guard_soc == 80


async def test_stop_clears_guard_without_writing_charge_setpoint(data, hold):
    await activate(hold)
    switch, _ = hold
    data.handler.write_float.reset_mock()
    await switch.async_turn_off()
    assert writes(data) == [(1040, 6000), (1042, 5)]
    assert data.discharge_hold_owner is None
    assert data.discharge_hold_min_soc is None
    assert data.discharge_limit_guard_soc is None
    assert data.discharge_limit_delivering_since is None


async def test_fault_then_number_recovery_cannot_renew_retired_guard(data, hass, hold):
    await activate(hold)
    switch, _ = hold
    data.mark_communication_lost('test outage')
    data.notify_connection_lost()
    assert not switch.is_on
    assert data.discharge_limit_guard_soc is None
    assert data.discharge_limit_delivering_since is None
    assert data.discharge_limit_blocked  # Physical limit still needs later restoration.
    data.mark_communication_restored()
    data.last_stop_time = 0
    minimum = KostalMinSocLimitNumber(data, 'test')
    minimum.hass = hass
    minimum._attr_native_value = data.min_soc = 10
    data.handler.write_float.reset_mock()
    try:
        await minimum._async_write_tick()
        assert writes(data) == [(1042, 10)]
    finally:
        await minimum.async_will_remove_from_hass()


async def test_other_switch_cannot_retire_guard(data, hass, hold):
    await activate(hold)
    other = KostalChargeStartSwitch(data, 'test')
    other.hass = hass
    other._clear_local_control_state()
    data.handler.write_float.reset_mock()
    await other._release_discharge_hold()
    assert data.discharge_limit_guard_soc == 80
    assert writes(data) == []


async def test_guard_respects_lowered_and_unacknowledged_maximum(data, hass, hold):
    await activate(hold)
    maximum = KostalMaxSocLimitNumber(data, 'test')
    maximum.hass = hass
    try:
        data.handler.write_float.reset_mock()
        await maximum.async_set_native_value(50)
        trace = writes(data)
        assert trace.index((1040, 0)) < trace.index((1042, 50)) < trace.index((1044, 50))
        data.max_soc = 90  # Desired but not yet acknowledged.
        assert data.effective_min_soc() == 50
        data.min_soc = 60
        assert data.effective_min_soc() == 60  # Never below configured floor.
        data.handler.write_float.reset_mock()
        # Reserve released above its target, while a maximum increase is pending.
        await data.async_write_discharge_hold(70)
        assert all(reg != 1042 for reg, _ in writes(data))
        assert data.discharge_limit_guard_soc is None
    finally:
        await maximum.async_will_remove_from_hass()


async def test_mode_switch_and_return_starts_guard_detection_again(data, hold):
    await activate(hold)
    data.hold_use_discharge_limit = False
    data.handler.write_float.reset_mock()
    await _async_switch_hold_register(data, True)
    assert writes(data) == [(1042, 80), (1040, 6000)]
    assert data.discharge_limit_guard_soc is None
    data.hold_use_discharge_limit = True
    data.handler.write_float.reset_mock()
    await _async_switch_hold_register(data, False)
    assert writes(data) == [(1040, 0), (1042, 5)]
    assert data.discharge_limit_guard_soc is None


async def test_connection_loss_restarts_evidence_timer(data, hold):
    _, tick = hold
    await tick()
    data.mark_communication_lost('test')
    data.mark_communication_restored()
    await tick(30)
    assert data.discharge_limit_guard_soc is None
    await tick(30)
    assert data.discharge_limit_guard_soc == 80


@pytest.mark.parametrize('auto_resume', [False, True])
async def test_guard_write_failure_revokes_or_resumes_with_current_soc(data, hass, hold, auto_resume):
    await activate(hold)
    data.auto_resume_on_recovery = auto_resume
    switch, tick = hold

    async def fail_guard(register, value):
        if register == REG_BATTERY_MIN_SOC:
            raise ConnectionError('failed guard write')

    data.handler.write_float.side_effect = fail_guard
    await tick(15, soc=79, power=0)
    assert switch._faulted
    assert switch.is_on is auto_resume
    assert not data.communication_ok
    if not auto_resume:
        assert data.discharge_limit_guard_soc is None
        assert data.discharge_hold_owner is None
    data.handler.write_float.side_effect = None
    data.handler.write_float.reset_mock()
    data.mark_communication_restored()
    # Quiet period blocks both control and minimum-number keep-alives.
    await tick(15, soc=78, power=0)
    assert writes(data) == []
    data.last_stop_time = 0
    await tick(15, soc=77, power=0)
    if auto_resume:
        assert writes(data) == [(1040, 0), (1042, 77)]
    else:
        assert writes(data) == []
        minimum = KostalMinSocLimitNumber(data, 'test')
        minimum.hass = hass
        minimum._attr_native_value = data.min_soc = 10
        try:
            await minimum._async_write_tick()
            assert writes(data) == [(1042, 10)]
        finally:
            await minimum.async_will_remove_from_hass()

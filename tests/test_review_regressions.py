"""Cross-feature regressions found by the independent reliability review."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
import voluptuous_serialize

from homeassistant.helpers.selector import SelectSelector

from custom_components.kostal_modbus_control import KostalData
from custom_components.kostal_modbus_control.config_flow import ConfigFlow
from custom_components.kostal_modbus_control.const import (
    DOMAIN, CONF_MODBUS_TIMEOUT, PREDBAT_MODE_ENTITY, PREDBAT_RESERVE_ENTITY,
    REG_BATTERY_SOC, REG_BATTERY_POWER, REG_TOTAL_ACTIVE_POWER,
    REG_BATTERY_MIN_SOC, REG_BATTERY_MAX_SOC, REG_BATTERY_MAX_DISCHARGE_POWER_W,
    REG_BATTERY_MAX_DISCHARGE_LIMIT, REG_CURRENT_PHASE1, REG_CURRENT_PHASE2,
    REG_CURRENT_PHASE3, REG_VOLTAGE_PHASE1, REG_VOLTAGE_PHASE2, REG_VOLTAGE_PHASE3,
)
from custom_components.kostal_modbus_control.modbus_handler import KostalModbusHandler
from custom_components.kostal_modbus_control.number import KostalMinSocLimitNumber, KostalMaxSocLimitNumber
from custom_components.kostal_modbus_control.switch import (
    KostalChargeStartSwitch, KostalEMSSwitch, KostalBlockDischargeSwitch,
)


def healthy(data):
    data.control_values_valid = True
    data.last_control_update = time.monotonic()
    data.inverter_max_power_w = 6000
    data.charge_power_fixed_w = 6000
    data.coordinator = SimpleNamespace(data={
        REG_BATTERY_SOC: 80, REG_BATTERY_POWER: 0,
        REG_BATTERY_MAX_DISCHARGE_LIMIT: 6000,
        REG_CURRENT_PHASE1: 20, REG_CURRENT_PHASE2: 20, REG_CURRENT_PHASE3: 20,
        REG_VOLTAGE_PHASE1: 230, REG_VOLTAGE_PHASE2: 230, REG_VOLTAGE_PHASE3: 230,
    })


def charge_pair(data, hass):
    healthy(data)
    charge = KostalChargeStartSwitch(data, 'test')
    charge.hass = hass
    charge._attr_is_on = True
    ems = KostalEMSSwitch(data, 'test', charge)
    ems.hass = hass
    return charge, ems


async def test_initial_config_form_serializes_and_enforces_timeout_range():
    result = await ConfigFlow().async_step_user()
    # Use the real serializer. Only HA's selector serialization is a boundary
    # double here; arbitrary validators must not be accepted by this adapter.
    fields = voluptuous_serialize.convert(result['data_schema'], custom_serializer=lambda schema:
        {'type': 'select'} if isinstance(schema, SelectSelector) else voluptuous_serialize.UNSUPPORTED)
    timeout = next(field for field in fields if field['name'] == CONF_MODBUS_TIMEOUT)
    assert timeout['type'] == 'integer'
    assert timeout['valueMin'] == 10 and timeout['valueMax'] == 3600
    for value in (9, 3601):
        with pytest.raises(vol.Invalid):
            result['data_schema']({'host': 'unused', CONF_MODBUS_TIMEOUT: value, 'inverter_type': 'hybrid'})


async def test_restart_quiet_period_covers_restored_limits_and_switches(data, hass):
    fresh = KostalData(handler=data.handler, soc_limits_ready=True)
    healthy(fresh)
    minimum = KostalMinSocLimitNumber(fresh, 'test')
    minimum.hass = hass
    minimum.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=10))
    await minimum.async_added_to_hass()
    await minimum._write_task
    switch = KostalBlockDischargeSwitch(fresh, 'test')
    switch.hass = hass
    switch._attr_is_on = True
    await switch._async_handle_loop_tick()
    assert fresh.control_settling()
    data.handler.write_float.assert_not_awaited()
    fresh.last_stop_time = 0
    await minimum._async_write_tick()
    data.handler.write_float.assert_awaited_once_with(REG_BATTERY_MIN_SOC, 10)
    await minimum.async_will_remove_from_hass()


@pytest.mark.parametrize('revocation', ['shutdown', 'outage', 'stale'])
async def test_queued_stop_checks_permission_at_real_transport(data, hass, revocation):
    charge, _ems = charge_pair(data, hass)
    data.coordinator.data[REG_TOTAL_ACTIVE_POWER] = -2000
    data.coordinator.data[REG_BATTERY_POWER] = -1000
    client = SimpleNamespace(connected=True, close=Mock(),
                             write_registers=AsyncMock(return_value=SimpleNamespace(isError=lambda: False)))
    handler = KostalModbusHandler('unused', 1502, 71)
    handler._client = client
    handler._unit_kwarg = 'device_id'
    data.handler = handler
    entered = asyncio.Event()
    original = handler.write_float
    async def observed_write(*args):
        entered.set()
        await original(*args)
    handler.write_float = observed_write
    await handler._lock.acquire()
    stopping = asyncio.create_task(charge.async_turn_off())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if revocation == 'shutdown':
            data.shutting_down = True
        elif revocation == 'outage':
            data.mark_communication_lost('test')
        else:
            data.last_control_update -= 1000
        handler._lock.release()
        await asyncio.wait_for(stopping, 1)
        client.write_registers.assert_not_awaited()
        assert not charge.is_on
    finally:
        if handler._lock.locked():
            handler._lock.release()
        await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.parametrize('use_limit', [False, True])
async def test_discharge_hold_above_maximum_uses_1040_without_crossed_soc(data, hass, use_limit):
    healthy(data)
    data.max_soc = 50
    data.hold_use_discharge_limit = use_limit
    switch = KostalBlockDischargeSwitch(data, 'test')
    switch.hass = hass
    switch._attr_is_on = True
    await switch._async_handle_loop_tick()
    writes = [call.args for call in data.handler.write_float.await_args_list]
    assert (REG_BATTERY_MAX_DISCHARGE_POWER_W, 0) in writes
    assert (REG_BATTERY_MIN_SOC, 5) in writes
    assert data.discharge_hold_min_soc == 80
    assert data.effective_min_soc() <= data.max_soc


async def test_lowering_and_raising_maximum_moves_active_hold_between_registers(data, hass):
    data.hold_use_discharge_limit = False
    healthy(data)
    maximum = KostalMaxSocLimitNumber(data, 'test'); maximum.hass = hass
    switch = KostalBlockDischargeSwitch(data, 'test'); switch.hass = hass; switch._attr_is_on = True
    await switch._async_handle_loop_tick()
    data.handler.write_float.reset_mock()
    await maximum.async_set_native_value(50)
    writes = [call.args for call in data.handler.write_float.await_args_list]
    assert writes.index((1040, 0)) < writes.index((1044, 50))
    assert (1042, 5) in writes
    data.handler.write_float.reset_mock()
    await maximum.async_set_native_value(100)
    writes = [call.args for call in data.handler.write_float.await_args_list]
    assert writes.index((1044, 100)) < writes.index((1042, 80)) < writes.index((1040, 6000))
    await maximum.async_will_remove_from_hass()


@pytest.mark.parametrize('queued_writer', ['minimum', 'hold'])
async def test_queued_writer_cannot_observe_unapplied_maximum_increase(data, hass, queued_writer):
    data.hold_use_discharge_limit = False
    healthy(data)
    minimum = KostalMinSocLimitNumber(data, 'test'); minimum.hass = hass
    maximum = KostalMaxSocLimitNumber(data, 'test'); maximum.hass = hass
    await minimum.async_set_native_value(10)
    await maximum.async_set_native_value(50)
    block = KostalBlockDischargeSwitch(data, 'test'); block.hass = hass; block._attr_is_on = True
    await block._async_handle_loop_tick()
    data.handler.write_float.reset_mock()
    await data.control_lock.acquire()
    earlier = asyncio.create_task(minimum._async_write_tick() if queued_writer == 'minimum' else block._async_handle_loop_tick())
    raising = asyncio.create_task(maximum.async_set_native_value(90))
    try:
        await asyncio.sleep(0)
        assert data.max_soc == 50
        data.control_lock.release()
        await asyncio.wait_for(asyncio.gather(earlier, raising), 1)
        writes = [c.args for c in data.handler.write_float.await_args_list]
        raised = writes.index((1044, 90))
        assert all(reg != 1042 or value <= 50 for reg, value in writes[:raised])
        assert (1042, 80) in writes[raised + 1:]
    finally:
        if data.control_lock.locked():
            data.control_lock.release()
        await asyncio.gather(earlier, raising, return_exceptions=True)
        await minimum.async_will_remove_from_hass()
        await maximum.async_will_remove_from_hass()


async def test_failed_maximum_increase_keeps_hold_on_1040_until_acknowledged(data, hass):
    healthy(data)
    data.auto_resume_on_recovery = True
    maximum = KostalMaxSocLimitNumber(data, 'test'); maximum.hass = hass
    await maximum.async_set_native_value(50)
    block = KostalBlockDischargeSwitch(data, 'test'); block.hass = hass; block._attr_is_on = True
    await block._async_handle_loop_tick()
    async def refuse_maximum(reg, value):
        if reg == 1044 and value == 90:
            raise ConnectionError('test')
    data.handler.write_float.side_effect = refuse_maximum
    await maximum.async_set_native_value(90)
    assert data.max_soc == 90 and data.applied_max_soc == 50
    assert block._resume_pending
    assert data.control_settling()
    data.mark_communication_restored()
    data.handler.write_float.side_effect = None
    data.handler.write_float.reset_mock()
    await block._async_handle_loop_tick()
    data.handler.write_float.assert_not_awaited()
    data.last_stop_time = 0  # Opt-in recovery still waits for inverter settling.
    await block._async_handle_loop_tick()
    writes = [c.args for c in data.handler.write_float.await_args_list]
    assert (1040, 0) in writes
    assert all(reg != 1042 or value <= 50 for reg, value in writes)
    await maximum._async_write_tick()
    assert data.applied_max_soc == 90
    await maximum.async_will_remove_from_hass()


@pytest.mark.parametrize('hold_soc', [40, 70])
async def test_pending_minimum_never_crosses_unacknowledged_maximum_in_hold_or_release(data, hass, hold_soc):
    healthy(data)
    minimum = KostalMinSocLimitNumber(data, 'test'); minimum.hass = hass
    maximum = KostalMaxSocLimitNumber(data, 'test'); maximum.hass = hass
    await maximum.async_set_native_value(50)
    data.mark_communication_lost('test')
    await maximum.async_set_native_value(90)
    await minimum.async_set_native_value(65)
    assert data.applied_max_soc == 50
    data.mark_communication_restored()
    block = KostalBlockDischargeSwitch(data, 'test'); block.hass = hass
    data.handler.write_float.reset_mock()
    await block._hold_discharge_at(hold_soc)
    await block._release_discharge_hold()
    assert all(c.args[0] != 1042 or c.args[1] <= 50 for c in data.handler.write_float.await_args_list)
    await maximum._async_write_tick()
    await minimum._async_write_tick()
    writes = [c.args for c in data.handler.write_float.await_args_list]
    assert writes.index((1044, 90)) < writes.index((1042, 65))
    await minimum.async_will_remove_from_hass()
    await maximum.async_will_remove_from_hass()


@pytest.mark.parametrize('fault', ['poll', 'stale_stop', 'runtime'])
async def test_stopped_hold_does_not_reappear_via_minimum_keepalive(data, hass, fault):
    healthy(data)
    minimum = KostalMinSocLimitNumber(data, 'test'); minimum.hass = hass
    await minimum.async_set_native_value(10)
    block = KostalBlockDischargeSwitch(data, 'test'); block.hass = hass; block._attr_is_on = True
    await block._async_handle_loop_tick()
    if fault == 'poll':
        data.mark_communication_lost('test')
        data.notify_connection_lost()
    elif fault == 'stale_stop':
        data.last_control_update -= 1000
    else:
        await block._handle_runtime_fault(ConnectionError('test'), 'test')
    await block.async_turn_off()
    assert data.discharge_hold_min_soc is None
    data.mark_communication_restored()
    data.last_control_update = time.monotonic()
    data.last_stop_time = 0
    data.handler.write_float.reset_mock()
    await minimum._async_write_tick()
    data.handler.write_float.assert_awaited_once_with(1042, 10)
    await minimum.async_will_remove_from_hass()


async def test_redundant_stop_of_background_hold_establishes_write_silence(data, hass):
    charge, _ems = charge_pair(data, hass)
    charge._attr_is_on = False
    states = {PREDBAT_MODE_ENTITY: SimpleNamespace(state='Control charge'),
              PREDBAT_RESERVE_ENTITY: SimpleNamespace(state='60')}
    hass.states.get = states.get
    await charge._async_reserve_hold_tick()
    assert data.discharge_hold_min_soc == 60
    states[PREDBAT_MODE_ENTITY].state = 'Monitor'
    await charge.async_turn_off()
    assert data.discharge_hold_min_soc is None
    assert data.control_settling()
    data.handler.write_float.reset_mock()
    await charge._async_reserve_hold_tick()
    data.handler.write_float.assert_not_awaited()


async def test_stop_detects_background_hold_established_while_waiting_for_lock(data, hass):
    charge, _ems = charge_pair(data, hass)
    charge._attr_is_on = False
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def delayed_hold():
        entered.set()
        await proceed.wait()
        await charge._hold_discharge_at(70)
        charge._reserve_holding = True
    charge._evaluate_reserve_hold = delayed_hold
    holding = asyncio.create_task(charge._async_reserve_hold_tick())
    await asyncio.wait_for(entered.wait(), 1)
    stopping = asyncio.create_task(charge.async_turn_off())
    try:
        await asyncio.sleep(0)
        assert not stopping.done()
        proceed.set()
        await asyncio.wait_for(asyncio.gather(holding, stopping), 1)
        assert data.discharge_hold_min_soc is None
        assert data.control_settling()
    finally:
        proceed.set()
        await asyncio.gather(holding, stopping, return_exceptions=True)


@pytest.mark.parametrize('failure', ['stale', 'outage', 'write_error'])
async def test_inactive_soc_release_is_retried_until_success(data, hass, failure):
    healthy(data)
    minimum = KostalMinSocLimitNumber(data, 'test'); minimum.hass = hass
    maximum = KostalMaxSocLimitNumber(data, 'test'); maximum.hass = hass
    await minimum.async_set_native_value(20)
    await maximum.async_set_native_value(80)
    if failure == 'stale':
        data.last_control_update -= 1000
    elif failure == 'outage':
        data.mark_communication_lost('test')
    else:
        data.handler.write_float.side_effect = ConnectionError('test')
    await maximum.async_set_native_value(100)
    assert maximum._release_pending and maximum._remove_timer is not None
    data.handler.write_float.side_effect = None
    data.handler.write_float.reset_mock()
    data.mark_communication_restored()
    data.last_control_update = time.monotonic()
    await minimum._async_write_tick()
    await maximum._async_write_tick()
    assert (1044, 100) in [c.args for c in data.handler.write_float.await_args_list]
    assert not maximum._release_pending and maximum._remove_timer is None
    await minimum.async_will_remove_from_hass()
    await maximum.async_will_remove_from_hass()


async def test_enabled_ems_restores_and_blocks_charge_until_monitor_ready(data, hass):
    charge, ems = charge_pair(data, hass)
    ems.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state='on'))
    await ems.async_added_to_hass()
    assert ems.is_on
    assert charge._charge_target_watts() == 0
    await ems._start_task
    assert 0 < charge._charge_target_watts() <= 1725
    await ems.async_turn_off()


@pytest.mark.parametrize('battery_power,expected', [(0, 1725), (-2000, 3725), (6000, 0)])
async def test_ems_limits_use_measured_signed_power(data, hass, battery_power, expected):
    charge, ems = charge_pair(data, hass)
    ems._attr_is_on = True
    data.coordinator.data[REG_BATTERY_POWER] = battery_power
    await ems._async_handle_loop_tick()
    assert charge._charge_target_watts() == expected
    await charge._async_handle_loop_tick()
    assert data.handler.write_float.await_args.args == (1034, -expected)


async def test_ems_decreases_immediately_even_before_its_next_timer(data, hass):
    charge, ems = charge_pair(data, hass)
    ems._attr_is_on = True
    ems._ems_smoothed_limit = data.ems_charge_limit_w = 6000
    assert charge._charge_target_watts() == 1725
    await ems._async_handle_loop_tick()
    assert data.ems_charge_limit_w == 1725


@pytest.mark.parametrize('loss', ['mode_unavailable', 'mode_missing', 'target_unavailable', 'target_nan', 'target_out_of_range'])
@pytest.mark.parametrize('initial_target', ['40', '90'])
async def test_predbat_input_loss_never_becomes_manual_charge(data, hass, loss, initial_target):
    charge, _ems = charge_pair(data, hass)
    states = {PREDBAT_MODE_ENTITY: SimpleNamespace(state='Control charge'),
              'predbat.best_charge_limit': SimpleNamespace(state=initial_target)}
    hass.states.get = states.get
    await charge._async_handle_loop_tick()
    if loss == 'mode_missing':
        states.pop(PREDBAT_MODE_ENTITY)
    elif loss == 'mode_unavailable':
        states[PREDBAT_MODE_ENTITY].state = 'unavailable'
    else:
        states['predbat.best_charge_limit'].state = {
            'target_unavailable': 'unavailable', 'target_nan': 'nan', 'target_out_of_range': '110'}[loss]
    data.handler.write_float.reset_mock()
    await charge._async_handle_loop_tick()
    assert not charge.is_on
    assert data.control_settling()
    assert data.discharge_hold_min_soc is None
    minimum = KostalMinSocLimitNumber(data, 'test'); minimum.hass = hass
    await minimum.async_set_native_value(20)
    data.handler.write_float.assert_not_awaited()
    await minimum.async_will_remove_from_hass()

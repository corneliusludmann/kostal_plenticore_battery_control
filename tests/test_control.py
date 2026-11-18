"""Real switch/coordinator logic with mocked HA and Modbus boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.kostal_modbus_control.const import REG_BATTERY_SOC, REG_BATTERY_POWER
from custom_components.kostal_modbus_control.switch import (
    KostalChargeStartSwitch, KostalInverterControlSwitch,
)


def active_charge(data, hass):
    data.last_control_update = time.monotonic()
    data.control_values_valid = True
    data.coordinator = SimpleNamespace(data={REG_BATTERY_SOC: 50, REG_BATTERY_POWER: 0},
                                       last_update_success=True)
    data.charge_power_fixed_w = 1000
    data.inverter_max_power_w = 1000
    switch = KostalChargeStartSwitch(data, 'test')
    switch.hass = hass
    switch._attr_is_on = True
    return switch


async def test_stop_drains_inflight_periodic_charge(data, hass):
    """A stop must not return before the old cycle can issue its last write."""
    switch = active_charge(data, hass)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_restore():
        entered.set()
        await release.wait()
    data.async_restore_discharge_limit = blocked_restore
    writes = []
    async def record_write(register, value):
        writes.append(value)
    data.handler.write_float.side_effect = record_write
    running = asyncio.create_task(switch._async_handle_loop_tick())
    await asyncio.wait_for(entered.wait(), 1)
    stopping = asyncio.create_task(switch.async_turn_off())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stopping.done(), 'Stop returned while a charge cycle could still write'
        release.set()
        await asyncio.wait_for(asyncio.gather(running, stopping), 1)
        completed = list(writes)
        await switch._async_handle_loop_tick()
        assert writes == completed
        assert writes[-1] >= 0, 'Last command after completed stop must not request charge'
        assert not switch.is_on
    finally:
        release.set()
        await asyncio.gather(running, stopping, return_exceptions=True)


async def test_unload_drains_inflight_charge(data, hass):
    switch = active_charge(data, hass)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_restore():
        entered.set()
        await release.wait()
    data.async_restore_discharge_limit = blocked_restore
    running = asyncio.create_task(switch._async_handle_loop_tick())
    await asyncio.wait_for(entered.wait(), 1)
    removing = asyncio.create_task(switch.async_will_remove_from_hass())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not removing.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(running, removing), 1)
        count = data.handler.write_float.await_count
        await switch._async_handle_loop_tick()
        assert data.handler.write_float.await_count == count
        assert not switch.is_on
    finally:
        release.set()
        await asyncio.gather(running, removing, return_exceptions=True)


@pytest.mark.parametrize('age', [61, 3600])
async def test_stale_grid_must_not_replay_old_setpoint(data, hass, age):
    data.source_grid_power_entity = 'sensor.grid'
    data.control_max_data_age = 60
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age)
    hass.states.get = lambda _: SimpleNamespace(state='200', last_reported=stamp, last_updated=stamp)
    switch = KostalInverterControlSwitch(data, 'test')
    switch.hass = hass
    switch._last_grid_reading = stamp
    data.last_inverter_control_setpoint_w = -1000
    with pytest.raises(ValueError):
        await switch._calculate_and_write()
    data.handler.write_float.assert_not_awaited()


async def test_fresh_unchanged_grid_can_repeat_setpoint(data, hass):
    data.source_grid_power_entity = 'sensor.grid'
    stamp = datetime.now(timezone.utc)
    hass.states.get = lambda _: SimpleNamespace(state='200', last_reported=stamp, last_updated=stamp)
    switch = KostalInverterControlSwitch(data, 'test')
    switch.hass = hass
    switch._last_grid_reading = stamp
    data.last_inverter_control_setpoint_w = -1000
    data.inverter_max_power_w = 2000
    await switch._calculate_and_write()
    data.handler.write_float.assert_awaited_once_with(data.charge_discharge_reg, -1000.0)


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', 'unavailable', 'unknown'])
async def test_invalid_grid_never_sends_control_value(data, hass, value):
    data.source_grid_power_entity = 'sensor.grid'
    stamp = datetime.now(timezone.utc)
    hass.states.get = lambda _: SimpleNamespace(state=value, last_reported=stamp, last_updated=stamp)
    switch = KostalInverterControlSwitch(data, 'test')
    switch.hass = hass
    with pytest.raises(ValueError):
        await switch._calculate_and_write()
    data.handler.write_float.assert_not_awaited()


@pytest.mark.parametrize('age,healthy,expected', [(0, True, True), (61, True, False),
                                                 (0, False, False)])
def test_control_data_age_and_health(data, age, healthy, expected):
    data.last_control_update = time.monotonic() - age
    data.communication_ok = healthy
    data.control_values_valid = True
    assert data.control_data_fresh() is expected


def test_no_successful_poll_is_not_fresh(data):
    assert not data.control_data_fresh()


async def test_stale_coordinator_blocks_charge(data, hass):
    switch = active_charge(data, hass)
    data.last_control_update = time.monotonic() - 3600
    await switch._async_handle_loop_tick()
    data.handler.write_float.assert_not_awaited()


def test_recovery_requires_explicit_opt_in(data, hass):
    switch = active_charge(data, hass)
    switch._resume_pending = True
    switch._faulted = True
    switch._schedule_start_loop = Mock()
    switch.handle_connection_restored()
    switch._schedule_start_loop.assert_not_called()


def test_opted_in_recovery_schedules_resume(data, hass):
    data.auto_resume_on_recovery = True
    switch = active_charge(data, hass)
    switch._resume_pending = True
    switch._faulted = True
    switch._schedule_start_loop = Mock()
    switch.handle_connection_restored()
    switch._schedule_start_loop.assert_called_once()


async def test_transport_guard_revokes_charge_queued_behind_connect(data, hass):
    """Exercise actual switch AND handler; only the socket client is replaced."""
    from custom_components.kostal_modbus_control.modbus_handler import KostalModbusHandler
    from unittest.mock import AsyncMock
    entered, release = asyncio.Event(), asyncio.Event()
    response = SimpleNamespace(isError=lambda: False)
    client = SimpleNamespace(connected=False, close=Mock(),
                             write_registers=AsyncMock(return_value=response))
    async def connect():
        entered.set()
        await release.wait()
        client.connected = True
        return True
    client.connect = connect
    handler = KostalModbusHandler('unit-test.invalid', 1502, 71)
    handler._client = client
    handler._unit_kwarg = 'device_id'
    data.handler = handler
    switch = active_charge(data, hass)
    running = asyncio.create_task(switch._async_handle_loop_tick())
    await asyncio.wait_for(entered.wait(), 1)
    stopping = asyncio.create_task(switch.async_turn_off())
    try:
        await asyncio.sleep(0)
        assert not switch.is_on
        release.set()
        await asyncio.wait_for(asyncio.gather(running, stopping), 1)
        # The revoked -1000 W command never reaches the transport. Only stop=0.
        assert client.write_registers.await_count == 1
        assert client.write_registers.await_args.kwargs['values'] == [0, 0]
    finally:
        release.set()
        await asyncio.gather(running, stopping, return_exceptions=True)


async def test_charge_expiry_stops_and_cannot_recover(data, hass):
    data.auto_resume_on_recovery = True
    switch = active_charge(data, hass)
    switch._charge_deadline = time.monotonic() - 1
    await switch._expire_charge()
    assert not switch.is_on
    switch._schedule_start_loop = Mock()
    switch.handle_connection_restored()
    switch._schedule_start_loop.assert_not_called()


async def test_recovery_does_not_extend_original_charge_deadline(data, hass):
    data.auto_resume_on_recovery = True
    switch = active_charge(data, hass)
    deadline = time.monotonic() + 300
    switch._charge_deadline = deadline
    switch._resume_pending = True
    switch._schedule_start_loop = Mock()
    switch.handle_connection_restored()
    switch._schedule_start_loop.assert_called_once()
    assert switch._charge_deadline == deadline


async def test_manual_start_creates_bounded_session(data, hass):
    switch = active_charge(data, hass)
    switch._attr_is_on = False
    switch._schedule_start_loop = Mock()
    data.charge_max_duration_minutes = 10
    before = time.monotonic()
    await switch.async_turn_on()
    after = time.monotonic()
    assert before + 600 <= switch._charge_deadline <= after + 600
    original = switch._charge_deadline
    await switch.async_turn_on()
    assert switch._charge_deadline == original


async def test_unhealthy_start_rejected_without_write(data, hass):
    switch = active_charge(data, hass)
    switch._attr_is_on = False
    data.control_values_valid = False
    with pytest.raises(ValueError):
        await switch.async_turn_on()
    assert not switch.is_on
    data.handler.write_float.assert_not_awaited()


@pytest.mark.parametrize('soc,power,mode,expected', [
    (50, -500, 2, True), (None, 0, 2, False), (float('nan'), 0, 2, False),
    (101, 0, 2, False), (-1, 0, 2, False), (50, None, 2, False),
    (50, 0, 0, False), (50, 0, None, False),
])
async def test_coordinator_only_marks_usable_control_values_fresh(data, hass, soc, power, mode, expected):
    from custom_components.kostal_modbus_control import KostalCoordinator
    from custom_components.kostal_modbus_control.const import REG_BATTERY_MGMT_MODE
    from unittest.mock import AsyncMock
    values = {REG_BATTERY_SOC: soc, REG_BATTERY_POWER: power, REG_BATTERY_MGMT_MODE: mode}
    # All other registers are optional and may be missing without stopping reads.
    data.handler.read_float = AsyncMock(side_effect=lambda address: values.get(address))
    data.handler.read_int16 = AsyncMock(side_effect=lambda address: values.get(address))
    data.handler.read_uint8 = AsyncMock(side_effect=lambda address: values.get(address))
    for name in ('read_uint16', 'read_uint32', 'read_uint32_big_endian', 'read_string'):
        setattr(data.handler, name, AsyncMock(return_value=None))
    coordinator = KostalCoordinator(hass, data.handler, data, 'unit-test.invalid')
    await coordinator._async_update_data()
    assert data.control_data_fresh() is expected


@pytest.mark.parametrize('expires_during_settle', [False, True])
async def test_real_start_loop_waits_for_settling_and_rechecks_deadline(data, hass, monkeypatch, expires_during_settle):
    import custom_components.kostal_modbus_control.switch as switches
    from types import ModuleType
    switch = active_charge(data, hass)
    switch._charge_deadline = time.monotonic() + 300
    data.last_stop_time = time.time()
    entered, release = asyncio.Event(), asyncio.Event()
    durations = []
    async def controlled_sleep(delay):
        durations.append(delay)
        entered.set()
        await release.wait()
        data.last_stop_time = 0  # Simulate completed inverter settling period.
        if expires_during_settle:
            switch._charge_deadline = time.monotonic() - 1
    proxy = ModuleType('controlled_asyncio')
    proxy.__dict__.update(asyncio.__dict__)
    proxy.sleep = controlled_sleep
    monkeypatch.setattr(switches, 'asyncio', proxy)
    running = asyncio.create_task(switch._run_start_loop('test start'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert durations[0] > 0
        data.handler.write_float.assert_not_awaited()
        release.set()
        await asyncio.wait_for(running, 1)
        if expires_during_settle:
            data.handler.write_float.assert_not_awaited()
            assert not switch.is_on
            assert switch._remove_timer is None
        else:
            data.handler.write_float.assert_awaited()
            assert switch._remove_timer is not None
        assert switch._start_task is None
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


async def test_actual_fault_recovery_preserves_deadline_then_expiry_blocks_restart(data, hass):
    data.auto_resume_on_recovery = True
    switch = active_charge(data, hass)
    deadline = time.monotonic() + 300
    switch._charge_deadline = deadline
    await switch._handle_runtime_fault(ConnectionError('test loss'), 'test')
    assert switch.is_on and switch._resume_pending
    assert not data.communication_ok
    assert switch._charge_deadline == deadline
    data.mark_communication_restored()
    data.last_stop_time = 0
    data.last_control_update = time.monotonic()
    switch.handle_connection_restored()
    task = switch._start_task
    assert task is not None
    await asyncio.wait_for(task, 1)
    assert switch._charge_deadline == deadline
    assert switch._remove_timer is not None
    assert not switch._resume_pending
    await switch._handle_runtime_fault(ConnectionError('second loss'), 'test')
    switch._charge_deadline = time.monotonic() - 1
    data.mark_communication_restored()
    data.last_stop_time = 0
    data.handler.write_float.reset_mock()
    switch.handle_connection_restored()
    assert switch._start_task is None
    await switch._expire_charge()
    assert not switch.is_on
    # Expiry may issue a stop, but never a negative charge command.
    assert all(call.args[1] >= 0 for call in data.handler.write_float.await_args_list)


async def test_unload_entry_revokes_queued_action_before_platform_teardown(data, hass):
    from custom_components.kostal_modbus_control import async_unload_entry
    from custom_components.kostal_modbus_control.const import DOMAIN
    from unittest.mock import AsyncMock
    switch = active_charge(data, hass)
    entered, release = asyncio.Event(), asyncio.Event()
    async def unload_platforms(entry, platforms):
        entered.set()
        await release.wait()
        await switch.async_will_remove_from_hass()
        return True
    data.coordinator.async_shutdown = AsyncMock()
    hass.config_entries = SimpleNamespace(async_unload_platforms=unload_platforms)
    hass.data = {DOMAIN: {'test': data}}
    await data.control_lock.acquire()
    queued = asyncio.create_task(switch._async_handle_loop_tick())
    unload = asyncio.create_task(async_unload_entry(hass, SimpleNamespace(entry_id='test')))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert data.shutting_down
        data.control_lock.release()
        await asyncio.wait_for(queued, 1)
        data.handler.write_float.assert_not_awaited()
        release.set()
        assert await asyncio.wait_for(unload, 1)
        assert 'test' not in hass.data[DOMAIN]
    finally:
        if data.control_lock.locked():
            data.control_lock.release()
        release.set()
        await asyncio.gather(queued, unload, return_exceptions=True)


async def test_disabling_ems_monitor_does_not_pause_charge_writer(data, hass):
    from custom_components.kostal_modbus_control.switch import KostalEMSSwitch
    charge = active_charge(data, hass)
    ems = KostalEMSSwitch(data, 'test', charge)
    ems.hass = hass
    ems._attr_is_on = True
    data.last_stop_time = 0
    data.ems_status = 'Protecting'
    data.ems_charge_limit_w = 100
    await ems.async_turn_off()
    assert not ems.is_on
    assert data.ems_charge_limit_w is None
    assert data.last_stop_time == 0
    await charge._async_handle_loop_tick()
    data.handler.write_float.assert_awaited_once_with(data.charge_discharge_reg, -1000.0)


async def test_failed_coordinator_poll_revokes_active_controls_immediately(data, hass):
    from custom_components.kostal_modbus_control import KostalCoordinator
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from unittest.mock import AsyncMock
    switch = active_charge(data, hass)
    switch._remove_timer = Mock()
    data.handler.read_float = AsyncMock(side_effect=ConnectionError('disconnected'))
    coordinator = KostalCoordinator(hass, data.handler, data, 'unit-test.invalid')
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert not data.communication_ok
    assert not switch.is_on
    assert switch._remove_timer is None
    assert switch._faulted
    data.handler.write_float.assert_not_awaited()

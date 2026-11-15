"""Validation and transport regression tests using the actual integration classes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.kostal_modbus_control.config_flow import validate_control_options
from custom_components.kostal_modbus_control.modbus_handler import (
    CONTROL_WRITE_GUARD, KostalModbusHandler, KostalModbusConnectionError,
    KostalModbusWriteAborted, validate_modbus_timeout,
)
from custom_components.kostal_modbus_control.number import (
    KostalMinSocLimitNumber, KostalMaxSocLimitNumber,
)


@pytest.mark.parametrize('value', [-1, 0, 9, 3601, True, 30.5, '30', float('nan'), float('inf')])
def test_invalid_timeouts(value):
    with pytest.raises(ValueError):
        validate_modbus_timeout(value)


@pytest.mark.parametrize('value', [10, 30, 3600])
def test_valid_timeouts(value):
    assert validate_modbus_timeout(value) == value


@pytest.mark.parametrize('options', [
    {'control_max_data_age': float('nan')}, {'control_max_data_age': 9},
    {'charge_max_duration_minutes': 0}, {'charge_max_duration_minutes': 1.5},
    {'auto_resume_on_recovery': 'false'}, {'external_control_ema_alpha': float('inf')},
])
def test_invalid_options(options):
    with pytest.raises(ValueError):
        validate_control_options(options)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), 1e100])
async def test_invalid_floats_never_connect(value):
    handler = KostalModbusHandler('unused', 1502, 71)
    handler._ensure_connected = AsyncMock()
    with pytest.raises(ValueError):
        await handler.write_float(1034, value)
    handler._ensure_connected.assert_not_called()


@pytest.mark.parametrize('byte_order,expected', [(0, [0, 50304]), (1, [50304, 0])])
def test_float_encoding_known_negative_setpoint(byte_order, expected):
    handler = KostalModbusHandler('unused', 1502, 71)
    handler.set_modbus_byte_order(byte_order)
    assert handler._float_to_register_words(-1024) == expected


async def test_unknown_unit_api_never_sends():
    client = SimpleNamespace(connected=True, close=lambda: None)
    async def unknown_read(address, count):
        pytest.fail('request sent without an explicit unit ID')
    client.read_holding_registers = unknown_read
    client.write_registers = AsyncMock()
    handler = KostalModbusHandler('unused', 1502, 71)
    handler._client = client
    with pytest.raises(KostalModbusConnectionError):
        await handler.write_float(1034, -1000)
    client.write_registers.assert_not_called()


@pytest.mark.parametrize('method,args', [('write_float', (1034, -1000)), ('write_register', (1, 0))])
async def test_guard_rechecked_after_transport_lock(method, args):
    client = SimpleNamespace(connected=True, close=lambda: None)
    async def read(address, *, count, device_id):
        pass
    client.read_holding_registers = read
    client.write_registers = AsyncMock()
    client.write_register = AsyncMock()
    handler = KostalModbusHandler('unused', 1502, 71)
    handler._client = client
    authorized = True
    token = CONTROL_WRITE_GUARD.set(lambda: authorized)
    await handler._lock.acquire()
    try:
        task = asyncio.create_task(getattr(handler, method)(*args))
        await asyncio.sleep(0)
        authorized = False
        handler._lock.release()
        with pytest.raises(KostalModbusWriteAborted):
            await task
    finally:
        CONTROL_WRITE_GUARD.reset(token)
    client.write_registers.assert_not_called()
    client.write_register.assert_not_called()
    assert handler._client is client  # authorization failure is not a transport fault


def limits(data, hass):
    minimum = KostalMinSocLimitNumber(data, 'test')
    maximum = KostalMaxSocLimitNumber(data, 'test')
    minimum.hass = maximum.hass = hass
    return minimum, maximum


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 101])
async def test_soc_invalid_value_has_no_effect(data, hass, value):
    minimum, maximum = limits(data, hass)
    with pytest.raises(ValueError):
        await minimum.async_set_native_value(value)
    assert data.min_soc == 5
    data.handler.write_float.assert_not_called()


async def test_crossed_soc_limits_rejected_without_mutation(data, hass):
    minimum, maximum = limits(data, hass)
    data.max_soc = 50
    with pytest.raises(ValueError):
        await minimum.async_set_native_value(60)
    assert data.min_soc == 5
    data.min_soc = 40
    with pytest.raises(ValueError):
        await maximum.async_set_native_value(30)
    assert data.max_soc == 50
    data.handler.write_float.assert_not_called()


async def test_queued_limit_does_not_write_after_stop(data, hass):
    minimum, maximum = limits(data, hass)
    minimum._attr_native_value = 20
    settling = False
    data.control_settling = lambda: settling
    data.control_data_fresh = lambda: True
    await data.control_lock.acquire()
    task = asyncio.create_task(minimum._async_write_limit())
    await asyncio.sleep(0)
    settling = True
    data.control_lock.release()
    await task
    data.handler.write_float.assert_not_called()


async def test_stale_coordinator_blocks_limit_keepalive(data, hass):
    minimum, maximum = limits(data, hass)
    data.control_data_fresh = lambda: False
    await minimum._async_write_limit()
    data.handler.write_float.assert_not_called()


async def test_removed_limit_blocks_queued_write(data, hass):
    minimum, maximum = limits(data, hass)
    data.control_data_fresh = lambda: True
    await minimum.async_will_remove_from_hass()
    await minimum._async_write_limit()
    data.handler.write_float.assert_not_called()


@pytest.mark.parametrize('value', [float('nan'), 200])
async def test_invalid_restore_does_not_start_limit_writer(data, hass, value):
    minimum, maximum = limits(data, hass)
    minimum.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=value))
    await minimum.async_added_to_hass()
    assert data.min_soc == 5
    assert minimum._remove_timer is None
    data.handler.write_float.assert_not_called()


async def test_invalid_restored_pair_blocks_both_writers(data, hass):
    minimum, maximum = limits(data, hass)
    data.control_data_fresh = lambda: True
    minimum.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=60))
    maximum.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=50))
    await minimum.async_added_to_hass()
    await maximum.async_added_to_hass()
    if minimum._write_task is not None:
        await minimum._write_task
    await maximum._async_write_limit()
    assert not data.soc_limits_valid
    data.handler.write_float.assert_not_called()
    await minimum.async_will_remove_from_hass()
    await maximum.async_will_remove_from_hass()


@pytest.mark.parametrize('order', [False, True])
async def test_valid_restored_pair_in_either_order(data, hass, order):
    minimum, maximum = limits(data, hass)
    data.control_data_fresh = lambda: True
    minimum.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=80))
    maximum.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=90))
    entities = (maximum, minimum) if order else (minimum, maximum)
    for entity in entities:
        await entity.async_added_to_hass()
    for entity in entities:
        if entity._write_task is not None:
            await entity._write_task
    assert (data.min_soc, data.max_soc) == (80, 90)
    assert getattr(data, 'soc_limits_valid', True)
    for entity in entities:
        await entity.async_will_remove_from_hass()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 1e10])
async def test_power_and_fuse_reject_invalid_values(data, value):
    from custom_components.kostal_modbus_control.number import KostalPowerRateNumber, KostalFuseSizeNumber
    rate = KostalPowerRateNumber(data, 'test', 'charge', 'Charge', 'charge_power_fixed_w')
    fuse = KostalFuseSizeNumber(data, 'test')
    old_fuse = data.fuse_size
    with pytest.raises(ValueError):
        await rate.async_set_native_value(value)
    with pytest.raises(ValueError):
        await fuse.async_set_native_value(value)
    assert data.charge_power_fixed_w is None
    assert data.fuse_size == old_fuse


@pytest.mark.parametrize('disabled', [set(), {'min_soc_limit'}, {'max_soc_limit'}, {'min_soc_limit', 'max_soc_limit'}])
async def test_platform_restore_barrier_handles_disabled_entities(data, hass, monkeypatch, disabled):
    from custom_components.kostal_modbus_control import number
    from custom_components.kostal_modbus_control.const import DOMAIN
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: unique_id,
        async_get=lambda entity_id: SimpleNamespace(
            disabled_by='user' if entity_id.removeprefix('test_') in disabled else None),
    )
    monkeypatch.setattr(number.er, 'async_get', lambda hass: registry)
    hass.data = {DOMAIN: {'test': data}}
    entities = []
    await number.async_setup_entry(hass, SimpleNamespace(entry_id='test'), entities.extend)
    active = [entity for entity in entities[-2:] if entity._key not in disabled]
    data.control_data_fresh = lambda: True
    for index, entity in enumerate(active):
        entity.hass = hass
        entity.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(
            native_value=60 if entity._data_attr == 'min_soc' else 50))
        await entity.async_added_to_hass()
        # Allow scheduled writers/timer callbacks to run before the second
        # entity finishes restoring; no partial pair may reach the transport.
        if index < len(active) - 1:
            await entity._async_write_tick()
            await asyncio.sleep(0)
            assert not data.soc_limits_ready
            data.handler.write_float.assert_not_called()
    assert data.soc_limits_ready
    for entity in active:
        if entity._write_task is not None:
            await entity._write_task
    if not disabled:
        assert not data.soc_limits_valid
        data.handler.write_float.assert_not_called()
    for entity in active:
        await entity.async_will_remove_from_hass()


async def test_number_unload_cancels_periodic_write_waiting_for_real_transport(data, hass):
    minimum, maximum = limits(data, hass)
    data.control_data_fresh = lambda: True
    minimum._attr_native_value = 20
    connecting = asyncio.Event()
    continue_connect = asyncio.Event()
    async def connect():
        connecting.set()
        await continue_connect.wait()
        return True
    async def read(address, *, count, device_id):
        pass
    client = SimpleNamespace(connected=False, connect=connect, close=AsyncMock(),
                             read_holding_registers=read, write_registers=AsyncMock())
    # pymodbus close is synchronous.
    from unittest.mock import Mock
    client.close = Mock()
    handler = KostalModbusHandler('unused', 1502, 71)
    handler._client = client
    data.handler = handler
    periodic = asyncio.create_task(minimum._async_write_tick())
    await connecting.wait()
    await minimum.async_will_remove_from_hass()
    assert periodic.done()
    assert periodic.cancelled()
    continue_connect.set()
    await minimum._async_write_tick()
    client.write_registers.assert_not_called()
    client.close.assert_called_once()
    assert handler._client is None
    assert not minimum._active_write_tasks

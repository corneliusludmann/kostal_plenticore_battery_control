"""Restart and unload regressions for fixed-power battery control."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.kostal_modbus_control import KostalCoordinator, async_unload_entry
from custom_components.kostal_modbus_control.const import (
    DOMAIN, REG_SOFTWARE_VERSION, REG_BATTERY_SOC, REG_BATTERY_VOLTAGE,
    REG_BATTERY_AC_POWER_SETPOINT_W, REG_BATTERY_MAX_DISCHARGE_LIMIT,
)
from custom_components.kostal_modbus_control.modbus_handler import KostalModbusHandler
from custom_components.kostal_modbus_control.number import KostalPowerRateNumber


@pytest.mark.parametrize('direction', ['charge', 'discharge'])
@pytest.mark.parametrize('source', ['learned_voltage', 'bms_derating'])
async def test_fixed_power_survives_lower_restart_maximum(data, direction, source):
    data.inverter_max_power_w = 10000
    readings = {REG_SOFTWARE_VERSION: '1.0', REG_BATTERY_SOC: 70,
                REG_BATTERY_VOLTAGE: 350, REG_BATTERY_MAX_DISCHARGE_LIMIT: 6000}
    data.coordinator = SimpleNamespace(data=readings, async_add_listener=lambda cb: lambda: None)
    if source == 'bms_derating':
        data.charge_discharge_reg = REG_BATTERY_AC_POWER_SETPOINT_W
    attr = f'{direction}_power_fixed_w'
    saved = 8000 if source == 'learned_voltage' else 5000
    original = KostalPowerRateNumber(data, 'test', direction, direction, attr)
    await original.async_set_native_value(saved)
    if source == 'learned_voltage':
        readings.update({REG_BATTERY_SOC: 100, REG_BATTERY_VOLTAGE: 400})
        data.update_observed_battery_current(readings)
    else:
        readings[REG_BATTERY_MAX_DISCHARGE_LIMIT] = 4000
    restored = KostalPowerRateNumber(data, 'test', direction, direction, attr)
    restored.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(
        native_value=saved, native_unit_of_measurement='W'))
    restored.async_get_last_state = AsyncMock(return_value=SimpleNamespace(
        attributes={'follows_max': False}))
    await restored.async_added_to_hass()
    assert restored.native_max_value < saved
    assert restored.native_value == saved
    assert not restored.extra_state_attributes['follows_max']
    assert data.clamp_power_setpoint_w(-saved) == -restored.native_max_value
    assert data.clamp_power_setpoint_w(saved) == restored.native_max_value
    if source == 'bms_derating':
        readings[REG_BATTERY_MAX_DISCHARGE_LIMIT] = 6000
        restored._handle_coordinator_update()
        assert restored.native_value == saved
        assert data.clamp_power_setpoint_w(saved) == saved
    # Explicit new input still respects the currently advertised range.
    with pytest.raises(ValueError):
        await restored.async_set_native_value(restored.native_max_value + 100)
    data.handler.write_float.assert_not_awaited()


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), -float('inf'), 'invalid'])
async def test_invalid_power_restore_remains_zero(data, value):
    rate = KostalPowerRateNumber(data, 'test', 'charge', 'Charge', 'charge_power_fixed_w')
    rate.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(
        native_value=value, native_unit_of_measurement='W'))
    await rate.async_added_to_hass()
    assert rate.native_value == 0
    assert not rate.extra_state_attributes['follows_max']


class BlockingClient:
    connected = True

    def __init__(self, events, started, cancelled):
        self.events, self.started, self.cancelled = events, started, cancelled

    def close(self):
        self.events.append('close')

    async def read_holding_registers(self, address, *, count, device_id):
        self.events.append(('read', address))
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.events.append('read finished')
            self.cancelled.set()


@pytest.mark.parametrize('separate_meter', [False, True])
async def test_unload_cancels_entire_poll_before_closing_transports(data, hass, separate_meter):
    events, started, cancelled = [], asyncio.Event(), asyncio.Event()
    handler = KostalModbusHandler('unused.invalid', 1502, 71)
    handler._client = BlockingClient(events, started, cancelled)
    if separate_meter:
        data.ksem_handler = handler
        data.handler = AsyncMock()
        data.handler.read_uint16_scale_pair.return_value = (10000, 0)
        data.handler.read_float.return_value = 50
        data.handler.read_string.return_value = '1.0'
        for method in ('read_int16', 'read_uint8', 'read_uint16', 'read_uint32', 'read_uint32_big_endian'):
            getattr(data.handler, method).return_value = 0
    else:
        data.handler = handler
    coordinator = KostalCoordinator(hass, data.handler, data, 'unused.invalid')
    data.coordinator = coordinator
    hass.data = {DOMAIN: {'test': data}}
    hass.config_entries = SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
    poll = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(started.wait(), 1)
    assert await asyncio.wait_for(async_unload_entry(hass, SimpleNamespace(entry_id='test')), 1)
    with pytest.raises(asyncio.CancelledError):
        await poll
    assert cancelled.is_set()
    assert events == [events[0], 'read finished', 'close']
    assert events[0][0] == 'read'
    assert handler._client is None
    assert not coordinator._poll_tasks
    assert 'test' not in hass.data[DOMAIN]
    # Even a queued/direct refresh cannot reopen a closed handler.
    with pytest.raises(UpdateFailed, match='shutting down'):
        await coordinator._async_update_data()
    await coordinator.async_shutdown()  # HA may invoke its registered hook too.
    assert handler._client is None


async def test_failed_platform_unload_leaves_polling_available(data, hass):
    coordinator = KostalCoordinator(hass, data.handler, data, 'unused.invalid')
    coordinator._async_poll_data = AsyncMock(return_value={'still': 'running'})
    data.coordinator = coordinator
    hass.data = {DOMAIN: {'test': data}}
    hass.config_entries = SimpleNamespace(async_unload_platforms=AsyncMock(return_value=False))
    assert not await async_unload_entry(hass, SimpleNamespace(entry_id='test'))
    assert not data.shutting_down
    assert await coordinator._async_update_data() == {'still': 'running'}
    data.handler.close.assert_not_awaited()

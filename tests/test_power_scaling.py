"""Documented register scaling and ambiguous battery identity regressions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.kostal_modbus_control import (
    KostalCoordinator, _async_options_update_listener,
)
from custom_components.kostal_modbus_control.config_flow import (
    KostalOptionsFlowHandler, validate_control_options,
)
from custom_components.kostal_modbus_control.const import (
    CONF_MAX_CONTROL_POWER_W, DOMAIN, REG_INVERTER_MAX_POWER,
    REG_INVERTER_MAX_POWER_SCALE, REG_BATTERY_SOC, REG_BATTERY_POWER,
    REG_BATTERY_MGMT_MODE, REG_BATTERY_MODEL_ID, REG_BATTERY_TYPE,
    REG_BATTERY_VOLTAGE, REG_SOFTWARE_VERSION, MAX_POWER_SOURCE_CONFIGURED,
    MAX_POWER_SOURCE_BATTERY_VOLTAGE, MAX_POWER_SOURCE_LEARNED,
)
from custom_components.kostal_modbus_control.modbus_handler import (
    KostalModbusHandler, KostalModbusRequestError, KostalModbusConnectionError,
)
from custom_components.kostal_modbus_control.number import KostalPowerRateNumber
from custom_components.kostal_modbus_control.sensor import (
    KostalBatteryMaxControlPowerSensor, KostalBatteryModelIdTextSensor,
)


@pytest.mark.parametrize('byte_order', [0, 1])
@pytest.mark.parametrize('words,expected', [([100, 2], (100, 2)), ([10000, 0xffff], (10000, -1))])
async def test_power_and_signed_scale_are_one_request_not_word_swapped(byte_order, words, expected):
    calls = []
    async def read(address, *, count, device_id):
        calls.append((address, count, device_id))
        return SimpleNamespace(isError=lambda: False, registers=words)
    handler = KostalModbusHandler('unused.invalid', 1502, 71)
    handler._client = SimpleNamespace(connected=True, read_holding_registers=read)
    handler.set_modbus_byte_order(byte_order)
    assert await handler.read_uint16_scale_pair(531) == expected
    assert calls == [(531, 2, 71)]


@pytest.mark.parametrize('pair,watts', [
    ((100, 2), 10000), ((8500, 0), 8500), ((8500, -1), 850),
    ((1, -10), 1e-10), ((1, 10), 1e10),
    (None, None), ((100, None), None), ((100, -32768), None),
    ((100, -11), None), ((100, 11), None), ((0, 0), None),
    ((65535, 0), None), ((-1, 0), None), ((100, True), None),
    ((True, 0), None), ((100, float('nan')), None),
])
def test_scale_validation_discards_previous_watt_value(data, pair, watts):
    data.inverter_max_power_w = 10000
    data.update_inverter_max_power(pair)
    assert data.inverter_max_power_w == watts


def polling(data, hass):
    readings = {REG_BATTERY_SOC: 4, REG_BATTERY_POWER: 0, REG_BATTERY_MGMT_MODE: 2,
                REG_SOFTWARE_VERSION: '3.07.10.27989', REG_BATTERY_VOLTAGE: 0}
    for method in ('read_float', 'read_int16', 'read_uint8', 'read_uint16',
                   'read_uint32', 'read_uint32_big_endian', 'read_string'):
        setattr(data.handler, method, AsyncMock(side_effect=lambda address, *args: readings.get(address)))
    data.handler.read_uint16_scale_pair = AsyncMock(return_value=(100, 2))
    c = KostalCoordinator(hass, data.handler, data, 'unused.invalid')
    data.coordinator = c
    return c, readings


async def test_poll_updates_maximum_and_ui_without_restart(data, hass):
    c, readings = polling(data, hass)
    rate = KostalPowerRateNumber(data, 'test', 'charge', 'Charge', 'charge_power_fixed_w')
    sensor = KostalBatteryMaxControlPowerSensor(c, data, 'test')
    for pair, expected in [((100, 0), 100), ((100, 2), 10000), ((90, 2), 9000)]:
        data.handler.read_uint16_scale_pair.return_value = pair
        c.data = await c._async_update_data()
        assert rate.native_max_value == expected
        assert rate.native_value == expected
        assert data.clamp_power_setpoint_w(-15000) == -expected
        assert sensor.extra_state_attributes['inverter_max_power_raw'] == pair[0]
        assert sensor.extra_state_attributes['inverter_max_power_scale'] == pair[1]
        assert sensor.extra_state_attributes['inverter_max_power_w'] == expected
        assert c.data[REG_INVERTER_MAX_POWER_SCALE] == pair[1]
    data.handler.write_float.assert_not_called()


async def test_invalid_scale_recovers_without_reconnect_and_voltage_fallback(data, hass):
    c, readings = polling(data, hass)
    c.data = await c._async_update_data()
    data.handler.read_uint16_scale_pair.return_value = (100, -32768)
    c.data = await c._async_update_data()
    assert data.max_battery_power() == (None, None)
    assert c.data[REG_INVERTER_MAX_POWER] == 100  # raw diagnosis remains visible
    readings[REG_BATTERY_VOLTAGE] = 400
    c.data = await c._async_update_data()
    assert data.max_battery_power() == (12000, MAX_POWER_SOURCE_BATTERY_VOLTAGE)
    data.handler.read_uint16_scale_pair.return_value = (100, 2)
    c.data = await c._async_update_data()
    assert data.inverter_max_power_w == 10000


async def test_missing_pair_is_optional_and_reprobed_after_transport_failure(data, hass):
    c, _ = polling(data, hass)
    c.data = await c._async_update_data()
    data.handler.read_uint16_scale_pair.side_effect = KostalModbusRequestError('unsupported')
    c.data = await c._async_update_data()
    assert data.inverter_max_power_w is None
    assert data.control_data_fresh()
    await c._async_update_data()
    assert data.handler.read_uint16_scale_pair.await_count == 2  # absent pair skipped
    data.handler.read_float.side_effect = KostalModbusConnectionError('offline')
    with pytest.raises(UpdateFailed):
        await c._async_update_data()
    data.handler.read_float.side_effect = lambda address: {REG_BATTERY_SOC: 4}.get(address)
    data.handler.read_uint16_scale_pair.side_effect = None
    c.data = await c._async_update_data()
    assert data.handler.read_uint16_scale_pair.await_count == 3
    assert data.inverter_max_power_w == 10000


@pytest.mark.parametrize('value', [0, 99, 50001, 8550.6, True, False, '8500', float('nan'), float('inf')])
def test_override_rejects_invalid_options(value):
    with pytest.raises(ValueError):
        validate_control_options({CONF_MAX_CONTROL_POWER_W: value})


async def test_override_precedence_clear_and_retained_fixed_request(data, hass):
    c, readings = polling(data, hass)
    c.data = await c._async_update_data()
    data.battery_max_voltage_learned = 400
    rate = KostalPowerRateNumber(data, 'test', 'charge', 'Charge', 'charge_power_fixed_w')
    assert rate.native_value == 12000
    hass.data = {DOMAIN: {'test': data}}
    entry = SimpleNamespace(entry_id='test', options={CONF_MAX_CONTROL_POWER_W: 8550})
    await _async_options_update_listener(hass, entry)
    assert data.max_battery_power() == (8550, MAX_POWER_SOURCE_CONFIGURED)
    assert rate.native_max_value == 8550  # do not round an explicit ceiling up
    assert rate.native_value == 8550
    await rate.async_set_native_value(8000)
    entry.options = {CONF_MAX_CONTROL_POWER_W: 5000}
    await _async_options_update_listener(hass, entry)
    assert rate.native_value == 8000  # retain intent, clamp only the actual command
    assert data.clamp_power_setpoint_w(-8000) == -5000
    assert data.clamp_power_setpoint_w(8000) == 5000
    entry.options = {}
    await _async_options_update_listener(hass, entry)
    assert data.max_battery_power() == (12000, MAX_POWER_SOURCE_LEARNED)
    assert rate.native_value == 8000
    data.handler.write_float.assert_not_called()


async def test_optional_override_schema_can_be_cleared(data, hass):
    flow = KostalOptionsFlowHandler()
    flow.hass = hass
    flow.config_entry = SimpleNamespace(entry_id='test', options={CONF_MAX_CONTROL_POWER_W: 8500})
    hass.data = {DOMAIN: {'test': data}}
    form = await flow.async_step_init()
    submitted = form['data_schema']({})
    assert CONF_MAX_CONTROL_POWER_W not in submitted
    result = await flow.async_step_init(submitted)
    assert CONF_MAX_CONTROL_POWER_W not in result['data']


@pytest.mark.parametrize('battery_type,model_id,expected', [
    (4, 4, 'BYD (model ID 4)'), (4, 3, 'BYD (model ID 3)'),
    (4, 999, 'BYD (model ID 999)'), (8, 4, 'BMZ (model ID 4)'),
    (None, 4, 'Unknown battery (model ID 4)'), (4, None, None),
])
def test_model_id_never_invents_capacity_or_hvs_generation(data, battery_type, model_id, expected):
    c = SimpleNamespace(data={REG_BATTERY_TYPE: battery_type, REG_BATTERY_MODEL_ID: model_id})
    sensor = KostalBatteryModelIdTextSensor(c, 'test')
    assert sensor.native_value == expected
    assert sensor.extra_state_attributes == {'model_id': model_id, 'model_name_verified': False}
    assert sensor._attr_unique_id == 'test_battery_model_id_text'


@pytest.mark.parametrize('override', [10000, None])
async def test_new_override_never_discards_explicit_directional_caps(data, hass, override):
    from custom_components.kostal_modbus_control.const import (
        CONF_EXTERNAL_CONTROL_MAX_CHARGE_W, CONF_EXTERNAL_CONTROL_MAX_DISCHARGE_W,
    )
    c, _ = polling(data, hass)
    c.data = await c._async_update_data()
    data.inverter_max_power_w = 100
    data.max_control_power_w = 100 if override is None else None
    flow = KostalOptionsFlowHandler()
    flow.hass = hass
    flow.config_entry = SimpleNamespace(entry_id='test', options={})
    hass.data = {DOMAIN: {'test': data}}
    form = await flow.async_step_init()
    submitted = {CONF_EXTERNAL_CONTROL_MAX_CHARGE_W: 2000,
                 CONF_EXTERNAL_CONTROL_MAX_DISCHARGE_W: 3000}
    if override is not None:
        submitted[CONF_MAX_CONTROL_POWER_W] = override
    result = await flow.async_step_init(submitted)
    assert result['data'][CONF_EXTERNAL_CONTROL_MAX_CHARGE_W] == 2000
    assert result['data'][CONF_EXTERNAL_CONTROL_MAX_DISCHARGE_W] == 3000
    empty = form['data_schema']({})
    assert CONF_EXTERNAL_CONTROL_MAX_CHARGE_W not in empty
    assert CONF_EXTERNAL_CONTROL_MAX_DISCHARGE_W not in empty


def test_upstream_sunspec_estimate_preserves_explicit_fork_ceiling(data):
    from custom_components.kostal_modbus_control.const import (
        KEY_BATTERY_MAX_VOLTAGE_SUNSPEC, MAX_POWER_SOURCE_SUNSPEC,
    )
    data.coordinator = SimpleNamespace(data={
        REG_SOFTWARE_VERSION: '3.07.10.27989', KEY_BATTERY_MAX_VOLTAGE_SUNSPEC: 450,
    })
    data.inverter_max_power_w = 100  # Restricted feed-in reading must not override SunSpec.
    data.battery_max_voltage_learned = 420
    assert data.max_battery_power() == (13500, MAX_POWER_SOURCE_SUNSPEC)
    data.max_control_power_w = 8550
    assert data.max_battery_power() == (8550, MAX_POWER_SOURCE_CONFIGURED)
    assert data.max_battery_power_step_w() == 8550
    data.max_control_power_w = None
    assert data.max_battery_power() == (13500, MAX_POWER_SOURCE_SUNSPEC)


async def test_sunspec_sentinel_keeps_learned_fallback_and_recovers(data, hass):
    from custom_components.kostal_modbus_control.const import (
        KEY_BATTERY_MAX_VOLTAGE_SUNSPEC, MAX_POWER_SOURCE_SUNSPEC,
    )
    coordinator = KostalCoordinator(hass, data.handler, data, 'unused.invalid')
    coordinator._sunspec_battery_model_address = 40416
    data.handler.read_uint16 = AsyncMock(return_value=0xffff)
    data.handler.read_int16 = AsyncMock(return_value=-2)
    data.coordinator = coordinator
    data.battery_max_voltage_learned = 420
    coordinator.data = {REG_SOFTWARE_VERSION: '3.07.10.27989'}
    coordinator.data[KEY_BATTERY_MAX_VOLTAGE_SUNSPEC] = await coordinator._async_read_sunspec_battery_max_voltage()
    assert coordinator.data[KEY_BATTERY_MAX_VOLTAGE_SUNSPEC] is None
    assert data.max_battery_power() == (12600, MAX_POWER_SOURCE_LEARNED)
    # A temporarily unavailable value must be retried without reconnecting.
    data.handler.read_uint16.return_value = 45000
    coordinator.data[KEY_BATTERY_MAX_VOLTAGE_SUNSPEC] = await coordinator._async_read_sunspec_battery_max_voltage()
    assert coordinator.data[KEY_BATTERY_MAX_VOLTAGE_SUNSPEC] == 450
    assert data.max_battery_power() == (13500, MAX_POWER_SOURCE_SUNSPEC)

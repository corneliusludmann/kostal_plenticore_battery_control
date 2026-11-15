"""Explicit, minimal HA API doubles for unit tests of real integration modules.

These tests do not boot HA or connect to an inverter. Production modules are
imported normally; only framework boundaries (entity base classes, timers,
storage) are replaced. See tests/README.md for the remaining integration checks.
"""
import asyncio
from datetime import datetime, timezone
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def module(name, **members):
    result = ModuleType(name)
    result.__path__ = []
    result.__dict__.update(members)
    sys.modules[name] = result
    if '.' in name:
        parent, attr = name.rsplit('.', 1)
        setattr(sys.modules[parent], attr, result)
    return result


class Entity:
    entity_id = None
    @property
    def name(self):
        return self._attr_name
    @property
    def is_on(self):
        return self._attr_is_on
    @property
    def native_value(self):
        return getattr(self, '_attr_native_value', None)
    def async_write_ha_state(self):
        pass
    def async_on_remove(self, callback):
        pass
    async def async_added_to_hass(self):
        pass
    async def async_will_remove_from_hass(self):
        pass


class RestoreEntity(Entity):
    async def async_get_last_state(self):
        return None


class RestoreNumber(RestoreEntity):
    async def async_get_last_number_data(self):
        return None


class Coordinator:
    def __class_getitem__(cls, item):
        return cls
    def __init__(self, hass, *args, **kwargs):
        self.hass = hass
        self.data = None
        self.last_update_success = True
    def async_add_listener(self, callback):
        return lambda: None


class Flow:
    def __init_subclass__(cls, **kwargs):
        pass
    def async_show_form(self, **kwargs):
        return {'type': 'form', **kwargs}
    def async_create_entry(self, **kwargs):
        return {'type': 'create_entry', **kwargs}
    def async_abort(self, **kwargs):
        return {'type': 'abort', **kwargs}


class Selector:
    def __init__(self, config=None):
        self.config = config
    def __call__(self, value):
        return value


class Store:
    def __init__(self, *args, **kwargs):
        pass
    async def async_load(self):
        return None
    def async_delay_save(self, *args):
        pass


module('homeassistant')
module('homeassistant.components')
module('homeassistant.components.switch', SwitchEntity=type('SwitchEntity', (Entity,), {}))
module('homeassistant.components.number', RestoreNumber=RestoreNumber,
       NumberDeviceClass=SimpleNamespace(POWER='power'), NumberMode=SimpleNamespace(BOX='box'))
module('homeassistant.config_entries', ConfigEntry=SimpleNamespace, ConfigFlow=Flow,
       OptionsFlow=Flow, ConfigFlowResult=dict)
module('homeassistant.core', HomeAssistant=SimpleNamespace, callback=lambda f: f)
module('homeassistant.const', CONF_HOST='host', PERCENTAGE='%',
       Platform=SimpleNamespace(SENSOR='sensor', SWITCH='switch', NUMBER='number'),
       EntityCategory=SimpleNamespace(CONFIG='config', DIAGNOSTIC='diagnostic'),
       UnitOfPower=SimpleNamespace(WATT='W'))
module('homeassistant.exceptions', ConfigEntryNotReady=type('ConfigEntryNotReady', (Exception,), {}),
       HomeAssistantError=type('HomeAssistantError', (Exception,), {}))
module('homeassistant.helpers')
module('homeassistant.helpers.entity_platform', AddEntitiesCallback=object)
module('homeassistant.helpers.event', async_call_later=lambda *a, **kw: Mock(),
       async_track_state_change_event=lambda *a, **kw: Mock(),
       async_track_time_interval=lambda *a, **kw: Mock())
module('homeassistant.helpers.dispatcher', async_dispatcher_send=lambda *a: None)
module('homeassistant.helpers.device_registry', DeviceInfo=dict,
       async_get=lambda hass: Mock())
module('homeassistant.helpers.entity_registry', async_get=lambda hass: SimpleNamespace(
    async_get_entity_id=lambda *args: None, async_get=lambda entity_id: None))
module('homeassistant.helpers.restore_state', RestoreEntity=RestoreEntity)
module('homeassistant.helpers.storage', Store=Store)
module('homeassistant.helpers.aiohttp_client', async_get_clientsession=lambda hass: Mock())
module('homeassistant.helpers.update_coordinator', DataUpdateCoordinator=Coordinator,
       UpdateFailed=type('UpdateFailed', (Exception,), {}))
module('homeassistant.helpers.selector', BooleanSelector=Selector, EntitySelector=Selector,
       EntitySelectorConfig=dict, NumberSelector=Selector, NumberSelectorConfig=dict,
       NumberSelectorMode=SimpleNamespace(BOX='box'), SelectSelector=Selector,
       SelectSelectorConfig=dict, SelectSelectorMode=SimpleNamespace(DROPDOWN='dropdown'))
module('homeassistant.util')
module('homeassistant.util.dt', utcnow=lambda: datetime.now(timezone.utc),
       now=lambda: datetime.now(timezone.utc))


@pytest.fixture
def hass():
    return SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: None),
                           bus=SimpleNamespace(async_fire=Mock()),
                           async_create_task=asyncio.create_task,
                           services=SimpleNamespace(async_call=AsyncMock()), data={})


@pytest.fixture
def data():
    from custom_components.kostal_modbus_control import KostalData
    handler = SimpleNamespace(write_float=AsyncMock(), close=AsyncMock(),
                              read_int16=AsyncMock(return_value=0))
    return KostalData(handler=handler, soc_limits_ready=True)

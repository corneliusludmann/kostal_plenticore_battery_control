from __future__ import annotations

import logging
import asyncio
import math
from datetime import timedelta
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    DOMAIN,
    NUMBER_CHARGE_RATE,
    NUMBER_DISCHARGE_RATE,
    NUMBER_FUSE_SIZE,
    NUMBER_MIN_SOC_LIMIT,
    NUMBER_MAX_SOC_LIMIT,
    POWER_RATE_FALLBACK_MAX_W,
    POWER_RATE_STEP_W,
    DEFAULT_FUSE_SIZE,
    DEFAULT_MIN_SOC_LIMIT,
    DEFAULT_MAX_SOC_LIMIT,
    MIN_SOC_LIMIT_RANGE,
    MAX_SOC_LIMIT_RANGE,
    REG_BATTERY_MIN_SOC,
    REG_BATTERY_MAX_SOC,
)

from .modbus_handler import CONTROL_WRITE_GUARD, KostalModbusWriteAborted

_LOGGER = logging.getLogger(__name__)


def _finite_in_range(value: float, minimum: float, maximum: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"Value must be finite and between {minimum} and {maximum}")
    return value

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Kostal Modbus numbers."""
    data = hass.data[DOMAIN][entry.entry_id]

    entities = [
        KostalPowerRateNumber(data, entry.entry_id, NUMBER_CHARGE_RATE, "Set Charge Rate", "charge_power_fixed_w"),
        KostalPowerRateNumber(data, entry.entry_id, NUMBER_DISCHARGE_RATE, "Set Discharge Rate", "discharge_power_fixed_w"),
        KostalFuseSizeNumber(data, entry.entry_id),
        KostalMinSocLimitNumber(data, entry.entry_id),
        KostalMaxSocLimitNumber(data, entry.entry_id),
    ]

    # Restoring one limit must not start a writer before the other limit has
    # been validated. Disabled entities retain their inactive defaults and are
    # deliberately excluded: Home Assistant never calls their added hook.
    registry = er.async_get(hass)
    data._soc_limit_entities = entities[-2:]
    data._soc_restore_pending = set()
    for entity in data._soc_limit_entities:
        entity_id = registry.async_get_entity_id("number", DOMAIN, entity._attr_unique_id)
        registered = registry.async_get(entity_id) if entity_id is not None else None
        if registered is None or registered.disabled_by is None:
            data._soc_restore_pending.add(entity._key)
    data.soc_limits_ready = not data._soc_restore_pending
    async_add_entities(entities)


class KostalPowerRateNumber(RestoreNumber):
    """Charge or discharge rate in Watts.

    A rate follows the maximum battery control power until the user sets a
    lower value, which is then kept, also across restarts. Setting it back to
    the maximum or above makes it follow the maximum again. Which of the two
    applies is restored from the ``follows_max`` state attribute.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_min_value = 0.0
    _attr_native_step = POWER_RATE_STEP_W
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = NumberDeviceClass.POWER
    _attr_mode = NumberMode.BOX

    def __init__(self, data, entry_id: str, key: str, name: str, data_attr: str) -> None:
        self._data = data
        self._entry_id = entry_id
        self._key = key
        self._data_attr = data_attr
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name
        self._last_written: tuple[float | None, float] | None = None
        self._set_fixed(None)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._entry_id)})

    def _fixed(self) -> float | None:
        return getattr(self._data, self._data_attr)

    def _set_fixed(self, value: float | None) -> None:
        setattr(self._data, self._data_attr, value)

    @property
    def native_value(self) -> float | None:
        fixed = self._fixed()
        if fixed is not None:
            return fixed
        return self._data.max_battery_power_step_w()

    @property
    def native_max_value(self) -> float:
        max_power = self._data.max_battery_power_step_w()
        return POWER_RATE_FALLBACK_MAX_W if max_power is None else max_power

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"follows_max": self._fixed() is None}

    async def async_added_to_hass(self) -> None:
        """Restore the user's fixed value, or keep following the maximum."""
        await super().async_added_to_hass()
        last_number = await self.async_get_last_number_data()
        last_state = await self.async_get_last_state()
        if last_number is None or last_number.native_value is None:
            self._set_fixed(None)
        elif last_number.native_unit_of_measurement == PERCENTAGE:
            # Older versions stored the rate in % of the inverter's nominal
            # value, which cannot be converted reliably — start at the maximum
            self._set_fixed(None)
            _LOGGER.info(
                "%s was %.0f%% in an earlier version — it now follows the maximum battery power in W",
                self.name,
                last_number.native_value,
            )
        elif last_state is not None and last_state.attributes.get("follows_max") is True:
            self._set_fixed(None)
        else:
            try:
                # A learned maximum or a temporary BMS derating may be lower
                # than when the user saved this value. Preserve the fixed
                # request; control writes clamp it to the current maximum.
                self._set_fixed(_finite_in_range(last_number.native_value, 0, math.inf))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid restored power rate; using zero W")
                self._set_fixed(0.0)

        coordinator = self._data.coordinator
        if coordinator is not None:
            self.async_on_remove(coordinator.async_add_listener(self._handle_coordinator_update))
        self._write_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        # The displayed value and maximum track the battery voltage
        if (self.native_value, self.native_max_value) != self._last_written:
            self._write_state()

    @callback
    def _write_state(self) -> None:
        self._last_written = (self.native_value, self.native_max_value)
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        value = _finite_in_range(value, 0, self.native_max_value)
        max_power = self._data.max_battery_power_step_w()
        if value >= (POWER_RATE_FALLBACK_MAX_W if max_power is None else max_power):
            self._set_fixed(None)
        else:
            self._set_fixed(float(value))
        self._write_state()


class KostalFuseSizeNumber(RestoreNumber):
    """Input for house fuse size in Ampere."""

    _attr_has_entity_name = True
    _attr_unique_id = None  # Set in __init__
    _attr_native_min_value = 6.0
    _attr_native_max_value = 125.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "A"
    _attr_mode = NumberMode.BOX
    _attr_name = "House Fuse Size"
    _attr_icon = "mdi:fuse"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, data, entry_id: str) -> None:
        self._data = data
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{NUMBER_FUSE_SIZE}"
        self._attr_native_value = DEFAULT_FUSE_SIZE
        self._data.fuse_size = DEFAULT_FUSE_SIZE

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_number_data()) is not None and state.native_value is not None:
            try:
                value = _finite_in_range(state.native_value, self._attr_native_min_value, self._attr_native_max_value)
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid restored house fuse size")
            else:
                self._attr_native_value = value
                self._data.fuse_size = value

    async def async_set_native_value(self, value: float) -> None:
        value = _finite_in_range(value, self._attr_native_min_value, self._attr_native_max_value)
        self._attr_native_value = value
        self._data.fuse_size = value
        self.async_write_ha_state()


class KostalSocLimitNumber(RestoreNumber):
    """Base for the battery min/max SOC limits.

    Registers 1042/1044 are governed by the inverter's Modbus timeout: a limit
    only holds while it is being sent, and the inverter reverts to its own
    settings (min 5%, max 100%) once the writes stop. A limit that is in use is
    therefore written continuously — from the moment it is set, on the same
    cadence as the charge/discharge loops — so it stays in force for as long as
    it is set. The inverter tapers charge/discharge power as the SoC approaches
    the limit; that taper is what makes "stop charging at 50%" actually hold.

    Setting a limit back to the inverter's own value (min 5%, max 100%) means
    "not in use": that value is written once so the release takes effect
    straight away instead of waiting out the inverter's timeout, and nothing is
    sent afterwards. While a discharge hold is active (Block Discharge or a
    Predbat hold), the min SOC sends the hold's value instead.

    No write is sent while the inverter settles after a charge or discharge
    stop (KostalData.control_settling): every write restarts the inverter's
    timeout, and a running keep-alive would keep the stopped setpoint in force.
    A release made during that wait is sent once the wait is over.
    """

    _key: str
    _register: int
    _data_attr: str
    _inactive_value: float

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, data, entry_id: str) -> None:
        self._data = data
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{self._key}"
        self._attr_native_value = self._inactive_value
        self._removed = False
        self._release_pending = False
        self._remove_timer = None
        self._write_task = None
        self._active_write_tasks: set[asyncio.Task] = set()
        self._remove_deferred_write = None
        # Write twice per inverter timeout, same as the charge/discharge loops
        self._loop_interval = max(int(self._data.inverter_timeout / 2), 5)
        self._store_value(self._inactive_value)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._entry_id)})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "active": self._is_active(),
            "writing": self._remove_timer is not None,
            "release_pending": self._release_pending,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_number_data()) is not None and state.native_value is not None:
            try:
                self._attr_native_value = self._validate_value(state.native_value)
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid restored %s limit; SOC writes require an explicit valid setting", self._data_attr)
                self._data.soc_limits_valid = False
                self._attr_native_value = self._inactive_value
        if self._attr_native_value is None:
            self._attr_native_value = self._inactive_value
        self._store_value(self._attr_native_value)
        self._apply()
        pending = getattr(self._data, "_soc_restore_pending", None)
        if pending is not None:
            pending.discard(self._key)
            self._data.soc_limits_ready = not pending
            if self._data.soc_limits_ready:
                for entity in self._data._soc_limit_entities:
                    if entity._is_active():
                        entity._schedule_write()
        elif self._is_active():
            self._schedule_write()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        self._cancel_timer()
        if self._remove_deferred_write is not None:
            self._remove_deferred_write()
            self._remove_deferred_write = None
        tasks = set(self._active_write_tasks)
        if self._write_task is not None:
            tasks.add(self._write_task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._write_task = None
        await super().async_will_remove_from_hass()

    async def async_set_native_value(self, value: float) -> None:
        # Apply desired state and its writes under the same lock. A queued hold
        # must not see a higher maximum before register 1044 has been raised.
        await self._async_write_limit(new_value=float(value))

    def _validate_value(self, value: float) -> float:
        """Reject invalid pairs before mutating state or issuing any write."""
        value = float(value)
        if not math.isfinite(value) or not self._attr_native_min_value <= value <= self._attr_native_max_value:
            raise ValueError("SOC limit must be finite and within its allowed range")
        minimum = value if self._data_attr == "min_soc" else self._data.min_soc
        maximum = value if self._data_attr == "max_soc" else self._data.max_soc
        if not minimum <= maximum:
            raise ValueError("Minimum SOC must not exceed maximum SOC")
        return value

    def _store_value(self, value: float) -> None:
        setattr(self._data, self._data_attr, value)

    def _is_active(self) -> bool:
        """False when the limit equals the inverter's own limit, i.e. unused."""
        if self._attr_native_value is None:
            return False
        return abs(self._attr_native_value - self._inactive_value) >= 0.5

    @callback
    def _apply(self) -> None:
        """Bring the keep-alive write loop in line with the current value."""
        if self._is_active() or self._release_pending:
            self._start_timer()
        else:
            self._cancel_timer()

    def _start_timer(self) -> None:
        if self._remove_timer is not None:
            return
        _LOGGER.info(
            "%s set to %.0f%% — writing register %d every %ds",
            self.name,
            self._attr_native_value,
            self._register,
            self._loop_interval,
        )
        self._remove_timer = async_track_time_interval(
            self.hass, self._async_write_tick, timedelta(seconds=self._loop_interval)
        )

    def _cancel_timer(self) -> None:
        if self._remove_timer is None:
            return
        self._remove_timer()
        self._remove_timer = None
        _LOGGER.info("%s released — register %d no longer written continuously", self.name, self._register)

    def _schedule_write(self) -> None:
        if self._write_task is not None and not self._write_task.done():
            return
        self._write_task = self.hass.async_create_task(self._async_write_limit())

    async def _async_write_tick(self, *args) -> None:
        await self._async_write_limit()

    async def _async_write_limit(self, *, new_value: float | None = None) -> None:
        task = asyncio.current_task()
        self._active_write_tasks.add(task)
        try:
            # Serialize the final safety check with stop and all other writers.
            async with self._data.control_lock:
                if new_value is not None:
                    self._attr_native_value = self._validate_value(new_value)
                    self._data.soc_limits_valid = True
                    self._store_value(self._attr_native_value)
                    self._release_pending = not self._is_active()
                    self._apply()
                    self.async_write_ha_state()
                if not self._write_allowed():
                    return
                token = CONTROL_WRITE_GUARD.set(
                    lambda: self._write_allowed() and not self._data.control_settling()
                )
                try:
                    await self._async_write_limit_locked()
                finally:
                    CONTROL_WRITE_GUARD.reset(token)
        finally:
            self._active_write_tasks.discard(task)

    def _write_allowed(self) -> bool:
        return (
            not self._removed
            and self._data.soc_limits_ready
            and self._data.soc_limits_valid
            and self._data.control_data_fresh()
        )

    async def _async_write_limit_locked(self) -> None:
        value = self._attr_native_value
        if value is None:
            return
        if self._data.control_settling():
            # A write now would keep a stopped charge/discharge setpoint alive.
            # An active limit is written by the next tick after the wait. A
            # release is written once the wait is over: a charge resumed in the
            # meantime keeps the inverter in external control with the old limit.
            _LOGGER.debug("%s: write to register %d deferred while the inverter settles", self.name, self._register)
            if not self._is_active() and self._remove_deferred_write is None:
                self._remove_deferred_write = async_call_later(
                    self.hass, self._data.control_settle_remaining() + 1.0, self._async_deferred_write
                )
            return
        try:
            hold = self._data.discharge_hold_min_soc
            changing_max = self._data_attr == "max_soc" and hold is not None
            if self._data_attr == "min_soc" and self._register_value(float(value)) > self._data.applied_max_soc:
                return  # Retry after the maximum's pending write is acknowledged.
            if changing_max and self._data.hold_uses_discharge_limit():
                # Establish 1040 before lowering 1044 below an existing hold.
                await self._data.async_write_discharge_hold(hold)
            elif self._data_attr == "max_soc" and value < self._data.applied_max_soc:
                # Retire an older, higher minimum before lowering the maximum.
                await self._data.handler.write_float(REG_BATTERY_MIN_SOC, self._data.effective_min_soc())
            await self._data.handler.write_float(self._register, self._register_value(float(value)))
            if self._data_attr == "max_soc":
                self._data.applied_max_soc = float(value)
            if changing_max and not self._data.hold_uses_discharge_limit():
                await self._data.async_write_discharge_hold(hold)
            if value == self._attr_native_value and not self._is_active():
                self._release_pending = False
                self._cancel_timer()
                if self._remove_deferred_write is not None:
                    self._remove_deferred_write()
                    self._remove_deferred_write = None
                self.async_write_ha_state()
        except KostalModbusWriteAborted:
            return
        except Exception as err:
            self._data.mark_communication_lost(f"{self.name}: {err}")
            # Revoke active sessions before a successful poll can restore
            # communication, just as a failed coordinator poll does.
            self._data.notify_connection_lost()
            _LOGGER.warning("%s failed to write register %d: %s", self.name, self._register, err)
            await self._data.handler.close()

    async def _async_deferred_write(self, *args) -> None:
        self._remove_deferred_write = None
        await self._async_write_limit()

    def _register_value(self, value: float) -> float:
        """Value actually written to the register for this limit."""
        return value


class KostalMinSocLimitNumber(KostalSocLimitNumber):
    """Minimum SOC. 5% means unused — that is the inverter's own minimum."""

    _key = NUMBER_MIN_SOC_LIMIT
    _register = REG_BATTERY_MIN_SOC
    _data_attr = "min_soc"
    _inactive_value = DEFAULT_MIN_SOC_LIMIT
    _attr_name = "Battery Minimum SOC Limit"
    _attr_icon = "mdi:battery-arrow-down-outline"
    _attr_native_min_value = MIN_SOC_LIMIT_RANGE[0]
    _attr_native_max_value = MIN_SOC_LIMIT_RANGE[1]

    def _register_value(self, value: float) -> float:
        # Block Discharge and Predbat's reserve hold write 1042 as well — send the
        # same value so the keep-alives never overwrite each other
        return self._data.effective_min_soc()


class KostalMaxSocLimitNumber(KostalSocLimitNumber):
    """Maximum SOC. 100% means unused — that is the inverter's own maximum."""

    _key = NUMBER_MAX_SOC_LIMIT
    _register = REG_BATTERY_MAX_SOC
    _data_attr = "max_soc"
    _inactive_value = DEFAULT_MAX_SOC_LIMIT
    _attr_name = "Battery Maximum SOC Limit"
    _attr_icon = "mdi:battery-arrow-up-outline"
    _attr_native_min_value = MAX_SOC_LIMIT_RANGE[0]
    _attr_native_max_value = MAX_SOC_LIMIT_RANGE[1]

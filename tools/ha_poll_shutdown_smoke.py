"""Real HA coordinator shutdown regression, with in-memory Modbus transport.

Run: python tools/ha_poll_shutdown_smoke.py
Platform teardown is mocked; coordinator refresh/shutdown and Modbus locking
are real. No live Home Assistant or inverter is accessed.
"""

import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.kostal_modbus_control import (
    KostalCoordinator, KostalData, async_unload_entry,
)
from custom_components.kostal_modbus_control.const import DOMAIN
from custom_components.kostal_modbus_control.modbus_handler import KostalModbusHandler


class FakeClient:
    connected = True

    def __init__(self, handler):
        self.handler = handler

    def close(self):
        self.handler.events.append("close")

    async def read_holding_registers(self, address, *, count, device_id):
        handler = self.handler
        handler.events.append(("read", address))
        if not handler.first_read_started.is_set():
            handler.first_read_started.set()
            await handler.finish_first_read.wait()
        return SimpleNamespace(isError=lambda: False, registers=[0] * count)


class FakeTransportHandler(KostalModbusHandler):
    def __init__(self):
        super().__init__("in-memory", 1502, 71)
        self.events = []
        self.first_read_started = asyncio.Event()
        self.finish_first_read = asyncio.Event()
        self._client = FakeClient(self)

    async def _ensure_connected(self):
        if self._client is None:
            self.events.append("reconnect")
            self._client = FakeClient(self)


async def reproduce(config_dir):
    hass = HomeAssistant(config_dir)
    frame.async_setup(hass)
    handler = FakeTransportHandler()
    data = KostalData(handler=handler)
    coordinator = KostalCoordinator(hass, handler, data, "in-memory")
    data.coordinator = coordinator
    hass.data[DOMAIN] = {"review": data}
    hass.config_entries = SimpleNamespace(
        async_unload_platforms=AsyncMock(return_value=True),
    )

    poll = asyncio.create_task(coordinator.async_refresh())
    await handler.first_read_started.wait()

    assert await asyncio.wait_for(
        async_unload_entry(hass, SimpleNamespace(entry_id="review")), timeout=1
    )
    result = await asyncio.gather(poll, return_exceptions=True)
    # HA consumes cancellation in async_refresh; a direct poll propagates it.
    assert result[0] is None or isinstance(result[0], asyncio.CancelledError)
    events_at_unload = list(handler.events)
    handler.finish_first_read.set()
    await coordinator.async_refresh()  # HA ignores refreshes after shutdown.
    await coordinator.async_shutdown()  # Registered config-entry cleanup is idempotent.
    assert handler.events == events_at_unload
    assert len(handler.events) == 2
    assert handler.events[0][0] == "read"
    assert handler.events[1] == "close"
    assert handler._client is None
    assert not coordinator._poll_tasks
    from homeassistant.const import __version__
    print(f"PASS: HA {__version__} refresh cancelled, transport closed, no reads/reconnect after unload")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="kostal-lifecycle-review-") as config_dir:
        asyncio.run(reproduce(config_dir))

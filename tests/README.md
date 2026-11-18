# Unit-test scope

Run `python -m pip install -r requirements-test.txt` and `python -m pytest --cov`.
The tests import the actual integration modules and execute their asynchronous
methods. Explicit framework doubles in `conftest.py` replace HA entities, timers,
selectors and storage; all Modbus I/O is mocked. No device, network or live HA
access is involved. Race tests use events rather than timing-sensitive sleeps.

These are unit/regression tests, not HA integration or hardware certification.
They cannot establish correct HA lifecycle registration, real timer dispatch,
frontend selector validation, device response or physical timeout behavior.
Before deployment, validate setup/reload/unload in a disposable HA instance and
then a supervised low-power charge/stop, disconnect/reconnect, expiration and
stale-source scenario on the supported inverter. Publish coverage as a measured
property, not a claim that the complete pre-existing integration is covered.


## Real Home Assistant smoke test

In a separate CPython 3.14 virtual environment, install
`homeassistant==2026.11.1 pymodbus`, then run `python tools/ha_smoke.py` from the
repository root. Independently verified with CPython 3.14.10. The script sits
outside this directory so `conftest.py` cannot substitute the framework.

It checks the actual HA form serializer and actual EntityComponent/RestoreNumber
restore and removal hooks, startup write suppression, post-settle writes and
timer/task cleanup. Modbus is mocked and standalone entities omit device
association. It does not certify full integration setup/reload, device registry
association, real-time timer dispatch or physical inverter timeout behavior.

`test_restart_measurements.py` replays hypothetical Modbus samples based on a
short 0%/Off → 82%/GridPreCheck observation from the separate HA REST
integration. It is not a device capture. The cases verify startup/restored-limit
silence, reconnect behavior with/without automatic resume, and rejection of a
partial failed poll. Genuine 0% remains valid after settling; there is no general
SoC-jump detector or inverter-state whitelist. The protection demonstrated here
is the existing startup/active-control fault pause, not rejection of every
transient zero. The pause starts at startup/loss, not at recovery: after a long
outage, an initial numerically valid zero is still accepted with opt-in resume. An outage with only SoC numbers active does not independently
start a new pause.

`test_hold_diagnostics.py` exercises control-driven hold method transitions,
failed maximum acknowledgements, explicit 1040 selection and stale observations.

## Fixed-power restore and coordinator shutdown

`test_restore_and_poll_shutdown.py` covers retained fixed charge/discharge
requests when learning a lower hybrid maximum or after BMS derating, invalid
restored values, cancellation during inverter/KSEM reads, blocked refreshes after
unload, and continued polling if platform unload fails. Control writes still
clamp retained requests to the current maximum; new user input remains bounded.

Run `python tools/ha_poll_shutdown_smoke.py` in the separate real-HA environment
to check an in-flight `async_refresh` through successful entry unload and repeated
shutdown. It uses the real coordinator and Modbus locking with an in-memory
transport; platform unload is mocked successful. No live services are accessed.

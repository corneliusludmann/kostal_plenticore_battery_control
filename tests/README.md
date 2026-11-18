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

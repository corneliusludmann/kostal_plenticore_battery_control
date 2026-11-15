# Control reliability changes in this fork

Based on upstream 4.1.2 (83e1a23e11dc74c3654c0e9de7eb998faf37c8bb).
These changes have automated unit/regression tests against actual integration
modules with explicit Home Assistant and socket boundary doubles. They have not
been deployed to an inverter or validated in a complete Home Assistant instance.

## Behavior changes

- A completed Stop drains active control operations. Queued operations recheck
  authorization after acquiring the Modbus connection lock, immediately before
  sending. Unload revokes writers before closing connections.
- A shared control lock coordinates switch actions, SoC limits, reserve holds
  and option-driven hold changes. The settling clock starts after the final
  stop operation. Turning the non-writing EMS monitor off does not introduce
  a battery-control pause.
- Controls require a successful recent poll, finite battery measurements, a
  valid SoC and Modbus battery-management mode. The poll-start timestamp is
  used conservatively; slow optional reads cannot make old samples appear new.
- A failed coordinator poll immediately revokes active control permission,
  even if the failure occurs between two write ticks.
- Grid control rejects absent, nonfinite, future-dated and stale external
  measurements. An unchanged but still fresh value can retain a setpoint.
  The maximum age is checked again before transmission.
- Enabled EMS protection blocks charging when phase-current data or its initial
  computed charge limit is unavailable. It can resume monitoring fresh data.
- Automatic communication recovery is opt-in, **off by default**. No active
  control switch is restored after an HA restart. SoC numbers retain their
  documented restore behavior after pair validation.
- Every Charge Start session has a monotonic expiry (default 60 minutes).
  Startup waits and communication outages count toward this duration. Recovery
  does not extend it. Changing the configured duration affects the next session.
  This applies to Charge Start including Predbat operation; external schedulers
  must deliberately start a new session after expiry. Other continuous modes
  retain their existing lifetime but require fresh measurements.
- Stop is not an emergency disconnect. A packet already sent cannot be recalled;
  the inverter may take its own timeout to resume internal control. Internal
  regulation can then charge or discharge normally.

## Configuration

Options add:

| Option | Default | Accepted values |
| --- | --- | --- |
| Automatic recovery | off | boolean |
| Maximum measurement age | 60 seconds | whole seconds, 10–300 |
| Charge Start maximum duration | 60 minutes | whole minutes, 1–1440 |
| Local HTTP firmware fallback | off | boolean |

The last option only allows reading firmware information from the configured
inverter when its version register is unsupported. It does not enable external
telemetry. Leaving it disabled avoids that unencrypted HTTP request.

The configured inverter timeout must be an integer of 10–3600 seconds. This is
an implementation constraint, **not a statement of the manufacturer's permitted
range**. Set it to the real external battery-control timeout. The integration
does not configure that setting on the inverter; do not confuse it with the
separate G3 limiter timeout register.

SoC settings must be finite, in range, and minimum must not exceed maximum.
Enabled minimum/maximum entities complete restoration together before any
restored limit is written. Disabled entities retain inactive defaults and do not
block setup. Invalid restored limits block control until an explicit consistent
setting is made. If an invalid restored power is encountered it defaults to zero,
not maximum power. Enter an explicit modest charge rate before commissioning.

A maximum SoC limit is not a complete charge-session target. It can also limit
later PV charging while active. Use a separate HA automation to stop a session
at the desired SoC and deliberately release temporary limits after settling.

## Regression coverage

Tests exercise actual production methods rather than extracted copies:

- active writes and a simultaneous Stop or unload;
- a real handler waiting for connection while permission is revoked;
- refused/unknown unit-addressing APIs and nonfinite/overflow payloads;
- stale versus unchanged fresh external grid values;
- invalid critical poll values and read failures;
- enabled EMS with lost phase-current data;
- actual startup/fault/recovery/expiry paths;
- limits waiting behind control operations, invalid restored pairs with an
  explicit scheduling gap, and disabled number entities;
- runtime/UI option validation and invalid restored power/fuse inputs.

Run with `python -m pytest --cov --cov-report=term-missing`. The CI matrix runs
Python 3.13 and 3.14. Overall coverage includes the much larger unchanged sensor
and Predbat code; it is not a certification or a promise of zero defects.
See [test harness scope](../tests/README.md).

Before deploying, verify the configured timeout, word order, device mode and
single-writer ownership. Test a small bounded charge, manual stop, expiry,
communication loss/recovery and HA restart on the actual installation. This
repository change alone does not authorize or perform those operations.

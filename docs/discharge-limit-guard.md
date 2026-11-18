# Residual-discharge guard on upstream 4.2.0

This fork rebases its reliability fixes onto upstream `69021a3` (4.2.0).
The previous fork tip was `9c17b34`. All seven fork commits were retained;
conflicts reconcile the new guard and SunSpec maximum-voltage estimate with
writer ownership, acknowledged SoC-limit ordering, explicit maximum-power
overrides and signed 531/532 scaling. No register 1282 control is introduced.

## Behavior

With **Hold through the discharge limit (1040)** selected, a hold first blocks
1040. Upstream now defaults this option to on; an explicitly saved off value
continues selecting the existing minimum-SOC-only behavior.

When the battery continues delivering more than 50 W for at least 30 seconds,
the integration also uses a 1042 minimum-SOC guard. Detection uses monotonic
time, valid fresh measurements, and resets pending evidence on low/missing
power or communication failure. The switch polling interval determines when
the threshold is acted on.

Unlike upstream's upward-only ratchet, this additional guard tracks current SoC
both down and up. The configured regular minimum is its floor (normally 5%,
not hardcoded). The guard does not add the hold offset. Desired and acknowledged
maximum SoC also bound writes; if that maximum is below actual SoC the bounded
guard cannot fully stop residual discharge. If the configured minimum itself is
above the acknowledged maximum, its write is deferred until the maximum allows
it. Existing maximum-setting ordering remains in force.

The purpose is to prevent active battery discharge without asking the inverter
to refill an old held SoC after drift or an estimation correction. It does not
suppress necessary inverter/battery protection or guarantee prevention of
maintenance charging. PV charging remains allowed within the existing limits.
Fixed Predbat hold targets and the explicitly selected minimum-SOC-only mode
retain their existing semantics; the new tracking concerns the additional
residual-discharge guard.

The guard remains active when power reaches zero, because zero is the intended
result, not evidence that the guard is no longer needed. It is retired on hold
release, above-target reserve release, or ownership-sensitive local cleanup.
A non-resuming fault must not leave a guard that a number keep-alive can renew.
With opted-in recovery, an active retained guard uses the new valid SoC when
control resumes after the normal settling pause.

All minimum-SOC writers share the same effective target, including a number
keep-alive running before the next switch tick. Targets reflect the latest
valid snapshot at computation; software polling is not atomic with inverter
measurements or the inverter's internal control loop.

## Diagnostics

The existing Discharge Hold Mode sensor retains its states. In discharge-limit
mode it now additionally exposes:

- `minimum_soc_guard_active`: the additional minimum-SOC guard is locally active.
- `guard_min_soc`: its effective target with configured minimum/maximum bounds.

These describe local intent, not hardware acknowledgement. Read-back attributes
and actual battery power remain necessary to assess the physical outcome.

## Verification scope

`tests/test_discharge_limit_guard.py` covers detection, zero-power latching,
downward/upward tracking, non-default minimum floors, offset independence,
number-timer interleavings, invalid/stale samples, stop/fault/ownership cleanup,
mode changes, pending maximum increases and optional recovery after a guard
write failure. Legacy tests explicitly configure minimum-SOC mode where they
exercise transitions from that mode. A power-scaling regression checks that
upstream's SunSpec estimate does not override the fork's explicit power ceiling.

Run `python -m pytest -q`. The separate real-HA scripts are
`tools/ha_smoke.py` and `tools/ha_poll_shutdown_smoke.py`. All Modbus traffic is
mocked in these checks. No live Home Assistant or inverter writes are part of
the code update; hardware efficacy is still to be observed after deployment.

## Sources

- [Upstream release 4.2.0](https://github.com/mbuhansen/kostal_plenticore_battery_control/releases/tag/4.2.0)
- [G3 rest-discharge and minimum-SOC reports](https://github.com/mbuhansen/kostal_plenticore_battery_control/issues/15)
- [KOSTAL Modbus interface, sections 3.4/3.5](https://www.kostal-solar-electric.com/fileadmin/downloadcenter/kse/BA/Protokollbeschreibung/BA_KOSTAL_Interface_MODBUS-TCP_SunSpec_with_Control.pdf)

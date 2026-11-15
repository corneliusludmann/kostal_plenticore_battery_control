# Maximum power and battery model diagnostics

## What changed

An inverter reporting a raw maximum of `100` previously limited both rate controls
to 100 W: the integration ignored the adjacent scale factor and cached the value
at setup. It now reads registers 531 and 532 together on every poll and computes
`raw × 10^signed_scale`. The independent 16-bit fields must not be word-swapped
according to the inverter's 32-bit/float byte-order setting.

Examples (test cases, not measured factors on a particular installation):

| Raw 531 | Signed 532 | Result |
|---|---|---|
| 100 | 2 | 10000 W |
| 100 | 0 | 100 W |
| 8500 | -1 | 850 W |
| 100 | -32768 (unimplemented sentinel) | Unknown; never treated as 100 W |

The documented factor range is -10 through 10. Missing scales, out-of-range
scales, and zero/unimplemented U16 mantissas are rejected. An absent pair is
optional and is probed again after a communication outage. A bad factor in a
successful response is retried on the next poll. Transport failures still fail
the poll and trigger the existing communication/control fault handling.

The Battery Max Control Power sensor exposes `inverter_max_power_raw`,
`inverter_max_power_scale`, `inverter_max_power_w`, and
`configured_max_control_power_w`, alongside the existing battery estimates.
A missing/invalid pair clears the previous device-derived maximum; automatic
calculation can still use learned/observed voltage. If no automatic maximum is
known, the pre-existing generic fallback behavior remains; an unknown value is
not proof of a device's capability.

## Why scaling may not be the whole problem

A published PLENTICORE capture reports 531/532 and SunSpec WRtg at 6825 W while
actual AC generation was 7397 W. Thus this field must not be assumed to be a
static hardware nameplate rating. The report does not establish that it changes
on every poll, nor that an observed 100 W is necessarily a scaling error.

Configure → **Maximum battery control power override** is an optional software
ceiling when a verified suitable limit is known. It takes precedence over
learned voltage, reported inverter maximum and BI-derived limits. Its supported
UI range is 100–50000 whole W (a software range, not a supported-device rating). It is
not automatically filled from product names or battery capacities. It applies
to hybrid DC and BI AC setpoints and is never rounded up above the chosen value.
Blank restores automatic estimation. Fixed requested Watts are retained, but the
command is clamped; rates that follow the maximum change with the ceiling.
Changing the option can therefore change a running control's next command.
Configure it with controls stopped. It does not change inverter configuration
or override physical device limits.

## Battery identity

The former lookup `4 → HVS 12.8` conflicts with a reported HVS+ 10.2 installation
also returning ID 4. No verified manufacturer/firmware-specific mapping was
found in the cited KOSTAL register descriptions. Revision 2.9 no longer lists
525 at all. Replacing the label with another guessed capacity would preserve
the underlying problem.

The existing text entity and unique ID remain, but the state is now e.g.
`BYD (model ID 4)`, with `model_id: 4` and `model_name_verified: false`.
Unsupported IDs/manufacturers remain diagnostic rather than invented models.
The raw-ID sensor remains available. Model text is not used to calculate control
power. Automations matching the old model-name string need adjustment.

## Checks after installing an update

With controls stopped, inspect the raw maximum, factor and scaled Watts. If the
result is still implausibly low, compare the inverter's configured feed-in limit
and verified inverter/battery specifications before choosing an override. The
update does not by itself establish the installed device's factor or rating.
Observe battery voltage/current, the BMS limits, the external control mode and
actual battery/grid power during a bounded charge/stop test. A retained setpoint
register does not prove that external control is currently active.

No factor was read directly from a live inverter for this change, and no live
HA configuration, inverter setting or charge command was changed. Unit tests and
mock-transport HA smoke checks cannot replace the physical test.

## Sources

- [KOSTAL Modbus/SunSpec interface, revision 2.9, 17 July 2026](https://www.kostal-solar-electric.com/fileadmin/downloadcenter/kse/BA/Protokollbeschreibung/BA_KOSTAL_Interface_MODBUS-TCP_SunSpec_with_Control.pdf), pp. 21–22: 531/532 and signed scaling; pp. 25–26: external battery controls. Register 582 remains S16 Watts: no unsupported association with factor 576 is introduced.
- [evcc issue 34118](https://github.com/evcc-io/evcc/issues/34118), device owner's measurement and subsequent maintainer discussion. The earlier bot explanation is explicitly corrected later in the thread; it is not used as measurement evidence.

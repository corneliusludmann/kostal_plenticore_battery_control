# Implementation review — 2026-11-15

Baseline: upstream 4.1.2, commit `83e1a23e11dc74c3654c0e9de7eb998faf37c8bb`.
Follow-up review: `980e5456a2c658466ed144353dbbe92e70984f80`.

The follow-up found nine required corrections: HA configuration serialization,
restoration of the non-writing EMS monitor, transport authorization for queued
stop writes, startup write silence, discharge holds above the maximum SoC,
local hold cleanup after faults, retried inactive-limit releases, EMS headroom
from measured battery power, and invalid/lost Predbat input handling.
All nine were corrected with targeted regression coverage.

Independent control and lifecycle reviewers inspected the corrections. Their
additional findings covered signed battery discharge in EMS headroom, background
reserve holds during repeated stop, late hold ownership, and concurrent SoC
changes before maximum acknowledgement. These were corrected and re-reviewed;
neither reviewer found remaining blocking issues in the assigned scope.

Local verification:

- 131 pytest tests passed, including 36 follow-up regression cases.
- Production modules and tests compile successfully.
- Independent real-framework smoke passed with Home Assistant 2026.11.1 and
  CPython 3.14.10 (`python tools/ha_smoke.py`): actual configuration form
  conversion/serialization, Number restore through EntityComponent (20%/80%),
  startup write suppression, post-settle writes and real entity removal with
  timer/task cleanup. This runs outside the stubbed pytest harness.
  Device association is omitted; Modbus remains mocked.
- Whole integration statement/branch combined coverage: 49%, including unchanged
  legacy sensors and Predbat code. This is not full-system coverage.

The regression harness imports real production modules and uses explicit
HA/socket boundary doubles. Physical inverter behavior has not been tested.
No deployment or plant changes were made.

## Hold diagnostics and restart regression follow-up

Added an enabled diagnostic sensor for selected hold method and fallback reason,
including requested/acknowledged limits and fresh register observations. This
does not claim physical blocking or eliminate the inverter's standby draw.

Two independent reviewers inspected the diagnostic implementation and restart
regressions. No blocking issues remained. The startup baseline of 100% before
the first acknowledged maximum write and the boundaries of transient-zero
protection are documented. The partial-poll regression also executes a control
tick after permission has been revoked.

- 142 unit/regression tests pass (11 additional cases).
- The real HA smoke also validates diagnostic entity registration, coordinator
  updates, outage attributes and listener cleanup on HA 2026.11.1.
- Restart cases use hypothetical Modbus samples based on a separate HA REST
  observation; existing quiet intervals cover the tested short transients.
  No general zero filter or state whitelist was added.

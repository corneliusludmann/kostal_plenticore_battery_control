# Implementation review — 2026-11-15

Baseline: upstream 4.1.2, commit `83e1a23e11dc74c3654c0e9de7eb998faf37c8bb`.

Two independent review agents inspected the implementation. Neither authored
production changes. The first identified a restored-SoC interleaving and missing
EMS input handling; the second identified a non-writing EMS stop incorrectly
starting global settling, a startup retry gap and stale README wording. Those
findings were corrected and re-reviewed. Both reviewers independently ran the
95-test suite successfully and found no remaining required corrections in scope.

Local verification:

- 95 pytest tests passed.
- Compile checks and `git diff --check` passed.
- 265 of 328 added/changed executable production lines exercised: 80.8%.
  Measured by intersecting added lines in the diff with coverage XML statement
  lines; this is line coverage, not branch coverage.
- Whole integration statement/branch combined coverage: 41%, including unchanged
  legacy sensors and Predbat code. This is not a claim of full-system coverage.

The test harness imports real production modules and uses explicit HA/socket
boundary doubles. Full HA startup/lifecycle integration and physical inverter
behavior were not tested. No deployment or plant changes were made. These limits
remain relevant even though no required code-review findings remain.

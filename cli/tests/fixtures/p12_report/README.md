# P12 report fixture

Shapes trimmed from the 2026-11-26 contest data and de-identified: no real commit, account,
session, device, host or workspace value is kept. `Op` carries the two `report.json` shapes
(a suite-level run and a legacy single bench-run with raw `device_us` samples) plus a stored P10
paired report; `Op2` has only separate runs so `diff` exercises its unpaired interval fallback.
The test copies this tree, creates git history and renames the `sha-cand`/`sha-base` directories
(and the `sha` fields) to the commits it makes.

# Real sliced files for the simulator

Trimmed Orca exports (`python tests/sim/trim_gcode.py IN OUT`, below about 1 MB each) that
`tests/test_sim_real.py` runs end to end with no faults, then through the M8 disconnect and M9
power-loss subsets. A file placed here with a `.gcode` extension is picked up automatically;
the tests skip when none is present.

Still wanted from the user (none is checked in yet):

| File | Where it comes from | What it exercises |
|---|---|---|
| `baby_yoda*.gcode` | `baby_yoda_christmas_xmas_PLA_155.3g_8h49m.gcode` | The 2026-11-25 incident: T4 on head 2 and a stock load on head 4 at print start, many tool changes. `test_incident_5_on_the_baby_yoda_fixture`. |
| `stock_only*.gcode` | any print that uses only the stock feeders | No BMCU command in the file; BMCU must stay out of the way. |
| `bmcu_only*.gcode` | any print that uses only BMCU channels | Every tool change and load is BMCU's. |
| `bmcu_test_print*.gcode` | the post-install `BMCU-test-print` (six bands) | The print the install guide asks the user to run first. |

Record here, for each file added: where it came from (slicer, profile, date), the size before
and after trimming, and the trimmer options used.

`tests/sim/fixtures/real/original/` is git-ignored: put the untrimmed exports there to run
`test_trimmed_file_plans_the_same_tool_changes` against them.

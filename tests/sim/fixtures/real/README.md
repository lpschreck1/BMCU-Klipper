# Real sliced files for the simulator

Trimmed Orca exports (`python tests/sim/trim_gcode.py IN OUT`, below about 1 MB each) that
`tests/test_sim_real.py` runs end to end with no faults, then through the M8 disconnect and M9
power-loss subsets. A file placed here with a `.gcode` extension is picked up automatically;
the tests skip when none is present.

Supplied by the user on 2026-11-26 (OrcaSlicer with the BMCU-Klipper Tool Assignment plugin, Snapmaker U1):

| File | Original | Lines | Trimmed with | What it exercises |
|---|---|---|---|---|
| `baby_yoda.gcode` | `baby_yoda_christmas_xmas_PLA_155.3g_8h49m.gcode`, 1,700,936 lines | 53,654 | `--lean --every 5000` | The 2026-11-25 incident: stock T0/T1/T3 and BMCU T4, 874 tool changes. `Incident5Test.test_on_the_baby_yoda_fixture`. |
| `bmcu_only.gcode` | `band_4_PLA_11.9g_37m10s.gcode` (the six-band test print, every band on BMCU) | 35,413 | not trimmed | BMCU T4–T7 only, 76 changes, fast same-Head swaps on Heads 3 and 4. |
| `bmcu_test_print.gcode` | `band_1_PLA_5.5g_14m34s.gcode` (the six-band test print, one stacked tower) | 46,468 | not trimmed | The post-install check: T4 into Head 3, T7 into Head 4, stock T1, stock T0, Head 4 handed back to stock T3, T4 swapped for T5 on Head 3. `TestPrintTest` pauses in band 4 and resumes. |
| `bmcu_test_print_back_to_back.gcode` | `band_1_PLA_5.5g_14m34s.gcode` re-sliced 2026-11-26 with a back-to-back swap | 46,467 | not trimmed | T4 then T5 on Head 3 back to back, T6 into Head 4, stock T1 and T0, Head 4 handed back to stock T3. `BackToBackTestPrintTest` runs every `TestPrintTest` case on it. |
| `bmcu_mixed_side_by_side.gcode` | `band_6_PLA_11.9g_37m10s.gcode` (the six bands sliced side by side, mixed) | 35,471 | not trimmed | Stock T0/T1 and BMCU T4–T7, 76 changes. Found F14. |
| `stock_only.gcode` | `Small_PLA_4h8m.gcode` | 43,628 | not trimmed | Stock T0/T2 only, no BMCU commands. BMCU must stay out of the way. |

`bmcu_only.gcode` and `bmcu_mixed_side_by_side.gcode` have 75 filament changes, not 5: the six parts were sliced side by side, not as one stacked object.

Record here, for each file added: where it came from (slicer, profile, date), the size before
and after trimming, and the trimmer options used.

For a long print (many hours), add `--lean`, then raise `--every` if it is still over 1 MB.

`tests/sim/fixtures/real/original/` is git-ignored: put the untrimmed exports there to run
`test_trimmed_file_plans_the_same_tool_changes` against them.

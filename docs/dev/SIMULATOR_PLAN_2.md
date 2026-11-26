# Simulator plan 2: restarts, disconnects and real files

Continues `docs/dev/SIMULATOR_PLAN.md` (M1–M7, done). Read that plan,
`tests/sim/README.md` and `tests/sim/FINDINGS.md` first. Same goal: **no print
may fail in a way it can't be resumed.** Same rules (below). Findings continue
from **F5**.

## What's already there

- `faults.BmcuDisconnect(duration)` and `faults.HelperStuck` exist, but no
  scenario or sweep case uses them.
- `Sim(state_dir=...)` keeps BMCU's state files in a directory you choose. Nothing
  yet builds a second `Sim` over the same `state_dir` and `World`.
- The sweep (`sweep.py`) injects faults at G-code lines. BMCU prestages the next
  source in the background, so faults injected at a tool-change line often land too
  late to hit the load.
- Prints come only from `gcode_gen.py`.

## Rules (unchanged)

- **Don't fix product code** (`klippy/`, `scripts/`, `web/`). A bug is an
  `@unittest.expectedFailure` test plus a `FINDINGS.md` row: trigger, what
  happens, what should happen, the suspected code with `file:function`, and
  your confidence.
- **No seams in production code.** Drive it from outside, as M1–M7 did.
- Deterministic: virtual clock, seeded randomness, no real sleeps or threads.
- Python 3.8 compatible. Everything new must pass on Linux CI (3.8, 3.11, 3.13).
- Assert on public effects (status, world, G-code sent, pause messages), not
  private manager state.
- A scenario that reproduces a fixed incident needs a **negative control**
  (`mock.patch.object` the fix off, and the old failure comes back).
- The default suite must stay under about 90 s. Put larger sweeps behind
  `BMCU_SIM_FULL=1`, and always run a representative subset.
- Commit after each milestone with a message starting `M8:`, `M9:` and so on, and
  push so CI runs. Don't move on while CI is red.

## Every scenario ends with the same checks

After any fault, restart or disconnect, plus whatever the user does (`clear_fault`,
`resume`):

1. The print **completes**, or it is paused with a message that names what the
   user should do. "Stuck" means no sequence of reasonable user actions finishes
   it, and that is a finding.
2. `world.consistent() == []`: every source BMCU thinks is loaded is physically
   where it thinks.
3. No channel or endpoint lock is left once the print ends (what the web page
   shows as "locked").
4. Every head printed with plastic: no layer extruded while the world shows the
   nozzle empty (the "air on the prime tower" failure).

Write a shared helper for this, `assert_recoverable(sim, actions)`, so every
milestone uses the same definition.

## M8: phase triggers and BMCU disconnects

1. **Phase triggers** in `faults.py` that fire on simulated machine state instead of
   G-code lines:
   - `BmcuMoving(device, direction='load'|'unload', after_mm=…)`: the fake
     firmware has moved that channel `after_mm` in that direction.
   - `StockFeeding(head, phase)`: the fake Snapmaker feeder is in a
     `filament_feed` phase (`load_feeding`, `load_extruding`, `load_flushing`,
     `unload_*`).
   - `BeforeOnUse(head)`: inside BMCU's BEFORE_ON_USE / toolhead check.
   - `Prestaging(tool)`: BMCU's background prestage for `tool` is running.
   Trigger only from the world or fake-firmware state, never from manager
   internals.
2. **Disconnect sweep.** `BmcuDisconnect` with durations of 3 s (a glitch), 30 s
   (unplug and replug) and "for the rest of the print, until the user replugs" (the
   loose cable you had). Inject at idle mid-layer, at `BmcuMoving` load at 50 mm and
   at 500 mm, at `BmcuMoving` unload at 50 mm, at `BeforeOnUse`, and at `Prestaging`.
   For the long case, the user action is to replug and then Resume.
3. **Staging faults** (the open item from M6). Run `Snag`, `DeformedTip` and
   `Runout` through `Prestaging` and `BmcuMoving`, so faults hit the load
   itself and not the tool-change line.
4. **Helper death.** The transport helper process exits mid-print, and the
   bootstrap `--repair` path brings it back. Use the existing helper fake and add
   an `exit` mode.

Add the results to `SWEEP.md`, in a new section with the same table format.

## M9: Klipper restart and power loss mid-print

Add `Sim.restart(kind, at)`. It throws away the fake Klipper, the Snapmaker
modules and the BMCU manager, and builds new ones over **the same `World`**
(filament stays where it physically is) and **the same `state_dir`**. Two kinds:

- `'klipper'`: Klipper restarts (a crash or `RESTART`). The BMCU helper and
  firmware keep running, and the bootstrap reuses the helper
  (`bmcu_host_bootstrap.py --repair` decides when). The print is lost, and the
  user starts it again.
- `'power'`: everything loses power. The BMCU firmware restarts, the helper
  restarts, heaters go cold, and motors stop wherever they were (the world must
  stop motion at that instant). On boot the U1 offers **power-loss resume**.

**Model the U1's power-loss resume** in `virtual_sdcard.py` from the 2.1.0
source, `klippy/extras/virtual_sdcard.py`: `record_power_loss_info`,
`power_loss_info_check`, `parse_power_loss_move_env`,
`cmd_SDCARD_PRINT_PL_RESTORE` and the restore path that runs
`G28 X Y PL_SAVE_Z_HOP`, re-selects the extruder, and runs
`move_extrude_macro` and the temperature restore. Model only as much as BMCU
can see: the file offset it resumes from, the active extruder, the G-code it
replays and the order of events. Record what you left out in
`tests/sim/README.md`.

Restart points (use the M8 phase triggers):

| Point | Why |
|---|---|
| Idle, mid-layer | baseline |
| BMCU loading, 50 mm and near arrival | tip is between the BMCU and the head |
| BMCU unloading, 50 mm | tip is in the shared path |
| Between unload and load on the shared head | both channels are "out" |
| Stock hand-back, during Snapmaker's `load_feeding` | ownership is changing hands |
| BEFORE_ON_USE | load not confirmed |
| Just after a durable state write is queued but before it lands | state file is older than the world (replace `DurableWriteWorker` with one that can drop its last write) |
| `bmcu_state.json` truncated or empty on boot | corrupted save |

For each point, check:

- After the restart, BMCU's view either matches the world, or BMCU knows it
  doesn't (UNCERTAIN routes, identification at print start, or a pause naming
  the head). A dead end like the 2026-11-24 "expected UNCERTAIN route, got
  LOADED" is a finding.
- `'power'`: power-loss resume completes, or pauses with an actionable message
  and then completes. BMCU's tool-change plan must line up with the resumed
  file offset. The planner rescans after boot, so check that the plan cursor
  matches the resume point and that no tool change is skipped or repeated.
- `'klipper'`: starting the same print again completes, and nothing BMCU had
  loaded is forgotten or double-loaded.

## M10: real sliced files

1. **Trimmer.** `tests/sim/trim_gcode.py IN OUT` shrinks a real Orca export
   below about 1 MB while keeping what BMCU and Snapmaker react to:
   - the header and config block;
   - the start and end G-code;
   - every tool change, `BMCU_*` and `SET_PRINT_*` line, temperature command,
     and layer change, each with about 30 lines of context;
   - the first and last few moves of each layer, keeping only every Nth move in
     between.
   Extrusion along each kept move must still total the original layer's E
   distance, so purge and prime volumes stay realistic. Add a test that the
   trimmed file plans the same tool changes, in the same order with the same
   temperatures, as the original, using `gcode_plan.scan_file` on both. The test
   runs only if the original is present, so CI skips it.
2. **Fixtures.** Put trimmed files the user supplies in
   `tests/sim/fixtures/real/`, with a `README.md` saying where each came from and
   what it exercises. Ask the user for: the Baby Yoda print
   (`baby_yoda_christmas_xmas_PLA_155.3g_8h49m.gcode`), one print that uses only
   the stock feeders, one that uses only BMCU, and the post-install test print
   (`BMCU-test-print`, six bands).
3. **Run them.** `Sim.slice_file(path)` loads a real file in place of
   `gcode_gen`. Each fixture must run end to end with no faults. Then run the M8
   disconnect subset and the M9 `'power'` subset on one of them.
4. **Any file, locally.** `BMCU_SIM_GCODE=/path/file.gcode python -m unittest
   tests.test_sim_real` runs any file through the simulator, so the user can
   check a print before starting it.
5. **The 2026-11-25 incident.** At print start the stock load on head 4 failed
   with `extruder[3]: state: load_extruding, error: move_extrude!`, after
   Snapmaker's extrude test retried for about 5 minutes. T4 had just loaded on
   head 2. Reproduce it on the Baby Yoda fixture with `Clog(3)` at
   `StockFeeding(3, 'load_extruding')`. Check that clearing the fault and
   resuming completes, and that the pause message is understandable.

## Report back

When M8–M10 are done and CI is green, give the user a report like the M1–M7
one:

- What was built, with test counts and suite time.
- The new `SWEEP.md` totals: no effect, resumed, stuck.
- Each new finding (F5 onward), with a one-line summary, the trigger,
  confidence, and the test that shows it.
- What the simulator still can't model.

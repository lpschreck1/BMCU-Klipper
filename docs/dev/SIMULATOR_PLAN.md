# Snapmaker U1 + BMCU simulator: implementation plan

This plan is for the engineer, or AI agent, who builds it. It assumes no prior context.

## 1. Goal

**No print may fail in a way it can't be resumed.** Some failures are physical and will always happen: a snag, a clog, a spool running out. Each of those must end as a **pause** that leaves the printer in a known state and says what to fix. After the user fixes it and presses **Resume**, the print must continue to the end.

Today nothing tests that end to end. The unit tests in `tests/` check single decisions, while every real failure so far has come from how steps *combine*: fail, pause, resume path, retry. Each fix was only proven on the real printer, one pause at a time. This simulator lets the whole sequence run in CI.

### Success criteria

1. A scenario test can run a short sliced print through the real `bmcu_core` code, inject a fault at any step, act as the user (clear the fault, press Resume) and check that the print reaches the end.
2. The incidents in section 6 are reproduced as scenarios. Each one fails on the code that had the bug and passes on the current code.
3. A **resume sweep** (milestone M6) injects each fault type at every tool change of a reference print. It reports every case where Resume can't finish the print.
4. Runs in CI (`.github/workflows/tests.yml`), on Python 3.8, 3.11 and 3.13, deterministically, in under 2 minutes, with no real sleeps.

### Non-goals

- Simulating Klipper's motion planner, steppers or MCUs. Only positions, homed state and temperatures matter.
- Simulating the BMCU firmware's control loops: PID, PWM, pressure control.
- Snapmaker firmware 2.1.2. Its source isn't published, so we model 2.1.0 (see 2.3).
- Klipper failing to start (missing MCUs and the like). That happens before BMCU runs.

## 2. Context for the implementer

### 2.1 The system

- **Printer:** Snapmaker U1, a 4-head toolchanger running Snapmaker's Klipper fork.
  - Each head (1–4, `extruder`…`extruder3`, `e0`…`e3`) has a stock feeder channel: `filament_feed left` serves heads 1 and 2, `filament_feed right` heads 3 and 4.
  - Each head has an entry sensor (`filament_motion_sensor eN_filament`) and a coil sensor at the gears (`get_coil_freq`).
- **BMCU:** an external 4-channel filament unit with one encoder wheel per channel. It talks USB serial to a helper process (`scripts/bmcu_transportd.py`), which Klipper reaches over a Unix socket.
  - BMCU channels are routed to heads. On the reference machine, channels 1–2 (T4, T5) go to head 3 and channels 3–4 (T6, T7) go to head 4.
  - Where a head has two inputs, a splitter joins the BMCU tube to the stock feeder tube.
- **BMCU-Klipper:** a Klipper extra (`klippy/extras/bmcu_core/`). It owns logical tools T4–T7, and loads, unloads, swaps and recovers them around Snapmaker's own feeder logic. It also wraps several Snapmaker commands (`RESUME`, `CANCEL_PRINT`, `SM_PRINT_AUTO_FEED`, `INNER_AUTO_REPLENISH_FILAMENT`).

### 2.2 Code you will drive (do not rewrite it)

| Module | Lines | Role |
|---|---|---|
| `bmcu_core/manager.py` | ~19,700 | Commands, print plan, tool changes, recovery, ownership. |
| `bmcu_core/endpoints.py` | ~5,700 | Per-head logic, including all Snapmaker U1 interaction. |
| `bmcu_core/device.py` | ~1,900 | One BMCU, talking to its helper over a Unix socket. |
| `bmcu_core/protocol.py` | ~600 | Frames, message types, status and runtime-sync packing. |
| `bmcu_core/transport.py` | ~80 | Socket framing to the helper. |
| `bmcu_core/state.py`, `persistence.py` | | Saved state (`bmcu_state.json`). |
| `bmcu_core/refill.py` | | Backup spools. |
| `bmcu_core/gcode_plan.py` | | Plans tool changes from the G-code. |

Existing tests are in `tests/`, using unittest. `tests/_support.py` has a `FakeReactor`. Read `tests/README.md` and `tests/test_u1_stock_handoff.py` first: they show which stubs the code tolerates.

**Base revision:** BMCU-Klipper `main` with `bmcu-klipper-u1-stock-handoff.patch` applied (branch `u1-preload-clear`, BMCU firmware 1.0.5).

### 2.3 Behaviour sources

| What | Source |
|---|---|
| Snapmaker Klipper 2.1.0 | `github.com/Snapmaker/u1-klipper`. Key files: `klippy/extras/filament_feed.py`, `filament_motion_sensor.py`, `print_task_config.py`, `gcode_move.py` (`printing_e_pos`), `lava/fluidd.cfg` (macros: `INNER_RESUME`, `SM_PRINT_AUTO_FEED`, `INNER_FLUSH_FILAMENT`, `INNER_PREEXTRUDE_FILAMENT`, …), `lava/printer.cfg`. |
| BMCU firmware | `firmware/src/` (C++, CH32V). `bmcu_protocol.cpp` defines command semantics; `Motion_control.cpp` covers autoload, including the 30 s pause over 1 m. |
| Print start / tool-change G-code | `printers/Snapmaker_U1/Snapmaker_Orca/machine/Snapmaker U1 BMCU.json` (`machine_start_gcode`, `change_filament_gcode`). |
| Real failure logs | Section 6. |

Wherever this plan says *observed*, the behaviour was seen on the printer and should be confirmed in the source.

## 3. Architecture

Pure Python, in-process, virtual time. Five layers:

```
 tests/sim/scenarios/*.py          scenario tests (pytest-free unittest)
          │
 tests/sim/runner.py               runs G-code lines, injects faults, plays "user"
          │
 tests/sim/klipper.py              fake Klipper: printer, reactor, gcode, macros,
          │                        print_stats, virtual_sdcard, toolhead, heaters
 tests/sim/snapmaker.py            fake Snapmaker modules (filament_feed, sensors,
          │                        print_task_config, gcode_move, macros from fluidd.cfg)
 tests/sim/bmcu_fw.py              fake BMCU firmware at protocol.py level
          │
 tests/sim/world.py                physical model: where every filament tip is
          │
 real code: klippy/extras/bmcu_core/*   (unchanged, except agreed seams, see 7)
```

### 3.1 Virtual time

One clock, owned by the fake reactor. `reactor.pause(t)` and timers advance it; nothing sleeps for real. Every fake (feeders, heaters, BMCU ops) advances its physical state through a single `world.step(dt)` called from the clock. Extend `FakeReactor` in `tests/_support.py` rather than starting another one.

### 3.2 Physical model (`world.py`)

This is the heart of the simulator: if it is right, everything above it can be checked against it.

- **Paths.** Each head has a path made of segments in mm:
  - BMCU path per channel: BMCU outlet → (BMCU tube length) → splitter → (shared length) → head entry sensor → (sensor to gears) → gears → (gears to nozzle, ~70 mm) → nozzle.
  - Stock path: feeder → (stock tube length) → splitter → shared → … the same from the splitter on.
  - All lengths are configurable per head and channel. Defaults follow the reference machine: preload 850 mm, BMCU tube ~1.5 m, sensor to nozzle 70 mm.
- **Filaments.** Each source (BMCU channel or stock feeder) has at most one filament, described by:
  - tip position (mm along its path) and tip quality (`good` / `deformed`);
  - whether it's in the gears;
  - spool remaining.
- **Collision rules.** Two tips can't occupy the shared segment past each other. A push into an occupied segment stalls the pushing source's wheel. That is what makes "long autoload parks in the shared tube" fail realistically.
- **Derived sensors:**
  - **Head entry sensor:** any tip past it. Model the *lag* separately (3.4).
  - **Coil frequency:** rises by about 1000 over the baseline when filament moves through the gears to the nozzle, and stays near the baseline otherwise. Seen on the printer: a good grab is about 1000 or more, a miss is under 100. The BMCU check needs 400 or more.
  - **BMCU encoder (`meters`):** integrates its filament's motion, including being *pulled* by the head's gears. The identification feature depends on this.
  - **Stock feeder wheel counts:** `cnt_a/b`, with ppr 6. Distance in mm = counts × 31.4159 / 12.
- **Heaters:** first-order approach to the target at about 5 °C/s. Extruding is refused below 170 °C, mirroring Klipper's `can_extrude`.
- **Nozzle:** flow is blocked when a `clog` fault is active.

### 3.3 Fake BMCU firmware (`bmcu_fw.py`)

Fake it at the **protocol** level, so that `device.py` and `protocol.py` run for real.

- Preferred boundary: a fake helper. Serve the socket that `device.py` connects to (`transport.py` framing: `encode_message`, `MessageDecoder`) and answer the protocol frames. That also exercises the helper's control messages.
- If socket plumbing in tests is awkward, a fallback is to fake `device.BMCUDevice`'s I/O method only, keeping all of its parsing. This must stay one swappable class.

Handle at least these messages (`protocol.py`): `HELLO`/`SESSION_CONFIRM`, `PING`, `GET_STATUS`, `GET_CAPS`, `GET_CALIBRATION`, `GET_OP_RESULT`, `GET_SNAPSHOT`, `SET_MOTION`, `SET_ACTIVE_CH`, `FEED_TO_CONTACT`, `FEED_DISTANCE`, `CHANNEL_RETRACT`, `CHANNEL_PRELOAD`, `CHANNEL_AUTOLOAD`, `ABORT_OP`, `MARK_LOADED`, `MARK_UNLOADED`, `RUNTIME_SYNC` (validate it like `runtime_value_valid`: autoload 0.01–2.0 m), `SET_SLOTS`, `STOP_ALL`, `RESET_ERROR`.

The state it keeps:
- per-channel `route_state` (`EMPTY`/`LOADED`/`UNCERTAIN`) and `present`;
- `meters` from the world;
- `buffer_pct`, which rises when the channel pushes against a stall;
- `motion`, `calibration_valid_mask`, and op ids with results.

Report firmware version 1.0.5, because the host requires an exact match (`klippy/extras/bmcu_core/release.py`).

### 3.4 Fake Snapmaker modules (`snapmaker.py`)

Model behaviour from the 2.1.0 source. Fake only the attributes BMCU-Klipper actually touches (the inventory is in section 4).

- **`filament_feed left|right`** (a module with 2 channels):
  - `channel_state`: `wait_insert`, `preload_finish`, `load_finish`, `unload_finish`, …
  - preload stops after `_feed_preload_counts` or at the head sensor, whichever is first;
  - `FEED_AUTO MODULE CHANNEL LOAD=1 PRINTING=1`, run in phases: phase 2 pushes at the load position until the head sensor triggers. Phase 3 moves to the discard spot, heats, and makes grab attempts judged on coil change and wheel counts. Then comes `INNER_FLUSH_FILAMENT`;
  - the 60 s `FEED_LOAD_TIMEOUT_TIME`, and the `wheel_speed` error after 20 still cycles;
  - attributes: `runout_sensor`, `_put_into_drive`, `_hang_neutral`, `motor.run_one_cycle`, `wheel[ch].get_counts()`.
- **`filament_motion_sensor eN_filament`:**
  - `runout_buttun_state` is the raw switch;
  - `runout_helper.filament_present` **lags**: it stays true for `detection_length` mm of extruder motion after the switch opens, driven by `_extruder_pos_update_timer`;
  - `get_coil_freq()`.
- **`print_task_config`:** per-head `filament_type`, `filament_exist`, `filament_edit`, colours and `extruders_used`; `INNER_CHECK_AND_RELOAD_FILAMENT_INFO` raises *"eN not edit filament"* when the type is `NONE`.
- **`gcode_move`:** `printing_e_pos` accumulation while printing (clamped at 0 when positive).
- **Macros.** Implement as Python only the ones that run, modelled on `lava/fluidd.cfg`: `SM_PRINT_AUTO_FEED`, `MOVE_TO_DISCARD_FILAMENT_POSITION`, `MOVE_TO_XY_IDLE_POSITION_EXTRUDER`, `INNER_FLUSH_FILAMENT` (80 mm purge, then retract `|printing_e_pos|`, then `M104 S0`), `INNER_PREEXTRUDE_FILAMENT`, `SM_PRINT_PREEXTRUDE_FILAMENT`, `PARK_EXTRUDERn`, the cleaning macros (no-ops that log), `SM_PRINT_EXTRUDER_PREHEAT`, `SM_PRINT_FLOW_CALIBRATE` (no-op), `PAUSE`, `INNER_RESUME`/`RESUME`, `CANCEL_PRINT`.
- **`INNER_RESUME` must be faithful:**
  - its variables (`last_extruder_index`, temperatures);
  - its checks: `filament_exist` false means "filament runout" and no resume; `INNER_CHECK_AND_RELOAD_FILAMENT_INFO`;
  - its sequence: `G28 X Y`, `T{last} A0`, `SM_PRINT_AUTO_FEED EXTRUDER={last}`, `MOVE_TO_DISCARD_FILAMENT_POSITION`, and so on.

  Most unresumable bugs so far have lived in this path.

### 3.5 Fake Klipper (`klipper.py`)

- `printer.lookup_object(name, default)` and `printer.get_reactor()`.
- `gcode`:
  - `register_command` (with the `None` detach semantics BMCU uses to wrap commands: see `_install_u1_stock_feed_wrapper`);
  - `ready_gcode_handlers`;
  - `run_script_from_command` (multi-line);
  - `respond_info`;
  - `gcmd.error(..., action='pause')`.
- `T0`–`T3 A0`: the Snapmaker physical head select, which parks and picks heads and sets the active extruder.
- `print_stats` state: `standby` / `printing` / `paused` / `complete` / `cancelled`.
- **Pause on error:** when a command raises an error with `action='pause'`, pause, and on Resume **run the failed command again** before continuing.
  - *Observed:* a failed `BMCU_TOOL_CHANGE` was retried on Resume.
  - Confirm this against Snapmaker's `virtual_sdcard` / `print_stats` / `pause_resume` handling and model what the source does.
- `virtual_sdcard`: feeds lines from a G-code file, keeps the file position (BMCU reads it through `_u1_current_sd_position`), and supports pause/resume.
- `toolhead`: `get_status()['homed_axes']`, position, `wait_moves`, and `G28`.
- `heaters`, extruders (`get_heater()`, `target_temp`, `min_extrude_temp`, `can_extrude`), `M104`/`M109`/`SET_HEATER_TEMPERATURE`/`TEMPERATURE_WAIT`.
- `G1 E…` moves the active head's extruder: the world pulls filament through the gears, and the BMCU encoder of the channel holding it moves too. Also `M82`/`M83`, `SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE` and `M400`.
- `configfile` settings (nozzle sizes), `idle_timeout`, `machine_state_manager`, `buttons`, `output_pin`, `stepper_enable`, `filament_detect`, `filament_parameters`: start them as minimal stubs and grow them on demand.

### 3.6 Runner (`runner.py`)

```python
sim = Sim.reference_machine()                 # 4 heads, 1 BMCU, splitters on heads 3-4
sim.load_spools({4: 'PLA red', 6: 'PLA black', 3: 'PLA white'})
sim.slice(tools=[0, 4, 6, 3], layers=3)       # synthesize a small print (see 3.7)
sim.inject(at=ToolChange(tool=4, n=1), fault=Snag(head=3, after_mm=58))
result = sim.run_until_pause()
assert 'ENDPOINT_NOT_REACHED' in result.message
sim.user.clear_fault()                        # what the human would do
sim.user.resume()
sim.run_to_end()
assert sim.print_state == 'complete'
assert sim.world.consistent()                 # every filament where BMCU's state says
```

### 3.7 G-code

Generate small prints from the real BMCU profile templates (start G-code and `change_filament_gcode`, from the machine JSON), with tool changes at chosen layers and short extrusion moves. Optionally run the output through the OrcaSlicer plugin's post-processing (`bmcu_klipper_plugin.py` in the tool-assignment repo), so the `BMCU_PRINT_*` plan matches real prints.

## 4. Interfaces to fake (inventory)

These are generated from the code. Re-run the commands after changes and treat any new entry as work to do.

- **`lookup_object` targets:** `toolhead` (15), `print_stats`, `virtual_sdcard`, `print_task_config`, `filament_detect`, `filament_parameters`, `filament_feed %s`, `idle_timeout`, `heaters`, `gcode_move`, `gcode_macro INNER_RESUME`, `stepper_enable`, `output_pin %s`, `machine_state_manager`, `gcode`, `configfile`, `buttons`.
  ```
  grep -rhoE "lookup_object\(\s*['\"][^'\"]+['\"]" klippy/extras/bmcu_core/ | sort | uniq -c
  ```
- **G-code BMCU sends:** `RESUME`, `M109`, `MOVE_TO_DISCARD_FILAMENT_POSITION`, `PAUSE`, `SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE`, `M104`, `INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD`, `INNER_DISCARD_FILAMENT_BASE_DISCARD`, `SM_PRINT_AUTO_FEED`, `INNER_CUTOFF_BASE_DISCARD`, `G28`, `T<n> A0`, `INNER_APPLY_FLOW_K`, `CANCEL_PRINT`, `PARK_EXTRUDER<n>`, `MOVE_TO_XY_IDLE_POSITION_EXTRUDER`, `INNER_AUTO_REPLENISH_FILAMENT`, `FEED_AUTO`, `SET_HEATER_TEMPERATURE`, `TEMPERATURE_WAIT`, `SM_PRINT_PREEXTRUDE_FILAMENT`, `INNER_FILAMENT_UNLOAD`, cleaning macros.
- **Snapmaker internals touched** (from `endpoints.py`):
  - `feeder._feed_preload_counts`, `_bmcu_printer_cfg_preload_counts` (BMCU's own), `feeder.runout_sensor`;
  - `sensor.runout_buttun_state`, `sensor.runout_helper` (`filament_present`, `get_ffi`), `sensor._extruder_pos_update_timer`, `sensor.get_coil_freq()`, `sensor.get_status()`;
  - feeder motor and wheels (`run_one_cycle`, `get_counts`), `_put_into_drive`, `_hang_neutral`;
  - module constants (`FEED_WHEEL_CIRCUMFERENCE`).
- **Commands BMCU wraps:** `RESUME`, `CANCEL_PRINT`, `SM_PRINT_AUTO_FEED`, `INNER_AUTO_REPLENISH_FILAMENT`. The fake gcode must support detaching with `register_command(name, None)` and re-registering.

## 5. Faults to inject

Each fault is a small class, applied through the world or a fake, and triggered by an event (`ToolChange(n)`, `PrintStart(head)`, `Resume(k)`, a time, or a G-code line).

| Fault | Effect |
|---|---|
| `Snag(head, path, after_mm)` | Filament stops advancing at that point; the wheel stalls. |
| `Clog(head)` | Gears grip but nothing reaches the nozzle; coil change stays small. |
| `DeformedTip(source)` | Gears miss the grab (phase-3 retries). |
| `Runout(source, at_mm)` | Spool empties; `present` goes false. |
| `SensorLag(head, mm)` / `SensorStuck(head, value)` | Head sensor lags or sticks. |
| `ParkedInShared(channel)` | Autoload longer than its tube; the tip sits past the splitter. |
| `Unrecorded(head, source)` | Filament physically in the head; BMCU state says empty. |
| `BmcuDisconnect(duration)` / `HelperStuck()` | Transport goes silent; the helper keeps the port open with no session. |
| `UserManual(action)` | The user loads or unloads at the screen, or pulls filament by hand. |

## 6. Incidents to reproduce first (acceptance scenarios)

Each is a real event from the reference printer. Write the scenario, confirm it fails on the commit before the fix (or on an older patch level), and passes now. Where a fix landed in the patch, its PR item number from `upstream-pr-description.md` is given.

1. **Failed T4 load → Resume refused** (PR items 14–15). A snag 58 mm into head 3's path gives `ENDPOINT_NOT_REACHED`. Resumes failed with *"e2 filament runout"* / *"e2 not edit filament"* (cleared projection), then *"RESIDUAL_FILAMENT"*. Expect: after the snag clears, Resume finishes the print.
2. **Print-start head-3 load fails its coil check → pending recovery blocks T6 on head 4** (item 16). A `Clog(head 3)` at print start gives `U1_TOOLHEAD_NOT_LOADED`. The user clears the clog and resumes through `SM_PRINT_AUTO_FEED`. Later `BMCU_TOOL_CHANGE TOOL=6` must not fail with *"recovery for T4 is pending before T6"*. Also cover the stale case: the route is `LOADED` while the recovery expects `UNCERTAIN`.
3. **Flush retract after a BMCU→stock switch** (item 4a). T6→T3 on head 4: `printing_e_pos` holds -67.5 mm from BMCU's tip shaping and pullback, so the purge retracts the new stock filament 67.5 mm and the next layer prints dry. Expect at most a 5 mm retract. Assert flow resumes within the prime tower.
4. **Stale head sensor skips the stock push.** The sensor still reports present after the BMCU unload, so `FEED_AUTO` skips phase 2. Expect the stale reading cleared and the load completes.
5. **Stock load after BMCU loses its position.** After a stock load the print resumed at the purge spot with the heater off. Expect the temperature and XY restored.
6. **Stock print on a head holding BMCU filament.** No BMCU plan, T6 in head 4, Snapmaker's own profile. Expect T6 unloaded and handed back, then the stock load.
7. **T3 stock preloaded on head 4 while T6 is loaded.** The print plan must accept it with the preload-clear setting on, and refuse with the stated reason when it's off.
8. **Long autoload parks in the shared tube** (`ParkedInShared`). The next load of the other channel must pause with a clear message, not jam. Resume after the user clears it.
9. **Unrecorded filament at print start** (`Unrecorded(head 3, T4)`). Expect it identified by encoder, kept; and swapped when the plan needs T5.
10. **Helper stuck after a bad boot** (item 12). The service start must replace it, and the helper's not-ready backstop must reopen the port.

## 7. Seams in production code

Avoid touching `bmcu_core/`. Where something can't be faked from outside, add the smallest seam that injects the dependency: a module-level factory, or a constructor argument that defaults to today's behaviour. Write a one-line comment naming the simulator. List every seam in the PR. Don't change behaviour.

When the simulator finds a bug, **don't fix it inside the simulator work.** Instead:
1. Add the failing scenario, marked `@unittest.expectedFailure` with a comment naming the bug.
2. Add it to a `tests/sim/FINDINGS.md` table: scenario, failure, suspected cause and code location.
3. Continue.

Fixes come in separate changes, reviewed by the maintainer.

## 8. Milestones

Effort estimates are for one experienced engineer. "Done" means the listed tests pass in CI.

| # | Milestone | Done when | Est. |
|---|---|---|---|
| M1 | Virtual clock, fake Klipper core, world model with one head and one stock path | Unit tests for the world: preload stops at the count or sensor; collisions stall; encoder follows pulled filament; heater approach | 1.5 h |
| M2 | Fake Snapmaker feeder and sensors, macros, `print_task_config`, `gcode_move` | Scenario 4 and a plain stock load/unload, through the real `endpoints.py` | 2 h |
| M3 | Fake BMCU at protocol level, session handshake, status, ops | A BMCU load and unload of T4 into head 3 through `manager.load_tool`/unload, matching the world | 2 h |
| M4 | `virtual_sdcard`, print plan, `BMCU_PRINT_*`, `BMCU_AUTO_FEED`, `BMCU_TOOL_CHANGE`, pause on error + Resume with retry | A 3-layer T0/T4/T6/T3 print completes with no faults | 2 h |
| M5 | Fault injection and user actions; scenarios 1–10 | All of section 6, each red on the pre-fix code where it applies | 2.5 h |
| M6 | Resume sweep: every fault type × every tool change (and print start) of the reference print | A report listing each combination where the print can't be finished by fix + Resume; added to `FINDINGS.md` | 1 h |
| M7 | CI wiring, docs (`tests/sim/README.md`: how to write a scenario), runtime under 2 min | Green on 3.8 / 3.11 / 3.13 | 0.5 h |

Total: about 11–12 hours. The earlier estimate of 6–10 h didn't include the resume sweep or the full incident set.

## 9. Risks and open questions

- **Fidelity of Snapmaker behaviour.** We model 2.1.0. Note every place the model is a guess, mark it `# MODEL:` in the code, and list them in `tests/sim/README.md`.
- **Pause-on-error retry semantics.** The "failed command runs again on Resume" behaviour is observed, not yet confirmed in the source. Confirm it, because scenarios 1 and 2 depend on it.
- **`manager.py` size and private state.** Scenarios should assert through public effects (status, the world, G-code sent, errors) rather than private attributes, so refactors don't break them.
- **Socket boundary.** If the fake helper is unreliable in CI, use the documented fallback in 3.3. Keep the protocol encoding real.
- **Time.** A few modules call `time.monotonic()` directly instead of using the reactor: `gcode_plan.py` (2), `persistence.py` (2), `transport.py` (3). Either patch them in the simulator or add a seam (section 7).

## 10. Hand-off prompt

Paste this to start the work:

> Build the Snapmaker U1 + BMCU simulator described in `docs/dev/SIMULATOR_PLAN.md` in this repo (BMCU-Klipper with the u1-stock-handoff patch applied). Work milestone by milestone (M1→M7). Commit each milestone separately, with its tests passing (`python -m unittest discover -s tests`).
>
> Model Snapmaker behaviour from `github.com/Snapmaker/u1-klipper` (2.1.0) and the BMCU firmware in `firmware/src`. Do not rewrite or refactor `klippy/extras/bmcu_core/`; add only the minimal seams allowed in section 7, and list them. When a scenario exposes a bug, record it as an expected failure and in `tests/sim/FINDINGS.md` instead of fixing it.
>
> Keep everything deterministic, with virtual time and no real sleeps, and compatible with Python 3.8. At the end, report: what was built; every `# MODEL:` assumption; every seam; and the findings table.

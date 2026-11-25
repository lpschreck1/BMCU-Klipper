# Snapmaker U1 + BMCU simulator

Runs a sliced print through the **real** `klippy/extras/bmcu_core` code (manager, endpoints,
device, protocol, planner scan) against a fake Snapmaker U1 (firmware 2.1.0 behaviour) and
a fake BMCU (firmware 1.0.5 protocol), in virtual time. Plan: `docs/dev/SIMULATOR_PLAN.md`.
Findings: `FINDINGS.md`. Resume sweep: `SWEEP.md`.

```sh
python -m pip install greenlet
python -m unittest discover -s tests        # whole suite, about 25 s, no real sleeps
python tests/sim/sweep.py --write           # refresh SWEEP.md
```

Python 3.8 compatible (CI runs 3.8, 3.11, 3.13). Deterministic: one virtual clock, seeded
randomness, no threads (the durable writer is replaced by a synchronous one).

## Layers

| File | What it is |
|---|---|
| `clock.py` | `SimReactor`: virtual time with Klipper's greenlet concurrency (every timer/callback/fd handler in its own greenlet). Extends `tests/_support.FakeReactor`. |
| `world.py` | Physical model: tip positions on each head's paths, splitter collisions, snags, spool, encoder, coil, heaters. |
| `klipper.py` | Fake printer, gcode (mux commands, detaching handlers), heaters, toolhead. |
| `snapmaker.py`, `feeder.py` | Fake Snapmaker modules and macros (`INNER_RESUME` etc.), stock feeder, head sensors. |
| `virtual_sdcard.py` | SD print with Snapmaker's pause-on-error semantics. |
| `bmcu_fw.py`, `bmcu_host.py` | Fake firmware at protocol level, fake helper, socket/os/time proxies. |
| `gcode_gen.py` | Builds a print from the Orca BMCU profile's start/end G-code. |
| `faults.py` | Faults and triggers. `runner.py`: `Sim`, the user, and result helpers. |

## Writing a scenario

```python
sim = Sim.reference_machine()          # 4 heads, 1 BMCU: ch 1-2 -> head 3, ch 3-4 -> head 4
self.addCleanup(sim.close)
sim.boot(15.0)                         # klippy:ready, BMCU connects
sim.setup_u1()                         # preset, routes, unload distances (preload_clear=True)
sim.load_spools({0: 'PLA grey', 4: 'PLA red', 6: 'PLA black', 3: 'PLA white'})
sim.slice([[0], [4, 6], [3]])          # tools per layer; T0-T3 stock, T4-T7 BMCU channels 1-4
sim.inject(faults.GcodeLine('PRINT_START'), faults.Snag('bmcu0', 58.0))
result = sim.run_until_pause()         # after sim.start()
assert 'ENDPOINT_NOT_REACHED' in result.message
sim.user.clear_fault()                 # what the human does
assert sim.user.resume() == 'complete'
assert sim.world.consistent() == []
```

Triggers: `GcodeLine(prefix, n)`, `ToolChange(tool, n)`, `PrintStart(head)`, `Resume(k)`,
`AtTime(t)`. Faults: `Snag`, `Clog`, `DeformedTip`, `Runout`, `SensorLag`, `SensorStuck`,
`ParkedInShared`, `Unrecorded`, `BmcuDisconnect`, `HelperStuck`, `UserManual`.
`sim.watch(prefix, fn)` observes the state just before a line. Assert on public effects
(status, the world, G-code sent, errors), not on private manager state.

A fix is proved with a **negative control**: rerun the scenario with the fix disabled via
`mock.patch.object` and assert it fails the way the incident did. A bug the simulator finds
is an `@unittest.expectedFailure` test plus a `FINDINGS.md` row, not a fix.

## Seams in production code

**None.** `device.py`, `manager.py` and `transport.py` are driven from outside by replacing
their module-level `os`, `socket` and `time` names and `manager.DurableWriteWorker` for the
duration of a test (`bmcu_host.BmcuRig`); `bmcu_core/` is unchanged.

## `# MODEL:` assumptions

Every guess is marked `# MODEL:` in the code. Summary:

* **world.py**: coil reading is a state, about +1000 over baseline when a tip is within 30 mm
  of the nozzle, otherwise baseline (a miss is +20); a clog stops the tip 5 mm past the gears;
  a tip past the splitter blocks the other source from entering; a source's motor only carries
  a tip from the sensor up to the gears while it is *assisting* (BMCU in SEND_OUT, BEFORE_ON_USE
  or ON_USE, or the stock feeder loading); a deformed tip or a clog is missed by the gears.
* **feeder.py**: `_do_feed` is rewritten at behaviour level (states, error codes, phases,
  60 s timeout, 20 stalled cycles), not at motor/PWM level; load success needs the coil to
  react; unload is abbreviated.
* **snapmaker.py**: moves block until done (no queued motion); `print_stats` has state names
  only; `CHECK_FILAMENT_RUNOUT` passes when the head switch is present; re-selecting the
  active head takes 1 s; user-facing macros not on BMCU's path are no-ops.
* **bmcu_fw.py**: speeds, buffer percentage dynamics (50 % idle, +60 %/s when blocked, -25 %/s
  back), bare SEND_OUT keeps pushing, PULL_BACK retracts the configured channel distance.
* **faults.py**: clearing a clog or a deformed tip means the user hand-feeds the filament
  through the nozzle.
* **gcode_gen.py**: the tool-change script is hand-translated from the profile (a drift test
  checks the profile still contains its lines).
* **runner.py**: the early-park tip tail (`BMCU_PARK_HEAD`, the default tip profile) needs
  Klipper's C step generator and is not simulated; the sim uses Snapmaker's native movements.

Snapmaker facts taken from source (2.1.0), not guessed: on a pause-action error
`virtual_sdcard.work_handler` breaks before advancing `file_position`, so **Resume runs the
failed line again** (plan section 9 asked for this to be confirmed); `INNER_RESUME`,
`INNER_FLUSH_FILAMENT`, `SM_PRINT_AUTO_FEED`, `printing_e_pos` accumulation (only while
printing, clamped at 0), the head-sensor lag, the feeder channel map.

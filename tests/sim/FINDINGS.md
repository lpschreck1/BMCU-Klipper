# Findings from the simulator

Bugs and gaps the simulator exposed in `klippy/extras/bmcu_core/`. None is fixed in the
simulator work (plan section 7): each has an `expectedFailure` test that will start to
*fail loudly* (unexpected success) the day the bug is fixed, so remove the decorator then.

"Confidence" says how sure we are that the behaviour is a production bug and not a
simulator artefact: **high** = the code path is read-through and the failure follows from
the source; **medium** = plausible on a printer but the physical trigger is modelled.

## Table

| ID | Scenario | Failure | Suspected cause and location | Confidence | Test |
|----|----------|---------|------------------------------|------------|------|
| F1 | Incident 6: stock print (Snapmaker profile, no BMCU plan) on a head that holds BMCU filament (T6 in head 4) | After BMCU unloads T6 and hands the head back, Snapmaker's stock load flushes with `printing_e_pos` ≈ -190 mm still pending (BMCU's tip forming and pullback ran while `print_stats.state == 'printing'`). `INNER_FLUSH_FILAMENT` retracts that much of the *new* stock filament, so the stock tip ends ~75 mm short of the nozzle and the first moves of the print are dry (5 mm of 5 mm dry in the reference slice). Same mechanism as incident 3 (item 4a), through a different entry point. | `endpoints.py` `prepare_native_stock_load()` (~l.2278), used by the `SM_PRINT_AUTO_FEED` hand-back wrapper (`manager.py` `_cmd_u1_stock_feed_wrapper`, ~l.1778), only clears the stale head-sensor report. The 5 mm cap `_limit_native_flush_retract()` (~l.2263) is only called from `native_feeder_load()`. | high | `test_sim_scenarios.Incident6StockPrintOnBmcuHeadTest.test_stock_print_lays_down_plastic_after_the_hand_back` |
| F2 | Incident 8: `ParkedInShared`: a BMCU tip left past the splitter blocks the other channel's load | Pauses correctly and Resume finishes once the user clears it, but the message is only `ENDPOINT_NOT_REACHED: buffer_limit after 1500.0 mm`. It names neither the blocking channel nor the shared tube, and BMCU had already fed the full 1500 mm into the obstruction. The parked channel is `EMPTY` in BMCU's state, so nothing warns beforehand. | `manager.py` ~l.12728-12760 (`ENDPOINT_NOT_REACHED`) formats the failure without checking the other channels on the same endpoint; the preload-clear / shared-path checks look at BMCU route state and the stock feeder, not at encoder travel of a sibling channel. | high (diagnostic gap, not a hang) | `Incident8ParkedInSharedTubeTest.test_pause_message_names_the_blocking_channel` |
| F3 | Incident 9: `Unrecorded`: filament physically in the head, BMCU state says empty (T4's spool), plan needs T4 then T5 | BMCU identifies the head filament by encoder travel ("checking which Channel it is"), marks the channel LOADED, then the load that follows stops: `u1_head2 has BMCU route ownership without a captured native feeder baseline`. The print pauses and Resume repeats the same error, so it cannot be finished by Resume. | `manager.py` `_arm_u1_persistent_hold()` (~l.6707): once the identified route is LOADED, `bmcu_occupied` is true but no baseline was captured (that normally happens at takeover), so it raises. `_identify_u1_head_filament()` (~l.16168) commits ownership without going through takeover. | medium (the physical trigger, a lost BMCU state, is modelled by `faults.Unrecorded`) | `Incident9UnrecordedFilamentTest.test_identified_kept_then_swapped_for_t5` |
| F4 | Fault-free print T0, T6, T4, T3 (Head 4 goes BMCU -> another head -> stock) | The print pauses with `Snapmaker background preparation failed on u1_head3: u1_head3 has an active transition or path lock`. Adjacent changes (T4, T6, T3) are fine. | `manager.py` `_u1_background_worker()` (~l.10839) calls `_release_u1_persistent_hold_if_safe(..., ignore_job=job)` to hand the head back "ignoring this job's own channel locks", but `_handoff_u1_to_native()` (~l.6235) refuses on `endpoint.name in self.endpoint_locks or path_group in self.path_locks` without honouring `ignore_job`. The job's own locks are still held at that point. The unit tests stub `_handoff_u1_to_native`, so this was never exercised. | high | `FoundByTheSimulatorTest.test_background_handoff_to_the_stock_feeder_is_not_blocked_by_its_own_locks` |

## Coverage gaps (not bugs)

* **Early-park tip tail** (`BMCU_PARK_HEAD` marker, the *default* tip profile). It drives
  Klipper's C step generator through `chelper` (`endpoints.py` ~l.4534) and cannot run in
  pure Python. The simulator uses Snapmaker's own tip movements (no marker), so tip forming
  runs attached and the head parks afterwards. Scenarios involving the early-park path
  (the real default) are *not* covered. `Sim.setup_u1(early_park=True)` selects the default
  profile and will fail at the `chelper` call.
* **Helper side of incident 10** (the service start replacing a stuck helper, the helper's
  not-ready backstop reopening the port) lives in `scripts/` and is covered by
  `tests/test_transport_recovery.py`. The simulator covers the host side: it stays offline
  without wedging and reconnects when the helper is replaced.
* **Pre-fix code** is not available (the patch is one squashed commit), so each "red on the
  pre-fix code" requirement is shown with a *negative control*: the same scenario with the fix
  switched off through `mock.patch`. See the `test_without_*` tests in `test_sim_scenarios.py`.
  Incident 7's old behaviour is the preload-clear setting off.

## Resume sweep (M6)

See `tests/sim/SWEEP.md` for the full table; unresumable combinations are listed there.

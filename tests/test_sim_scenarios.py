"""M5: the incidents of docs/dev/SIMULATOR_PLAN.md section 6, as scenarios.

Each one runs a sliced print through the real BMCU code with a fault injected, plays the
user (clear the fault, press Resume) and checks the print reaches the end. Where a fix
landed in the patch there is also a *negative control*: the same scenario with that fix
switched off (``mock.patch``) must fail the way the original incident did. That proves the
simulator would have caught the bug.

Scenarios that expose a bug are ``expectedFailure`` and listed in tests/sim/FINDINGS.md.
"""
import unittest
from unittest import mock

import _support  # noqa: F401
from bmcu_core import endpoints as E
from bmcu_core import manager as M
from sim import checks, faults as F
from sim.klipper import CommandError
from sim.runner import Sim

PLAN = [[0], [4, 6], [3]]
SPOOLS = {0: 'PLA grey', 4: 'PLA red', 6: 'PLA black', 3: 'PLA white'}


def make_sim(test, plan=PLAN, spools=None, **setup):
    sim = Sim.reference_machine()
    test.addCleanup(sim.close)
    sim.boot(15.0)
    sim.setup_u1(**setup)
    sim.load_spools(SPOOLS if spools is None else spools)
    sim.slice(plan)
    return sim


class Incident1FailedT4LoadTest(unittest.TestCase):
    """A snag 58 mm into head 3's path fails the T4 load (ENDPOINT_NOT_REACHED). The user
    clears it and presses Resume: the print must finish (PR items 14-15)."""

    def run_to_first_pause(self):
        sim = make_sim(self)
        sim.inject(F.GcodeLine('PRINT_START'), F.Snag('bmcu0', 58.0))
        sim.start()
        result = sim.run_until_pause()
        return sim, result

    def test_resume_finishes_the_print(self):
        sim, result = self.run_to_first_pause()
        self.assertEqual(result.state, 'paused')
        self.assertIn('ENDPOINT_NOT_REACHED', result.message)
        sim.user.clear_fault()
        self.assertEqual(sim.user.resume(), 'complete')
        self.assertEqual(sim.world.consistent(), [])

    def test_failed_load_clears_the_projection_like_the_incident(self):
        sim, _ = self.run_to_first_pause()
        cfg = sim.print_task_config.print_task_config
        self.assertFalse(cfg['filament_exist'][2])          # why Snapmaker refused to resume

    def test_without_the_projection_fix_resume_is_refused(self):
        sim, _ = self.run_to_first_pause()
        sim.user.clear_fault()
        # Both fixes off: the projection, and loading the empty Head before
        # Snapmaker's resume checks (2026-11-26) also rescues this case.
        with mock.patch.object(M.BMCUManager, '_prepare_u1_resume_projection',
                               lambda self: False), \
                mock.patch.object(M.BMCUManager, '_u1_paused_head', lambda self: None):
            with self.assertRaises(CommandError) as ctx:
                sim.user.resume()
        self.assertIn('e2 filament runout', str(ctx.exception))
        self.assertEqual(sim.print_state, 'paused')


class Incident2PendingRecoveryTest(unittest.TestCase):
    """A clog at head 3 fails the print-start load coil check (U1_TOOLHEAD_NOT_LOADED). The
    user clears it and resumes through SM_PRINT_AUTO_FEED. A later T6 change on head 4 must
    not fail with 'recovery for T4 is pending before T6' (PR item 16)."""

    def first_pause(self):
        # T6 (head 4) comes before T4 (head 3), whose print-start load failed
        sim = make_sim(self, plan=[[0], [6, 4]])
        sim.inject(F.GcodeLine('BMCU_AUTO_FEED EXTRUDER=2'), F.Clog(2))
        sim.start()
        return sim, sim.run_until_pause()

    def test_t6_is_not_blocked_after_the_resume(self):
        sim, result = self.first_pause()
        self.assertEqual(result.state, 'paused')
        self.assertIn('U1_TOOLHEAD_NOT_LOADED', result.message)
        sim.user.clear_fault()
        self.assertEqual(sim.user.resume(), 'complete')
        self.assertNotIn('pending before', sim.print_stats.message)
        self.assertFalse(sim.manager._u1_toolchange_resume_pending)
        self.assertEqual(sim.world.consistent(), [])

    def test_without_the_fix_t6_is_blocked_by_the_stale_recovery(self):
        sim, _ = self.first_pause()
        sim.user.clear_fault()
        # the code before item 16: the resume never ran the pending recovery, and every
        # tool change tried to run it for its own tool
        with mock.patch.object(M.BMCUManager, '_u1_resume_feed_runs_pending_recovery',
                               lambda self, gcmd: False),                 mock.patch.object(M.BMCUManager, '_u1_pending_recovery_is_for',
                                  lambda self, tool: True):
            state = sim.user.resume()
        self.assertEqual(state, 'paused')
        self.assertIn('recovery for T4 is pending before T6', sim.print_stats.message)


    def test_without_the_wait_resume_is_refused_right_after_homing(self):
        sim, _ = self.first_pause()
        sim.user.clear_fault()
        # 2026-11-26 on the real printer: Snapmaker's resume homes X/Y and then runs
        # the reload inside BMCU's 0.5 s release window, and every Resume failed
        with mock.patch.object(M.BMCUManager, '_wait_critical_motion_released',
                               lambda self, timeout=None: False):
            with self.assertRaises(CommandError) as ctx:
                sim.user.resume()
        self.assertIn('cannot run during printer motion', str(ctx.exception))


class Incident11PulledBackBeforeTheHeadTest(unittest.TestCase):
    """2026-11-26 on the real printer: T5 (bmcu0 Channel 2) stayed recorded LOADED in
    Head 3 after a print, and the user pulled it back behind the Head sensor. The next
    print started with T4 on Head 3. Snapmaker arms a 0.2 mm runout on a Head whose
    switch is open when a print starts, so BMCU's tip forming for T5 tripped it, and
    the print paused right after T4 had loaded."""

    def second_print(self):
        sim = make_sim(self, plan=[[5]], spools={5: 'PLA red', 4: 'PLA black'})
        # the first print's T5 load stops in Head 3 (coil check fails); the user
        # cancels, confirms the route LOADED, then pulls T5 back behind the sensor
        sim.inject(F.GcodeLine('BMCU_AUTO_FEED EXTRUDER=2'), F.Clog(2))
        sim.start()
        self.assertIn('U1_TOOLHEAD_NOT_LOADED', sim.run_until_pause().message)
        sim.user.clear_fault()
        sim.user.cancel()
        self.assertEqual(sim.user.confirm_routes(), [(1, 'LOADED')])
        source = sim.world.sources['bmcu1']
        sim.world.pull(source, source.tip - (source.sensor_p - 30.0))
        sim.settle(2.0)
        self.assertFalse(sim.sensors[2].runout_buttun_state)
        sim.slice([[4]])
        sim.start()
        return sim

    def test_t4_loads_and_the_print_completes(self):
        sim = self.second_print()
        self.assertEqual(sim.run_until_stopped(), 'complete', sim.print_stats.message)
        self.assertEqual(sim.sensors[2].runout_helper.runout_events, 0)
        self.assertEqual(sim.world.consistent(), [])

    def test_without_the_check_tip_forming_trips_the_stock_runout(self):
        with mock.patch.object(M.BMCUManager, '_u1_bmcu_filament_short_of_head',
                               lambda self, endpoint, device, channel: False):
            sim = self.second_print()
            state = sim.run_until_stopped()
        self.assertEqual(state, 'paused')
        self.assertIn('e2_filament runout', sim.print_stats.message)


class F18UncertainAfterCancelTest(unittest.TestCase):
    """F18: a print-start load stops in Head 3 (coil check fails), the user cancels and
    starts the next print without confirming the route, so the Channel is still
    UNCERTAIN. The next print must not fail with 'feeder owner is bmcu_busy' (an
    error, which cannot be resumed): print start settles the route first."""

    def cancelled(self, pull_back_mm=None):
        sim = make_sim(self, plan=[[5]], spools={5: 'PLA red', 4: 'PLA black'})
        sim.inject(F.GcodeLine('BMCU_AUTO_FEED EXTRUDER=2'), F.Clog(2))
        sim.start()
        self.assertIn('U1_TOOLHEAD_NOT_LOADED', sim.run_until_pause().message)
        sim.user.clear_fault()
        sim.user.cancel()
        if pull_back_mm is not None:
            source = sim.world.sources['bmcu1']
            sim.world.pull(source, source.tip - (source.sensor_p - pull_back_mm))
            sim.settle(2.0)
        sim.slice([[4]])
        return sim

    def test_filament_still_in_the_head(self):
        sim = self.cancelled()
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'complete', sim.print_stats.message)
        self.assertEqual(sim.world.consistent(), [])

    def test_filament_pulled_back_behind_the_sensor(self):
        sim = self.cancelled(pull_back_mm=30.0)
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'complete', sim.print_stats.message)
        self.assertEqual(sim.sensors[2].runout_helper.runout_events, 0)
        self.assertEqual(sim.world.consistent(), [])

    def test_without_the_fix_the_print_fails(self):
        sim = self.cancelled()
        with mock.patch.object(M.BMCUManager, '_settle_u1_uncertain_for_print_start',
                               lambda self, endpoint, gcmd=None: False):
            sim.start()
            state = sim.run_until_stopped()
        self.assertEqual(state, 'error')
        self.assertIn('feeder owner is bmcu_busy', sim.print_stats.message)


class Incident3FlushRetractTest(unittest.TestCase):
    """T6->T3 on head 4: BMCU's tip shaping and pullback leave printing_e_pos at about
    -67.5 mm, so Snapmaker's flush would retract the new stock filament that far and the
    next layer prints dry (item 4a). At most 5 mm may be retracted."""

    def flush_retracts(self, sim):
        return [float(l.split('=')[1]) for l in sim.macros.log if l.startswith('FLUSH retract=')]

    def test_retract_is_limited_and_flow_resumes(self):
        sim = make_sim(self)
        self.assertEqual(sim.run_print(), 'complete')
        retracts = self.flush_retracts(sim)
        self.assertTrue(retracts)
        self.assertLessEqual(max(retracts), 5.0)
        e3 = sim.extruders[3]
        self.assertEqual(round(e3.print_commanded - e3.print_flow, 1), 0.0)

    def test_without_the_limit_the_retract_is_huge_and_the_next_layer_is_dry(self):
        sim = make_sim(self)
        with mock.patch.object(E.SnapmakerU1Endpoint, '_limit_native_flush_retract',
                               lambda self: None):
            self.assertEqual(sim.run_print(), 'complete')
        self.assertGreater(max(self.flush_retracts(sim)), 30.0)
        e3 = sim.extruders[3]
        self.assertGreater(e3.print_commanded - e3.print_flow, 20.0)      # printed dry


class Incident5RestoreAfterStockLoadTest(unittest.TestCase):
    """After a stock load Snapmaker leaves the head at the purge spot with the heater off.
    BMCU must restore the printing temperature and the XY idle position."""

    def snapshot_after_t3_change(self, sim):
        taken = {}

        def grab(sim_, line):
            taken['target'] = sim_.extruders[3].heater.target_temp
            taken['xy'] = tuple(sim_.toolhead.position[:2])
            taken['active'] = sim_.toolhead.active
        sim.watch('BMCU_PRINT_PREEXTRUDE TOOL=3', grab)
        return taken

    def test_temperature_and_position_are_restored(self):
        sim = make_sim(self)
        taken = self.snapshot_after_t3_change(sim)
        self.assertEqual(sim.run_print(), 'complete')
        self.assertGreaterEqual(taken['target'], 200.0)
        self.assertEqual(taken['xy'], (30.0, 5.0))        # MOVE_TO_XY_IDLE_POSITION_EXTRUDER
        self.assertEqual(taken['active'], 3)

    def test_without_the_restore_the_head_stays_at_the_purge_spot(self):
        sim = make_sim(self)
        taken = self.snapshot_after_t3_change(sim)
        with mock.patch.object(E.SnapmakerU1Endpoint, '_restore_after_native_load',
                               lambda self, temperature: None):
            sim.run_print()
        self.assertEqual(taken['xy'], (28.5, 55.5))       # the discard position


class Incident4StaleHeadSensorTest(unittest.TestCase):
    """After BMCU unloads T6 the head sensor still reports present (it lags while
    printing), so Snapmaker's FEED_AUTO would skip its push. The stale reading must be
    cleared before the stock T3 load."""

    def test_stock_load_completes_inside_a_print(self):
        sim = make_sim(self)
        self.assertEqual(sim.run_print(), 'complete')

    def test_without_the_fix_the_push_is_skipped(self):
        sim = make_sim(self)
        with mock.patch.object(E.SnapmakerU1Endpoint, '_clear_stale_native_head_presence',
                               lambda self, channel: False):
            state = sim.run_print()
        self.assertNotEqual(state, 'complete')


class Incident6StockPrintOnBmcuHeadTest(unittest.TestCase):
    """No BMCU plan: the print was sliced with Snapmaker's own profile, and T6 is loaded in
    head 4. T6 must be unloaded and handed back, then the stock load runs."""

    def test_bmcu_filament_is_unloaded_before_the_stock_load(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.boot(15.0)
        sim.setup_u1()
        sim.load_spools({3: 'PLA white', 6: 'PLA black'})
        sim.run_gcode('BMCU_LOAD TOOL=6')
        self.assertEqual(sim.world.consistent({'bmcu2': 'loaded'}), [])
        sim.slice_stock([[3]])
        self.assertEqual(sim.run_print(), 'complete', sim.print_stats.message)
        self.assertEqual(sim.world.consistent({'bmcu2': 'empty'}), [])
        self.assertEqual(sim.bmcu.fw.route_state, [0, 0, 0, 0])
        self.assertEqual(sim.manager.loaded_tools, {})
        self.assertEqual(sim.feeders['right'].channel_state[1], 'load_finish')

    # FINDINGS.md F1: SM_PRINT_AUTO_FEED's hand-back path never caps the pending retract
    # (printing_e_pos), so Snapmaker's flush pulls the new stock filament back out of the
    # nozzle and the first moves of the print are dry.
    def test_stock_print_lays_down_plastic_after_the_hand_back(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.boot(15.0)
        sim.setup_u1()
        sim.load_spools({3: 'PLA white', 6: 'PLA black'})
        sim.run_gcode('BMCU_LOAD TOOL=6')
        sim.slice_stock([[3]])
        self.assertEqual(sim.run_print(), 'complete')
        e3 = sim.extruders[3]
        self.assertEqual(e3.print_commanded - e3.print_flow, 0.0)
        self.assertEqual(sim.world.consistent({'stock3': 'loaded'}), [])

    def test_without_the_wrapper_the_stock_load_runs_on_top_of_bmcu_filament(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.boot(15.0)
        sim.setup_u1()
        sim.load_spools({3: 'PLA white', 6: 'PLA black'})
        sim.run_gcode('BMCU_LOAD TOOL=6')
        sim.slice_stock([[3]])
        with mock.patch.object(M.BMCUManager, '_prepare_u1_head_for_stock_print',
                               lambda self, endpoint, gcmd: None):
            state = sim.run_print()
        self.assertTrue(state != 'complete' or
                        sim.world.sources['bmcu2'].tip != 0.0)


class Incident7PreloadClearTest(unittest.TestCase):
    """T3 stock preloaded on head 4 while T6 is loaded: accepted with the preload-clear
    setting on, refused with the stated reason when it is off."""

    def test_accepted_when_on(self):
        sim = make_sim(self)
        self.assertEqual(sim.run_print(), 'complete')

    def test_refused_with_a_reason_when_off(self):
        sim = make_sim(self, preload_clear=False)
        state = sim.run_print()
        self.assertNotEqual(state, 'complete')
        self.assertIn('T6 cannot use u1_head3', sim.print_stats.message)
        self.assertIn('native_busy', sim.print_stats.message)


class Incident8ParkedInSharedTubeTest(unittest.TestCase):
    """A BMCU autoload longer than its tube leaves a tip past the splitter. The next load of
    the other channel on that head must pause, not jam; Resume works once it is cleared."""

    def setup_print(self):
        sim = make_sim(self, plan=[[0], [4], [3]],
                       spools={0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 3: 'PLA white'})
        sim.inject(F.GcodeLine('PRINT_START'), F.ParkedInShared('bmcu1', 100.0))
        return sim

    def test_load_pauses_and_resume_finishes_after_the_user_clears_it(self):
        sim = self.setup_print()
        sim.start()
        result = sim.run_until_pause()
        self.assertEqual(result.state, 'paused')
        self.assertIn('ENDPOINT_NOT_REACHED', result.message)
        self.assertTrue(sim.world.sources['bmcu1'].in_shared)         # still parked there
        self.assertFalse(sim.world.sources['bmcu0'].past(sim.world.sources['bmcu0'].gears_p))
        sim.user.clear_fault()
        self.assertEqual(sim.user.resume(), 'complete')
        self.assertEqual(sim.world.consistent(), [])

    # FINDINGS.md F2 (diagnostics): the pause names no cause.
    def test_pause_message_names_the_blocking_channel(self):
        sim = self.setup_print()
        sim.start()
        result = sim.run_until_pause()
        self.assertTrue('Channel 2' in result.message or 'shared' in result.message.lower()
                        or 'splitter' in result.message.lower(), result.message)


class Incident9UnrecordedFilamentTest(unittest.TestCase):
    """Filament is physically in head 3 but BMCU's state says empty (T4's spool). It must
    be identified by its encoder and kept, then swapped when the plan needs T5."""

    # FINDINGS.md F3: the channel is identified and marked LOADED, then the load that
    # follows refuses it: "u1_head2 has BMCU route ownership without a captured native
    # feeder baseline" (manager._arm_u1_persistent_hold).
    def test_identified_kept_then_swapped_for_t5(self):
        sim = make_sim(self, plan=[[0], [4, 5], [3]],
                       spools={0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 3: 'PLA white'})
        sim.inject(F.GcodeLine('PRINT_START'), F.Unrecorded('bmcu0'))
        self.assertEqual(sim.run_print(), 'complete', sim.print_stats.message)
        self.assertIn('BMCU loaded T5 from bmcu0 Channel 2 into u1_head2',
                      sim.gcode.output)
        self.assertEqual(sim.world.consistent({'bmcu0': 'empty', 'bmcu1': 'empty'}), [])

    def test_the_head_filament_is_identified_by_its_encoder(self):
        sim = make_sim(self, plan=[[0], [4, 5], [3]],
                       spools={0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 3: 'PLA white'})
        sim.inject(F.GcodeLine('PRINT_START'), F.Unrecorded('bmcu0'))
        sim.run_print()
        self.assertTrue(any('checking which Channel it is' in l for l in sim.gcode.output))


class WeakPressureAssistTest(unittest.TestCase):
    """2026-11-26 on the printer: bmcu0 Channel 2 stopped short of Head 3's gears
    three times (coil path_delta 28, 14), the pressure hold at its buffer target and
    not pushing. The user pushed by hand and it loaded. BMCU now makes that push
    itself, once, before it pauses."""

    def sim(self):
        sim = make_sim(self, plan=[[0], [4], [5], [3]],
                       spools={0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 3: 'PLA white'})
        sim.inject(F.GcodeLine('PRINT_START'), F.WeakPressure('bmcu1'))
        return sim

    def test_the_assist_push_loads_it_without_a_pause(self):
        sim = self.sim()
        real = M.BMCUManager._u1_coil_assist_push
        calls = []
        def spy(self, *args):
            calls.append(args[1])
            return real(self, *args)
        with mock.patch.object(M.BMCUManager, '_u1_coil_assist_push', spy):
            self.assertEqual(sim.run_print(), 'complete', sim.print_stats.message)
        self.assertEqual(calls, [1])            # bmcu0 Channel 2, once
        self.assertEqual(sim.tool_changes, sim.planned_tool_changes())
        self.assertEqual(sim.world.consistent(), [])

    def test_without_the_assist_it_pauses(self):
        sim = self.sim()
        def no_assist(self, device, channel, endpoint, material, exc):
            raise exc
        with mock.patch.object(M.BMCUManager, '_u1_coil_assist_push', no_assist):
            sim.start()
            state = sim.run_until_stopped()
        self.assertEqual(state, 'paused')
        self.assertIn('U1_TOOLHEAD_NOT_LOADED', sim.print_stats.message)

    def test_a_clog_still_pauses_and_says_to_push_by_hand(self):
        sim = make_sim(self, plan=[[0], [4], [5], [3]],
                       spools={0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 3: 'PLA white'})
        sim.inject(F.GcodeLine('PRINT_START'), F.Clog(2))
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'paused')
        self.assertIn('without success: push bmcu0 Channel', sim.print_stats.message)
        out = checks.recover(sim, ('clear_fault',), start=False)
        self.assertEqual(out.state, 'complete', out.messages)


class BufferSlackIdentifyTest(unittest.TestCase):
    """2026-11-26 on the printer: a failed load left bmcu0 Channel 3's buffer at 82 %.
    Identifying the Head 4 filament saw 0.0 mm on two Resume presses and 11 mm on the
    third: the Head's pull came out of the buffer before the encoder turned."""

    def run_with_slack(self):
        sim = make_sim(self, plan=[[0], [4, 5], [3]],
                       spools={0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 3: 'PLA white'})
        sim.inject(F.GcodeLine('PRINT_START'), F.Unrecorded('bmcu0'))
        sim.inject(F.GcodeLine('PRINT_START'), F.BufferSlack('bmcu0', 30.0))
        return sim

    def test_identified_in_one_go(self):
        sim = self.run_with_slack()
        self.assertEqual(sim.run_print(), 'complete', sim.print_stats.message)
        self.assertTrue(any('Head 3 holds bmcu0 Channel 1' in l for l in sim.gcode.output))

    def test_with_a_single_extrusion_it_is_not_identified(self):
        sim = self.run_with_slack()
        with mock.patch.object(M.BMCUManager, 'IDENTIFY_ROUNDS', 1):
            sim.start()
            state = sim.run_until_stopped()
        self.assertNotEqual(state, 'complete')
        self.assertIn('none of its BMCU Channels moved', sim.print_stats.message)


class Incident10HelperStuckTest(unittest.TestCase):
    """A helper that kept the port open with no session after a bad boot (item 12). Replacing
    it is the bootstrap script's job (tests/test_transport_recovery.py covers that); here:
    the host stays offline without wedging, then reconnects when the helper is replaced and
    the print runs."""

    def test_host_recovers_when_the_helper_is_replaced(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.bmcu.helper.stuck = True
        sim.boot(40.0)
        device = sim.manager.devices[0]
        self.assertFalse(device.connected)
        self.assertFalse(device.ready)
        # the service start replaces the helper: the port is reopened with a session
        sim.bmcu.helper.stuck = False
        sim.bmcu.helper.drop_clients()
        sim.settle(60.0)
        self.assertTrue(device.ready and device.status_reconciled)
        sim.setup_u1()
        sim.load_spools(SPOOLS)
        sim.slice(PLAN)
        self.assertEqual(sim.run_print(), 'complete')



class FoundByTheSimulatorTest(unittest.TestCase):
    """Bugs the simulator found; none is fixed here (plan section 7)."""

    # FINDINGS.md F4: a fault-free print whose Head 4 goes T6 -> (another head) -> stock T3
    # pauses: the background worker that hands the head back to its stock feeder is refused
    # by _handoff_u1_to_native because the worker still holds its own endpoint/path locks.
    def test_background_handoff_to_the_stock_feeder_is_not_blocked_by_its_own_locks(self):
        sim = make_sim(self, plan=[[0], [6], [4], [3]])
        self.assertEqual(sim.run_print(), 'complete', sim.print_stats.message)


if __name__ == '__main__':
    unittest.main()

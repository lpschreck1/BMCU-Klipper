"""M8: phase triggers, BMCU disconnects, staging faults and helper death.

The phase triggers fire on simulated state (world, fake firmware, fake feeder); the tests
here check each fires at the phase it names. The ``Findings`` classes are scenarios that
expose a bug (``expectedFailure``, rows F5-F10 in tests/sim/FINDINGS.md).
"""
import unittest
from unittest import mock

import _support  # noqa: F401
from bmcu_core import device as D
from bmcu_core import manager as M
from bmcu_core import protocol as P
from sim import checks
from sim import faults as F
from sim.runner import Sim

PLAN = [[0], [4, 6], [5], [6, 0]]
SPOOLS = {0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 6: 'PLA black'}


def make_sim(test, plan=PLAN, spools=None):
    sim = Sim.reference_machine()
    test.addCleanup(sim.close)
    sim.boot(15.0)
    sim.setup_u1()
    sim.load_spools(SPOOLS if spools is None else spools)
    sim.slice(plan)
    return sim


class Probe(F.Fault):
    """A fault that only records the machine state at the instant its trigger fires."""
    name = 'probe'

    def __init__(self, read):
        self.read = read
        self.seen = None

    def apply(self, sim):
        self.seen = self.read(sim)

    def clear(self, sim):
        return None


def fire_state(test, trigger, read, plan=PLAN, spools=None):
    sim = make_sim(test, plan, spools)
    probe = Probe(read)
    sim.inject(trigger, probe)
    sim.start()
    sim.run_until_stopped()
    test.assertIsNotNone(probe.seen, 'trigger never fired')
    return sim, probe.seen


class PhaseTriggerTest(unittest.TestCase):
    def test_bmcu_moving_load_fires_after_the_distance(self):
        base = {}

        def read(sim):
            return sim.world.sources['bmcu0'].encoder_mm - base['start']
        sim = make_sim(self)
        base['start'] = sim.world.sources['bmcu0'].encoder_mm
        probe = Probe(read)
        sim.inject(F.BmcuMoving('bmcu0', 'load', 50), probe)
        sim.start()
        sim.run_until_stopped()
        self.assertTrue(50.0 <= probe.seen <= 60.0, probe.seen)

    def test_bmcu_moving_unload_fires_while_the_tip_comes_back(self):
        def read(sim):
            fw = sim.bmcu.fw
            return fw.motion[0], fw.op.type if fw.op else None, sim.world.sources['bmcu0'].tip
        _, (motion, op_type, tip) = fire_state(
            self, F.BmcuMoving('bmcu0', 'unload', 50), read)
        self.assertTrue(motion in (P.MOTION_PULL_BACK, P.MOTION_BEFORE_PULL_BACK) or
                        op_type == P.OP_CHANNEL_RETRACT)
        self.assertLess(tip, 1890.0 - 40.0)             # it has moved back from the nozzle

    def test_before_on_use_fires_inside_the_toolhead_check(self):
        _, motion = fire_state(self, F.BeforeOnUse(2),
                               lambda sim: sim.bmcu.fw.motion[0])
        self.assertEqual(motion, P.MOTION_BEFORE_ON_USE)

    def test_prestaging_fires_in_the_background_of_another_tool_change(self):
        def read(sim):
            return [line for command, line in sim.gcode.running
                    if command.startswith('BMCU_')]
        _, running = fire_state(self, F.Prestaging(5), read)
        self.assertTrue(any('TOOL=6' in line for line in running), running)
        self.assertFalse(any('TOOL=5' in line for line in running), running)

    def test_stock_feeding_fires_in_the_feeder_phase(self):
        def read(sim):
            return sim.feeders['right'].channel_state[1]
        _, state = fire_state(self, F.StockFeeding(3, 'load_extruding'), read,
                              plan=[[0], [4, 6], [3]], spools={0: 'PLA grey', 4: 'PLA red', 6: 'PLA black', 3: 'PLA white'})
        self.assertEqual(state, 'load_extruding')

    def test_a_phase_that_never_happens_does_not_fire(self):
        sim = make_sim(self)
        probe = Probe(lambda sim: True)
        sim.inject(F.BmcuMoving('bmcu3', 'load', 10), probe)         # no T7 in this print
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'complete')
        self.assertIsNone(probe.seen)


class LinkAndHelperTest(unittest.TestCase):
    def test_a_down_link_delivers_nothing_to_the_host(self):
        sim = make_sim(self)
        device = sim.manager.devices[0]
        before = sim.bmcu.helper.seq
        sim.bmcu.helper.set_link(False)
        sim.bmcu.fw.send_status()
        sim.settle(3.0)
        self.assertEqual(sim.bmcu.helper.seq, before)
        self.assertFalse(device.connected)

    def test_helper_exit_closes_the_socket_and_the_host_reconnects_after_repair(self):
        sim = make_sim(self)
        device = sim.manager.devices[0]
        sim.bmcu.helper.exit()
        sim.settle(5.0)
        self.assertFalse(device.connected)
        sim.bmcu.helper.restart()
        sim.settle(30.0)
        self.assertTrue(device.ready)

    def test_a_fault_free_print_passes_the_shared_checks(self):
        sim = make_sim(self)
        outcome = checks.assert_recoverable(self, sim)
        self.assertEqual(outcome.kind, 'no_effect')


class SweepOutcomesTest(unittest.TestCase):
    """The M8 sections of tests/sim/SWEEP.md, as assertions. Every case that is not
    expected to fail must finish; the ones in KNOWN_BAD are findings (FINDINGS.md)."""

    @staticmethod
    def finding_for(title, point, fault, kind='stuck', detail=''):
        """The finding (FINDINGS.md) that explains a bad case, or None if it is new."""
        if title == 'M9 restarts':
            if 'cannot replace the active BMCU print plan' in detail:
                return 'F11'
            if 'power' in fault:
                if point in ('BMCU load 50 mm', 'BMCU load near arrival', 'BEFORE_ON_USE'):
                    return 'F12'
                if point in ('idle mid-layer', 'BMCU unload 50 mm',
                             'durable write queued, not landed'):
                    return 'F8'
            return None
        if title == 'M8 disconnect sweep':
            if point == 'idle mid-layer' and 'replug' not in fault:
                return None                       # a glitch with nothing moving is harmless
            return 'F8' if point in ('unload 50 mm', 'prestaging T5') else 'F5'
        if title == 'M8 helper death':
            return 'F8' if point == 'unload 50 mm' else 'F5'
        if title == 'M8 staging faults':
            if point == 'load T4 20 mm':
                return None                       # the M6 incidents: these resume
            return 'F10' if 'deformed' in fault else 'F9'
        return None

    # Findings fixed in claude-fixes: a case they explained going bad again is a
    # regression, not a known finding.
    FIXED = frozenset(('F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12'))

    def test_every_case_is_recoverable_or_a_known_finding(self):
        from sim import sweep
        sections = sweep.cached_sweep()
        unexplained, regressed, fixed = set(), set(), set()
        for title, _intro, rows in sections[1:]:
            for point, fault, kind, detail in rows:
                finding = self.finding_for(title, point, fault, kind, detail)
                bad = (kind in ('stuck', 'dirty') or
                       'cannot replace the active BMCU print plan' in detail)
                if bad and finding is None:
                    unexplained.add((title, point, fault))
                elif bad and finding in self.FIXED:
                    regressed.add((title, point, fault, finding))
                if not bad and finding is not None and finding not in self.FIXED:
                    fixed.add((title, point, fault))
        self.assertEqual(unexplained, set(), 'new unrecoverable cases')
        self.assertEqual(regressed, set(), 'a fixed finding came back')
        self.assertEqual(fixed, set(), 'fixed: update finding_for and FINDINGS.md')

    def test_the_first_sweep_has_no_stuck_case_either(self):
        from sim import sweep
        rows = sweep.cached_sweep()[0][2]
        self.assertEqual(
            [(point, fault) for point, fault, kind, _detail in rows
             if kind in ('stuck', 'dirty')], [])


class Findings(unittest.TestCase):
    """Scenarios that fail today. Each is expectedFailure and has a FINDINGS.md row."""

    def disconnect(self, trigger, duration, actions=('replug',)):
        sim = make_sim(self)
        sim.inject(trigger, F.BmcuDisconnect(duration))
        outcome = checks.recover(sim, actions)
        return sim, outcome

    # F5 ---------------------------------------------------------------------------
    def test_f5_cable_glitch_during_a_load_can_be_resumed(self):
        sim, outcome = self.disconnect(F.BmcuMoving('bmcu0', 'load', 50), 3.0)
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    def test_f5_cable_glitch_in_before_on_use_can_be_resumed_without_dry_printing(self):
        sim, outcome = self.disconnect(F.BeforeOnUse(2), 3.0)
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])
        self.assertEqual(checks.verify(sim), [])

    def test_f5_helper_exit_mid_print_can_be_resumed_after_repair(self):
        sim = make_sim(self)
        sim.inject(F.Idle(40.0), F.HelperExit(None))
        outcome = checks.recover(sim, ('clear_fault',))
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    def test_f5_diagnosis_negative_control(self):
        """Switch off the two behaviours the diagnosis blames (deferred tasks held while a
        print is paused; status fingerprint kept across a link reset) and the load glitch
        recovers: the cause is where FINDINGS.md says it is."""
        original = D.BMCUDevice._reset_link_handshake if hasattr(D, 'BMCUDevice') else None
        device_cls = [c for c in vars(D).values()
                      if isinstance(c, type) and hasattr(c, '_reset_link_handshake')][0]
        original = device_cls._reset_link_handshake

        def reset(self, now):
            self._last_manager_status_fingerprint = None
            return original(self, now)

        def not_busy(self, eventtime=None):
            if self._print_state() == 'printing':
                return True
            return self._printer_background_busy(eventtime)
        with mock.patch.object(device_cls, '_reset_link_handshake', reset), \
                mock.patch.object(M.BMCUManager, '_printer_deferred_busy', not_busy):
            sim, outcome = self.disconnect(F.BmcuMoving('bmcu0', 'load', 50), 3.0)
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    # F6 ---------------------------------------------------------------------------
    def test_f6_cable_glitch_at_idle_leaves_the_device_ready_again(self):
        sim = make_sim(self)
        device = sim.manager.devices[0]
        sim.bmcu.helper.set_link(False)
        sim.settle(3.0)
        sim.bmcu.helper.set_link(True)
        sim.settle(30.0)
        self.assertTrue(device.ready)

    # F7 ---------------------------------------------------------------------------
    def test_f7_the_pause_for_a_lost_cable_says_to_check_the_cable(self):
        sim = make_sim(self)
        sim.inject(F.BmcuMoving('bmcu0', 'load', 50), F.BmcuDisconnect(None))
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'paused')
        self.assertRegex(sim.print_stats.message, r'(?i)cable|usb|plug|connection')

    # F8 ---------------------------------------------------------------------------
    def test_f8_cable_glitch_during_an_unload_can_be_resumed(self):
        sim, outcome = self.disconnect(F.BmcuMoving('bmcu0', 'unload', 50), 3.0)
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    def test_f8_cable_glitch_during_a_prestage_can_be_resumed(self):
        sim, outcome = self.disconnect(F.Prestaging(5), 3.0)
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    # F9 ---------------------------------------------------------------------------
    def test_f9_a_snag_in_the_background_prestage_can_be_resumed(self):
        sim = make_sim(self)
        sim.inject(F.Prestaging(5), F.Snag('bmcu1', 58.0))
        outcome = checks.recover(sim, ('clear_fault',))
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    def test_f9_a_runout_in_the_background_prestage_can_be_resumed(self):
        sim = make_sim(self)
        sim.inject(F.Prestaging(5), F.Runout('bmcu1'))
        outcome = checks.recover(sim, ('clear_fault',))
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    # F10 --------------------------------------------------------------------------
    def test_f10_deformed_tip_on_a_tool_change_load_resumes_without_dry_printing(self):
        sim = make_sim(self)
        sim.inject(F.Prestaging(5), F.DeformedTip('bmcu1'))
        outcome = checks.recover(sim, ('clear_fault',))
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])
        self.assertLessEqual(max(sim.dry_mm().values()), checks.DRY_TOLERANCE_MM)


if __name__ == '__main__':
    unittest.main()

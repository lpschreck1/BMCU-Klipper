"""M9: Klipper restarts and power loss in the middle of a print.

``Sim.restart(kind, at)`` throws away the fake Klipper, the Snapmaker modules and the BMCU
manager and builds new ones over the same world and state directory. These tests check the
machinery (what survives a restart, what does not), the power-loss resume model, and the
findings F11-F12 (``expectedFailure``; see tests/sim/FINDINGS.md).
"""
import json
import os
import unittest

import _support  # noqa: F401
from bmcu_core import protocol as P
from sim import checks
from sim import faults as F
from sim import sweep
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
    name = 'probe'

    def __init__(self, read):
        self.read = read
        self.seen = None

    def apply(self, sim):
        self.seen = self.read(sim)

    def clear(self, sim):
        return None


class RestartMachineryTest(unittest.TestCase):
    def test_a_power_cut_stops_motion_where_it_was(self):
        sim = make_sim(self)
        probe = Probe(lambda s: (s.world.sources['bmcu0'].tip, s.world.now))
        trigger = F.BmcuMoving('bmcu0', 'load', 300)
        sim.inject(trigger, probe)
        sim.restart('power', at=F.BmcuMoving('bmcu0', 'load', 300))
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'restarted')
        tip, _ = probe.seen
        self.assertEqual(sim.world.sources['bmcu0'].tip, tip)     # nothing moved in the cut
        self.assertEqual(sim.restarts[0][0], 'power')

    def test_a_klipper_restart_leaves_the_bmcu_firmware_running(self):
        sim = make_sim(self)
        probe = Probe(lambda s: s.world.sources['bmcu0'].tip)
        sim.inject(F.BmcuMoving('bmcu0', 'load', 300), probe)
        sim.restart('klipper', at=F.BmcuMoving('bmcu0', 'load', 300))
        firmware = sim.bmcu.fw
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'restarted')
        self.assertIs(sim.bmcu.fw, firmware)                      # same firmware object
        self.assertGreater(sim.world.sources['bmcu0'].tip, probe.seen)   # it kept pushing

    def test_a_power_cut_restarts_the_firmware_but_keeps_its_flash(self):
        sim = make_sim(self)
        firmware = sim.bmcu.fw
        probe = Probe(lambda s: list(s.bmcu.fw.route_state))
        sim.inject(F.BmcuMoving('bmcu0', 'load', 300), probe)
        sim.restart('power', at=F.BmcuMoving('bmcu0', 'load', 300))
        sim.start()
        sim.run_until_stopped()
        self.assertIsNot(sim.bmcu.fw, firmware)
        self.assertEqual(sim.bmcu.fw.route_state, probe.seen)
        self.assertEqual(sim.bmcu.fw.motion, [P.MOTION_IDLE] * 4)
        self.assertEqual(sim.bmcu.fw.meters(0), 0.0)              # the encoder counter restarted

    def test_both_kinds_turn_the_heaters_off(self):
        for kind in ('klipper', 'power'):
            sim = make_sim(self)
            sim.restart(kind, at=F.Idle(300.0))
            sim.start()
            sim.run_until_stopped()
            self.assertTrue(all(h.target == 0.0 for h in sim.world.heaters.values()), kind)

    def test_a_durable_write_still_queued_is_lost(self):
        sim = make_sim(self)
        sim.bmcu.write_lag = 30.0
        probe = Probe(lambda s: len(s.bmcu.pending_writes))
        sim.inject(F.WriteQueued(300.0), probe)
        sim.restart('klipper', at=F.WriteQueued(300.0))
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'restarted')
        self.assertGreater(probe.seen, 0)
        self.assertEqual(sim.bmcu.pending_writes, [])             # the new rig starts clean

    def test_a_truncated_state_file_loses_the_routing_at_the_boot(self):
        sim = make_sim(self)
        before = sim.manager.get_status()['devices'][0]['channels'][0]['endpoint']
        self.assertEqual(before, 'u1_head2')
        sim.restart('klipper', at=F.Idle(300.0), corrupt_state='truncated')
        sim.start()
        sim.run_until_stopped()
        sim.settle(10.0)
        after = sim.manager.get_status()['devices'][0]['channels'][0]['endpoint']
        self.assertNotEqual(after, 'u1_head2')

    def test_the_world_and_the_dry_counters_survive_a_restart(self):
        sim = make_sim(self)
        tip = {}
        sim.inject(F.Idle(300.0), Probe(lambda s: tip.update(
            {n: x.tip for n, x in s.world.sources.items()})))
        sim.restart('klipper', at=F.Idle(300.0))
        sim.start()
        sim.run_until_stopped()
        for name, value in tip.items():
            self.assertEqual(sim.world.sources[name].tip, value, name)


class PowerLossResumeTest(unittest.TestCase):
    """virtual_sdcard.py's restore: a cut move is assumed done, anything else (a macro, a
    tool change) runs again from its start."""

    def cut_at(self, line_prefix):
        sim = make_sim(self)
        sim.restart('power', at=F.GcodeLine(line_prefix))
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'restarted')
        info = sim.persist['pl']
        found, _ = sim.sdcard.power_loss_info_check()
        self.assertTrue(found)
        return sim, info

    def test_a_cut_tool_change_is_run_again(self):
        sim, info = self.cut_at('BMCU_TOOL_CHANGE TOOL=6')
        self.assertEqual(info['cmd'], 'BMCU_TOOL_CHANGE')
        sim.power_loss_restore()
        outcome = checks.recover(sim, ('clear_fault', 'confirm_routes'), start=False)
        self.assertIn('BMCU_TOOL_CHANGE TOOL=6', sim.sdcard.run_log[0])
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    def test_a_cut_move_is_assumed_finished(self):
        sim, info = self.cut_at('G1 X150 Y250 E8.00')
        self.assertEqual(info['cmd'], 'G1')
        sim.power_loss_restore()
        self.assertNotEqual(sim.sdcard.lines, 0)
        self.assertEqual(sim.sdcard.file_position, info['next_pos'])
        outcome = checks.recover(sim, ('clear_fault', 'confirm_routes'), start=False)
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])

    def test_the_offer_goes_away_when_the_print_ends(self):
        sim = make_sim(self)
        self.assertEqual(sim.run_print(), 'complete')
        self.assertFalse(sim.sdcard.power_loss_info_check()[0])

    def test_restore_is_refused_unless_the_printer_is_idle(self):
        sim = make_sim(self)
        sim.restart('power', at=F.Idle(300.0))
        sim.start()
        sim.run_until_stopped()
        sim.machine_state.main_state = 'PRINTING'
        with self.assertRaises(Exception) as ctx:
            sim.run_gcode('SDCARD_PRINT_PL_RESTORE')
        self.assertIn('power-loss recovery is not allowed', str(ctx.exception))


class RestartSweepTest(unittest.TestCase):
    def test_after_a_restart_bmcu_either_matches_the_world_or_knows_it_does_not(self):
        sim = make_sim(self)
        sim.restart('power', at=F.BmcuMoving('bmcu0', 'unload', 50))
        sim.start()
        sim.run_until_stopped()
        self.assertEqual(checks.view_problems(sim), [])


class Findings(unittest.TestCase):
    """Fail today; rows F8, F11 and F12 of tests/sim/FINDINGS.md."""

    def test_f11_the_first_print_after_a_klipper_restart_is_not_refused(self):
        outcome, detail = sweep.run_restart_case('klipper', F.Idle(300.0))
        self.assertNotIn('cannot replace the active BMCU print plan', detail)
        self.assertIn(outcome, ('no_effect', 'resumed'))

    def test_f12_power_loss_resume_during_the_print_start_load_continues(self):
        outcome, detail = sweep.run_restart_case(
            'power', F.BmcuMoving('bmcu0', 'load', 50))
        self.assertIn(outcome, ('no_effect', 'resumed'), detail)

    def test_f8_power_loss_during_an_unload_can_be_resumed(self):
        outcome, detail = sweep.run_restart_case(
            'power', F.BmcuMoving('bmcu0', 'unload', 50))
        self.assertIn(outcome, ('no_effect', 'resumed'), detail)

    def test_f8_power_loss_with_a_durable_write_still_queued_can_be_resumed(self):
        def lag(sim):
            sim.bmcu.write_lag = 5.0
        outcome, detail = sweep.run_restart_case(
            'power', F.WriteQueued(300.0), hook=lag)
        self.assertIn(outcome, ('no_effect', 'resumed'), detail)


if __name__ == '__main__':
    unittest.main()

"""M4: a sliced print runs end to end through the real BMCU manager.

Includes the pause-on-error / Resume-retries-the-failed-line semantics (confirmed
against Snapmaker's virtual_sdcard.py, see sim/virtual_sdcard.py).
"""
import json
import unittest

import _support  # noqa: F401
from sim import gcode_gen
from sim.runner import Sim

REFERENCE_PLAN = [[0], [4, 6], [3]]
REFERENCE_SPOOLS = {0: 'PLA grey', 4: 'PLA red', 6: 'PLA black', 3: 'PLA white'}


def reference_sim(**setup):
    sim = Sim.reference_machine()
    sim.boot(15.0)
    sim.setup_u1(**setup)
    sim.load_spools(REFERENCE_SPOOLS)
    sim.slice(REFERENCE_PLAN)
    return sim


class ReferencePrintTest(unittest.TestCase):
    def setUp(self):
        self.sim = reference_sim()
        self.addCleanup(self.sim.close)

    def test_three_layer_print_completes_without_faults(self):
        state = self.sim.run_print()
        self.assertEqual(state, 'complete', self.sim.print_stats.message)
        sd = self.sim.sdcard
        changes = [l for l in sd.run_log if l.startswith('BMCU_TOOL_CHANGE')]
        self.assertEqual(changes, ['BMCU_TOOL_CHANGE TOOL=4', 'BMCU_TOOL_CHANGE TOOL=6',
                                   'BMCU_TOOL_CHANGE TOOL=3'])

    def test_every_sliced_move_laid_down_plastic(self):
        self.sim.run_print()
        dry = {e.name: round(e.print_commanded - e.print_flow, 1)
               for e in self.sim.extruders}
        self.assertEqual(dry, {'extruder': 0.0, 'extruder1': 0.0, 'extruder2': 0.0,
                               'extruder3': 0.0})
        self.assertEqual([e.print_commanded for e in self.sim.extruders],
                         [20.0, 0.0, 53.0, 106.0])

    def test_filaments_end_where_the_state_says(self):
        self.sim.run_print()
        world = self.sim.world
        self.assertEqual(world.consistent(), [])
        self.assertEqual(self.sim.manager.loaded_tools, {})            # BMCU unloaded
        self.assertEqual(self.sim.bmcu.fw.route_state, [0, 0, 0, 0])
        self.assertEqual(self.sim.print_stats.state, 'complete')
        self.assertEqual(self.sim.machine_state.main_state, 'IDLE')

    def test_run_is_deterministic(self):
        self.sim.run_print()
        first = (round(self.sim.reactor.monotonic(), 3), list(self.sim.gcode.history))
        other = reference_sim()
        self.addCleanup(other.close)
        other.run_print()
        second = (round(other.reactor.monotonic(), 3), list(other.gcode.history))
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1][-200:], second[1][-200:])


class PlanAcceptanceTest(unittest.TestCase):
    def test_plan_refuses_stock_filament_on_a_shared_head_when_preload_clear_is_off(self):
        # scenario 7 (refusal half): T3 stock is preloaded on head 4 and T6 needs it
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.boot(15.0)
        sim.setup_u1(preload_clear=False)
        sim.load_spools(REFERENCE_SPOOLS)
        sim.slice(REFERENCE_PLAN)
        state = sim.run_print()
        self.assertNotEqual(state, 'complete')
        self.assertIn('native_busy', sim.print_stats.message)
        self.assertIn('T6 cannot use u1_head3', sim.print_stats.message)

    def test_plan_accepts_it_when_preload_clear_is_on(self):
        sim = reference_sim()
        self.addCleanup(sim.close)
        self.assertEqual(sim.run_print(), 'complete')


class PauseOnErrorTest(unittest.TestCase):
    """A command that raises an error with action='pause' pauses the print; Resume
    runs the same line again (virtual_sdcard does not advance the file position)."""

    def make(self):
        sim = Sim.reference_machine(bmcu=False)
        self.addCleanup(sim.close)
        sim.printer.send_event('klippy:ready')
        sim.insert_stock(0)
        sim.settle(60.0)
        sim.print_task_config.set_head(0)
        sim.print_task_config.print_task_config['extruders_used'][0] = True
        sim.run_gcode('G28 X Y Z\nT0 A0\nFEED_AUTO MODULE=left CHANNEL=1 LOAD=1 PRINTING=0')
        self.attempts = []
        sim.fail_times = 1

        def flaky(gcmd):
            self.attempts.append(sim.sdcard.file_position)
            if sim.fail_times > 0:
                sim.fail_times -= 1
                raise gcmd.error('flaky failed', action='pause', id=522)
        sim.gcode.register_command('FLAKY', flaky)
        sim.sdcard.load(sim.tmp + '/p.gcode', 'M83\nG1 E5 F300\nFLAKY\nG1 E5 F300\nM400\n')
        return sim

    def test_resume_runs_the_failed_command_again_and_finishes(self):
        sim = self.make()
        sim.print_stats.state = 'standby'
        sim.machine_state.main_state = 'PRINTING'
        self.assertEqual(sim.run_print(), 'paused')
        self.assertEqual(sim.print_stats.message, 'flaky failed')
        failed_at = sim.sdcard.file_position
        self.assertEqual(sim.sdcard.run_log[-1], 'FLAKY')
        self.assertEqual(sim.user.resume(), 'complete')
        self.assertEqual(self.attempts, [failed_at, failed_at])      # same line, twice
        self.assertEqual(sim.sdcard.run_log.count('FLAKY'), 2)
        self.assertEqual(sim.sdcard.run_log[-2:], ['G1 E5 F300', 'M400'])

    def test_an_error_without_a_pause_action_ends_the_print(self):
        sim = self.make()
        sim.gcode.register_command(
            'FLAKY', lambda gcmd: (_ for _ in ()).throw(gcmd.error('fatal')))
        self.assertEqual(sim.run_print(), 'error')


class ProfileDriftTest(unittest.TestCase):
    """The tool-change G-code is a hand translation of the profile's script; fail
    if the profile stops containing the lines the translation emits."""

    def test_profile_still_emits_what_we_translate(self):
        profile = gcode_gen.load_profile()
        script = profile['change_filament_gcode']
        for fragment in ('BMCU_TOOL_CHANGE TOOL=', 'BMCU_PRINT_PREEXTRUDE TOOL=',
                         'MOVE_TO_XY_IDLE_POSITION_EXTRUDER', 'nonblocking preheat',
                         'local move_z = 1.5', 'max_speed_toolchange = 350.0'):
            self.assertIn(fragment, script)
        start = profile['machine_start_gcode']
        for fragment in ('BMCU_PRINT_BEGIN SCHEMA=1 RESET=1', 'BMCU_PRINT_COMMIT',
                         'BMCU_AUTO_FEED EXTRUDER=0'):
            self.assertIn(fragment, start)


if __name__ == '__main__':
    unittest.main()

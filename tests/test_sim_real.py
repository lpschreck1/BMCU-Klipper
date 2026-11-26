"""M10: real sliced files.

* the trimmer (``sim/trim_gcode.py``) keeps what BMCU and Snapmaker react to and the
  extrusion of every layer, on a synthetic export (always) and on the untrimmed originals in
  ``tests/sim/fixtures/real/original/`` (when present);
* every trimmed file in ``tests/sim/fixtures/real/`` runs end to end with no faults, and
  through the M8 disconnect and M9 power-loss subsets;
* ``BMCU_SIM_GCODE=/path/file.gcode python -m unittest tests.test_sim_real`` runs any file
  through the simulator, so a print can be checked before it is started;
* the 2026-11-25 incident (plan 2, M10 item 5).
"""
import glob
import os
import re
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401
from bmcu_core import gcode_plan
from sim import checks
from sim import faults as F
from sim import gcode_gen
from sim import trim_gcode
from sim.runner import Sim

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sim', 'fixtures', 'real')
PLAN = [[0], [4, 6], [3]]
SPOOLS = {0: 'PLA grey', 4: 'PLA red', 6: 'PLA black', 3: 'PLA white'}


def plan_projection(path):
    """What the planner reads out of a file, minus the byte offsets."""
    result = gcode_plan.scan_file(path)
    entries = [{k: v for k, v in item.items() if k != 'offset' and not k.startswith('_')}
               for item in result['plan']]
    return entries, result['defaults']


def bulky(plan, moves=150):
    """gcode_gen's print with every body move cut into ``moves`` small ones, like a slicer's
    output (so the trimmer has something to thin)."""
    out = []
    for line in gcode_gen.build(plan, {t: 'PLA' for layer in plan for t in layer}).split('\n'):
        match = re.match(r'^G1 X(\d+) Y(\d+) E([\d.]+) F1800$', line)
        if match:
            x, y, e = int(match.group(1)), match.group(2), float(match.group(3))
            out.extend('G1 X%.2f Y%s E%.5f F1800' % (x + i * 0.01, y, e / moves)
                       for i in range(moves))
        else:
            out.append(line)
    return '\n'.join(out) + '\n'


class TrimmerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bmcu-trim-')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        self.original = os.path.join(self.tmp, 'original.gcode')
        with open(self.original, 'w') as stream:
            stream.write(bulky(PLAN))
        self.trimmed = os.path.join(self.tmp, 'trimmed.gcode')
        trim_gcode.main([self.original, self.trimmed])

    def lines(self, path):
        with open(path) as stream:
            return stream.read().split('\n')

    def test_it_is_much_smaller(self):
        self.assertLess(os.path.getsize(self.trimmed), os.path.getsize(self.original) / 5)

    def test_every_layer_keeps_its_extrusion(self):
        before = trim_gcode.layer_extrusion(self.lines(self.original))
        after = trim_gcode.layer_extrusion(self.lines(self.trimmed))
        self.assertEqual(len(before), len(after))
        for index, (a, b) in enumerate(zip(before, after)):
            self.assertAlmostEqual(a, b, delta=0.01, msg='layer %d' % index)

    def test_every_command_that_is_not_a_move_survives_in_order(self):
        def commands(path):
            return [l for l in self.lines(path)
                    if l.strip() and not l.startswith(';') and
                    not trim_gcode.MOVE.match(l.strip())]
        self.assertEqual(commands(self.original), commands(self.trimmed))

    def test_the_planner_reads_the_same_tool_changes_and_temperatures(self):
        self.assertEqual(plan_projection(self.original), plan_projection(self.trimmed))

    def test_the_trimmed_print_runs_to_the_end_in_the_simulator(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.boot(15.0)
        sim.setup_u1()
        sim.load_spools(SPOOLS)
        tools = sim.slice_file(self.trimmed)
        self.assertEqual(tools, [0, 3, 4, 6])
        outcome = checks.assert_recoverable(self, sim)
        self.assertEqual(outcome.kind, 'no_effect')
        self.assertEqual(sim.tool_changes, sim.planned_tool_changes())

    def test_the_originals_plan_like_their_trimmed_files(self):
        originals = glob.glob(os.path.join(FIXTURES, 'original', '*.gcode'))
        if not originals:
            self.skipTest('no untrimmed originals in tests/sim/fixtures/real/original/')
        for original in originals:
            trimmed = os.path.join(self.tmp, os.path.basename(original))
            trim_gcode.main([original, trimmed])
            self.assertEqual(plan_projection(original), plan_projection(trimmed), original)
            self.assertLess(os.path.getsize(trimmed), 1.2e6, original)


def run_file(test, path, faults=()):
    sim = Sim.reference_machine()
    test.addCleanup(sim.close)
    sim.boot(15.0)
    sim.setup_u1()
    with open(path, encoding='utf-8', errors='replace') as stream:
        text = stream.read()
    sim.load_spools(Sim.spools_for_file(text, sim.tools_in(text)))
    sim.slice_file(path)
    for trigger, fault in faults:
        sim.inject(trigger, fault)
    return sim


def fixture_files():
    return sorted(glob.glob(os.path.join(FIXTURES, '*.gcode')))


class FixtureTest(unittest.TestCase):
    def end_to_end(self, files):
        for path in files:
            sim = run_file(self, path)
            outcome = checks.assert_recoverable(self, sim)
            self.assertEqual(outcome.kind, 'no_effect', path)
            self.assertEqual(sim.tool_changes, sim.planned_tool_changes(), path)

    def subsets(self, path):
        """The M8 cable glitch and the M9 power cut on one file. Both are findings today
        (F5, F12), so only a crash of the simulator itself is an error here."""
        from sim import sweep
        results = [
            sweep.run_spec(F.Idle(300.0), F.BmcuDisconnect(3.0), ('replug',), path=path),
            sweep.run_spec(F.BmcuMoving('bmcu0', 'load', 50), F.BmcuDisconnect(3.0),
                           ('replug',), path=path),
            sweep.run_restart_case('power', F.Idle(300.0), path=path),
            sweep.run_restart_case('klipper', F.Idle(300.0), path=path),
        ]
        for kind, detail in results:
            self.assertNotIn('sim error', detail, path)
        return results

    def test_every_fixture_runs_end_to_end_with_no_faults(self):
        files = fixture_files()
        if not files:
            self.skipTest('no real fixtures in tests/sim/fixtures/real/')
        self.end_to_end(files)

    def test_the_disconnect_and_power_loss_subsets_on_the_first_fixture(self):
        files = fixture_files()
        if not files:
            self.skipTest('no real fixtures in tests/sim/fixtures/real/')
        self.subsets(files[0])

    def test_the_same_checks_on_a_trimmed_synthetic_file(self):
        tmp = tempfile.mkdtemp(prefix='bmcu-fixture-')
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        original = os.path.join(tmp, 'a.gcode')
        with open(original, 'w') as stream:
            stream.write(bulky(PLAN))
        trimmed = os.path.join(tmp, 'b.gcode')
        trim_gcode.main([original, trimmed])
        self.end_to_end([trimmed])
        self.subsets(trimmed)


class AnyFileTest(unittest.TestCase):
    """``BMCU_SIM_GCODE=/path/file.gcode python -m unittest tests.test_sim_real``."""

    def test_the_file_from_the_environment(self):
        path = os.environ.get('BMCU_SIM_GCODE')
        if not path:
            self.skipTest('set BMCU_SIM_GCODE=/path/file.gcode to check a print')
        sim = run_file(self, path)
        outcome = checks.recover(sim, ('clear_fault', 'confirm_routes'))
        problems = list(outcome.problems)
        if outcome.state == 'complete':
            problems.extend(checks.verify(sim))
        self.assertEqual(outcome.state, 'complete', outcome)
        self.assertEqual(problems, [])
        self.assertEqual(sim.tool_changes, sim.planned_tool_changes())


class Incident5Test(unittest.TestCase):
    """2026-11-25: at print start the stock load on head 4 failed with
    ``extruder[3]: state: load_extruding, error: move_extrude!`` after Snapmaker's extrude
    test had retried for about 5 minutes; T4 had just loaded on head 2."""

    def run_it(self, make_sim):
        sim = make_sim()
        sim.inject(F.StockFeeding(3, 'load_extruding'), F.Clog(3))
        started = sim.world.now
        sim.start()
        state = sim.run_until_stopped()
        return sim, state, sim.world.now - started

    def check(self, sim, state, elapsed):
        self.assertEqual(state, 'paused')
        self.assertIn('extruder[3]: state: load_extruding, error: move_extrude!',
                      sim.print_stats.message)
        self.assertGreater(elapsed, 250.0)             # the retries took minutes, as it did
        self.assertEqual(sim.fired[0][1], 'clog')
        self.assertGreater(sim.world.sources['bmcu0'].tip, 1800.0)    # T4 had loaded
        outcome = checks.recover(sim, ('clear_fault',), start=False)
        self.assertEqual(outcome.state, 'complete', outcome.messages[-1:])
        self.assertEqual(checks.verify(sim), [])

    def test_on_a_synthetic_print(self):
        def make():
            sim = Sim.reference_machine()
            self.addCleanup(sim.close)
            sim.boot(15.0)
            sim.setup_u1()
            sim.load_spools({4: 'PLA red', 3: 'PLA white'})
            sim.slice([[4], [3], [4]])
            return sim
        sim, state, elapsed = self.run_it(make)
        self.check(sim, state, elapsed)
        # the sim's pause is recoverable; the *words* are Snapmaker's (F13)

    def test_on_the_baby_yoda_fixture(self):
        files = glob.glob(os.path.join(FIXTURES, 'baby_yoda*.gcode'))
        if not files:
            self.skipTest('tests/sim/fixtures/real/baby_yoda*.gcode not supplied yet')
        sim, state, elapsed = self.run_it(lambda: run_file(self, files[0]))
        self.check(sim, state, elapsed)

    def test_f13_the_pause_message_tells_the_user_which_head_and_what_to_do(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.boot(15.0)
        sim.setup_u1()
        sim.load_spools({4: 'PLA red', 3: 'PLA white'})
        sim.slice([[4], [3], [4]])
        sim.inject(F.StockFeeding(3, 'load_extruding'), F.Clog(3))
        sim.start()
        sim.run_until_stopped()
        message = sim.print_stats.message
        self.assertRegex(message, r'(?i)head 4')
        self.assertRegex(message, r'(?i)clog|jam|clear|nozzle')


class ForegroundSelectedPrestageTest(unittest.TestCase):
    """F14, found on the user's mixed six-band test print: Head 3 swapped T4 for T5
    in the background while Head 2 printed a short stock T1 section. The foreground
    then selected Head 3 for that same T5 while the background job still waited for
    it to be PARKED, and the print paused ("Snapmaker head 2 is not PARKED; prestage
    is blocked") with no fault at all."""

    def path(self):
        path = os.path.join(FIXTURES, 'bmcu_test_print.gcode')
        if not os.path.exists(path):
            self.skipTest('tests/sim/fixtures/real/bmcu_test_print.gcode not supplied')
        return path

    def test_the_print_completes_without_a_pause(self):
        sim = run_file(self, self.path())
        sim.start()
        self.assertEqual(sim.run_until_stopped(), 'complete', sim.print_stats.message)
        self.assertEqual(checks.verify(sim), [])

    def test_negative_control_the_old_wait_pauses(self):
        from bmcu_core import manager as M

        def old_wait(self, endpoint, timeout, background_job=None):
            endpoint.wait_prestage_safe(timeout)
            return False
        with mock.patch.object(M.BMCUManager, '_u1_wait_parked_or_foreground_selected',
                               old_wait):
            sim = run_file(self, self.path())
            sim.start()
            state = sim.run_until_stopped()
        self.assertEqual(state, 'paused')
        self.assertIn('is not PARKED; prestage is blocked', sim.print_stats.message)


if __name__ == '__main__':
    unittest.main()

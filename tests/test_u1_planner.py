"""Snapmaker U1 print-start source planner.

A large sliced file (43.7 MB, an 8h49m print) took 16.2 s to scan on the
printer, past the 15 s answer timeout.  BMCU then restarted the planner in the
middle of its scan and the second try timed out the same way, so the print
never started.  These tests cover the faster scan and the wait for a planner
that is still working.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest

import _support
from bmcu_core import gcode_plan as G
from bmcu_core import manager as M

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')

try:
    import fcntl  # noqa: F401
    HAVE_POSIX = hasattr(socket, 'AF_UNIX')
except ImportError:
    HAVE_POSIX = False


SAMPLE = (
    '; nozzle_temperature = 220,215,230\n'
    '; nozzle_temperature_initial_layer = 225,220,235\n'
    '  ; nozzle_temperature_range_high = 240,240,250\n'
    'G28\n'
    'G1 X1 Y1 E1\n'
    '; BMCU_TOOL_CHANGE TOOL=2\n'
    'g1 x2\n'
    'M104 S210\n'
    'BMCU_TOOL_CHANGE TOOL=1\n'
    'G1 X3 E1\n'
    '  M104 T1 S230 ; indented\n'
    '  bmcu_tool_change tool = 2 ; indented\n'
    'm109 s245\n'
    'G1 X4\n'
)


class FastScanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'sample.gcode')
        with open(self.path, 'w') as stream:
            stream.write(SAMPLE)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_moves_and_comments_are_skipped_without_losing_anything(self):
        result = G.scan_file(self.path)
        plan = result['plan']
        self.assertEqual([item['tool'] for item in plan], [1, 2])
        self.assertEqual(plan[0]['offset'], SAMPLE.index('BMCU_TOOL_CHANGE TOOL=1'))
        self.assertEqual(plan[1]['offset'], SAMPLE.index('  bmcu_tool_change'))
        self.assertEqual(plan[0]['temperature'], 230.0)
        self.assertEqual(plan[1]['temperature'], 245.0)
        self.assertEqual(result['defaults']['2']['temperature_max'], 250.0)
        self.assertEqual(result['defaults']['0']['initial_temperature'], 225.0)

    def test_same_result_as_the_full_parse(self):
        fast = G.scan_file(self.path)
        original = G._MOVE_FIRST_BYTES
        try:
            G._MOVE_FIRST_BYTES = ()
            full = G.scan_file(self.path)
        finally:
            G._MOVE_FIRST_BYTES = original
        for result in (fast, full):
            result.pop('elapsed_ms')
        self.assertEqual(fast, full)


SLOW_PLANNER = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, {scripts!r})
    import bmcu_plannerd as daemon
    real = daemon.scan_file
    def slow(path, expected):
        time.sleep({delay!r})
        return real(path, expected)
    daemon.scan_file = slow
    sys.argv = ['bmcu_plannerd', '--socket', {socket!r}, '--result-dir', {results!r}]
    sys.exit(daemon.main())
''')


@unittest.skipUnless(HAVE_POSIX, 'needs Unix sockets and fcntl')
class SlowPlannerTest(unittest.TestCase):
    """The real planner daemon, made slow, answering the real Klipper client."""

    def start_planner(self, delay):
        self.root = tempfile.mkdtemp()
        os.chmod(self.root, 0o700)
        self.socket = os.path.join(self.root, 'u1-planner.sock')
        self.results = os.path.join(self.root, 'u1-plans')
        script = os.path.join(self.root, 'slow_planner.py')
        with open(script, 'w') as stream:
            stream.write(SLOW_PLANNER.format(
                scripts=SCRIPTS, delay=delay, socket=self.socket,
                results=self.results))
        self.process = subprocess.Popen(
            [sys.executable, script], stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10.0
        while not os.path.exists(self.socket):
            if time.monotonic() > deadline or self.process.poll() is not None:
                self.fail('planner did not start')
            time.sleep(0.02)
        self.gcode = os.path.join(self.root, 'print.gcode')
        with open(self.gcode, 'w') as stream:
            stream.write(SAMPLE)

    def tearDown(self):
        process = getattr(self, 'process', None)
        if process is not None:
            if process.poll() is None:
                process.send_signal(18)  # SIGCONT, in case a test stopped it
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stderr.close()
        shutil.rmtree(getattr(self, 'root', ''), ignore_errors=True)

    def client(self, timeout):
        m = object.__new__(M.BMCUManager)
        m.reactor = types.SimpleNamespace(
            monotonic=time.monotonic,
            pause=lambda until: time.sleep(max(0.0, until - time.monotonic())))
        m.printer_analysis = {'features': {'snapmaker_u1': True}}
        m.u1_planner_timeout = timeout
        m.u1_planner_poll_interval = 0.05
        m._u1_planner_socket = self.socket
        m._u1_planner_result_dir = self.results
        m._u1_planner_requests = 0
        m._u1_planner_failures = 0
        m._u1_planner_max_wait_ms = 0.0
        m._klippy_disconnecting = False
        m._reset_u1_lookahead = lambda stop_jobs=False: None
        m._virtual_sd_file_path = lambda: self.gcode
        m._validate_u1_external_plan = lambda result, path, identity: (
            result['plan'], result['defaults'])
        m.recoveries = []

        def recover(restart=False):
            m.recoveries.append(restart)
            raise M.BMCUError('restarted a working planner')
        m._recover_u1_planner = recover
        return m

    def test_waits_for_a_planner_still_scanning(self):
        self.start_planner(delay=1.5)
        m = self.client(timeout=0.4)
        self.assertTrue(m._load_u1_toolchange_plan())
        self.assertEqual([item['tool'] for item in m._u1_toolchange_plan], [1, 2])
        self.assertEqual(m.recoveries, [])
        self.assertGreaterEqual(m._u1_planner_last_wait_ms, 1400.0)
        self.assertEqual(
            [name for name in os.listdir(self.results) if name.endswith('.busy')], [])
        self.process.terminate()
        log = self.process.communicate(timeout=5)[1].decode()
        self.assertIn('scanned print.gcode in 1.5 s', log)

    def test_fast_planner_answers_inside_the_timeout(self):
        self.start_planner(delay=0.0)
        m = self.client(timeout=2.0)
        self.assertTrue(m._load_u1_toolchange_plan())
        self.assertEqual(m.recoveries, [])

    def test_hung_planner_still_gets_recovered(self):
        self.start_planner(delay=0.0)
        self.process.send_signal(19)  # SIGSTOP: alive but never reads
        m = self.client(timeout=0.3)
        try:
            with self.assertRaises(M.BMCUError):
                m._load_u1_toolchange_plan()
        finally:
            self.process.send_signal(18)  # SIGCONT
        self.assertEqual(m.recoveries, [True])

    def test_dead_planner_with_a_left_over_busy_marker_is_recovered(self):
        self.start_planner(delay=0.0)
        m = self.client(timeout=0.3)
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        original = m._u1_planner_busy

        def busy(request_id):
            with open(os.path.join(self.results, request_id + '.busy'), 'w') as s:
                s.write('%d\n' % dead.pid)
            return original(request_id)
        m._u1_planner_busy = busy
        self.process.send_signal(19)
        try:
            with self.assertRaises(M.BMCUError):
                m._load_u1_toolchange_plan()
        finally:
            self.process.send_signal(18)
        self.assertEqual(m.recoveries, [True])


if __name__ == '__main__':
    unittest.main()

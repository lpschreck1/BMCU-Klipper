"""G-code source planner: tool-change order, offsets and temperatures."""
import os
import shutil
import tempfile
import unittest

import _support  # noqa: F401  (path setup)
from bmcu_core import gcode_plan


class ScanFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        path = os.path.join(self.dir, 'print.gcode')
        with open(path, 'wb') as stream:
            stream.write(text.encode('ascii'))
        return path

    def test_tool_order_and_offsets(self):
        text = ('; nozzle_temperature = 210,220,230\n'
                'G28\n'
                'BMCU_TOOL_CHANGE TOOL=0\n'
                'G1 X1\n'
                'bmcu_tool_change tool = 2\n'
                'G1 X2\n'
                'BMCU_TOOL_CHANGE TOOL=1\n')
        result = gcode_plan.scan_file(self.write(text))
        plan = result['plan']
        self.assertEqual([item['tool'] for item in plan], [0, 2, 1])
        for item in plan:
            line = text.encode('ascii')[item['offset']:].split(b'\n', 1)[0]
            self.assertIn(b'TOOL', line.upper())
        self.assertEqual(result['schema'], gcode_plan.PLAN_SCHEMA)

    def test_ignores_comments_and_out_of_range_tools(self):
        text = ('; BMCU_TOOL_CHANGE TOOL=5\n'
                'BMCU_TOOL_CHANGE TOOL=99\n'
                'BMCU_TOOL_CHANGE TOOL=3\n')
        plan = gcode_plan.scan_file(self.write(text))['plan']
        self.assertEqual([item['tool'] for item in plan], [3])

    def test_empty_file(self):
        self.assertEqual(gcode_plan.scan_file(self.write(''))['plan'], [])

    def test_rejects_bad_paths(self):
        with self.assertRaises(gcode_plan.PlanError):
            gcode_plan.scan_file(os.path.join(self.dir, 'missing.gcode'))
        with self.assertRaises(gcode_plan.PlanError):
            gcode_plan.scan_file(self.dir)

    def test_changed_identity_is_rejected(self):
        path = self.write('BMCU_TOOL_CHANGE TOOL=0\n')
        identity = gcode_plan.stat_identity(os.stat(path))
        with open(path, 'ab') as stream:
            stream.write(b'G1 X1\n')
        os.utime(path, ns=(1, 1))
        with self.assertRaises(gcode_plan.PlanError):
            gcode_plan.scan_file(path, expected_identity=identity)


class TemperatureTest(unittest.TestCase):
    def test_parse_temperature_command(self):
        self.assertEqual(gcode_plan.parse_temperature_command(b'M104 S215 T2\n', 0), (2, 215.0))
        self.assertEqual(gcode_plan.parse_temperature_command(b'M109 S230\n', 1), (1, 230.0))
        self.assertIsNone(gcode_plan.parse_temperature_command(b'M109 S230\n', None))
        self.assertIsNone(gcode_plan.parse_temperature_command(b'G1 X1 S5\n', 0))

    def test_parse_temperature_list(self):
        values = gcode_plan.parse_temperature_list('210,220, 230')
        self.assertEqual([float(v) for v in values], [210.0, 220.0, 230.0])


if __name__ == '__main__':
    unittest.main()

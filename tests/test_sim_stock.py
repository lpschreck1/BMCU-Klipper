"""M2: fake Snapmaker feeder, sensors and macros, driven through the real endpoints.py.

Covers a plain stock preload, load and unload, the head-sensor lag, and incident 4
(a stale head sensor must not skip the stock push).
"""
import unittest

import _support  # noqa: F401
from sim.runner import Sim
from sim import world as W

HEAD = 3                      # stock head used in these tests (extruder3, 'Head 4')
FEEDER = ('right', 1)         # u1_head3 -> filament_feed right, channel 1


class StockTestBase(unittest.TestCase):
    def setUp(self):
        self.sim = Sim.reference_machine(bmcu='manager')
        self.addCleanup(self.sim.close)
        self.sim.printer.send_event('klippy:ready')
        self.sim.settle(2.0)
        self.sim.run_gcode('BMCU_APPLY_PRESET PRESET=snapmaker_u1')
        self.endpoint = self.sim.manager.endpoints['u1_head%d' % HEAD]
        self.stock = self.sim.world.sources['stock%d' % HEAD]
        self.feeder = self.sim.feeders[FEEDER[0]]
        self.sim.print_task_config.set_head(HEAD)

    def preload(self, short_route=False):
        if short_route:
            self.stock.tube_len = 100.0
        self.sim.insert_stock(HEAD)
        self.sim.settle(60.0)


class PreloadTest(StockTestBase):
    def test_preload_stops_at_the_wheel_count(self):
        self.preload()
        self.assertEqual(self.feeder.channel_state[FEEDER[1]], 'preload_finish')
        self.assertAlmostEqual(self.stock.tip, 850.0, delta=15.0)
        self.assertFalse(self.sim.world.sensor_physical(HEAD))

    def test_preload_stops_at_the_head_sensor_on_a_short_route(self):
        self.preload(short_route=True)
        self.assertEqual(self.feeder.channel_state[FEEDER[1]], 'preload_finish')
        self.assertTrue(self.sim.world.sensor_physical(HEAD))
        self.assertAlmostEqual(self.stock.tip, self.stock.sensor_p, delta=15.0)

    def test_preload_stalls_on_a_snag(self):
        self.stock.snag_at = 300.0
        self.sim.insert_stock(HEAD)
        self.sim.settle(60.0)
        self.assertEqual(self.feeder.channel_state[FEEDER[1]], 'preload_fail')
        self.assertEqual(self.feeder.channel_error[FEEDER[1]], 'wheel_speed')


class StockLoadTest(StockTestBase):
    def test_load_and_status(self):
        self.preload(short_route=True)
        state = self.endpoint.native_feeder_load(printing=False)
        self.assertEqual(state['channel_state'], 'load_finish')
        self.assertTrue(self.stock.in_gears)
        self.assertGreaterEqual(self.stock.tip, self.stock.gears_p)
        self.assertEqual(self.sim.machine_state.main_state, 'IDLE')
        # the head is selected, the stock wheel did the pushing
        self.assertEqual(self.sim.toolhead.active, HEAD)
        self.assertGreater(self.stock.counts(), 0)

    def test_load_fails_when_the_tip_is_deformed(self):
        self.preload(short_route=True)
        self.stock.quality = 'deformed'
        with self.assertRaises(Exception) as ctx:
            self.endpoint.native_feeder_load(printing=False)
        self.assertIn('move_extrude', str(ctx.exception))
        self.assertEqual(self.feeder.channel_state[FEEDER[1]], 'load_fail')

    def test_load_times_out_after_60_seconds_when_the_wheel_slips(self):
        self.preload(short_route=True)
        self.stock.tip = 200.0              # pulled back by hand, nothing at the head
        self.stock.snag_at = 200.0
        with self.assertRaises(Exception):
            self.endpoint.native_feeder_load(printing=False)
        self.assertIn(self.feeder.channel_error[FEEDER[1]], ('wheel_speed', 'timeout'))


class HeadSensorLagTest(StockTestBase):
    def test_sensor_report_lags_while_printing(self):
        self.preload(short_route=True)
        sensor = self.sim.sensors[HEAD]
        self.assertTrue(sensor.runout_helper.filament_present)
        self.sim.print_stats.state = 'printing'
        self.stock.tip = 100.0                      # filament pulled back out of the head
        self.sim.settle(1.0)
        self.assertFalse(sensor.runout_buttun_state)               # the switch opened
        self.assertTrue(sensor.runout_helper.filament_present)     # but the report lags
        self.sim.extruders[HEAD].e_position += 8.0                 # detection_length of motion
        self.sim.settle(1.0)
        self.assertFalse(sensor.runout_helper.filament_present)

    def test_sensor_report_does_not_lag_when_idle(self):
        self.preload(short_route=True)
        self.stock.tip = 100.0
        self.sim.settle(1.0)
        self.assertFalse(self.sim.sensors[HEAD].runout_helper.filament_present)


class StaleHeadSensorTest(StockTestBase):
    """Incident 4: after a BMCU unload the sensor still reports present, so Snapmaker's
    FEED_AUTO skips its push (phase 2). BMCU must clear the stale reading first."""

    def make_stale(self):
        self.preload(short_route=True)
        self.sim.print_stats.state = 'printing'
        self.sim.machine_state.main_state = 'PRINTING'
        self.stock.tip = 50.0             # BMCU pulled the filament back, nothing moved the head
        self.sim.settle(1.0)
        helper = self.sim.sensors[HEAD].runout_helper
        self.assertTrue(helper.filament_present)
        self.assertFalse(self.sim.sensors[HEAD].runout_buttun_state)

    def test_load_completes_after_the_stale_reading_is_cleared(self):
        self.make_stale()
        self.endpoint.printer.lookup_object('extruder%d' % HEAD).heater.set_target(235.0)
        state = self.endpoint.native_feeder_load(printing=True)
        self.assertEqual(state['channel_state'], 'load_finish')
        self.assertTrue(self.stock.in_gears)

    def test_without_the_fix_the_push_is_skipped_and_the_load_fails(self):
        # negative control: the code before the fix never cleared the stale reading
        self.make_stale()
        self.endpoint._clear_stale_native_head_presence = lambda channel: False
        self.endpoint.printer.lookup_object('extruder%d' % HEAD).heater.set_target(235.0)
        with self.assertRaises(Exception):
            self.endpoint.native_feeder_load(printing=True)
        self.assertEqual(self.feeder.channel_state[FEEDER[1]], 'load_fail')
        self.assertFalse(self.stock.in_gears)


if __name__ == '__main__':
    unittest.main()

"""M1: the simulator's clock, physical model and fake Klipper core."""
import unittest

import _support  # noqa: F401  (sets sys.path)
from sim import klipper as K
from sim import world as W
from sim.clock import SimReactor


def head_with_two_sources():
    world = W.World()
    world.add_head(2)
    stock = world.add_source(2, 'stock3', 'stock')
    bmcu = world.add_source(2, 'bmcu1', 'bmcu')
    return world, stock, bmcu


class ClockTest(unittest.TestCase):
    def test_pause_advances_virtual_time_only(self):
        reactor = SimReactor(start=10.0)
        self.assertEqual(reactor.pause(12.5), 12.5)
        self.assertEqual(reactor.monotonic(), 12.5)

    def test_timers_run_in_order_and_reschedule(self):
        reactor = SimReactor()
        seen = []

        def make(name, period):
            def tick(now):
                seen.append((name, round(now - 100.0, 2)))
                return now + period
            return tick
        reactor.register_timer(make('a', 1.0), reactor.now + 1.0)
        reactor.register_timer(make('b', 1.5), reactor.now + 1.5)
        reactor.pause(reactor.now + 3.1)
        self.assertEqual(seen, [('a', 1.0), ('b', 1.5), ('a', 2.0), ('a', 3.0), ('b', 3.0)])

    def test_steppers_integrate_world(self):
        reactor = SimReactor()
        world = W.World()
        world.add_head(0)
        reactor.add_stepper(world.step)
        reactor.pause(reactor.now + 2.0)
        self.assertAlmostEqual(world.now, 2.0, places=6)

    def test_callbacks_run_once(self):
        reactor = SimReactor()
        seen = []
        reactor.register_callback(lambda now: seen.append(now), reactor.now + 1)
        reactor.pause(reactor.now + 5)
        self.assertEqual(len(seen), 1)


class PathTest(unittest.TestCase):
    def test_feed_stops_at_sensor(self):
        world, stock, _ = head_with_two_sources()
        world.insert(stock)
        moved, reason = world.feed_until(stock, 5000, stop_at_sensor=True)
        self.assertEqual(reason, 'sensor')
        self.assertAlmostEqual(stock.tip, stock.sensor_p, delta=5.0)
        self.assertTrue(world.sensor_physical(2))
        self.assertAlmostEqual(moved, stock.tip)

    def test_feed_stops_at_count(self):
        world, stock, _ = head_with_two_sources()
        world.insert(stock)
        moved, reason = world.feed_until(stock, 850)
        self.assertEqual(reason, 'done')
        self.assertAlmostEqual(stock.tip, 850.0)
        self.assertFalse(world.sensor_physical(2))
        # ppr 6: counts x 31.4159 / 12 is the distance back in mm
        self.assertAlmostEqual(stock.counts() * 31.4159 / 12, 850.0, delta=1.5)

    def test_snag_stalls_the_wheel(self):
        world, stock, _ = head_with_two_sources()
        world.insert(stock)
        stock.snag_at = 58.0
        moved, reason = world.feed_until(stock, 200)
        self.assertEqual(reason, 'stalled')
        self.assertAlmostEqual(stock.tip, 58.0)
        self.assertTrue(stock.stalled)

    def test_tip_in_shared_tube_blocks_the_other_source(self):
        world, stock, bmcu = head_with_two_sources()
        world.insert(bmcu)
        world.insert(stock)
        world.feed_until(bmcu, bmcu.tube_len + 100)        # parked past the splitter
        self.assertTrue(bmcu.in_shared)
        moved, reason = world.feed_until(stock, 2000)
        self.assertEqual(reason, 'stalled')
        self.assertAlmostEqual(stock.tip, stock.splitter_p)
        self.assertEqual(world.consistent(), [])

    def test_bmcu_encoder_follows_pulled_filament(self):
        world, _, bmcu = head_with_two_sources()
        world.insert(bmcu)
        world.feed_until(bmcu, bmcu.gears_p + 1, chunk=10)
        world.heads[2].heater.temp = 220.0
        before = bmcu.encoder_mm
        moved, flow = world.gear_move(2, 40.0)       # the head pulls by itself
        self.assertAlmostEqual(moved, 40.0)
        self.assertAlmostEqual(bmcu.encoder_mm - before, 40.0)
        self.assertAlmostEqual(bmcu.pushed_mm, bmcu.gears_p + 1 - 0.0, delta=10)
        back, _ = world.gear_move(2, -15.0)
        self.assertAlmostEqual(bmcu.encoder_mm - before, 25.0)

    def test_cold_extrusion_refused(self):
        world, _, bmcu = head_with_two_sources()
        world.insert(bmcu)
        world.feed_until(bmcu, bmcu.gears_p + 1)
        with self.assertRaises(W.WorldError):
            world.gear_move(2, 5.0)

    def test_flow_only_at_nozzle_and_not_when_clogged(self):
        world, _, bmcu = head_with_two_sources()
        world.insert(bmcu, tip=bmcu.nozzle_p - 1.0)
        world.heads[2].heater.temp = 230.0
        moved, flow = world.gear_move(2, 10.0)
        self.assertAlmostEqual(flow, 10.0)
        world.heads[2].clog = True
        _, flow = world.gear_move(2, 10.0)
        self.assertEqual(flow, 0.0)

    def test_runout_stops_feed(self):
        world, stock, _ = head_with_two_sources()
        world.insert(stock, spool_mm=120.0)
        moved, reason = world.feed_until(stock, 500)
        self.assertEqual(reason, 'stalled')
        self.assertAlmostEqual(moved, 120.0)

    def test_coil_frequency(self):
        world, _, bmcu = head_with_two_sources()
        world.heads[2].heater.temp = 230.0
        world.insert(bmcu, tip=bmcu.gears_p + 5)
        self.assertEqual(world.coil_freq(2), W.BASELINE_COIL)   # nothing gripped yet
        world.gear_move(2, 10.0)
        self.assertGreaterEqual(world.coil_freq(2) - W.BASELINE_COIL, 400)
        world.step(2.0)                                         # the reading decays
        self.assertEqual(world.coil_freq(2), W.BASELINE_COIL)

    def test_deformed_tip_is_missed_by_the_gears(self):
        world, _, bmcu = head_with_two_sources()
        world.heads[2].heater.temp = 230.0
        world.insert(bmcu, quality='deformed', tip=bmcu.sensor_p + 1)
        bmcu.assisting = True
        moved, _ = world.gear_move(2, 40.0)       # carried up to the gears, then missed
        self.assertAlmostEqual(bmcu.tip, bmcu.gears_p)
        self.assertLess(world.coil_freq(2) - W.BASELINE_COIL, 100)

    def test_clog_is_missed_and_blocks_flow(self):
        world, _, bmcu = head_with_two_sources()
        world.heads[2].heater.temp = 230.0
        world.heads[2].clog = True
        world.insert(bmcu, tip=bmcu.gears_p + 5)
        _, flow = world.gear_move(2, 20.0)
        self.assertEqual(flow, 0.0)
        self.assertLess(world.coil_freq(2) - W.BASELINE_COIL, 100)

    def test_unassisted_tip_before_the_gears_is_not_carried(self):
        world, _, bmcu = head_with_two_sources()
        world.heads[2].heater.temp = 230.0
        world.insert(bmcu, tip=bmcu.sensor_p + 1)
        moved, _ = world.gear_move(2, 40.0)
        self.assertEqual(moved, 0.0)


class HeaterTest(unittest.TestCase):
    def test_first_order_approach_about_5c_per_second(self):
        reactor = SimReactor()
        world = W.World()
        head = world.add_head(0)
        reactor.add_stepper(world.step)
        head.heater.target = 225.0
        reactor.pause(reactor.now + 10.0)
        self.assertAlmostEqual(head.heater.temp, 25.0 + 50.0, delta=0.5)
        reactor.pause(reactor.now + 60.0)
        self.assertEqual(head.heater.temp, 225.0)
        self.assertTrue(head.heater.can_extrude())

    def test_klipper_wait_uses_virtual_time(self):
        reactor = SimReactor()
        world = W.World()
        head = world.add_head(0)
        reactor.add_stepper(world.step)
        heater = K.FakeHeater(reactor, head.heater, 'extruder')
        heater.set_target(200.0)
        start = reactor.monotonic()
        heater.wait(200.0)
        self.assertAlmostEqual(reactor.monotonic() - start, 35.0, delta=1.0)


class GcodeTest(unittest.TestCase):
    def test_register_detach_and_reregister(self):
        printer = K.FakePrinter(SimReactor())
        gcode = printer.gcode
        seen = []
        gcode.register_command('RESUME', lambda g: seen.append('stock'))
        original = gcode.register_command('RESUME', None)
        self.assertIsNotNone(original)
        self.assertNotIn('RESUME', gcode.ready_gcode_handlers)
        gcode.register_command('RESUME', lambda g: (seen.append('wrap'), original(g)))
        gcode.run_script_from_command('RESUME')
        self.assertEqual(seen, ['wrap', 'stock'])

    def test_parameters(self):
        printer = K.FakePrinter(SimReactor())
        got = {}

        def handler(gcmd):
            got.update(extruder=gcmd.get_int('EXTRUDER', -1),
                       name=gcmd.get('NAME', ''), temp=gcmd.get_float('TEMP', 0.))
        printer.gcode.register_command('FOO', handler)
        printer.gcode.run_script_from_command('FOO EXTRUDER=2 NAME=x TEMP=215.5')
        self.assertEqual(got, {'extruder': 2, 'name': 'x', 'temp': 215.5})

    def test_traditional_parameters(self):
        printer = K.FakePrinter(SimReactor())
        got = {}
        printer.gcode.register_command('M104', lambda g: got.update(
            s=g.get_float('S'), t=g.get_int('T')))
        printer.gcode.run_script_from_command('M104 S200 T3')
        self.assertEqual(got, {'s': 200.0, 't': 3})

    def test_unknown_command_is_an_error(self):
        printer = K.FakePrinter(SimReactor())
        with self.assertRaises(K.CommandError):
            printer.gcode.run_script_from_command('NOPE')


if __name__ == '__main__':
    unittest.main()

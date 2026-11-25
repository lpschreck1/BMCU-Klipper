"""M3: the real manager and device.py against a fake BMCU firmware at protocol level."""
import struct
import unittest

import _support  # noqa: F401
from bmcu_core import protocol as P
from sim.runner import Sim


class BmcuTestBase(unittest.TestCase):
    def setUp(self):
        self.sim = Sim.reference_machine()
        self.addCleanup(self.sim.close)
        self.sim.boot(15.0)
        self.sim.setup_u1()
        self.device = self.sim.manager.devices[0]
        self.fw = self.sim.bmcu.fw
        self.world = self.sim.world


class HandshakeTest(BmcuTestBase):
    def test_session_is_established_and_reconciled(self):
        d = self.device
        self.assertTrue(d.connected and d.hello_validated and d.ready)
        self.assertTrue(d.firmware_compatible)
        self.assertTrue(d.status_reconciled)
        self.assertEqual(d.caps['firmware'], '1.0.5')
        self.assertEqual(d.status['route_state_name'], ['EMPTY'] * 4)

    def test_wrong_firmware_is_identified_but_not_operational(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.bmcu.fw.firmware = (1, 0, 4)
        sim.boot(15.0)
        d = sim.manager.devices[0]
        self.assertFalse(d.firmware_compatible)
        self.assertFalse(d.ready)
        self.assertIn('1.0.5', d.firmware_error)

    def test_helper_without_a_session_keeps_the_device_offline(self):
        sim = Sim.reference_machine()
        self.addCleanup(sim.close)
        sim.bmcu.helper.stuck = True
        sim.boot(15.0)
        d = sim.manager.devices[0]
        self.assertFalse(d.connected)
        self.assertFalse(d.ready)

    def test_runtime_sync_is_validated_like_the_firmware(self):
        payload = P.pack_runtime_sync(
            dict(self.fw.config), (0, 0, 0), channel_retract_m=[0.2] * 4,
            channel_autoload_m=[0.12] * 4)
        bad = bytearray(payload)
        struct.pack_into('<f', bad, 10 * 4, 5.0)          # autoload channel 1 = 5 m
        before = len(self.fw.runtime_syncs)
        with self.assertRaises(RuntimeError):
            self.device.request(P.MSG_RUNTIME_SYNC, bytes(bad), timeout=2.0)
        self.assertEqual(len(self.fw.runtime_syncs), before)


class StatusTest(BmcuTestBase):
    def test_present_follows_the_spool(self):
        self.assertEqual(self.device.status['present'], [0, 0, 0, 0])
        self.sim.insert_bmcu(1)
        self.sim.settle(2.0)
        self.assertEqual(self.device.status['present'], [0, 1, 0, 0])

    def test_encoder_meters_follow_the_world(self):
        self.sim.insert_bmcu(0)
        self.world.push('bmcu0', 500.0)
        self.sim.settle(2.0)
        self.assertAlmostEqual(self.device.status['meters'][0], 0.5, places=3)


class LoadUnloadTest(BmcuTestBase):
    def load_t4(self):
        self.sim.insert_bmcu(0)
        self.sim.settle(2.0)
        self.sim.run_gcode('BMCU_LOAD TOOL=4')

    def test_load_matches_the_world(self):
        self.load_t4()
        source = self.world.sources['bmcu0']
        self.assertEqual(self.device.status['route_state_name'][0], 'LOADED')
        self.assertEqual(self.world.consistent({'bmcu0': 'loaded'}), [])
        self.assertTrue(source.past(source.sensor_p))
        self.assertAlmostEqual(source.tip, source.nozzle_p, delta=2.0)
        self.assertEqual(self.sim.toolhead.active, 2)               # head 3 selected
        self.assertGreater(source.encoder_mm, source.sensor_p)
        # the other channel and the stock feeder on that head are untouched
        self.assertIsNone(self.world.sources['bmcu1'].tip)
        self.assertEqual(self.sim.manager.loaded_tools, {
            self.sim.manager._route_key(self.device, 0): 4})
        self.assertIn('BMCU loaded T4 from bmcu0 Channel 1 into u1_head2',
                      self.sim.gcode.output)

    def test_unload_returns_the_filament_to_the_channel(self):
        self.load_t4()
        self.sim.run_gcode('BMCU_UNLOAD')
        source = self.world.sources['bmcu0']
        self.assertEqual(self.device.status['route_state_name'][0], 'EMPTY')
        self.assertEqual(self.world.consistent({'bmcu0': 'empty'}), [])
        self.assertFalse(self.world.sensor_physical(2))
        self.assertAlmostEqual(source.tip, 0.0, delta=2.0)
        self.assertEqual(self.sim.manager.loaded_tools, {})

    def test_load_again_after_unload(self):
        self.load_t4()
        self.sim.run_gcode('BMCU_UNLOAD')
        self.sim.settle(2.0)
        self.sim.run_gcode('BMCU_LOAD TOOL=4')
        self.assertEqual(self.world.consistent({'bmcu0': 'loaded'}), [])

    def test_swap_t4_to_t5_on_the_same_head(self):
        self.load_t4()
        self.sim.insert_bmcu(1)
        self.sim.settle(2.0)
        self.sim.run_gcode('BMCU_LOAD TOOL=5')
        self.assertEqual(self.world.consistent({'bmcu1': 'loaded', 'bmcu0': 'empty'}), [])
        self.assertEqual(self.device.status['route_state_name'][:2], ['EMPTY', 'LOADED'])

    def test_a_snag_in_the_tube_stops_the_load_with_a_clear_error(self):
        self.sim.insert_bmcu(0)
        self.world.sources['bmcu0'].snag_at = 58.0
        self.sim.settle(2.0)
        with self.assertRaises(Exception) as ctx:
            self.sim.run_gcode('BMCU_LOAD TOOL=4')
        self.assertTrue(str(ctx.exception))
        self.assertNotEqual(self.device.status['route_state_name'][0], 'LOADED')
        self.assertFalse(self.world.sources['bmcu0'].past(
            self.world.sources['bmcu0'].gears_p))


if __name__ == '__main__':
    unittest.main()

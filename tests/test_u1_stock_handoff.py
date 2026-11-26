"""Snapmaker U1: switching a BMCU Head back to its stock feeder mid-print.

Covers the preload setting, the lagging head sensor, slow-load warnings, the
hand-back refusal reasons and a background job's own channel locks.
"""
import logging
import os
import types
import unittest
from unittest import mock

import _support
from bmcu_core import endpoints as E
from bmcu_core import manager as M
from bmcu_core import protocol


def u1_endpoint(config=None, feeder_state='preload_finish', input_detected=True):
    endpoint = object.__new__(E.SnapmakerU1Endpoint)
    endpoint._native_empty_projection = False
    endpoint.config = dict(config or {})
    endpoint.get = lambda key, default=None: endpoint.config.get(key, default)
    endpoint._native_feeder_status = lambda strict=False: {
        'channel_state': feeder_state, 'filament_detected': input_detected,
        'disable_auto': False}
    endpoint._head = lambda: 3
    return endpoint


class PreloadClearTest(unittest.TestCase):
    def test_off_by_default(self):
        path = u1_endpoint().native_path_status()
        self.assertTrue(path['busy'])
        self.assertTrue(path['manual_retract_required'])
        self.assertFalse(path['preload_clear'])

    def test_on_clears_only_preload(self):
        for state, busy in (('preload_finish', False), ('unload_finish', True),
                            ('load_finish', True), ('none', False)):
            path = u1_endpoint({'u1_preload_clear': True}, state).native_path_status()
            self.assertEqual(path['busy'], busy, state)
            self.assertEqual(path['preload_clear'], state == 'preload_finish', state)

    def test_capability_reported(self):
        # the slicer plugin detects builds with these fixes from this field
        endpoint = u1_endpoint({'u1_preload_clear': True})
        self.assertTrue(endpoint.preload_is_clear())


class StaleHeadSensorTest(unittest.TestCase):
    def endpoint(self, switch, reported, physical):
        endpoint = u1_endpoint()
        endpoint.manager = types.SimpleNamespace(reactor=_support.FakeReactor())
        self.sensor = types.SimpleNamespace(
            runout_buttun_state=switch, _extruder_pos_update_timer='timer',
            runout_helper=types.SimpleNamespace(filament_present=reported))
        feeder = types.SimpleNamespace(runout_sensor=[None, self.sensor])
        endpoint._native_feeder_object = lambda strict=False: feeder
        endpoint.entry_sensor_snapshot = lambda: {'physical_detected': physical}
        return endpoint

    def test_clears_lagging_presence_without_runout(self):
        endpoint = self.endpoint(switch=False, reported=True, physical=False)
        with self.assertLogs(level='INFO') as logs:
            self.assertTrue(endpoint._clear_stale_native_head_presence(1))
        self.assertFalse(self.sensor.runout_helper.filament_present)
        self.assertEqual(endpoint.manager.reactor.updates,
                         [('timer', endpoint.manager.reactor.NEVER)])
        self.assertIn('head 4', logs.output[0])

    def test_leaves_real_filament_alone(self):
        for switch, reported, physical in ((True, True, True), (False, True, True),
                                           (False, False, False)):
            endpoint = self.endpoint(switch, reported, physical)
            self.assertFalse(endpoint._clear_stale_native_head_presence(1))
            self.assertEqual(self.sensor.runout_helper.filament_present, reported)

    def test_missing_sensor_is_ignored(self):
        endpoint = u1_endpoint()
        endpoint._native_feeder_object = lambda strict=False: types.SimpleNamespace(runout_sensor=[])
        self.assertFalse(endpoint._clear_stale_native_head_presence(1))


class SlowStockLoadTest(unittest.TestCase):
    def endpoint(self):
        endpoint = u1_endpoint()
        endpoint.manager = types.SimpleNamespace(reactor=_support.FakeReactor())
        self.helper = types.SimpleNamespace(filament_present=False)
        feeder = types.SimpleNamespace(
            runout_sensor=[None, types.SimpleNamespace(runout_helper=self.helper)])
        endpoint._native_feeder_object = lambda strict=False: feeder
        endpoint.gcode = types.SimpleNamespace(respond_info=mock.Mock())
        return endpoint

    def test_measures_arrival_time(self):
        endpoint = self.endpoint()
        stop, result = endpoint._watch_native_feed_arrival(1)
        reactor = endpoint.manager.reactor
        reactor.run_until(reactor.now + 12.0)
        self.helper.filament_present = True
        reactor.run_until(reactor.now + 1.0)
        stop()
        self.assertAlmostEqual(result['seconds'], 12.25, delta=0.5)

    def test_warns_only_when_slow(self):
        endpoint = self.endpoint()
        endpoint._report_native_feed_time(15.0)
        endpoint._report_native_feed_time(None)
        endpoint.gcode.respond_info.assert_not_called()
        with self.assertLogs(level='WARNING'):
            endpoint._report_native_feed_time(45.0)
        message = endpoint.gcode.respond_info.call_args[0][0]
        self.assertIn('45 s', message)
        self.assertIn('head 4', message)


class RestoreAfterStockLoadTest(unittest.TestCase):
    """Snapmaker's load ends at the purge spot with the heater off; BMCU puts back
    the printing temperature and the XY idle position before printing resumes."""
    def endpoint(self, target, feeder_state='load_finish'):
        endpoint = u1_endpoint({}, feeder_state)
        endpoint.manager = types.SimpleNamespace(reactor=_support.FakeReactor())
        heater = types.SimpleNamespace(target_temp=target, min_extrude_temp=170.0)
        extruder = types.SimpleNamespace(get_heater=lambda: heater, printing_e_pos=-0.8)
        self.extruder = extruder
        endpoint.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None: extruder if name == 'extruder3' else default)
        endpoint.native_feeder_target = lambda: ('right', 1)
        endpoint._clear_stale_native_head_presence = lambda channel: False
        endpoint._native_feeder_object = lambda strict=False: None
        self.scripts = []
        endpoint._run = lambda script, wait_moves=True, printer_priority=False: self.scripts.append(script)
        return endpoint

    def test_restores_temperature_and_position(self):
        self.endpoint(235.0).native_feeder_load(printing=True)
        self.assertEqual(self.scripts, [
            'SAVE_GCODE_STATE NAME=BMCU_NATIVE_LOAD',
            'FEED_AUTO MODULE=right CHANNEL=1 LOAD=1 PRINTING=1',
            'SET_HEATER_TEMPERATURE HEATER=extruder3 TARGET=235.0',
            'MOVE_TO_XY_IDLE_POSITION_EXTRUDER',
            'TEMPERATURE_WAIT SENSOR=extruder3 MINIMUM=230.0',
            'RESTORE_GCODE_STATE NAME=BMCU_NATIVE_LOAD MOVE=0'])

    def test_bmcu_moves_not_retracted_again_by_flush(self):
        # BMCU's pullback counted into Snapmaker's pending retract; the flush would
        # pull the new filament back that far and the print would run dry.
        endpoint = self.endpoint(235.0)
        self.extruder.printing_e_pos = -92.4
        with self.assertLogs(level='INFO') as logs:
            endpoint.native_feeder_load(printing=True)
        self.assertEqual(self.extruder.printing_e_pos, -5.0)
        self.assertIn('limited to 5.0 mm', '\n'.join(logs.output))

    def test_slicer_retract_left_alone(self):
        self.endpoint(235.0).native_feeder_load(printing=True)
        self.assertEqual(self.extruder.printing_e_pos, -0.8)

    def test_cold_head_uses_preextrude_fallback(self):
        with self.assertLogs(level='WARNING'):
            self.endpoint(0.0).native_feeder_load(printing=True)
        self.assertIn('SET_HEATER_TEMPERATURE HEATER=extruder3 TARGET=220.0', self.scripts)

    def test_not_printing_leaves_head_alone(self):
        self.endpoint(235.0).native_feeder_load(printing=False)
        self.assertEqual(self.scripts, ['FEED_AUTO MODULE=right CHANNEL=1 LOAD=1 PRINTING=0'])

    def test_failed_load_does_not_restore(self):
        endpoint = self.endpoint(235.0, feeder_state='load_fail')
        with self.assertRaises(E.EndpointError):
            endpoint.native_feeder_load(printing=True)
        self.assertFalse(any(s.startswith('MOVE_TO_XY_IDLE') for s in self.scripts))


def manager_stub():
    manager = object.__new__(M.BMCUManager)
    manager._u1_ownership_self_job = None
    manager._u1_background_jobs = {}
    manager._u1_background_channel_locks = {}
    manager._u1_disconnect_hazards = {}
    manager._u1_devices_reconciled_once = set()
    return manager


class ReleaseRefusalTest(unittest.TestCase):
    def setUp(self):
        self.manager = manager_stub()
        self.endpoint = types.SimpleNamespace(
            name='u1_head3', driver='snapmaker_u1',
            native_path_status=lambda: {'known': True, 'busy': False})
        self.manager._u1_ownership_record = lambda name: {'persistent_hold': True}
        self.manager._endpoint_has_loaded_or_active_route = lambda name: False
        self.manager._u1_endpoint_ownership_evidence = lambda name: []
        self.manager._refresh_u1_disconnect_hazards = lambda name: None
        self.manager._assigned_routes_for_endpoint = lambda name: []
        self.manager._handoff_u1_to_native = mock.Mock(return_value=True)

    def test_releases_when_safe(self):
        self.assertTrue(self.manager._release_u1_persistent_hold_if_safe(self.endpoint, 'test'))
        self.manager._handoff_u1_to_native.assert_called_once()

    def test_nothing_held_is_not_logged(self):
        self.manager._u1_ownership_record = lambda name: {}
        with mock.patch.object(M.logging, 'info') as info:
            self.assertFalse(self.manager._release_u1_persistent_hold_if_safe(self.endpoint))
        info.assert_not_called()

    def refusal(self):
        with self.assertLogs(level='INFO') as logs:
            self.assertFalse(self.manager._release_u1_persistent_hold_if_safe(self.endpoint, 'test'))
        self.manager._handoff_u1_to_native.assert_not_called()
        return logs.output[-1]

    def test_logs_active_route(self):
        self.manager._endpoint_has_loaded_or_active_route = lambda name: True
        device = types.SimpleNamespace(name='bmcu0')
        self.manager._u1_endpoint_ownership_evidence = lambda name: [
            {'device': device, 'channel': 2, 'inflight': True, 'durable_loaded': False,
             'raw_state': protocol.ROUTE_EMPTY}]
        self.assertIn('bmcu0 Channel 3 in flight', self.refusal())

    def test_logs_unverified_module(self):
        self.manager._assigned_routes_for_endpoint = lambda name: [
            (types.SimpleNamespace(name='bmcu0'), 2)]
        self.assertIn('not verified since start: bmcu0', self.refusal())

    def test_logs_occupied_stock_path(self):
        self.endpoint.native_path_status = lambda: {
            'known': True, 'busy': True, 'channel_state': 'unload_finish',
            'feeder_input_detected': True}
        self.assertIn('stock path occupied (feeder state unload_finish, input detected)',
                      self.refusal())

    def test_ignore_job_is_scoped_to_the_call(self):
        seen = []
        self.manager._endpoint_has_loaded_or_active_route = (
            lambda name: seen.append(self.manager._u1_ownership_self_job) or False)
        job = object()
        self.manager._release_u1_persistent_hold_if_safe(self.endpoint, ignore_job=job)
        self.assertEqual(seen, [job])
        self.assertIsNone(self.manager._u1_ownership_self_job)


class BackgroundSelfLockTest(unittest.TestCase):
    """A background job's own channel lock isn't in-flight work while it releases."""
    def setUp(self):
        self.manager = manager_stub()
        self.device = types.SimpleNamespace(name='bmcu0')
        manager = self.manager
        manager._route_key = lambda device, channel: '%s:%d' % (device.name, channel)
        manager._channel_endpoint_name = lambda device, channel: 'u1_head3'
        manager._route_state = lambda device, channel: protocol.ROUTE_EMPTY
        manager._journal_route_aliases = lambda device, channel: set()
        manager.print_loaded_routes = set()
        manager.loaded_tools = {}
        manager.prestaged = {}
        manager.active_operations = {}
        manager.state = types.SimpleNamespace(data={'u1_ownership': {}})
        manager._device_uid = lambda device: ''
        self.job = {'state': 'running'}
        manager._u1_background_channel_locks[('bmcu0', 2)] = self.job

    def test_lock_counts_as_inflight_normally(self):
        evidence = self.manager._u1_route_ownership_evidence(self.device, 2)
        self.assertTrue(evidence['inflight'])

    def test_own_lock_ignored_for_the_releasing_job(self):
        self.manager._u1_ownership_self_job = self.job
        evidence = self.manager._u1_route_ownership_evidence(self.device, 2)
        self.assertFalse(evidence['inflight'])

    def test_other_jobs_lock_still_counts(self):
        self.manager._u1_ownership_self_job = {'state': 'other'}
        evidence = self.manager._u1_route_ownership_evidence(self.device, 2)
        self.assertTrue(evidence['inflight'])


class StockPrintTest(unittest.TestCase):
    """A print with no BMCU plan that uses a Head holding BMCU filament: unload it
    and hand the Head back to its stock feeder before Snapmaker's own load."""
    def setUp(self):
        manager = self.manager = manager_stub()
        self.endpoint = types.SimpleNamespace(
            name='u1_head3', get=lambda key, default=None: 3 if key == 'head_index' else default,
            prepare_native_stock_load=mock.Mock())
        self.device = types.SimpleNamespace(name='bmcu0')
        manager.print_map_active = False
        manager.controller_mode = 'standalone'
        manager._u1_stock_feed_inner = False
        manager._u1_task_config = lambda: (object(), {'extruders_used': [True, False, False, True]})
        manager._u1_endpoint_for_head = lambda head: self.endpoint if head == 3 else None
        manager._assigned_routes_for_endpoint = lambda name: [(self.device, 2)]
        self.loaded = [(self.device, 2)]
        manager._loaded_devices_for_endpoint = lambda name: list(self.loaded)
        manager._snapmaker_ungripped_tail = lambda device, channel, endpoint: False
        manager._lock = mock.Mock(); manager._unlock = mock.Mock()
        manager._validate_endpoint_for_operation = mock.Mock()
        manager._u1_active_temperature_profile = lambda: None
        manager._unload_locked = mock.Mock(side_effect=lambda *a, **k: self.loaded.clear())
        self.owner = 'bmcu'
        manager._u1_owner_status = lambda endpoint: {'owner': self.owner}
        manager._release_u1_persistent_hold_if_safe = mock.Mock(return_value=True)
        manager._record_error = mock.Mock()
        self.original = mock.Mock(return_value='stock-load')
        manager._u1_stock_feed_original = self.original
        self.gcmd = mock.Mock()
        self.gcmd.get_int = lambda name, default=None, **kw: 3
        self.gcmd.error = lambda message: RuntimeError(message)

    def test_unloads_bmcu_then_runs_stock_load(self):
        self.assertEqual(self.manager._cmd_u1_stock_feed_wrapper(self.gcmd), 'stock-load')
        self.manager._unload_locked.assert_called_once()
        kwargs = self.manager._release_u1_persistent_hold_if_safe.call_args[1]
        self.assertTrue(kwargs['allow_stock_path'])
        self.original.assert_called_once_with(self.gcmd)
        self.endpoint.prepare_native_stock_load.assert_called_once_with(printing=True)
        said = ' '.join(c[0][0] for c in self.gcmd.respond_info.call_args_list)
        self.assertIn('unloading bmcu0 Channel 3 from Head 4', said)
        self.assertIn('back on its stock feeder', said)
        self.assertFalse(self.manager._u1_stock_feed_inner)

    def test_empty_head_only_released(self):
        self.loaded.clear()
        self.manager._cmd_u1_stock_feed_wrapper(self.gcmd)
        self.manager._unload_locked.assert_not_called()
        self.manager._release_u1_persistent_hold_if_safe.assert_called_once()
        self.original.assert_called_once()

    def test_already_stock_owned_head_untouched(self):
        self.loaded.clear(); self.owner = 'native'
        self.manager._cmd_u1_stock_feed_wrapper(self.gcmd)
        self.manager._release_u1_persistent_hold_if_safe.assert_not_called()
        self.original.assert_called_once()

    def test_failure_pauses_instead_of_printing(self):
        self.manager._release_u1_persistent_hold_if_safe.return_value = False
        with mock.patch.object(M.BMCUManager, '_command_pause_error',
                               staticmethod(lambda gcmd, exc: RuntimeError('paused: %s' % exc))):
            with self.assertRaisesRegex(RuntimeError, "paused: .*couldn't be handed back"):
                self.manager._cmd_u1_stock_feed_wrapper(self.gcmd)
        self.original.assert_not_called()

    def test_passes_through_when_not_a_stock_print(self):
        cases = [('print_map_active', True), ('controller_mode', 'klipper'),
                 ('_u1_stock_feed_inner', True)]
        for attr, value in cases:
            self.setUp(); setattr(self.manager, attr, value)
            self.manager._print_state = lambda: 'standby'   # not a resume
            self.manager._cmd_u1_stock_feed_wrapper(self.gcmd)
            self.manager._unload_locked.assert_not_called()
            self.original.assert_called_once()
        self.setUp()
        self.manager._u1_task_config = lambda: (object(), {'extruders_used': [True, False, False, False]})
        self.manager._cmd_u1_stock_feed_wrapper(self.gcmd)
        self.manager._unload_locked.assert_not_called()
        self.setUp()
        self.manager._assigned_routes_for_endpoint = lambda name: []
        self.manager._cmd_u1_stock_feed_wrapper(self.gcmd)
        self.manager._unload_locked.assert_not_called()

    def test_stock_path_allowed_only_when_asked(self):
        manager = manager_stub()
        manager._endpoint_has_loaded_or_active_route = lambda name: False
        manager._refresh_u1_disconnect_hazards = lambda name: None
        manager._assigned_routes_for_endpoint = lambda name: []
        endpoint = types.SimpleNamespace(name='u1_head3', native_path_status=lambda: {
            'known': True, 'busy': True, 'channel_state': 'preload_finish', 'feeder_input_detected': True})
        self.assertIn('stock path occupied', manager._u1_release_refusal(endpoint))
        self.assertEqual(manager._u1_release_refusal(endpoint, allow_stock_path=True), '')


class SocketDirMessageTest(unittest.TestCase):
    def test_tmp_with_wrong_mode_gives_fix(self):
        import bmcu_host_bootstrap as bootstrap
        fake = os.stat_result((0o40700, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        with mock.patch.object(bootstrap.os, 'stat', return_value=fake):
            message = bootstrap._socket_dir_access_message('/tmp/bmcu-transport', 'lava')
        self.assertIn('/tmp is mode 0700', message)
        self.assertIn('chmod 1777 /tmp', message)

    def test_correct_tmp_suggests_removing_stale_dir(self):
        import bmcu_host_bootstrap as bootstrap
        fake = os.stat_result((0o41777, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        with mock.patch.object(bootstrap.os, 'stat', return_value=fake), \
                mock.patch.object(bootstrap.os.path, 'lexists', return_value=True):
            message = bootstrap._socket_dir_access_message('/tmp/bmcu-transport', 'lava')
        self.assertNotIn('chmod', message)
        self.assertIn('rm -rf /tmp/bmcu-transport', message)



class NativeStateChangeTest(unittest.TestCase):
    """A stock unload/preload marks the Head's lease stale so the panel updates."""
    def setUp(self):
        self.manager = manager_stub()
        self.feeder = {'channel_state': 'load_finish', 'filament_detected': True}
        endpoint = types.SimpleNamespace(
            name='u1_head3', driver='snapmaker_u1',
            _native_feeder_status=lambda strict=False: dict(self.feeder))
        self.manager.endpoints = {'u1_head3': endpoint}
        self.manager._u1_lease_state = {'u1_head3': {
            'owner': 'native_busy', 'path': {
                'known': True, 'channel_state': 'load_finish',
                'feeder_input_detected': True}}}

    def test_unchanged(self):
        self.assertFalse(self.manager._u1_native_state_changed())

    def test_preload_after_stock_print(self):
        self.feeder['channel_state'] = 'preload_finish'
        self.assertTrue(self.manager._u1_native_state_changed())

    def test_input_pulled(self):
        self.feeder['filament_detected'] = False
        self.assertTrue(self.manager._u1_native_state_changed())


class PreloadLengthTest(unittest.TestCase):
    """BMCU_SET_PRELOAD sets Snapmaker's preload stop per feeder module."""
    CIRCUMFERENCE = 31.4159

    def setUp(self):
        self.feeders = {
            'left': types.SimpleNamespace(_feed_preload_counts=int(950 / self.CIRCUMFERENCE * 2)),
            'right': types.SimpleNamespace(_feed_preload_counts=int(950 / self.CIRCUMFERENCE * 2))}
        self.manager = manager_stub()
        self.manager.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None: self.feeders.get(name.split()[-1], default)
            if name.startswith('filament_feed ') else default)
        self.manager.endpoints = {}
        self.manager.state = types.SimpleNamespace(data={'endpoints': {}}, save=lambda: None)
        self.manager._sync_status_cache_runtime = lambda: None
        for head in range(4):
            endpoint = object.__new__(E.SnapmakerU1Endpoint)
            endpoint.name = 'u1_head%d' % head
            endpoint.driver = 'snapmaker_u1'
            endpoint.config = {}
            endpoint.get = (lambda ep: lambda key, default=None: ep.config.get(key, default))(endpoint)
            endpoint._head = (lambda h: lambda: h)(head)
            endpoint.printer = self.manager.printer
            self.manager.endpoints[endpoint.name] = endpoint
            self.manager.state.data['endpoints'][endpoint.name] = {}

    def mm(self, side):
        return self.feeders[side]._feed_preload_counts * self.CIRCUMFERENCE / 2.0

    def gcmd(self, **params):
        replies = []
        def get_float(name, default=None, minval=None, maxval=None):
            value = float(params.get(name, default))
            if (minval is not None and value < minval) or (maxval is not None and value > maxval):
                raise RuntimeError('out of range')
            return value
        return replies, types.SimpleNamespace(
            get=lambda name, default=None: params.get(name, default),
            get_int=lambda name, default=0, minval=None, maxval=None: int(params.get(name, default)),
            get_float=get_float, error=RuntimeError, respond_info=replies.append)

    def test_default_is_850_on_both_sides(self):
        with self.assertLogs(level='INFO'):
            self.manager._apply_u1_preload_lengths()
        self.assertAlmostEqual(self.mm('left'), 850, delta=16)
        self.assertAlmostEqual(self.mm('right'), 850, delta=16)

    def test_set_one_side(self):
        replies, gcmd = self.gcmd(SIDE='right', LENGTH=800)
        self.manager.cmd_SET_PRELOAD(gcmd)
        self.assertAlmostEqual(self.mm('right'), 800, delta=16)
        self.assertEqual(self.manager.state.data['endpoints']['u1_head3']['u1_preload_length'], 800.0)
        self.assertEqual(self.manager.state.data['endpoints']['u1_head2']['u1_preload_length'], 800.0)
        self.assertNotIn('u1_preload_length', self.manager.state.data['endpoints']['u1_head0'])
        self.assertIn('Heads 3 and 4', replies[0])

    def test_head_picks_side_and_zero_restores_printer_cfg(self):
        self.manager._apply_u1_preload_lengths()
        replies, gcmd = self.gcmd(HEAD=1, LENGTH=0)
        self.manager.cmd_SET_PRELOAD(gcmd)
        self.assertAlmostEqual(self.mm('left'), 950, delta=16)
        self.assertAlmostEqual(self.mm('right'), 850, delta=16)
        self.assertIn('printer.cfg', replies[0])

    def test_rejects_below_snapmaker_minimum(self):
        _replies, gcmd = self.gcmd(SIDE='left', LENGTH=400)
        with self.assertRaises(RuntimeError):
            self.manager.cmd_SET_PRELOAD(gcmd)

    def test_firmware_without_counts_left_alone(self):
        self.feeders['left'] = types.SimpleNamespace()
        with self.assertLogs(level='WARNING'):
            self.manager._apply_u1_preload_lengths()
        self.assertFalse(hasattr(self.feeders['left'], '_feed_preload_counts'))


class PlanNativeOverBmcuTest(unittest.TestCase):
    """A print may plan a Head's stock feeder while BMCU filament is in it."""
    def check(self, owner='bmcu', state='preload_finish', input_detected=True,
              preload_clear=True, hazard=False, loaded=1):
        manager = manager_stub()
        endpoint = u1_endpoint({'u1_preload_clear': preload_clear}, state, input_detected)
        endpoint.name = 'u1_head3'
        manager._refresh_u1_disconnect_hazards = lambda name=None: None
        manager._u1_disconnect_hazards = {'u1_head3': ['x']} if hazard else {}
        manager._loaded_devices_for_endpoint = lambda name: [('bmcu0', 2)] * loaded
        return manager._u1_native_tool_can_take_over(endpoint, {'owner': owner})

    def test_bmcu_loaded_stock_preloaded_clear(self):
        self.assertTrue(self.check())

    def test_refusal_reasons(self):
        manager = manager_stub()
        manager._refresh_u1_disconnect_hazards = lambda name=None: None
        manager._u1_disconnect_hazards = {}
        manager._loaded_devices_for_endpoint = lambda name: [('bmcu0', 2)]
        endpoint = u1_endpoint({'u1_preload_clear': False}, 'preload_finish', True)
        endpoint.name = 'u1_head3'
        self.assertIn('SNAP_PRELOAD_CLEAR is off',
                      manager._u1_native_take_over_refusal(endpoint, {'owner': 'bmcu'}))
        endpoint = u1_endpoint({'u1_preload_clear': True}, 'preload_finish', False)
        endpoint.name = 'u1_head3'
        self.assertIn('no filament is detected',
                      manager._u1_native_take_over_refusal(endpoint, {'owner': 'bmcu'}))
        endpoint = u1_endpoint({'u1_preload_clear': True}, 'preload_finish', True)
        endpoint.name = 'u1_head3'
        self.assertIsNone(manager._u1_native_take_over_refusal(endpoint, {'owner': 'bmcu'}))

    def test_refused_cases(self):
        self.assertFalse(self.check(owner='bmcu_busy'))
        self.assertFalse(self.check(owner='safety_hold'))
        self.assertFalse(self.check(preload_clear=False))      # preload occupies the path
        self.assertFalse(self.check(state='unload_finish'))    # stock still in the path
        self.assertFalse(self.check(input_detected=False))     # nothing to load
        self.assertFalse(self.check(hazard=True))
        self.assertFalse(self.check(loaded=2))


class ChannelAutoloadLengthTest(unittest.TestCase):
    """The host sends autoload lengths up to 2 m to the BMCU."""
    def test_runtime_lengths(self):
        lengths = M.BMCUManager._channel_autoload_lengths_m({'channels': {
            '0': {'autoload_mm': 2000.0}, '1': {'autoload_mm': 2500.0},
            '2': {'autoload_mm': 1200.0}, '3': {}}})
        self.assertEqual(lengths, [2.0, 2.0, 1.2, 0.12])


class IdentifyHeadFilamentTest(unittest.TestCase):
    """Print start: filament BMCU has no record of is identified by which Channel's
    encoder follows a short extrusion, then recorded as loaded."""
    def setUp(self):
        self.manager = manager_stub()
        self.device = types.SimpleNamespace(name='bmcu0', ready=True)
        self.meters = [0.5, 0.8, 0.0, 0.0]
        self.follower = 0
        self.device.refresh = lambda: {'meters': list(self.meters)}
        self.endpoint = types.SimpleNamespace(name='u1_head2', driver='snapmaker_u1')
        self.manager._assigned_routes_for_endpoint = lambda name: [(self.device, 0), (self.device, 1)]
        self.manager._first_u1_route_for_endpoint = lambda name: {'tool': 4}
        self.manager._virtual_tool_for_source = lambda device, channel: 4 + channel
        self.manager._u1_temperature_profile = lambda plan_index=None, tool=None: {'temperature': 215.0}
        self.scripts = []
        def run(script):
            self.scripts.append(script)
            if 'G1 E' in script and self.follower is not None:
                self.meters[self.follower] += 0.0148      # 14.8 mm pulled through
        self.manager.gcode = types.SimpleNamespace(run_script_from_command=run)
        self.manager.reactor = _support.FakeReactor()
        self.manager._adopt_identified_u1_route = mock.Mock()

    def test_identifies_the_channel_that_follows(self):
        self.follower = 1
        with self.assertLogs(level='INFO') as logs:
            device, channel = self.manager._identify_u1_head_filament(self.endpoint, 2)
        self.assertEqual(channel, 1)
        self.manager._adopt_identified_u1_route.assert_called_once_with(
            self.device, 1, self.endpoint, 2)
        self.assertIn('T2 A0\nMOVE_TO_DISCARD_FILAMENT_POSITION\nM109 S215', self.scripts[0])
        self.assertIn('G1 E15.0 F180', self.scripts[1])
        self.assertIn('as bmcu0 Channel 2', '\n'.join(logs.output))

    def test_nothing_follows_means_not_a_bmcu_channel(self):
        self.follower = None
        with self.assertLogs(level='INFO'):
            with self.assertRaises(M.BMCUError) as ctx:
                self.manager._identify_u1_head_filament(self.endpoint, 2)
        self.assertIn("none of its BMCU Channels moved", str(ctx.exception))
        self.manager._adopt_identified_u1_route.assert_not_called()

    def test_ambiguous_result_is_refused(self):
        self.follower = 0
        def run(script):
            if 'G1 E' in script:
                self.meters[0] += 0.0148
                self.meters[1] += 0.004               # 4 mm: neither still nor following
        self.manager.gcode = types.SimpleNamespace(run_script_from_command=run)
        with self.assertLogs(level='INFO'):
            with self.assertRaises(M.BMCUError) as ctx:
                self.manager._identify_u1_head_filament(self.endpoint, 2)
        self.assertIn("didn't single out one Channel", str(ctx.exception))

    def test_unaccounted_filament_detection(self):
        manager = manager_stub()
        state = {'loaded': [], 'detected': True, 'channel_state': 'wait_insert'}
        manager._loaded_devices_for_endpoint = lambda name: state['loaded']
        endpoint = types.SimpleNamespace(
            name='u1_head2',
            require_entry_sensor_snapshot=lambda timeout=1.25: {'physical_detected': state['detected']},
            native_path_status=lambda: {'channel_state': state['channel_state']})
        self.assertTrue(manager._u1_unaccounted_head_filament(endpoint))
        state['detected'] = False
        self.assertFalse(manager._u1_unaccounted_head_filament(endpoint))
        state.update(detected=True, loaded=[('bmcu0', 0)])
        self.assertFalse(manager._u1_unaccounted_head_filament(endpoint))    # BMCU route loaded
        state.update(loaded=[], channel_state='load_finish')
        self.assertFalse(manager._u1_unaccounted_head_filament(endpoint))    # stock filament


class AdoptIdentifiedRouteTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        m = manager_stub()
        self.route = {'state': M.protocol.ROUTE_LOADED}
        self.device = types.SimpleNamespace(
            name='bmcu0',
            refresh=lambda: {'calibration_valid_mask': 0b11, 'route_state': [self.route['state']] * 4},
            mark_loaded=lambda ch: self.calls.append(('mark_loaded', ch)))
        m._route_key = lambda device, channel: (device.name, channel)
        m._lock = lambda *a, **k: self.calls.append('lock')
        m._unlock = lambda *a, **k: self.calls.append('unlock')
        m._require_exclusive_endpoint_route = lambda *a: self.calls.append('exclusive')
        m._route_states_from_status = lambda status: status['route_state']
        m._arm_u1_persistent_hold = lambda *a, **k: self.calls.append('hold')
        m._commit_u1_route_ownership = lambda *a: self.calls.append('commit')
        m._uncertain_routes = set()
        m.device_status_changed = lambda *a: self.calls.append('status')
        m._virtual_tool_for_source = lambda device, channel: 5
        m.loaded_tools = {}
        m._save_runtime = lambda: self.calls.append('save')
        self.manager = m

    def test_records_loaded_route(self):
        self.manager._adopt_identified_u1_route(self.device, 1, object(), 2)
        # the hold (and its stock-feeder baseline) comes before the route reads LOADED
        self.assertEqual(self.calls, ['lock', 'exclusive', 'hold', ('mark_loaded', 1),
                                      'commit', 'status', 'save', 'unlock'])
        self.assertEqual(self.manager.loaded_tools, {('bmcu0', 1): 5})

    def test_firmware_refusal_leaves_route_uncertain(self):
        self.route['state'] = M.protocol.ROUTE_EMPTY
        with self.assertRaises(M.BMCUError):
            self.manager._adopt_identified_u1_route(self.device, 1, object(), 2)
        self.assertIn(('bmcu0', 1), self.manager._uncertain_routes)
        self.assertNotIn('commit', self.calls)
        self.assertEqual(self.calls[-1], 'unlock')


class ResumeAfterFailedLoadTest(unittest.TestCase):
    """A Head whose BMCU load failed can be resumed: Snapmaker's resume checks see the
    filament it is about to get, and the retry identifies leftover filament instead
    of refusing it."""
    def setUp(self):
        m = manager_stub()
        self.device = types.SimpleNamespace(name='bmcu0', status={'present': [1, 1, 1, 1]})
        self.endpoint = types.SimpleNamespace(name='u1_head2', driver='snapmaker_u1',
                                              sync_active_filament=mock.Mock())
        m.print_map_active = True
        m._snapmaker_platform = lambda: True
        macro = types.SimpleNamespace(variables={'last_extruder_index': 2})
        m.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None: macro if name == 'gcode_macro INNER_RESUME' else default)
        m._u1_endpoint_for_head = lambda head: self.endpoint if head == 2 else None
        m._assigned_routes_for_endpoint = lambda name: [(self.device, 0), (self.device, 1)]
        self.loaded = []
        m._loaded_devices_for_endpoint = lambda name, excluded_route=None: list(self.loaded)
        m._u1_toolchange_cursor = 3
        m._u1_toolchange_plan = [{'tool': 6}, {'tool': 5}, {'tool': 6}, {'tool': 4}]
        m._first_u1_route_for_endpoint = lambda name: None
        m._u1_logical_route = lambda tool: {'kind': 'bmcu', 'endpoint': self.endpoint} if tool in (4, 5) else None
        m._resolve_tool = lambda tool: (self.device, tool - 4, None)
        m._channel_metadata = lambda device, channel: {'material': 'PLA', 'channel': channel}
        self.manager = m

    def test_failed_tool_change_head_gets_its_filament_info(self):
        with self.assertLogs(level='INFO'):
            self.assertTrue(self.manager._prepare_u1_resume_projection())
        self.endpoint.sync_active_filament.assert_called_once_with({'material': 'PLA', 'channel': 0})

    def test_loaded_route_wins(self):
        self.loaded = [(self.device, 1)]
        with self.assertLogs(level='INFO'):
            self.manager._prepare_u1_resume_projection()
        self.endpoint.sync_active_filament.assert_called_once_with({'material': 'PLA', 'channel': 1})

    def test_real_runout_is_left_alone(self):
        self.device.status['present'] = [0, 1, 1, 1]
        with self.assertLogs(level='INFO'):
            self.assertFalse(self.manager._prepare_u1_resume_projection())
        self.endpoint.sync_active_filament.assert_not_called()

    def test_not_a_bmcu_print_or_head(self):
        self.manager.print_map_active = False
        self.assertFalse(self.manager._prepare_u1_resume_projection())
        self.manager.print_map_active = True
        self.manager._u1_toolchange_plan = [{'tool': 2}]
        self.manager._u1_toolchange_cursor = 0
        self.assertFalse(self.manager._prepare_u1_resume_projection())     # stock tool next
        self.endpoint.sync_active_filament.assert_not_called()

    def test_identify_before_load_only_when_safe(self):
        m = manager_stub()
        state = {'print': 'paused', 'homed': 'xyz', 'unaccounted': True}
        m._print_state = lambda: state['print']
        m.prestaged = {}
        m.reactor = _support.FakeReactor()
        toolhead = types.SimpleNamespace(get_status=lambda now: {'homed_axes': state['homed']})
        m.printer = types.SimpleNamespace(lookup_object=lambda name, default=None: toolhead)
        m._u1_unaccounted_head_filament = lambda endpoint: state['unaccounted']
        ep = types.SimpleNamespace(name='u1_head2', driver='snapmaker_u1')
        self.assertTrue(m._u1_should_identify_before_load(ep))
        self.assertFalse(m._u1_should_identify_before_load(ep, prefetched=True))
        m.prestaged = {('bmcu0', 0): {'endpoint': 'u1_head2'}}        # BMCU's own staged filament
        self.assertFalse(m._u1_should_identify_before_load(ep))
        m.prestaged = {}
        state['print'] = 'standby'
        self.assertFalse(m._u1_should_identify_before_load(ep))
        state.update(print='printing', homed='z')
        self.assertFalse(m._u1_should_identify_before_load(ep))
        state.update(homed='xyz', unaccounted=False)
        self.assertFalse(m._u1_should_identify_before_load(ep))


class PendingRecoveryTest(unittest.TestCase):
    """A T4 recovery armed when Head 3's load failed at print start must not block a
    tool change on another Head, and must run when the print next needs Head 3."""
    def setUp(self):
        m = manager_stub()
        self.h3 = types.SimpleNamespace(name='u1_head2')
        self.h4 = types.SimpleNamespace(name='u1_head3')
        m._u1_toolchange_resume_pending = {'kind': 'before_on_use', 'tool': 4,
                                           'endpoint': 'u1_head2', 'phase': 'verify'}
        m._u1_logical_route = lambda tool: {'endpoint': self.h3 if tool in (4, 5) else self.h4}
        m._u1_temperature_profile = lambda plan_index=None, tool=None: {'temperature': 220.0}
        m._resume_u1_toolchange_before_on_use = mock.Mock(return_value=True)
        m.print_map_active = True
        m._u1_stock_feed_inner = False
        m._u1_endpoint_for_head = lambda head: {2: self.h3, 3: self.h4}.get(head)
        self.manager = m

    def test_which_tool_changes_it_affects(self):
        m = self.manager
        self.assertTrue(m._u1_pending_recovery_is_for(4))
        self.assertFalse(m._u1_pending_recovery_is_for(6))
        self.assertTrue(m._u1_pending_recovery_shares_head(5))      # T5 also feeds Head 3
        self.assertFalse(m._u1_pending_recovery_shares_head(6))     # T6 is Head 4: not blocked

    def test_runs_for_its_own_tool(self):
        with self.assertLogs(level='INFO'):
            self.assertTrue(self.manager._run_pending_u1_recovery())
        self.manager._resume_u1_toolchange_before_on_use.assert_called_once_with(
            4, -1, {'temperature': 220.0}, gcmd=None)

    def test_resume_reload_of_that_head_runs_it(self):
        gcmd = types.SimpleNamespace(get_int=lambda name, default=None: 2)
        with self.assertLogs(level='INFO'):
            self.assertTrue(self.manager._u1_resume_feed_runs_pending_recovery(gcmd))
        self.manager._resume_u1_toolchange_before_on_use.assert_called_once()

    def test_resume_reload_of_another_head_leaves_it(self):
        gcmd = types.SimpleNamespace(get_int=lambda name, default=None: 3)
        self.assertFalse(self.manager._u1_resume_feed_runs_pending_recovery(gcmd))
        self.manager._resume_u1_toolchange_before_on_use.assert_not_called()

    def test_nothing_pending(self):
        self.manager._u1_toolchange_resume_pending = {}
        gcmd = types.SimpleNamespace(get_int=lambda name, default=None: 2)
        self.assertFalse(self.manager._u1_resume_feed_runs_pending_recovery(gcmd))
        self.assertFalse(self.manager._u1_pending_recovery_shares_head(5))


class StaleRecoveryTest(unittest.TestCase):
    """A recovery armed for an UNCERTAIN route whose load was then completed by
    another path is dropped, so the tool change carries on."""
    def test_dropped_when_route_already_loaded(self):
        m = manager_stub()
        m._u1_toolchange_resume_pending = {'kind': 'before_on_use', 'tool': 4, 'device': 'bmcu0',
                                           'channel': 0, 'endpoint': 'u1_head2', 'phase': 'verify',
                                           'route_state': M.protocol.ROUTE_UNCERTAIN}
        m.active_operations = {}
        m._critical_motion_active = False
        m._critical_motion_depth = 0
        device = types.SimpleNamespace(name='bmcu0', status={}, refresh=lambda: None)
        finish = mock.Mock()
        endpoint = types.SimpleNamespace(name='u1_head2', finish_load_temperature=finish)
        m._resolve_tool = lambda tool: (device, 0, endpoint)
        m._route_states_from_status = lambda status: [M.protocol.ROUTE_LOADED] * 4
        calls = []
        m._lock = lambda *a, **k: calls.append('lock')
        m._unlock = lambda *a, **k: calls.append('unlock')
        with self.assertLogs(level='INFO') as logs:
            self.assertFalse(m._resume_u1_toolchange_before_on_use(4, -1, None))
        self.assertEqual(m._u1_toolchange_resume_pending, {})
        self.assertEqual(calls, ['lock', 'unlock'])
        finish.assert_not_called()
        self.assertIn('already LOADED', '\n'.join(logs.output))


class StockPrintFlushCapTest(unittest.TestCase):
    """F1: a stock-profile print that first unloads BMCU filament from the Head must
    not let the flush pull the new stock filament back by the unload length."""
    def test_cap_applied_at_print_start(self):
        endpoint = u1_endpoint({}, 'preload_finish')
        endpoint._clear_stale_native_head_presence = mock.Mock()
        endpoint._limit_native_flush_retract = mock.Mock()
        endpoint.prepare_native_stock_load(printing=True)
        endpoint._limit_native_flush_retract.assert_called_once_with()
        endpoint._clear_stale_native_head_presence.assert_called_once()

    def test_not_outside_a_print(self):
        endpoint = u1_endpoint({}, 'preload_finish')
        endpoint._limit_native_flush_retract = mock.Mock()
        endpoint.prepare_native_stock_load(printing=False)
        endpoint._limit_native_flush_retract.assert_not_called()


class BackgroundHandbackLocksTest(unittest.TestCase):
    """F4: a background job handing back its own Head isn't refused by the locks it
    holds itself; anyone else's lock still refuses."""
    def test_own_locks_recognised(self):
        m = manager_stub()
        endpoint = types.SimpleNamespace(name='u1_head3')
        m._u1_ownership_self_job = {'locked': True, 'endpoint': endpoint}
        self.assertTrue(m._u1_self_job_holds_locks(endpoint))
        m._u1_ownership_self_job = {'locked': False, 'endpoint': endpoint}
        self.assertFalse(m._u1_self_job_holds_locks(endpoint))
        m._u1_ownership_self_job = {'locked': True, 'endpoint': types.SimpleNamespace(name='u1_head2')}
        self.assertFalse(m._u1_self_job_holds_locks(endpoint))
        m._u1_ownership_self_job = None
        self.assertFalse(m._u1_self_job_holds_locks(endpoint))


class ArrivalFailureHintTest(unittest.TestCase):
    """F2: a load that runs its full distance names what to check."""
    def test_shared_head_names_the_other_sources(self):
        m = manager_stub()
        dev = types.SimpleNamespace(name='bmcu0')
        m._assigned_routes_for_endpoint = lambda name: [(dev, 0), (dev, 1)]
        m._channel_record = lambda d, c: {'autoload_mm': 1600.0}
        hint = m._arrival_failure_hint(types.SimpleNamespace(name='u1_head2'), dev, 0)
        self.assertIn('bmcu0 Channel 2 (autoload 160 cm)', hint)
        self.assertIn('parked past the splitter', hint)
        self.assertTrue(hint.endswith('Clear it, then Resume'))

    def test_single_source_head(self):
        m = manager_stub()
        dev = types.SimpleNamespace(name='bmcu0')
        m._assigned_routes_for_endpoint = lambda name: [(dev, 2)]
        hint = m._arrival_failure_hint(types.SimpleNamespace(name='u1_head0'), dev, 2)
        self.assertNotIn('splitter', hint)
        self.assertIn('snag', hint)


class StockFeedFailureTest(unittest.TestCase):
    """2026-11-25: Head 4's stock load failed move_extrude on a deformed tip,
    after Snapmaker retried its extrude test for 5 minutes."""
    def setUp(self):
        self.m = manager_stub()
        self.m._u1_stock_feed_inner = False
        self.m._u1_prepared_heads = set()
        self.m.printer_critical_section = lambda name: mock.MagicMock()
        self.m._print_state = lambda: 'printing'
        self.m._save_print_session = lambda: None
        self.endpoint = types.SimpleNamespace(
            native_path_status=lambda: {'known': True, 'channel_state': 'load_finish'})

    def test_move_extrude_pause_says_what_to_do(self):
        raw = ('extruder[3]: state: load_extruding, error: move_extrude!'
               'raw msg:logic error!')
        gcmd = types.SimpleNamespace(error=lambda message, **kw: RuntimeError(message))
        message = str(M.BMCUManager._command_pause_error(gcmd, raw))
        self.assertTrue(message.startswith(raw + ' Head 4: the stock feeder pushed'))
        self.assertTrue(message.endswith('then Resume'))
        # Not added twice when the message passes through again.
        self.assertEqual(M._with_stock_feed_hint(message), message)

    def test_tool_change_failure_gets_the_hint_and_the_banner_too(self):
        m = manager_stub()
        m._sync_status_cache_runtime = lambda: None
        m._record_error('SNAPMAKER_TOOL_CHANGE_FAILED', phase='TOOL_CHANGE',
                        details='extruder[0]: state: load_extruding, error: move_extrude!')
        self.assertIn('Head 1: the stock feeder', m.last_error['details'])

    def test_other_errors_unchanged(self):
        for raw in ('extruder[1]: error: heat', 'ENDPOINT_NOT_REACHED: x. Clear it, then Resume'):
            self.assertEqual(M._with_stock_feed_hint(raw), raw)

    def test_resumed_load_counts_as_a_real_load(self):
        self.m.gcode = types.SimpleNamespace(run_script_from_command=mock.Mock(
            side_effect=[RuntimeError('move_extrude'), None, None]))
        with self.assertRaises(RuntimeError):
            self.m._run_u1_stock_auto_feed(self.endpoint, 3, 'test')
        # Resume: Snapmaker still reports load_finish before the rerun.
        self.assertTrue(self.m._run_u1_stock_auto_feed(self.endpoint, 3, 'test'))
        self.assertIn(3, self.m._u1_prepared_heads)
        # A later, genuinely already-loaded head is reported as such again.
        self.m._u1_prepared_heads.clear()
        self.assertFalse(self.m._run_u1_stock_auto_feed(self.endpoint, 3, 'test'))


class LinkLostHintTest(unittest.TestCase):
    """F7: a lost BMCU link pauses with what to check."""
    def test_offline_messages_say_what_to_do(self):
        for raw in ('DEVICE_OFFLINE during feed operation',
                    'bmcu0 serial port is not ready',
                    'U1 control plane did not resume after printer motion'):
            message = M._with_stock_feed_hint(raw)
            self.assertIn("Check the BMCU's USB cable", message)
            self.assertTrue(message.endswith('Then Resume'))
            self.assertEqual(M._with_stock_feed_hint(message), message)


class ResumeRouteTest(unittest.TestCase):
    """F9: Resume looks ahead to the Head's next source, and stops at a stock one."""
    def setUp(self):
        self.m = manager_stub()
        self.endpoint = types.SimpleNamespace(name='u1_head2')
        self.bmcu = object()
        self.m._loaded_devices_for_endpoint = lambda name: []
        self.m._first_u1_route_for_endpoint = lambda name: None
        self.m._resolve_tool = lambda tool: (self.bmcu, tool - 4, None)
        here = {'endpoint': self.endpoint}
        other = {'endpoint': types.SimpleNamespace(name='u1_head3')}
        self.routes = {4: dict(here, kind='bmcu'), 5: dict(here, kind='bmcu'),
                       6: dict(other, kind='bmcu'), 3: dict(here, kind='native')}
        self.m._u1_logical_route = lambda tool: self.routes.get(tool)

    def test_a_later_change_on_this_head_is_found(self):
        self.m._u1_toolchange_plan = [{'tool': 4}, {'tool': 6}, {'tool': 5}]
        self.m._u1_toolchange_cursor = 1
        self.assertEqual(self.m._u1_resume_route(self.endpoint), (self.bmcu, 1))

    def test_a_stock_source_next_leaves_the_resume_to_the_stock_feeder(self):
        self.m._u1_toolchange_plan = [{'tool': 4}, {'tool': 3}, {'tool': 5}]
        self.m._u1_toolchange_cursor = 1
        self.assertIsNone(self.m._u1_resume_route(self.endpoint))


if __name__ == '__main__':
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()

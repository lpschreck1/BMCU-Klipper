"""Assembles the simulated machine and runs scenarios.

``Sim.reference_machine()`` builds 4 heads, stock feeders, head sensors, the fake
Snapmaker modules and (optionally) the real ``BMCUManager`` wired to a fake BMCU.
"""
import os
import shutil
import sys
import tempfile

import _support  # noqa: F401  (puts klippy/extras on sys.path)

from . import feeder as F
from . import klipper as K
from . import snapmaker as S
from . import virtual_sdcard as V
from . import world as W
from .clock import SimReactor

NOOP_COMMANDS = (
    'PRINT_START', 'DEFECT_DETECTION_START', 'DEFECT_DETECT_NOODLE_FIRST',
    'DEFECT_DETECTION_DETECT_BED', 'DEFECT_DETECTION_DETECT', 'TIMELAPSE_START',
    'TIMELAPSE_TAKE_FRAME', 'DETECT_BED_PLATE', 'WAIT_CHAMBER_TEMP',
    'BED_MESH_CALIBRATE', 'PRINT_END', 'M141', 'M190', 'M140', 'M107', 'M106',
    'SM_PRINT_FLOW_CALIBRATE', 'G29', 'M73', 'SET_VELOCITY_LIMIT', 'M201', 'M203',
    'M205', 'M204', 'M220', 'M221', 'EXCLUDE_OBJECT_DEFINE', 'M117', 'M486',
    'SET_PRINT_STATS_INFO', 'SET_FAN_SPEED', 'FILAMENT_DT_CLEAR', 'M84')


def _sync_write_file(reactor, path, data, safe_write=True, timeout=10.0):
    """queuefile.sync_write_file, synchronous (Snapmaker's helper, no C module)."""
    with open(path, 'w') as stream:
        stream.write(data)


sys.modules['queuefile'].sync_write_file = _sync_write_file


class Stub(object):
    """An object that answers any attribute with a no-op, for modules BMCU only probes."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def get_status(self, eventtime=None):
        return {}


class Sim(object):
    def __init__(self, bmcu=True, state_dir=None):
        # bmcu: True = real manager + fake BMCU, 'manager' = manager with no BMCU, False = stock only
        self.reactor = SimReactor()
        self.world = W.World()
        self.reactor.add_stepper(self.world.step)
        self.tmp = state_dir or tempfile.mkdtemp(prefix='bmcu-sim-')
        self._own_tmp = state_dir is None
        self.printer = K.FakePrinter(self.reactor, self.tmp)
        self.gcode = self.printer.gcode
        self.print_stats = S.PrintStats(self)
        self.machine_state = S.MachineState()
        self.heaters = {}
        self.extruders = []
        self.sensors = {}
        self._build_world()
        self._build_printer()
        self.reactor.add_stepper(self._poll_sensors)
        self.bmcu = None
        self.manager = None
        self.booted = False
        if bmcu:
            from . import bmcu_host
            bmcu_host.attach(self, with_device=(bmcu is True))

    # -- construction --------------------------------------------------------
    @classmethod
    def reference_machine(cls, **kwargs):
        return cls(**kwargs)

    def _build_world(self):
        world = self.world
        for head in range(4):
            world.add_head(head)
            world.add_source(head, 'stock%d' % head, 'stock')
        # BMCU channels 0,1 -> head 2 (T4,T5); channels 2,3 -> head 3 (T6,T7)
        self.channel_head = {0: 2, 1: 2, 2: 3, 3: 3}
        for channel, head in self.channel_head.items():
            world.add_source(head, 'bmcu%d' % channel, 'bmcu')

    def _build_printer(self):
        p = self.printer
        heaters = K.FakeHeaters(p)
        p.add_object('heaters', heaters)
        for head in range(4):
            state = self.world.heads[head].heater
            heater = K.FakeHeater(self.reactor, state, state.name)
            heaters.heaters[state.name] = heater
            self.heaters[head] = heater
        self.extruders = [S.U1Extruder(self, i) for i in range(4)]
        for ext in self.extruders:
            p.add_object(ext.name, ext)
        self.toolhead = K.FakeToolhead(p, self.extruders)
        p.add_object('toolhead', self.toolhead)
        p.add_object('print_stats', self.print_stats)
        p.add_object('machine_state_manager', self.machine_state)
        self.print_task_config = S.PrintTaskConfig(self)
        p.add_object('print_task_config', self.print_task_config)
        p.add_object('filament_detect', S.FilamentDetect())
        p.add_object('filament_parameters', S.FilamentParameters())
        for head in range(4):
            sensor = F.MotionSensor(self, head)
            self.sensors[head] = sensor
            p.add_object('filament_motion_sensor e%d_filament' % head, sensor)
        self.feeders = {name: F.Feeder(self, name) for name in F.FEEDERS}
        self.gcode_move = S.GcodeMove(self)
        p.add_object('gcode_move', self.gcode_move)
        self.macros = S.SnapmakerMacros(self)
        for name in NOOP_COMMANDS:
            if name not in self.gcode.ready_gcode_handlers:
                self.gcode.register_command(name, lambda gcmd: None)
        self.sdcard = V.VirtualSDCard(self)
        p.add_object('virtual_sdcard', self.sdcard)
        p.add_object('idle_timeout', Stub())
        p.add_object('buttons', Stub())
        p.add_object('stepper_enable', Stub(lookup_enable=lambda name: Stub(
            is_motor_enabled=lambda: True)))
        p.objects['configfile'].settings = {
            'printer': {'max_physical_extruder_num': 4},
            'pause_resume': {'recover_velocity': 50.0},
            'idle_timeout': {'timeout': 600, 'timeout_on_pause': 1800}}

    def coil_for(self, head):
        return self.extruders[head].binding_probe.sensor

    def _poll_sensors(self, dt):
        now = self.reactor.monotonic()
        for sensor in self.sensors.values():
            sensor.poll(now)

    # -- the user -------------------------------------------------------------
    def insert_stock(self, head, **kwargs):
        """The user inserts stock filament at a head's feeder port."""
        self.world.insert('stock%d' % head, **kwargs)
        module = 'left' if head in F.FEEDERS['left'] else 'right'
        channel = F.FEEDERS[module].index(head)
        self.feeders[module].port_changed(channel)

    def setup_u1(self):
        """Preset, routes (channels 1-2 -> head 3, 3-4 -> head 4) and spools."""
        self.run_gcode('BMCU_APPLY_PRESET PRESET=snapmaker_u1')
        for channel, head in self.channel_head.items():
            self.run_gcode('BMCU_SET_OUTPUT DEVICE=bmcu0 CHANNEL=%d ENDPOINT=u1_head%d'
                           % (channel, head))

    def insert_bmcu(self, channel, **kwargs):
        """The user puts a spool on a BMCU channel."""
        self.world.insert('bmcu%d' % channel, **kwargs)
        if self.bmcu is not None:
            self.bmcu.fw.send_status(0)

    def run_gcode(self, script):
        self.gcode.run_script_from_command(script)

    def settle(self, seconds=5.0):
        self.reactor.pause(self.reactor.monotonic() + seconds)

    def boot(self, seconds=20.0):
        """klippy:ready, then let the BMCU connect and reconcile."""
        self.printer.send_event('klippy:ready')
        self.booted = True
        self.settle(seconds)

    def close(self):
        if self.bmcu is not None:
            self.bmcu.stop()
        if self._own_tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

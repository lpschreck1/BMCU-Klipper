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
        self.reactor.add_stepper(self._poll_phases)
        self.bmcu = None
        self.manager = None
        self.booted = False
        self.retired_dry = {}         # dry extrusion of machines thrown away by restart()
        self.pending_faults = []      # (trigger, fault)
        self.watchers = []
        self.active_faults = []
        self.fired = []               # (virtual time, fault name, trigger) log
        self.gcode.on_dispatch = self._on_dispatch
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
        for head in range(4):
            fan = Stub(speed=0.0)
            fan.get_status = lambda eventtime=None, f=fan: {'speed': f.speed}
            p.add_object('fan' if head == 0 else 'fan_generic e%d_fan' % head, fan)
            p.add_object('heater_fan e%d_nozzle_fan' % head, Stub(
                probe_speed=1.0, fan_speed=1.0, original_fan_speed=None, last_speed=0,
                current_stepped_index=0, fan_timer=None))
        p.add_object('idle_timeout', Stub())
        p.add_object('buttons', Stub())
        p.add_object('stepper_enable', Stub(lookup_enable=lambda name: Stub(
            is_motor_enabled=lambda: True)))
        p.objects['configfile'].settings = {
            'printer': {'max_physical_extruder_num': 4},
            'pause_resume': {'recover_velocity': 50.0},
            'idle_timeout': {'timeout': 600, 'timeout_on_pause': 1800}}

    def dry_mm(self):
        """Per head: mm of plastic the extruder was asked for that never left the nozzle."""
        return {i: round(e.print_commanded - e.print_flow, 3) + self.retired_dry.get(i, 0.0)
                for i, e in enumerate(self.extruders)}

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

    def setup_u1(self, early_park=False, preload_clear=True):
        """Preset, routes (channels 1-2 -> head 3, 3-4 -> head 4).

        # MODEL: the early-park tip tail (``BMCU_PARK_HEAD``) drives Klipper's C step
        # generator through chelper and is not simulated. Unless ``early_park`` the tip
        # profile is Snapmaker's own movements, which have no marker: tip forming runs
        # attached, then the head is parked for the long pullback (endpoints.py:3899).
        """
        from bmcu_core import endpoints as E
        self.run_gcode('BMCU_APPLY_PRESET PRESET=snapmaker_u1')
        if preload_clear:
            for head in (2, 3):     # heads that share a tube between stock and BMCU
                self.run_gcode('BMCU_SET_ENDPOINT NAME=u1_head%d SNAP_PRELOAD_CLEAR=1' % head)
        head = self.world.heads[2]
        full = (1500.0 + head.shared + head.sensor_to_gears + head.gears_to_nozzle + 60.0)
        for channel in range(4):
            self.run_gcode(
                'BMCU_SET_FILAMENT DEVICE=bmcu0 CHANNEL=%d UNLOAD_RETRACT_MM=%d '
                'AUTOLOAD_MM=1200' % (channel, min(2000, int(full))))
        if not early_park and self.manager is not None:
            profile = E.u1_tip_profile_defaults()
            profile['movements'] = E.u1_tip_snapmaker_movement_defaults()
            self.manager.state.data['u1_tip_profiles'] = {'default': profile}
        for channel, head in self.channel_head.items():
            self.run_gcode('BMCU_SET_OUTPUT DEVICE=bmcu0 CHANNEL=%d ENDPOINT=u1_head%d'
                           % (channel, head))

    COLORS = {'red': 'FF0000', 'black': '101010', 'white': 'FFFFFF', 'blue': '0000FF',
              'green': '00AA00', 'yellow': 'FFFF00'}

    def load_spools(self, spools):
        """``{tool: 'PLA red'}``: T0-T3 are stock spools in that head's feeder, T4-T7
        are BMCU channels 1-4. Sets the material and colour the plan will check."""
        for tool, text in sorted(spools.items()):
            material, _, color = text.partition(' ')
            if tool >= 4:
                channel = tool - 4
                self.insert_bmcu(channel)
                self.run_gcode(
                    'BMCU_SET_FILAMENT DEVICE=bmcu0 CHANNEL=%d MATERIAL=%s COLOR=%s' %
                    (channel, material, self.COLORS.get(color, 'FFFFFF')))
            else:
                self.insert_stock(tool)
                self.print_task_config.set_head(tool, ftype=material)
        self.settle(70.0)

    def insert_bmcu(self, channel, **kwargs):
        """The user puts a spool on a BMCU channel."""
        self.world.insert('bmcu%d' % channel, **kwargs)
        if self.bmcu is not None:
            self.bmcu.fw.send_status(0)

    # -- printing ---------------------------------------------------------------
    def slice(self, plan, materials=None, colors=None, **kwargs):
        """Synthesise a print. ``plan`` lists the tools per layer, e.g. ``[[0], [4, 6], [3]]``.
        Marks the stock heads it uses in Snapmaker's print task and loads the file."""
        from . import gcode_gen
        tools = sorted(set(t for layer in plan for t in layer))
        materials = dict(materials or {})
        for tool in tools:
            materials.setdefault(tool, 'PLA')
        text = gcode_gen.build(plan, materials, colors, **kwargs)
        path = os.path.join(self.tmp, 'print.gcode')
        with open(path, 'w') as stream:
            stream.write(text)
        self.sdcard.load(path, text)
        cfg = self.print_task_config.print_task_config
        for tool in tools:
            if tool < 4:
                cfg['extruders_used'][tool] = True
                cfg['reprint_info']['extruders_used'][tool] = True
        self.gcode_text = text
        return path

    def slice_stock(self, plan, **kwargs):
        """A print sliced with Snapmaker's own profile (no BMCU commands)."""
        from . import gcode_gen
        text = gcode_gen.build_stock(plan, **kwargs)
        path = os.path.join(self.tmp, 'stock.gcode')
        with open(path, 'w') as stream:
            stream.write(text)
        self.sdcard.load(path, text)
        cfg = self.print_task_config.print_task_config
        for tool in set(t for layer in plan for t in layer):
            cfg['extruders_used'][tool] = True
            cfg['reprint_info']['extruders_used'][tool] = True
        self.gcode_text = text
        return path

    def start_print(self):
        self.run_gcode('SDCARD_PRINT_FILE')

    def run_until_stopped(self, limit=7200.0):
        """Advance until the print completes, pauses or fails. Returns the state."""
        end = self.reactor.monotonic() + limit
        while self.reactor.monotonic() < end:
            self.reactor.pause(self.reactor.monotonic() + 1.0)
            if not self.sdcard.is_active() and self.print_stats.state != 'printing':
                break
        return self.print_stats.state

    def run_print(self):
        self.start_print()
        return self.run_until_stopped()

    def start(self):
        """Start the print without waiting for it."""
        self.start_print()

    # -- fault injection -----------------------------------------------------------
    def inject(self, at, fault):
        """Apply ``fault`` just before the G-code line that ``at`` (a trigger) matches."""
        self.pending_faults.append((at, fault))

    def watch(self, prefix, callback):
        """Call ``callback(sim, commandline)`` just before each line starting with ``prefix``."""
        self.watchers.append((prefix.upper(), callback))

    def _poll_phases(self, dt):
        """Fire faults whose trigger is a machine phase (faults.PhaseTrigger)."""
        for item in list(self.pending_faults):
            trigger, fault = item
            if getattr(trigger, 'state', False) and self.bmcu is not None and                     self.bmcu.with_device and trigger.poll(self):
                self.pending_faults.remove(item)
                trigger.fired = True
                self._fire(trigger, fault, type(trigger).__name__)

    def _fire(self, trigger, fault, where):
        fault.apply(self)
        self.active_faults.append(fault)
        self.fired.append((round(self.world.now, 1), fault.name, where))

    def _on_dispatch(self, command, commandline):
        for prefix, callback in list(self.watchers):
            if commandline.upper().startswith(prefix):
                callback(self, commandline)
        for item in list(self.pending_faults):
            trigger, fault = item
            if trigger.matches(command, commandline, self):
                self.pending_faults.remove(item)
                if hasattr(trigger, 'fired'):
                    trigger.fired = True
                self._fire(trigger, fault, commandline)

    def run_until_pause(self, limit=7200.0):
        """Run until the print pauses, fails or completes; returns a Result."""
        state = self.run_until_stopped(limit)
        return Result(state, self.print_stats.message)

    def run_to_end(self, limit=7200.0):
        return self.run_until_pause(limit)

    @property
    def print_state(self):
        return self.print_stats.state

    @property
    def user(self):
        return User(self)

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


class Result(object):
    def __init__(self, state, message):
        self.state = state
        self.message = message

    def __repr__(self):
        return 'Result(%s, %r)' % (self.state, self.message)


class User(object):
    """What the human does at the screen."""

    def __init__(self, sim):
        self.sim = sim

    def resume(self, limit=7200.0):
        """Press Resume. Returns the state the print reaches next."""
        sim = self.sim
        sim.run_gcode('RESUME')
        return sim.run_until_stopped(limit)

    def clear_fault(self):
        """The human fixes what is physically wrong: every active fault is cleared."""
        for fault in list(self.sim.active_faults):
            fault.clear(self.sim)
        self.sim.active_faults = []

    def cancel(self):
        self.sim.run_gcode('CANCEL_PRINT')
        self.sim.settle(1.0)

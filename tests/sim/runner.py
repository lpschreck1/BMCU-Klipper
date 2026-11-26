"""Assembles the simulated machine and runs scenarios.

``Sim.reference_machine()`` builds 4 heads, stock feeders, head sensors, the fake
Snapmaker modules and (optionally) the real ``BMCUManager`` wired to a fake BMCU.
"""
import copy
import gc
import os
import re
import shutil
import sys
import tempfile

import _support  # noqa: F401  (puts klippy/extras on sys.path)

from . import feeder as F
from . import klipper as K
from . import snapmaker as S
from . import virtual_sdcard as V
from . import world as W
from .clock import Halted, SimReactor

NOOP_COMMANDS = (
    'PRINT_START', 'DEFECT_DETECTION_START', 'DEFECT_DETECT_NOODLE_FIRST',
    'DEFECT_DETECTION_DETECT_BED', 'DEFECT_DETECTION_DETECT', 'TIMELAPSE_START',
    'TIMELAPSE_TAKE_FRAME', 'DETECT_BED_PLATE', 'WAIT_CHAMBER_TEMP',
    'BED_MESH_CALIBRATE', 'PRINT_END', 'M141', 'M190', 'M140', 'M107', 'M106',
    'SM_PRINT_FLOW_CALIBRATE', 'G29', 'M73', 'SET_VELOCITY_LIMIT', 'M201', 'M203',
    'M205', 'M204', 'M220', 'M221', 'EXCLUDE_OBJECT_DEFINE', 'M117', 'M486',
    'SET_PRINT_STATS_INFO', 'SET_FAN_SPEED', 'FILAMENT_DT_CLEAR', 'M84', 'MOVE_EXTRUDE',
    # in real Orca exports (tests/sim/fixtures/real), none of them moving filament
    'G21', 'SET_PRESSURE_ADVANCE', 'EXCLUDE_OBJECT_START', 'EXCLUDE_OBJECT_END')


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
        self.bmcu_mode = bmcu
        self.reactor = SimReactor()
        self.world = W.World()
        self.tmp = state_dir or tempfile.mkdtemp(prefix='bmcu-sim-')
        self._own_tmp = state_dir is None
        self.persist = {}             # what survives a restart besides the world and state_dir
        self.retired_dry = {}         # dry extrusion of machines thrown away by restart()
        self.pending_faults = []      # (trigger, fault)
        self.watchers = []
        self.active_faults = []
        self.fired = []               # (virtual time, fault name, trigger) log
        self.restarts = []            # (kind, virtual time)
        self.user_refusals = []       # what the printer refused the user's actions with
        self.tool_changes = []        # every BMCU_TOOL_CHANGE dispatched, across restarts
        self.corrupt_state = None     # 'empty' | 'truncated': damage bmcu_state.json at boot
        self.restart_kind = None
        self.bmcu = None
        self.manager = None
        self.booted = False
        self._build_world()
        self._build_machine()
        if bmcu:
            from . import bmcu_host
            bmcu_host.attach(self, with_device=(bmcu is True))

    def _build_machine(self):
        """Klipper and the Snapmaker modules over the (persistent) world."""
        self.reactor.add_stepper(self.world.step)
        self.printer = K.FakePrinter(self.reactor, self.tmp)
        self.gcode = self.printer.gcode
        self.print_stats = S.PrintStats(self)
        self.machine_state = S.MachineState()
        self.heaters = {}
        self.extruders = []
        self.sensors = {}
        self._build_printer()
        self.reactor.add_stepper(self._poll_sensors)
        self.reactor.add_stepper(self._poll_phases)
        self.gcode.on_dispatch = self._on_dispatch

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

    def reconfigure_bmcu(self, spools):
        """What the user redoes when BMCU lost its settings (preset, routes, spool info);
        the filament itself stays where it is, except that the router will not change a
        channel that is not EMPTY, so the user first pulls every BMCU filament out and
        confirms the channel empty."""
        for channel in range(4):
            source = self.world.sources['bmcu%d' % channel]
            if source.tip is not None and source.tip > 0:
                self.world.pull(source, source.tip)
            if source.present:        # the spool is still on the channel: PARKED
                self.run_gcode('BMCU_ROUTE_CONFIRM DEVICE=bmcu0 CHANNEL=%d STATE=PARKED'
                               % channel)
        self.setup_u1()
        for tool, text in sorted(spools.items()):
            if tool >= 4:
                material, _, color = text.partition(' ')
                self.run_gcode('BMCU_SET_FILAMENT DEVICE=bmcu0 CHANNEL=%d MATERIAL=%s COLOR=%s'
                               % (tool - 4, material, self.COLORS.get(color, 'FFFFFF')))
        self.settle(5.0)

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
        self.print_tools = tools
        self.print_path = path
        return path

    FILE_TOOL = re.compile(r'^(?:T(\d+)|BMCU_TOOL_CHANGE\s+TOOL=(\d+)|BMCU_PRINT_MAP\s+TOOL=(\d+))',
                           re.I)

    def tools_in(self, text):
        """The logical tools a G-code file uses (T<n>, BMCU_TOOL_CHANGE, BMCU_PRINT_MAP)."""
        found = set()
        for line in text.split(chr(10)):
            match = self.FILE_TOOL.match(line.strip())
            if match:
                found.add(int([g for g in match.groups() if g is not None][0]))
        return sorted(found)

    def slice_file(self, path):
        """Load a real sliced file in place of ``gcode_gen``. Returns the tools it uses;
        ``spools_for_file`` says what to put on them."""
        with open(path, encoding='utf-8', errors='replace') as stream:
            text = stream.read()
        tools = self.tools_in(text)
        self.sdcard.load(path, text)
        # about 2.5 simulated minutes per tool change (874 in the Baby Yoda print)
        changes = sum(1 for line in text.split(chr(10))
                      if line.upper().startswith('BMCU_TOOL_CHANGE'))
        self.run_limit = max(7200.0, 150.0 * changes)
        cfg = self.print_task_config.print_task_config
        for tool in tools:
            if tool < 4:
                cfg['extruders_used'][tool] = True
                cfg['reprint_info']['extruders_used'][tool] = True
        self.gcode_text = text
        self.print_tools = tools
        self.print_path = path
        return tools

    @staticmethod
    def spools_for_file(text, tools):
        """``{tool: 'MATERIAL colour'}`` from the file's ``filament_type`` / ``filament_colour``
        config lines (PLA white if absent)."""
        def listed(key):
            match = re.search(r'^;\s*%s\s*=\s*(.*)$' % key, text, re.M)
            return [v.strip() for v in match.group(1).split(';')] if match else []
        materials = listed('filament_type')
        spools = {}
        for tool in tools:
            material = materials[tool] if tool < len(materials) and materials[tool] else 'PLA'
            spools[tool] = '%s white' % material.replace(' ', '')
        return spools

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

    # Simulated seconds a print may run before run_until_stopped gives up. A real file
    # with hundreds of tool changes needs far more than the synthetic prints (slice_file
    # raises it from the file's own tool-change count).
    run_limit = 7200.0

    def pending_runout_head(self):
        """The Head whose stock runout pause is queued, if any (see RunoutHelper)."""
        for head in sorted(self.sensors):
            if self.sensors[head].runout_helper.runout_pending:
                return head
        return None

    def run_until_stopped(self, limit=None):
        """Advance until the print completes, pauses or fails. Returns the state, or
        ``'restarted'`` if the machine was restarted on the way (see ``restart``)."""
        if limit is None:
            limit = self.run_limit
        try:
            end = self.reactor.monotonic() + limit
            while self.reactor.monotonic() < end:
                self.reactor.pause(self.reactor.monotonic() + 1.0)
                if not self.sdcard.is_active() and self.print_stats.state != 'printing':
                    break
            return self.print_stats.state
        except Halted:
            self._do_restart()
            return 'restarted'

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
        if command == 'BMCU_TOOL_CHANGE':
            for word in commandline.upper().split():
                if word.startswith('TOOL='):
                    self.tool_changes.append(int(word[5:]))
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
        try:
            self.gcode.run_script_from_command(script)
        except Halted:
            self._do_restart()

    def settle(self, seconds=5.0):
        try:
            self.reactor.pause(self.reactor.monotonic() + seconds)
        except Halted:
            self._do_restart()

    def boot(self, seconds=20.0):
        """klippy:ready, then let the BMCU connect and reconcile."""
        self.printer.send_event('klippy:ready')
        self.booted = True
        self.settle(seconds)

    # -- restarts ---------------------------------------------------------------------------
    def restart(self, kind, at=None, corrupt_state=None):
        """Restart the machine, over the same world and state directory.

        ``'klipper'``: Klipper crashes or is restarted; the BMCU helper and firmware keep
        running, queued durable writes are lost, heaters are off. ``'power'``: everything
        loses power at that instant; motion stops where it was, heaters cool, the BMCU
        firmware and helper restart (route states survive in flash), and the Snapmaker
        offers a power-loss resume. With ``at`` (a trigger) it happens when the trigger
        fires and ``run_until_stopped`` returns ``'restarted'``; without it, now."""
        assert kind in ('klipper', 'power'), kind
        if at is not None:
            from . import faults as F
            self.inject(at, F.Restart(kind, corrupt_state))
            return
        self.corrupt_state = corrupt_state
        self.request_restart(kind)
        self._do_restart()

    def request_restart(self, kind):
        """The machine dies *now* (called from a trigger, anywhere in the simulation)."""
        self.restart_kind = kind
        if self.bmcu is not None:
            self.bmcu.alive = False
            self.bmcu.pending_writes[:] = []
        if kind == 'power':
            self.world.frozen = True
        self.reactor.halt()

    DOWNTIME = {'klipper': 20.0, 'power': 90.0}

    def _do_restart(self):
        kind = self.restart_kind or 'klipper'
        self.restart_kind = None
        old, rig, world = self.reactor, self.bmcu, self.world
        self.restarts.append((kind, round(world.now, 1)))
        for index, extruder in enumerate(self.extruders):
            self.retired_dry[index] = (self.retired_dry.get(index, 0.0) +
                                       extruder.print_commanded - extruder.print_flow)
        self.persist['print_task'] = copy.deepcopy(self.print_task_config.print_task_config)
        keep = routes = values = None
        with_device = False
        if rig is not None:
            rig.alive = False
            rig.pending_writes[:] = []
            with_device, values = rig.with_device, rig.config_values
            if with_device and kind == 'klipper':
                keep = (rig.fw, rig.helper)
            elif with_device:
                routes = list(rig.fw.route_state)
            rig.stop()
        # throw the old machine away; its greenlets are killed (not resumed) by the
        # collector while nothing can move
        world.frozen = True
        old.timers[:] = []
        old._sleepers[:] = []
        old._callbacks[:] = []
        old.fd_handlers.clear()
        old.steppers[:] = []
        old._dispatch = None
        self.printer = self.gcode = self.manager = self.bmcu = self.sdcard = None
        self.extruders = []
        self.sensors = {}
        self.feeders = {}
        self.macros = None
        old_now = old.now
        del old, rig
        gc.collect()
        world.frozen = False
        for heater in world.heaters.values():      # Klipper's heater control is gone either way
            heater.target = 0.0
        self._damage_state_file()
        self.reactor = SimReactor(start=old_now)
        self._build_machine()
        self._restore_print_task(kind)
        if self.bmcu_mode:
            from . import bmcu_host
            bmcu_host.attach(self, values, with_device, keep=keep, routes=routes)
        self.reactor.pause(self.reactor.monotonic() + self.DOWNTIME[kind])
        self.boot(20.0)

    def _damage_state_file(self):
        path = os.path.join(self.tmp, 'bmcu_state.json')
        mode, self.corrupt_state = self.corrupt_state, None
        if mode is None or not os.path.exists(path):
            return
        with open(path, 'rb') as stream:
            data = stream.read()
        with open(path, 'wb') as stream:
            if mode == 'truncated':
                stream.write(data[:len(data) // 2])
        # 'empty' leaves a zero-length file

    def planned_tool_changes(self):
        """The BMCU_TOOL_CHANGE sequence of the loaded file."""
        found = []
        for line in self.gcode_text.split(chr(10)):
            if line.upper().startswith('BMCU_TOOL_CHANGE'):
                for word in line.upper().split():
                    if word.startswith('TOOL='):
                        found.append(int(word[5:]))
        return found

    def _restore_print_task(self, kind):
        saved = self.persist.get('print_task')
        if not saved:
            return
        cfg = self.print_task_config.print_task_config
        if kind == 'power':
            cfg.update(copy.deepcopy(saved))
        else:
            for key in self.print_task_config.KEYS:
                cfg[key] = copy.deepcopy(saved[key])
        cfg['filament_exist'] = [bool(self.world.sensor_physical(h)) for h in range(4)]

    def reload_print(self):
        """The user selects the same file again (after a Klipper restart)."""
        self.sdcard.load(self.print_path, self.gcode_text)
        cfg = self.print_task_config.print_task_config
        for tool in self.print_tools:
            if tool < 4:
                cfg['extruders_used'][tool] = True
                cfg['reprint_info']['extruders_used'][tool] = True

    def power_loss_restore(self):
        """The user taps "resume" on the power-loss prompt."""
        found, _info = self.sdcard.power_loss_info_check()
        if not found:
            raise AssertionError('the printer offers no power-loss resume')
        self.run_gcode('SDCARD_PRINT_PL_RESTORE')

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

    def resume(self, limit=None):
        """Press Resume. Returns the state the print reaches next."""
        sim = self.sim
        before = len(sim.restarts)
        sim.run_gcode('RESUME')
        if len(sim.restarts) != before:
            return 'restarted'
        return sim.run_until_stopped(limit)

    def confirm_routes(self):
        """What the BMCU panel asks for when a route is UNCERTAIN: the user looks at the
        physical path and confirms it (``BMCU_ROUTE_CONFIRM``). Returns the confirmed
        channels; raises what the printer refuses with (it refuses while a print is
        active or paused)."""
        sim = self.sim
        confirmed = []
        for device in sim.manager.get_status()['devices']:
            for channel in device['channels']:
                if not channel['uncertain']:
                    continue
                source = sim.world.sources['bmcu%d' % channel['channel']]
                if source.past(source.sensor_p):
                    # filament in the head: the user feeds it down to the nozzle by hand
                    if not source.past(source.nozzle_p - 2.0):
                        sim.world._apply(source, source.nozzle_p - 2.0 - source.tip)
                    state = 'LOADED'
                elif source.tip is None or source.tip <= 0:
                    state = 'EMPTY'
                else:
                    # PARKED means the printer route is empty (docs/COMMANDS.md): a
                    # tip still past the splitter is pulled back by hand first, or
                    # the user would be confirming something false.
                    if source.past(source.splitter_p):
                        sim.world._apply(source, source.splitter_p - 50.0 - source.tip)
                    state = 'PARKED'
                sim.run_gcode('BMCU_ROUTE_CONFIRM DEVICE=%s CHANNEL=%d STATE=%s' % (
                    device['name'], channel['channel'], state))
                confirmed.append((channel['channel'], state))
        return confirmed

    def clear_fault(self):
        """The human fixes what is physically wrong: every active fault is cleared."""
        for fault in list(self.sim.active_faults):
            fault.clear(self.sim)
        self.sim.active_faults = []

    def cancel(self):
        self.sim.run_gcode('CANCEL_PRINT')
        self.sim.settle(1.0)

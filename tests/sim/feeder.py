"""Fake Snapmaker stock feeder and head sensors (firmware 2.1.0).

Modelled on klippy/extras/filament_feed.py (_do_feed, cmd_FEED_AUTO) and
filament_motion_sensor.py from github.com/Snapmaker/u1-klipper. Anything that is a
guess is marked ``# MODEL:`` and listed in tests/sim/README.md.
"""
from . import klipper as K

# The module-level constant BMCU reads through sys.modules (endpoints.py:2046).
FEED_WHEEL_CIRCUMFERENCE = 31.4159

FEED_LOAD_TIMEOUT_TIME = 60
FEED_LOAD_WHEEL_ERR_CNT_MAX = 20
FEED_LOAD_EXTRUDE_TIMES_MAX = 20
FEED_LOAD_COUNTS_MAX = 1100.0 * 6 * 2 / FEED_WHEEL_CIRCUMFERENCE / 6
FEED_PRELOAD_TIMEOUT_TIME = 45
FEED_CHANNELS = 2

# printer.cfg filament_ch_1/2: left serves heads 1 and 0, right serves 2 and 3
FEEDERS = {'left': (1, 0), 'right': (2, 3)}


class Wheel(object):
    ppr = 6

    def __init__(self, source):
        self.source = source

    def get_counts(self):
        return self.source.counts()

    def get_rpm(self):
        return 0.0


class Motor(object):
    """# MODEL: the feeder motor is not simulated; feeding moves the world directly."""

    def __init__(self):
        self.runs = []

    def run(self, direction, value):
        self.runs.append(('run', direction, value))

    def run_one_cycle(self, direction, value, duration):
        self.runs.append(('cycle', direction, value, duration))


class RunoutHelper(object):
    """filament_switch_sensor.RunoutHelper, as far as a runout pause goes.

    A switch that opens while printing (or the motion sensor running out of
    detection_length) is a runout event: Snapmaker queues PAUSE IS_RUNOUT=1, which
    runs once the command in progress finishes (virtual_sdcard checks
    ``runout_pending``), then tries auto replenish. Events are ignored while one is
    pending (min_event_systime = NEVER)."""

    def __init__(self, sim=None, head=None):
        self.sim = sim
        self.head = head
        self.filament_present = False
        self.sensor_enabled = True
        self.runout_pending = False
        self.runout_events = 0

    def note_filament_present(self, state, force=False):
        state = bool(state)
        if state == self.filament_present and not force:
            return
        self.filament_present = state
        if (state or self.sim is None or self.runout_pending or
                not self.sensor_enabled):
            return
        if self.sim.print_stats.state == 'printing':
            self.runout_pending = True
            self.runout_events += 1

    def get_status(self, eventtime=None):
        return {'filament_detected': bool(self.filament_present),
                'enabled': bool(self.sensor_enabled)}


class MotionSensor(object):
    """filament_motion_sensor eN_filament.

    ``runout_buttun_state`` (sic) is the raw switch. ``runout_helper.filament_present``
    follows it, except that while printing it stays true for ``detection_length`` mm of
    extruder motion after the switch opens (the lag, 2.1.0 encoder_event)."""
    detection_length = 7.0

    def __init__(self, sim, head):
        self.sim = sim
        self.head = head
        self.runout_helper = RunoutHelper(sim, head)
        self.runout_buttun_state = False
        self.filament_runout_pos = None
        self._extruder_pos_update_timer = sim.reactor.register_timer(
            self._extruder_pos_update_event, sim.reactor.NEVER)
        self._last_switch = False
        sim.printer.register_event_handler(
            'print_stats:start', self._handle_start_print_job)

    def _handle_start_print_job(self):
        # 2.1.0: a Head whose switch is open when the print starts gets a fast
        # runout, 0.2 mm of its extruder's motion (seen on the real U1 2026-11-26)
        if not self.runout_buttun_state:
            self.filament_runout_pos = self.sim.extruders[self.head].e_position + 0.2
            self.sim.reactor.update_timer(self._extruder_pos_update_timer,
                                          self.sim.reactor.NOW)

    def get_status(self, eventtime=None):
        return self.runout_helper.get_status(eventtime)

    def get_coil_freq(self):
        return self.sim.world.coil_freq(self.head)

    def physical(self):
        stuck = self.sim.world.heads[self.head].sensor_stuck
        if stuck is not None:
            return bool(stuck)
        return bool(self.sim.world.sensor_physical(self.head))

    def poll(self, now):
        state = self.physical()
        if state != self._last_switch:
            self._last_switch = state
            self.encoder_event(now, state)

    def encoder_event(self, eventtime, state):
        self.runout_buttun_state = state
        reactor = self.sim.reactor
        if self.sim.print_stats.state == 'printing':
            if state:
                reactor.update_timer(self._extruder_pos_update_timer, reactor.NEVER)
                self.runout_helper.note_filament_present(True)
            else:
                reactor.update_timer(self._extruder_pos_update_timer, reactor.NOW)
                lag = self.sim.world.heads[self.head].sensor_lag_mm
                self.filament_runout_pos = (
                    self.sim.extruders[self.head].e_position +
                    (self.detection_length if lag is None else lag))
        else:
            self.runout_helper.note_filament_present(state)
            reactor.update_timer(self._extruder_pos_update_timer, reactor.NEVER)

    def _extruder_pos_update_event(self, eventtime):
        if self.sim.extruders[self.head].e_position >= (self.filament_runout_pos or 0.0):
            self.runout_helper.note_filament_present(False, True)
            return self.sim.reactor.NEVER
        return eventtime + 0.1


class Feeder(object):
    """filament_feed left|right: two stock channels, each fed by one stock source.

    # MODEL: _do_feed is re-implemented at behaviour level (states, error codes and
    # the phases of load) rather than at motor/PWM level. Timings follow the source
    # constants where they matter (60 s load timeout, 20 stalled cycles).
    """

    def __init__(self, sim, module):
        self.sim = sim
        self.module_name = module
        self.filament_ch = list(FEEDERS[module])
        self.channel_active = None
        self.channel_state = ['wait_insert'] * FEED_CHANNELS
        self.channel_action_state = ['none'] * FEED_CHANNELS
        self.channel_error_state = ['none'] * FEED_CHANNELS
        self.channel_error = ['ok'] * FEED_CHANNELS
        self.module_exist = [True] * FEED_CHANNELS
        self.manual_feeding = [False] * FEED_CHANNELS
        self.exception_code = [0] * FEED_CHANNELS
        self.auto_mode = [True] * FEED_CHANNELS
        self.config = {'auto_mode': self.auto_mode, 'load_finish': [False] * FEED_CHANNELS}
        self.config_path = '%s/%s_filament_feed.json' % (sim.tmp, module)
        self._feed_preload_counts = int(850.0 * 2 / FEED_WHEEL_CIRCUMFERENCE)
        self.motor = Motor()
        self.sources = [sim.world.sources['stock%d' % h] for h in self.filament_ch]
        self.wheel = [Wheel(s) for s in self.sources]
        self.wheel_2 = [Wheel(s) for s in self.sources]
        self.runout_sensor = [sim.sensors[h] for h in self.filament_ch]
        self.hangs = 0
        self.puts = 0
        self.log = []
        self._boot_states()
        sim.printer.add_object('filament_feed %s' % module, self)
        sim.gcode.register_mux_command('FEED_AUTO', 'MODULE', module, self.cmd_FEED_AUTO)

    def _boot_states(self):
        """A feeder that boots with filament already in its port (after a restart).
        # MODEL: a tip past the head sensor reads as load_finish, a tip inside the tube
        # as preload_finish; the feeder's own MCU does not forget between Klipper runs."""
        for ch, source in enumerate(self.sources):
            if not source.present or source.tip is None:
                continue
            if source.past(source.sensor_p):
                self.channel_state[ch] = 'load_finish'
                self.config['load_finish'][ch] = True
            elif source.tip > 0:
                self.channel_state[ch] = 'preload_finish'
            else:
                self.channel_state[ch] = 'wait_insert'

    # -- the hooks BMCU calls -------------------------------------------------
    def _hang_neutral(self, channel):
        self.hangs += 1

    def _put_into_drive(self, channel):
        self.puts += 1

    def port_detected(self, ch):
        return bool(self.sources[ch].present)

    def get_status(self, eventtime=None):
        out = {}
        for ch in range(FEED_CHANNELS):
            out['extruder%d' % self.filament_ch[ch]] = {
                'module_exist': self.module_exist[ch],
                'filament_detected': self.port_detected(ch),
                'disable_auto': not self.auto_mode[ch],
                'channel_state': self.channel_state[ch],
                'channel_error': self.channel_error[ch],
                'channel_error_state': self.channel_error_state[ch],
                'channel_action_state': self.channel_action_state[ch]}
        return out

    def _set_state(self, ch, state):
        self.channel_state[ch] = state
        if (state not in ('inited', 'wait_insert', 'test') and
                not state.startswith('preload_')):
            self.channel_action_state[ch] = state

    # -- the user inserts or removes stock filament ---------------------------
    def port_changed(self, ch):
        if not self.auto_mode[ch] or not self.module_exist[ch]:
            return
        if not self.runout_sensor[ch].runout_helper.sensor_enabled:
            return
        if self.sources[ch].present:
            self._set_state(ch, 'preload_prepare')
            self.sim.reactor.register_callback(lambda now, ch=ch: self.preload(ch))
        elif self.channel_state[ch] != 'load_prepare':
            self._set_state(ch, 'wait_insert')

    def preload(self, ch):
        """FEED_ACT_PRELOAD: stops at the head sensor or after _feed_preload_counts."""
        sim, world, reactor = self.sim, self.sim.world, self.sim.reactor
        source = self.sources[ch]
        while self.channel_active is not None:
            reactor.pause(reactor.monotonic() + 0.1)
        self.channel_active = ch
        self.channel_error[ch] = 'ok'
        try:
            self._set_state(ch, 'preload_prepare')
            if not source.present:
                self.channel_error[ch] = 'no_filament'
                raise ValueError
            if self.runout_sensor[ch].runout_helper.filament_present:
                self.channel_error[ch] = 'residual_filament'
                raise ValueError
            reactor.pause(reactor.monotonic() + 1.0)
            self._set_state(ch, 'preload_feeding')
            limit_mm = self._feed_preload_counts * FEED_WHEEL_CIRCUMFERENCE / 2.0
            _moved, reason = world.feed_until(
                source, limit_mm, stop_at_sensor=True, chunk=10.0,
                on_chunk=lambda m: reactor.pause(reactor.monotonic() + m / 80.0))
            if reason == 'stalled':
                # MODEL: a stalled wheel ends the preload with a wheel_speed error
                self.channel_error[ch] = 'wheel_speed'
                self.exception_code[ch] = 12
                raise ValueError
            self._set_state(ch, 'preload_finish')
        except ValueError:
            self._set_state(ch, 'preload_fail')
            self.channel_error_state[ch] = 'preload_fail'
        finally:
            self.channel_active = None

    # -- FEED_AUTO -------------------------------------------------------------
    def cmd_FEED_AUTO(self, gcmd):
        sim = self.sim
        ch = gcmd.get_int('CHANNEL')
        if ch < 0 or ch >= FEED_CHANNELS:
            raise gcmd.error('[feed] channel[%d] is out of range' % ch)
        auto = gcmd.get_int('AUTO', None)
        load = gcmd.get_int('LOAD', None)
        unload = gcmd.get_int('UNLOAD', None)
        printing = gcmd.get_int('PRINTING', 0)
        gcode = sim.gcode
        msm = sim.machine_state
        if str(msm.main_state) not in ('IDLE', 'PRINTING', 'AUTO_LOAD', 'AUTO_UNLOAD'):
            raise gcmd.error('[feed] channel[%d] machine main state error: %s' %
                             (ch, msm.main_state))
        if auto is not None:
            self.auto_mode[ch] = bool(auto)
            return
        head = self.filament_ch[ch]
        extruder = sim.extruders[head]
        if load:
            if self.channel_state[ch] == 'load_finish' and self.channel_error[ch] == 'ok':
                return
            if printing == 1 and not self.port_detected(ch):
                return
            if not self.module_exist[ch] or not self.auto_mode[ch]:
                return
            if not self.runout_sensor[ch].runout_helper.sensor_enabled:
                return
            last_temp = extruder.heater.target_temp
            need_restore = False
            try:
                if msm.main_state == 'PRINTING':
                    need_restore = True
                    if msm.action_code not in ('PRINT_RESUMING', 'PRINT_REPLENISHING'):
                        gcode.run_script_from_command(
                            'SET_ACTION_CODE ACTION=PRINT_AUTO_FEEDING')
                else:
                    gcode.run_script_from_command(
                        'SET_MAIN_STATE MAIN_STATE=AUTO_LOAD ACTION=AUTO_LOADING')
                self.do_load(ch, head)
            finally:
                if need_restore:
                    gcode.run_script_from_command('M104 S%s T%d A0' % (
                        gcmd.get_float('RESTORE_TEMP', last_temp), head))
                if msm.main_state == 'PRINTING':
                    if msm.action_code not in ('PRINT_RESUMING', 'PRINT_REPLENISHING'):
                        gcode.run_script_from_command('SET_ACTION_CODE ACTION=IDLE')
                else:
                    gcode.run_script_from_command(
                        'SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE')
            if self.channel_state[ch] != 'load_finish' or self.channel_error[ch] != 'ok':
                raise gcmd.error(
                    'extruder[%d]: state: %s, error: %s!' % (
                        head, self.channel_error_state[ch], self.channel_error[ch]),
                    action='pause', id=525, index=head,
                    code=self.exception_code[ch], oneshot=1, level=2)
            return
        if unload:
            self.do_unload(ch, head)

    def do_load(self, ch, head):
        """FEED_ACT_LOAD: prepare/home/pick, phase 2 push until the head sensor,
        phase 3 grab attempts, then INNER_FLUSH_FILAMENT."""
        sim, world, reactor = self.sim, self.sim.world, self.sim.reactor
        gcode = sim.gcode
        source = self.sources[ch]
        while self.channel_active is not None:
            reactor.pause(reactor.monotonic() + 0.1)
        self.channel_active = ch
        self.channel_error[ch] = 'ok'
        self.exception_code[ch] = 0
        load_temp = 250
        try:
            self.exception_code[ch] = 30
            self.channel_error_state[ch] = 'none'
            was_preloaded = self.channel_state[ch] == 'preload_finish'
            self._set_state(ch, 'load_prepare')
            if not self.port_detected(ch):
                self.channel_error[ch] = 'no_filament'
                self.exception_code[ch] = 33
                raise ValueError('no filament')
            gcode.run_script_from_command('M104 S%d T%d A0' % (load_temp - 70, head))
            try:
                self._set_state(ch, 'load_homing')
                homed = sim.toolhead.homed_axes
                if 'x' not in homed or 'y' not in homed:
                    gcode.run_script_from_command('G28 X Y')
            except K.CommandError:
                self.channel_error[ch] = 'move_home'
                raise
            try:
                self._set_state(ch, 'load_picking')
                gcode.run_script_from_command('T%d A0' % head)
            except K.CommandError:
                self.channel_error[ch] = 'move_switch'
                raise
            gcode.run_script_from_command(
                'M104 S%d' % (load_temp if was_preloaded else load_temp - 50))
            self._set_state(ch, 'load_feeding')
            self._put_into_drive(ch)
            sensor = self.runout_sensor[ch]
            # Phase 2: push until the head sensor reports filament. It is skipped when
            # the sensor already says present (the stale-sensor case, scenario 4).
            if not sensor.runout_helper.filament_present:
                gcode.run_script_from_command('G90\nG0 Y5 F18000\nG0 X150 F18000')
                reactor.pause(reactor.monotonic() + 0.105)
                start = reactor.monotonic()
                stalls = FEED_LOAD_WHEEL_ERR_CNT_MAX
                pushed0 = source.pushed_mm
                while True:
                    moved = world.push(source, 9.0)
                    reactor.pause(reactor.monotonic() + 0.105)
                    sensor.poll(reactor.monotonic())
                    if sensor.runout_helper.filament_present:
                        break
                    if not self.port_detected(ch):
                        self.channel_error[ch] = 'no_filament'
                        self.exception_code[ch] = 33
                        break
                    if reactor.monotonic() - start > FEED_LOAD_TIMEOUT_TIME:
                        self.channel_error[ch] = 'timeout'
                        self.exception_code[ch] = 34
                        break
                    if (source.pushed_mm - pushed0) * 2 / FEED_WHEEL_CIRCUMFERENCE > \
                            FEED_LOAD_COUNTS_MAX * 2:
                        self.channel_error[ch] = 'distance'
                        self.exception_code[ch] = 35
                        break
                    if moved < 0.5:
                        stalls -= 1
                        if stalls <= 0:
                            self.channel_error[ch] = 'wheel_speed'
                            self.exception_code[ch] = 32
                            break
                    else:
                        stalls = FEED_LOAD_WHEEL_ERR_CNT_MAX
                if self.channel_error[ch] != 'ok':
                    self._hang_neutral(ch)
                    raise ValueError('phase 2 failed')
            gcode.run_script_from_command('M104 S%d' % load_temp)
            try:
                gcode.run_script_from_command('MOVE_TO_DISCARD_FILAMENT_POSITION')
            except K.CommandError:
                self.channel_error[ch] = 'move'
                raise
            self._set_state(ch, 'load_heating')
            try:
                gcode.run_script_from_command('M109 S%d' % load_temp)
            except K.CommandError:
                self.channel_error[ch] = 'heat'
                raise
            self.exception_code[ch] = 50
            self._set_state(ch, 'load_extruding')
            gcode.run_script_from_command('M83')
            head_state = world.heads[head]
            extruded = False
            source.assisting = True
            try:
                for _retry in range(FEED_LOAD_EXTRUDE_TIMES_MAX):
                    reactor.pause(reactor.monotonic() + 0.105)
                    for _attempt in range(2):
                        # MODEL: success needs the coil to react to the grab (>= 400)
                        head_state.last_grip_at = None
                        gcode.run_script_from_command('G1 E20 F400')
                        reactor.pause(reactor.monotonic() + 0.3)
                        if sim.coil_for(head).get_coil_freq() - 3000.0 >= 400:
                            extruded = True
                            break
                    if extruded:
                        break
                    gcode.run_script_from_command('ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD')
                    gcode.run_script_from_command('G1 E40 F480')
                    reactor.pause(reactor.monotonic() + 0.7)
            finally:
                source.assisting = False
            self._hang_neutral(ch)
            if not extruded:
                self.channel_error[ch] = 'move_extrude'
                self.exception_code[ch] = 51
                raise ValueError('grab failed')
            self._set_state(ch, 'load_flushing')
            try:
                gcode.run_script_from_command(
                    'INNER_FLUSH_FILAMENT TEMP=%d SOFT=0 NOZZLE_DIAMETER=%f' % (
                        load_temp, sim.extruders[head].nozzle_diameter))
            except K.CommandError:
                self.channel_error[ch] = 'custom_gcode'
                raise
            self.channel_error[ch] = 'ok'
            self._set_state(ch, 'load_finish')
        except (ValueError, K.CommandError):
            self.channel_error_state[ch] = self.channel_state[ch]
            if self.channel_error[ch] == 'ok':
                self.channel_error[ch] = 'general'
            self._set_state(ch, 'load_fail')
        finally:
            gcode.run_script_from_command('M107')
            gcode.run_script_from_command('M104 S0')
            self.channel_active = None

    def do_unload(self, ch, head):
        """FEED_ACT_UNLOAD. # MODEL: abbreviated; BMCU does not drive it."""
        sim = self.sim
        self.channel_error[ch] = 'ok'
        sim.gcode.run_script_from_command('T%d A0' % head)
        sim.world.pull(self.sources[ch], self.sources[ch].tip or 0.0)
        self._set_state(ch, 'unload_finish')

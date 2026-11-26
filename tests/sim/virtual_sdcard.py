"""Fake virtual_sdcard: runs a G-code file line by line in virtual time.

Pause-on-error semantics follow Snapmaker's klippy/extras/virtual_sdcard.py
``work_handler`` (2.1.0): when a line raises an error whose ``action`` is ``pause``, the
handler runs ``PAUSE ON_ERROR=1`` and *breaks out of the loop before*
``self.file_position = self.next_file_position``. The file position therefore still
points at the failed line, so ``do_resume`` (called by RESUME_BASE) restarts the work
handler on that same line: **a failed command is run again on Resume**. This confirms
the behaviour the plan marked as observed (plan section 9).
"""
import re

from . import klipper as K

GENERIC_MOVE = ('G0', 'G1', 'G2', 'G3')
TOOL_CHANGE = re.compile(r'^T\d+$')
PL_SAVE_Z_HOP_TEMP = 170.0      # MODEL: the temperature the restore preheats to before G28
PL_PRE_EXTRUDE_MM = 20.0
PL_RETRACT_MM = 1.0


class _File(object):
    def __init__(self, name):
        self.name = name

    def close(self):
        return None


class VirtualSDCard(object):
    def __init__(self, sim):
        self.sim = sim
        self.reactor = sim.reactor
        self.lines_data = []          # (text, byte_length) of the loaded file
        self.file_position = 0        # byte offset of the next line to run
        self.next_file_position = 0
        self.work_timer = None
        self.must_pause_work = False
        self.cmd_from_sd = False
        self.current_file = None
        self.file_name = ''
        self.lines = 0
        self.current_line_gcode = ''
        self.run_log = []             # every line dispatched from the file
        self.sim.gcode.register_command('SDCARD_PRINT_FILE', self.cmd_SDCARD_PRINT_FILE)
        self.sim.gcode.register_command('SDCARD_PRINT_PL_RESTORE', self.cmd_PL_RESTORE)
        self.sim.gcode.register_command('SDCARD_PRINT_PL_CLEAR_ENV', self.cmd_PL_CLEAR)
        self.text = ''

    # -- loading --------------------------------------------------------------
    def load(self, name, text):
        self.file_name = name
        self.text = text
        self.current_file = _File(name)
        self.file_position = 0
        self.next_file_position = 0
        self.lines = 0
        self.run_log = []
        self.sim.print_stats.filename = name

    def file_path(self):
        return self.file_name

    def get_status(self, eventtime=None):
        return {'file_path': self.file_name, 'progress': 0.0,
                'is_active': self.is_active(), 'file_position': self.file_position,
                'file_size': len(getattr(self, 'text', ''))}

    # -- the API BMCU uses ------------------------------------------------------
    def is_active(self):
        return self.work_timer is not None

    def is_cmd_from_sd(self):
        return self.cmd_from_sd

    def get_file_position(self):
        return self.next_file_position

    def set_file_position(self, pos):
        self.next_file_position = pos

    def record_pl_print_temperature_env(self, temperatures, ignore_pl_condition=False):
        return None

    def force_refresh_move_env_extruder(self, name):
        return None

    # -- control ----------------------------------------------------------------
    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        self.start()

    def start(self):
        self.sim.gcode.run_script_from_command('SET_MAIN_STATE MAIN_STATE=PRINTING')
        self.do_resume()

    def do_pause(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            guard = 0
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
                guard += 1
                if guard > 100000:
                    raise RuntimeError('virtual_sdcard.do_pause did not finish')

    def do_resume(self):
        if self.work_timer is not None:
            raise K.CommandError('SD busy', action='none')
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(self.work_handler, self.reactor.NOW)

    def do_cancel(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file = None
            self.rm_power_loss_info()
            self.sim.print_stats.note_cancel()
            self.exit_to_idle()

    def exit_to_idle(self):
        self.sim.gcode.run_script_from_command('SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE')

    # -- the work handler ---------------------------------------------------------
    def work_handler(self, eventtime):
        sim = self.sim
        self.reactor.unregister_timer(self.work_timer)
        if self.current_file is None:
            self.work_timer = None
            return self.reactor.NEVER
        sim.print_stats.note_start()
        data = self.text.encode()[self.file_position:].decode()
        lines = data.split('\n')
        if lines and lines[-1] == '':
            lines.pop()
        lines.reverse()
        error_message = None
        action = 'cancel'
        finished = False
        while not self.must_pause_work:
            if not lines:
                self.current_file = None
                self.sim.gcode.respond_raw('Done printing file')
                finished = True
                break
            self.cmd_from_sd = True
            line = lines.pop()
            self.next_file_position = self.file_position + len(line.encode()) + 1
            try:
                self.lines += 1
                self.record_power_loss_info(line)
                self.run_log.append(line)
                sim.gcode.run_script(line)
                self.current_line_gcode = line
            except K.CommandError as exc:
                error_message = str(exc)
                action = exc.action
                if action == 'pause':
                    try:
                        sim.gcode.run_script('PAUSE ON_ERROR=1')
                    except K.CommandError:
                        action = 'cancel'
                elif action == 'pause_runout':
                    try:
                        sim.gcode.run_script('PAUSE IS_RUNOUT=1')
                    except K.CommandError:
                        action = 'cancel'
                else:
                    sim.gcode.respond_info('print error: %s' % error_message)
                # break before file_position advances: the line is run again on Resume
                break
            self.cmd_from_sd = False
            self.file_position = self.next_file_position
        self.cmd_from_sd = False
        self.work_timer = None
        if error_message is not None and action not in ('pause', 'pause_runout'):
            sim.print_stats.note_error(error_message)
            self.exit_to_idle()
        elif finished:
            sim.print_stats.note_complete()
            self.rm_power_loss_info()
            self.exit_to_idle()
        elif self.current_file is not None:
            sim.print_stats.note_pause(error_message)
        return self.reactor.NEVER

    # -- power-loss resume (2.1.0 virtual_sdcard.py) -------------------------------------
    #
    # MODEL: Snapmaker saves the print environment to flash/files and the MCU keeps the
    # file line of the last queued move. Here the "valid line" is exactly the line being
    # executed when the power is cut. Only what BMCU can see is kept: the file offset and
    # line, the active extruder, the heater targets and the print-task filament info.
    # Left out: Z and the z-hop compensation, bed mesh, fans, pressure advance, purifier,
    # exclude-object state, flow/speed factors, timelapse and defect detection.

    def record_power_loss_info(self, line):
        """Called before each file line runs (record_power_loss_info + the move env)."""
        words = line.strip().split()
        command = words[0].upper() if words and not words[0].startswith(';') else ''
        self.sim.persist['pl'] = {
            'file': self.file_name, 'line': self.lines,
            'file_pos': self.file_position, 'next_pos': self.next_file_position,
            'cmd': command, 'extruder': self.sim.toolhead.active}
        temps = self.sim.persist.setdefault('pl_temps', {})
        for extruder in self.sim.extruders:
            temps[extruder.name] = extruder.heater.target_temp

    def rm_power_loss_info(self):
        self.sim.persist.pop('pl', None)
        self.sim.persist.pop('pl_temps', None)

    def power_loss_info_check(self):
        """What the U1 checks at boot to offer the restore: ``(found, info)``."""
        info = self.sim.persist.get('pl')
        return (info is not None, dict(info) if info else None)

    def record_pl_print_temperature_env(self, temperatures, ignore_pl_condition=False):
        env = self.sim.persist.setdefault('pl_temps', {})
        env.update(temperatures)

    def cmd_PL_CLEAR(self, gcmd):
        if str(self.sim.machine_state.main_state) == 'PRINTING':
            raise gcmd.error('Do not delete power-loss info during printing', action='none')
        self.rm_power_loss_info()
        self.sim.print_task_config.reset_print_info()

    def cmd_PL_RESTORE(self, gcmd):
        state = str(self.sim.machine_state.main_state)
        if state != 'IDLE':
            raise gcmd.error('Current main state is %s power-loss recovery is not allowed'
                             % state, action='none')
        self.restore_print()

    def restore_print(self):
        sim = self.sim
        gcode = sim.gcode
        info = sim.persist.get('pl')
        try:
            gcode.run_script_from_command(
                'SET_MAIN_STATE MAIN_STATE=PRINTING ACTION=PRINT_PL_RESTORE')
            if info is None:
                raise K.CommandError('restore_print failed, environmental prerequisites '
                                     'not met', action='none')
            temps = dict(sim.persist.get('pl_temps') or {})
            head = int(info['extruder'])
            gcode_id = 'T%d' % head
            gcode.run_script_from_command('M140 S0')
            for index, extruder in enumerate(sim.extruders):          # preheat the active head
                if index == head and extruder.heater.smoothed_temp + 2 < PL_SAVE_Z_HOP_TEMP:
                    gcode.run_script_from_command(
                        'M109 T%d S%d A0' % (head, PL_SAVE_Z_HOP_TEMP))
            gcode.run_script_from_command('G28 X Y PL_SAVE_Z_HOP 1')
            gcode.run_script_from_command('%s A0' % gcode_id)
            gcode.run_script_from_command('MOVE_EXTRUDE')
            for index, extruder in enumerate(sim.extruders):          # restore the targets
                target = float(temps.get(extruder.name, 0.0))
                if index == head and target:
                    gcode.run_script_from_command('M109 T%d S%d A0' % (index, target))
                else:
                    gcode.run_script_from_command('M104 T%d S%d A0' % (index, target))
            move = info['cmd'] in GENERIC_MOVE
            active = sim.extruders[head]
            pre_extrude = (active.can_extrude() and
                           active.heater.target_temp > active.heater.min_extrude_temp and
                           not TOOL_CHANGE.match(info['cmd']) and
                           info['cmd'] != 'BED_MESH_CALIBRATE')
            if pre_extrude:
                gcode.run_script_from_command('M83')
                gcode.run_script_from_command('G0 E%d F300' % PL_PRE_EXTRUDE_MM)
                gcode.run_script_from_command('G0 E-%d F1800' % PL_RETRACT_MM)
            gcode.run_script_from_command('G0 Y10 F6000')
            gcode.run_script_from_command('G0 X10 F6000')
            gcode.run_script_from_command('G92 E0')
            sim.print_task_config.apply_reprint_info()
            if self.work_timer is not None:
                raise K.CommandError('SD busy', action='none')
            with open(info['file']) as stream:
                text = stream.read()
            self.load(info['file'], text)
            if move:                       # a move that was cut is assumed to have finished
                self.file_position = info['next_pos']
                self.lines = info['line']
            else:                          # anything else (a macro, a T command) runs again
                self.file_position = info['file_pos']
                self.lines = info['line'] - 1
            self.next_file_position = self.file_position
            gcode.run_script_from_command('TIMELAPSE_START TYPE=continue')
            gcode.run_script_from_command('DEFECT_DETECTION_START')
            gcode.run_script_from_command('SET_ACTION_CODE ACTION=IDLE')
            self.do_resume()
        except K.CommandError as exc:
            gcode.respond_info('Failed to restore pl_print, error: %s' % exc)
            if str(sim.machine_state.main_state) == 'PRINTING':
                gcode.run_script_from_command('SET_MAIN_STATE MAIN_STATE=IDLE')
            raise K.CommandError('Failed to restore pl_print, %s' % exc, action='none')

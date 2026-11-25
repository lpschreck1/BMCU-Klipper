"""Fake virtual_sdcard: runs a G-code file line by line in virtual time.

Pause-on-error semantics follow Snapmaker's klippy/extras/virtual_sdcard.py
``work_handler`` (2.1.0): when a line raises an error whose ``action`` is ``pause``, the
handler runs ``PAUSE ON_ERROR=1`` and *breaks out of the loop before*
``self.file_position = self.next_file_position``. The file position therefore still
points at the failed line, so ``do_resume`` (called by RESUME_BASE) restarts the work
handler on that same line: **a failed command is run again on Resume**. This confirms
the behaviour the plan marked as observed (plan section 9).
"""
from . import klipper as K


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

    # -- loading --------------------------------------------------------------
    def load(self, name, text):
        self.file_name = name
        self.text = text
        self.current_file = name
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
            self.exit_to_idle()
        elif self.current_file is not None:
            sim.print_stats.note_pause(error_message)
        return self.reactor.NEVER

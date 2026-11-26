"""Fake Snapmaker U1 core modules (firmware 2.1.0) and the macros that run.

Modelled on github.com/Snapmaker/u1-klipper: print_task_config.py, gcode_move.py,
pause_resume.py, print_stats.py and lava/fluidd.cfg. The stock feeder and the head
sensors are in feeder.py, the SD-card print in virtual_sdcard.py. Guesses carry a
``# MODEL:`` comment and are listed in tests/sim/README.md.
"""
import collections
import copy

from . import feeder as F
from . import klipper as K
from . import world as W

PHYSICAL_EXTRUDER_NUM = 4
Coord = collections.namedtuple('Coord', ('x', 'y', 'z', 'e'))


class Coil(object):
    def __init__(self, sim, head):
        self.sim = sim
        self.head = head

    def get_coil_freq(self):
        return self.sim.world.coil_freq(self.head)


class BindingProbe(object):
    def __init__(self, sim, head):
        self.sensor = Coil(sim, head)


class U1Extruder(K.FakeExtruder):
    def __init__(self, sim, index):
        K.FakeExtruder.__init__(self, sim.printer, index, sim.world, sim.heaters[index])
        self.sim = sim
        self.extruder_index = index
        self.nozzle_diameter = 0.4
        self.nozzle_volume_type = 'standard'
        self.total_e = 0.0               # all extruder motion, for the sensor lag
        self.print_commanded = 0.0       # E asked for while printing (forward moves)
        self.print_flow = 0.0            # of which actually left the nozzle
        self.binding_probe = BindingProbe(sim, index)

    def find_past_position(self, print_time):
        return self.total_e

    def get_status(self, eventtime=None):
        status = K.FakeExtruder.get_status(self, eventtime)
        status.update(extruder_index=self.extruder_index, printing_e_pos=self.printing_e_pos)
        return status


class MachineState(object):
    def __init__(self):
        self.main_state = 'IDLE'
        self.action_code = 'IDLE'

    def get_status(self, eventtime=None):
        return {'main_state': self.main_state, 'action_code': self.action_code}


class PrintStats(object):
    """print_stats.py. # MODEL: state names only; counters are not simulated."""

    def __init__(self, sim):
        self.sim = sim
        self.state = 'standby'
        self.filename = ''
        self.message = ''
        self._config = {'print_count': 0}
        self._config_path = '%s/print_stats.json' % sim.tmp

    def get_status(self, eventtime=None):
        return {'state': self.state, 'filename': self.filename,
                'message': self.message, 'info': {'current_layer': None}}

    def note_start(self):
        previous = self.state
        self.state = 'printing'
        if previous != 'paused':
            self.sim.printer.send_event('print_stats:start')

    def note_pause(self, message=None):
        self.state = 'paused'
        self.message = message or ''

    def note_complete(self):
        self.state = 'complete'
        self.sim.printer.send_event('print_stats:stop')

    def note_cancel(self):
        self.state = 'cancelled'
        self.sim.printer.send_event('print_stats:stop')

    def note_error(self, message):
        self.state = 'error'
        self.message = message
        self.sim.printer.send_event('print_stats:stop')


class FilamentDetect(object):
    def __init__(self):
        self.callbacks = []

    def register_cb_2_update_filament_info(self, callback):
        self.callbacks.append(callback)

    def get_status(self, eventtime=None):
        return {'state': [0, 0, 0, 0], 'info': [{}, {}, {}, {}]}

    def is_startup_stay(self):
        return False


class FilamentParameters(object):
    def get_filament_parameters(self, vendor, ftype, subtype, nozzle, volume):
        return {'load_temp': 250, 'unload_temp': 250, 'is_soft': ftype == 'TPU'}

    def get_load_temp(self, vendor, material, subtype):
        return 250.0

    def get_unload_temp(self, vendor, material, subtype):
        return 250.0

    def get_flow_temp(self, vendor, material, subtype):
        return 220.0

    def get_print_temp(self, vendor, material, subtype, nozzle, volume='standard'):
        return 220.0

    def get_is_soft(self, vendor, material, subtype):
        return str(material).upper().startswith('TPU')


class PrintTaskConfig(object):
    """print_task_config.py: per-head filament info and the backup used on runout."""

    KEYS = ('filament_vendor', 'filament_type', 'filament_sub_type', 'filament_soft',
            'filament_color', 'filament_color_rgba', 'filament_color_multi')

    def __init__(self, sim):
        self.sim = sim
        n = PHYSICAL_EXTRUDER_NUM
        self.print_task_config = {
            'filament_vendor': ['NONE'] * n, 'filament_type': ['NONE'] * n,
            'filament_sub_type': ['NONE'] * n, 'filament_color': [0xFFFFFFFF] * n,
            'filament_color_rgba': ['FFFFFFFF'] * n,
            'filament_color_multi': [{'nums': 1, 'alpha': 0xFF, 'mode': 0,
                                      'colors': ['FFFFFF']} for _ in range(n)],
            'filament_official': [False] * n, 'filament_sku': [0] * n,
            'filament_edit': [True] * n, 'filament_exist': [False] * n,
            'filament_soft': [False] * n,
            'extruder_map_table': list(range(n)) + [0] * 28,
            'extruders_used': [False] * n, 'extruders_replenished': list(range(n)),
            'auto_replenish_filament': True, 'end_unload_filament': [False] * n,
            'flow_calibrate': False, 'flow_calib_extruders': [True] * n,
            'reprint_info': {'extruder_map_table': list(range(n)) + [0] * 28,
                             'extruders_used': [False] * n,
                             'end_unload_filament': [False] * n,
                             'flow_calib_extruders': [True] * n},
        }
        self.config_path = '%s/print_task.json' % sim.tmp
        self.print_task_config_2 = {'nozzle_temp': [0] * 32, 'filament_type': [None] * 32}
        self.filament_info_backup = {
            'filament_vendor': ['NONE'] * n, 'filament_type': ['NONE'] * n,
            'filament_sub_type': ['NONE'] * n, 'filament_soft': [False] * n,
            'filament_color': [0xFFFFFFFF] * n, 'filament_color_rgba': ['FFFFFFFF'] * n,
            'filament_color_multi': [{}] * n}
        g = sim.gcode
        g.register_command('INNER_CHECK_AND_RELOAD_FILAMENT_INFO', self.cmd_check_reload)
        for name in ('SET_PRINT_USED_EXTRUDERS', 'SET_PRINT_EXTRUDER_MAP',
                     'INNER_AUTO_REPLENISH_FILAMENT', 'FILAMENT_DT_CLEAR',
                     'FLOW_RESET_K', 'SET_PRINT_FILAMENT_CONFIG', 'FILAMENT_DT_UPDATE',
                     'SET_PRINT_PREFERENCES'):
            g.register_command(name, lambda gcmd: None)

    def get_status(self, eventtime=None):
        return copy.deepcopy(self.print_task_config)

    def set_head(self, head, vendor='Generic', ftype='PLA', subtype='Basic', exist=True):
        cfg = self.print_task_config
        cfg['filament_vendor'][head] = vendor
        cfg['filament_type'][head] = ftype
        cfg['filament_sub_type'][head] = subtype
        cfg['filament_exist'][head] = exist

    REPRINT_KEYS = ('extruder_map_table', 'extruders_used', 'end_unload_filament',
                    'flow_calib_extruders')

    def apply_reprint_info(self):
        """Power-loss restore: the print's own settings come back from the saved copy."""
        cfg = self.print_task_config
        for key in self.REPRINT_KEYS:
            cfg[key] = copy.deepcopy(cfg['reprint_info'][key])

    def reset_print_info(self):
        cfg = self.print_task_config
        n = PHYSICAL_EXTRUDER_NUM
        defaults = {'extruder_map_table': list(range(n)) + [0] * 28,
                    'extruders_used': [False] * n, 'end_unload_filament': [False] * n,
                    'flow_calib_extruders': [True] * n}
        for key, value in defaults.items():
            cfg[key] = copy.deepcopy(value)
            cfg['reprint_info'][key] = copy.deepcopy(value)

    def backup_filament_info(self, extruder_index=None):
        cfg = self.print_task_config
        indices = range(PHYSICAL_EXTRUDER_NUM) if extruder_index is None else [extruder_index]
        for i in indices:
            if extruder_index is None or cfg['filament_vendor'][i] not in ('', 'NONE'):
                for key in self.KEYS:
                    self.filament_info_backup[key][i] = copy.deepcopy(cfg[key][i])

    def cmd_check_reload(self, gcmd):
        index = gcmd.get_int('EXTRUDER', minval=0, maxval=3)
        is_runout = gcmd.get_int('IS_RUNOUT')
        cfg = self.print_task_config
        if cfg['filament_type'][index] not in ('', 'NONE'):
            return
        backup = self.filament_info_backup
        if is_runout and backup['filament_type'][index] not in ('', 'NONE'):
            for key in self.KEYS:
                cfg[key][index] = copy.deepcopy(backup[key][index])
            cfg['filament_official'][index] = False
            cfg['filament_sku'][index] = 0
        if cfg['extruders_used'][index] and cfg['filament_type'][index] in ('', 'NONE'):
            raise gcmd.error('e%d not edit filament' % index, action='pause',
                             id=523, index=index, code=39, oneshot=1, level=2)


class GcodeMove(object):
    """gcode_move.py. Extrusion moves the head's gears in 5 mm steps and spends
    virtual time at the feed rate. ``printing_e_pos`` accumulates only while printing
    (gcode_move.py:156, clamped at 0) and is reset by G92. # MODEL: moves block until
    done, so ``M400`` and ``wait_moves`` are no-ops."""

    def __init__(self, sim):
        self.sim = sim
        self.absolute_extrude = True
        self.absolute_coord = True
        self.last_e = 0.0
        self.speed = 25.0
        self.saved = {}
        g = sim.gcode
        g.register_command('G0', self.cmd_G1)
        g.register_command('G1', self.cmd_G1)
        g.register_command('G90', lambda gcmd: setattr(self, 'absolute_coord', True))
        g.register_command('G91', lambda gcmd: setattr(self, 'absolute_coord', False))
        g.register_command('M82', lambda gcmd: setattr(self, 'absolute_extrude', True))
        g.register_command('M83', lambda gcmd: setattr(self, 'absolute_extrude', False))
        g.register_command('G92', self.cmd_G92)
        g.register_command('SAVE_GCODE_STATE', self.cmd_save)
        g.register_command('RESTORE_GCODE_STATE', self.cmd_restore)

    def get_status(self, eventtime=None):
        pos = self.sim.toolhead.position
        return {'gcode_position': Coord(*pos),
                'absolute_extrude': self.absolute_extrude, 'speed': self.speed}

    def cmd_G92(self, gcmd):
        if gcmd.get('E', None) is not None:
            self.last_e = gcmd.get_float('E')
        self.sim.toolhead.get_extruder().printing_e_pos = 0.0

    def cmd_save(self, gcmd):
        self.saved[gcmd.get('NAME', 'default')] = {
            'abs_e': self.absolute_extrude, 'abs': self.absolute_coord,
            'pos': list(self.sim.toolhead.position), 'speed': self.speed}

    def cmd_restore(self, gcmd):
        name = gcmd.get('NAME', 'default')
        state = self.saved.get(name)
        if state is None:
            raise gcmd.error("Unknown g-code state: %s" % name)
        self.absolute_extrude = state['abs_e']
        self.absolute_coord = state['abs']
        if gcmd.get_int('MOVE', 0):
            self.sim.toolhead.position[:3] = state['pos'][:3]

    def cmd_G1(self, gcmd):
        sim = self.sim
        params = gcmd.get_command_parameters()
        if params.get('F'):
            self.speed = float(params['F']) / 60.0
        position = sim.toolhead.position
        for index, axis in enumerate('XYZ'):
            if params.get(axis):
                value = float(params[axis])
                position[index] = value if self.absolute_coord else position[index] + value
        if params.get('E'):
            value = float(params['E'])
            if self.absolute_extrude:
                delta, self.last_e = value - self.last_e, value
            else:
                delta = value
            # a G1 straight from the print file (not from a macro) is the sliced print
            body = sim.sdcard.is_cmd_from_sd() and sim.gcode.depth == 1
            self.extrude(delta, body)
        else:
            sim.reactor.pause(sim.reactor.monotonic() + 0.05)

    BODY_RETRACT_CREDIT_MAX_MM = 12.0

    def extrude(self, delta, body=False):
        sim = self.sim
        extruder = sim.toolhead.get_extruder()
        if delta > 0 and not extruder.can_extrude():
            raise K.CommandError("Extrude below minimum temp")
        if sim.print_stats.state == 'printing':
            extruder.printing_e_pos = min(0.0, extruder.printing_e_pos + delta)
        remaining = abs(delta)
        sign = 1.0 if delta >= 0 else -1.0
        printing = body and sim.print_stats.state == 'printing' and delta > 0
        if body and sim.print_stats.state == 'printing' and delta < 0:
            # MODEL: the print's own retraction (G1 E-0.8 before a travel, E-10 at a
            # tool change). The un-retract that follows refills the nozzle rather than
            # printing, so it is not dry printing. Capped, so a load that stopped short
            # of the nozzle still shows.
            extruder.body_retract_credit = min(
                self.BODY_RETRACT_CREDIT_MAX_MM,
                getattr(extruder, 'body_retract_credit', 0.0) + abs(delta))
        while remaining > 1e-9:
            step = min(5.0, remaining)
            try:
                _moved, flow = sim.world.gear_move(extruder.index, sign * step)
            except W.WorldError:
                raise K.CommandError('Extrude below minimum temp')
            if printing:
                credit = min(max(step - flow, 0.0),
                             getattr(extruder, 'body_retract_credit', 0.0))
                if credit:
                    extruder.body_retract_credit -= credit
                extruder.print_commanded += step
                extruder.print_flow += flow + credit
            extruder.total_e += step
            sim.reactor.pause(sim.reactor.monotonic() + step / max(self.speed, 0.1))
            remaining -= step


class Macro(object):
    """A ``gcode_macro NAME`` object; only ``variables`` is read by BMCU."""

    def __init__(self, variables=None):
        self.variables = dict(variables or {})


class SnapmakerMacros(object):
    """The macros and commands BMCU-Klipper triggers, as Python."""

    NOOPS = (
        'M106', 'M107', 'M204', 'M220', 'M400', 'M84', 'M141', 'M140', 'M190',
        'SET_PURIFIER_MODE', 'TIMELAPSE_STOP', 'SET_PAUSE_NEXT_LAYER',
        'SET_PAUSE_AT_LAYER', 'M118', 'GET_POSITION', 'SET_IDLE_TIMEOUT',
        'INNER_APPLY_FLOW_K', 'SM_PRINT_FLOW_CALIBRATE', 'SET_LED',
        'SET_ACTION_CODE_LOG', 'ACTIVATE_EXTRUDER', 'BED_MESH_CLEAR', 'G4',
        'SM_PRINT_START_LINE', 'SM_PRINT_CHECK_SWITCH_EXTRUDER_SKIP',
        'SET_PRINT_STATS_INFO', 'EXCLUDE_OBJECT_DEFINE', 'SET_FAN_SPEED',
        'M221', 'M900', 'SET_GCODE_OFFSET', 'SET_VELOCITY_LIMIT', 'M205', 'M201')
    CLEANING = (
        'INNER_CUTOFF_BASE_DISCARD', 'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD',
        'INNER_DISCARD_FILAMENT_BASE_DISCARD', 'ROUGHLY_CLEAN_NOZZLE',
        'ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD', 'FINELY_CLEAN_NOZZLE_STAGE_1',
        'FINELY_CLEAN_NOZZLE_STAGE_2', 'INNER_FLOW_MEASURE_END_BASE_DISCARD')

    def __init__(self, sim):
        self.sim = sim
        self.g = sim.gcode
        self.log = []           # cleaning macros and other no-op events, in order
        self.paused = False
        self.pause_command_sent = False
        self.sd_paused = False
        self.resume_macro = Macro({
            'last_extruder_index': 0, 'last_extruder_temp': 0,
            'extruder0_temp': 0, 'extruder1_temp': 0, 'extruder2_temp': 0,
            'extruder3_temp': 0, 'is_pause_on_err': False, 'is_runout': 0,
            'flow_calibrating': False, 'pos_e_compensation': False})
        self.macros = {'INNER_RESUME': self.resume_macro}
        sim.printer.add_object('gcode_macro INNER_RESUME', self.resume_macro)
        g = self.g
        for name in self.NOOPS:
            g.register_command(name, lambda gcmd, n=name: None)
        for name in self.CLEANING:
            g.register_command(name, lambda gcmd, n=name: self.log.append(n))
        reg = g.register_command
        reg('G28', self.cmd_G28)
        for head in range(4):
            reg('T%d' % head, self.cmd_Tn)
            reg('PARK_EXTRUDER%d' % head, lambda gcmd, h=head: self.park(h))
        for tool in range(4, 32):
            # [gcode_macro T4..T31]: SWITCH_OF_EXTENDED_EXTRUDER INDEX=n picks the
            # physical head from extruder_map_table
            reg('T%d' % tool, self.cmd_extended_tool)
        reg('M104', self.cmd_M104)
        reg('M109', self.cmd_M109)
        reg('SET_HEATER_TEMPERATURE', self.cmd_SET_HEATER_TEMPERATURE)
        reg('TEMPERATURE_WAIT', self.cmd_TEMPERATURE_WAIT)
        reg('TURN_OFF_HEATERS', self.cmd_turn_off)
        reg('MOVE_TO_XY_IDLE_POSITION_EXTRUDER', self.cmd_move_idle)
        reg('MOVE_TO_DISCARD_FILAMENT_POSITION', self.cmd_move_discard)
        reg('SET_ACTION_CODE', self.cmd_SET_ACTION_CODE)
        reg('SET_MAIN_STATE', self.cmd_SET_MAIN_STATE)
        reg('SET_GCODE_VARIABLE', self.cmd_SET_GCODE_VARIABLE)
        reg('RESPOND', lambda gcmd: self.sim.gcode.respond_info(gcmd.get('MSG', '')))
        reg('RAISE_EXCEPTION', self.cmd_RAISE_EXCEPTION)
        reg('CHECK_FILAMENT_RUNOUT', self.cmd_CHECK_FILAMENT_RUNOUT)
        reg('INNER_FLUSH_FILAMENT', self.cmd_flush)
        reg('INNER_PREEXTRUDE_FILAMENT', self.cmd_preextrude)
        reg('SM_PRINT_PREEXTRUDE_FILAMENT', self.cmd_preextrude)
        reg('SM_PRINT_EXTRUDER_PREHEAT', self.cmd_preheat)
        reg('SM_PRINT_AUTO_FEED', self.cmd_sm_auto_feed)
        reg('SM_PRINT_CHECK_SWITCH_EXTRUDER', self.cmd_check_switch)
        reg('_CLIENT_RETRACT', self.cmd_client_retract)
        reg('_CLIENT_EXTRUDE', self.cmd_client_extrude)
        reg('INNER_FILAMENT_UNLOAD', self.cmd_inner_unload)
        reg('INNER_PRINT_END', lambda gcmd: None)
        # pause_resume.py and the INNER_* macros of fluidd.cfg
        reg('PAUSE', self.cmd_PAUSE)
        reg('INNER_PAUSE', self.cmd_INNER_PAUSE)
        reg('PAUSE_BASE', self.cmd_PAUSE_BASE)
        reg('RESUME', self.cmd_RESUME)
        reg('INNER_RESUME', self.cmd_INNER_RESUME)
        reg('RESUME_BASE', self.cmd_RESUME_BASE)
        reg('CLEAR_PAUSE', self.cmd_CLEAR_PAUSE)
        reg('CANCEL_PRINT', self.cmd_CANCEL_PRINT)
        reg('CANCEL_PRINT_BASE', self.cmd_CANCEL_PRINT_BASE)

    # -- basic printer commands ---------------------------------------------
    def spend(self, seconds):
        self.sim.reactor.pause(self.sim.reactor.monotonic() + seconds)

    def cmd_G28(self, gcmd):
        params = gcmd.get_command_parameters()
        axes = ''.join(a.lower() for a in 'XYZ' if a in params) or 'xyz'
        self.sim.printer.send_event('homing:homing_move_begin', None)
        self.spend(4.0)
        self.sim.printer.send_event('homing:homing_move_end', None)
        homed = self.sim.toolhead.homed_axes
        self.sim.toolhead.homed_axes = ''.join(sorted(set(homed) | set(axes)))
        self.log.append('G28 ' + axes)

    def park(self, head):
        self.sim.extruders[head].park_state = 'parked'
        self.log.append('PARK_EXTRUDER%d' % head)

    def cmd_Tn(self, gcmd):
        head = int(gcmd.get_command()[1:])
        toolhead = self.sim.toolhead
        if toolhead.active != head:
            self.park(toolhead.active)
            self.spend(3.0)
            toolhead.active = head
        else:
            self.spend(1.0)               # MODEL: re-selecting the active head still moves
        self.sim.extruders[head].park_state = 'active'
        self.log.append('T%d' % head)

    def cmd_extended_tool(self, gcmd):
        tool = int(gcmd.get_command()[1:])
        table = self.sim.print_task_config.print_task_config['extruder_map_table']
        self.g.run_script_from_command('T%d A0' % int(table[tool]))

    def _head_of(self, gcmd, default=None):
        t = gcmd.get_int('T', None)
        if t is None:
            return self.sim.toolhead.active
        # Snapmaker maps a logical tool to a physical head with extruder_map_table
        table = self.sim.print_task_config.print_task_config['extruder_map_table']
        return int(table[t]) if 0 <= t < len(table) else t

    def cmd_M104(self, gcmd):
        temp = gcmd.get_float('S', 0.0)
        head = self._head_of(gcmd)
        self.sim.extruders[head].heater.set_target(temp)

    def cmd_M109(self, gcmd):
        temp = gcmd.get_float('S', 0.0)
        head = self._head_of(gcmd)
        heater = self.sim.extruders[head].heater
        heater.set_target(temp)
        if temp > 0:
            heater.wait(temp)

    def cmd_SET_HEATER_TEMPERATURE(self, gcmd):
        name = gcmd.get('HEATER')
        heater = self.sim.printer.lookup_object('heaters').heaters.get(name)
        if heater is None:
            raise gcmd.error("Unknown heater '%s'" % name)
        heater.set_target(gcmd.get_float('TARGET', 0.0))

    def cmd_TEMPERATURE_WAIT(self, gcmd):
        name = gcmd.get('SENSOR')
        heater = self.sim.printer.lookup_object('heaters').heaters.get(name)
        if heater is None:
            raise gcmd.error("Unknown sensor '%s'" % name)
        minimum = gcmd.get_float('MINIMUM', None)
        if minimum is not None:
            heater.wait(minimum)

    def cmd_turn_off(self, gcmd):
        for ext in self.sim.extruders:
            ext.heater.set_target(0.0)

    IDLE_XY = (30.0, 5.0)

    def cmd_move_idle(self, gcmd):
        self.spend(2.0)
        self.sim.toolhead.position[0:2] = [
            self.IDLE_XY[0] + gcmd.get_float('X_OFFSET', 0.0),
            self.IDLE_XY[1] + gcmd.get_float('Y_OFFSET', 0.0)]
        self.log.append('MOVE_TO_XY_IDLE_POSITION_EXTRUDER')

    def cmd_move_discard(self, gcmd):
        # fluidd.cfg: the idle position plus X_OFFSET=3.5/-1.5 and Y_OFFSET=50.5
        x_offset = 3.5 if self.sim.toolhead.active == 0 else -1.5
        self.g.run_script_from_command(
            'MOVE_TO_XY_IDLE_POSITION_EXTRUDER X_OFFSET=%s Y_OFFSET=50.5' % x_offset)
        self.log.append('MOVE_TO_DISCARD_FILAMENT_POSITION')

    def cmd_SET_ACTION_CODE(self, gcmd):
        self.sim.machine_state.action_code = gcmd.get('ACTION', 'IDLE')

    def cmd_SET_MAIN_STATE(self, gcmd):
        self.sim.machine_state.main_state = gcmd.get('MAIN_STATE', 'IDLE')
        if gcmd.get('ACTION', None) is not None:
            self.sim.machine_state.action_code = gcmd.get('ACTION')

    def cmd_SET_GCODE_VARIABLE(self, gcmd):
        macro = self.macros.get(gcmd.get('MACRO'))
        if macro is None:
            return
        raw = gcmd.get('VALUE')
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = {'True': True, 'False': False}.get(raw, raw)
        macro.variables[gcmd.get('VARIABLE')] = value

    def cmd_RAISE_EXCEPTION(self, gcmd):
        raise gcmd.error(gcmd.get('MSG', 'exception'), id=gcmd.get_int('ID', 0),
                         index=gcmd.get_int('INDEX', 0), code=gcmd.get_int('CODE', 0),
                         level=gcmd.get_int('LEVEL', 2))

    def cmd_CHECK_FILAMENT_RUNOUT(self, gcmd):
        # MODEL: with filament on the entry switch the check passes; otherwise it is
        # a runout error that pauses the print again.
        name = gcmd.get('SENSOR', '')
        head = int(''.join(c for c in name.split('_')[0] if c.isdigit()) or 0)
        sensor = self.sim.sensors[head]
        if not sensor.runout_helper.filament_present:
            raise gcmd.error('e%d filament runout' % head, action='pause_runout',
                             id=523, index=head, code=0, oneshot=1, level=2)

    # -- extrusion helpers -----------------------------------------------------
    def cmd_client_retract(self, gcmd):
        length = abs(gcmd.get_float('LENGTH', 5.0))
        self.g.run_script_from_command('M83\nG1 E-%.4f F2100' % length)

    def cmd_client_extrude(self, gcmd):
        length = abs(gcmd.get_float('LENGTH', 5.0))
        self.g.run_script_from_command('M83\nG1 E%.4f F120' % length)

    def cmd_flush(self, gcmd):
        """INNER_FLUSH_FILAMENT: purge 80 mm, then retract |printing_e_pos| (while
        printing or paused) or 5 mm, then heater off. Incident 3 hangs on this."""
        sim = self.sim
        temp = gcmd.get_int('TEMP', 250)
        soft = gcmd.get_int('SOFT', 0)
        length = gcmd.get_float('LENGTH', 80.0)
        retract = gcmd.get_float('RETRACT_LENGTH', 5.0 if soft == 0 else 2.0)
        extruder = sim.toolhead.get_extruder()
        if sim.print_stats.state in ('paused', 'printing'):
            retract = abs(extruder.printing_e_pos)
        g = self.g
        for name in ('INNER_CUTOFF_BASE_DISCARD', 'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD',
                     'INNER_DISCARD_FILAMENT_BASE_DISCARD'):
            g.run_script_from_command(name)
        g.run_script_from_command('M109 S%d\nM83\nG1 E%.4f F360' % (temp, length))
        g.run_script_from_command('M104 S0')
        self.cmd_client_retract_value(retract)
        self.log.append('FLUSH retract=%.1f' % retract)

    def cmd_client_retract_value(self, length):
        if length > 0:
            self.g.run_script_from_command('M83\nG1 E-%.4f F2100' % length)

    def cmd_preextrude(self, gcmd):
        temp = gcmd.get_int('TEMP', 220)
        length = gcmd.get_float('LENGTH', 20.0)
        retract = abs(gcmd.get_float('RETRACT_LENGTH', 0.5))
        g = self.g
        g.run_script_from_command('SAVE_GCODE_STATE NAME=preextrude')
        g.run_script_from_command('MOVE_TO_DISCARD_FILAMENT_POSITION')
        g.run_script_from_command('M109 S%d\nM83\nG1 E%.4f F360\nG1 E-%.4f' % (temp, length, retract))
        g.run_script_from_command('MOVE_TO_XY_IDLE_POSITION_EXTRUDER')
        g.run_script_from_command('RESTORE_GCODE_STATE NAME=preextrude')
        self.log.append('PREEXTRUDE')

    def cmd_preheat(self, gcmd):
        head = gcmd.get_int('EXTRUDER', 999)
        temp = gcmd.get_int('TEMP', 140)
        cfg = self.sim.print_task_config.print_task_config
        if 0 <= head < PHYSICAL_EXTRUDER_NUM and cfg['extruders_used'][head]:
            self.g.run_script_from_command('M104 S%d T%d A0' % (temp, head))

    def cmd_check_switch(self, gcmd):
        used = self.sim.print_task_config.print_task_config['extruders_used']
        self.g.run_script_from_command('SET_ACTION_CODE ACTION=PRINT_SWITCH_CHECKING')
        for head in range(PHYSICAL_EXTRUDER_NUM):
            if used[head]:
                self.g.run_script_from_command('T%d A0' % head)
        self.g.run_script_from_command('SET_ACTION_CODE ACTION=IDLE')

    def cmd_inner_unload(self, gcmd):
        self.log.append('INNER_FILAMENT_UNLOAD')

    def cmd_sm_auto_feed(self, gcmd):
        head = gcmd.get_int('EXTRUDER', 999)
        cfg = self.sim.print_task_config.print_task_config
        if not (0 <= head < PHYSICAL_EXTRUDER_NUM):
            self.sim.gcode.respond_info('extruder error: %d!' % head)
            return
        if cfg['extruders_used'][head]:
            module = 'left' if head in F.FEEDERS['left'] else 'right'
            channel = F.FEEDERS[module].index(head)
            self.g.run_script_from_command(
                'FEED_AUTO MODULE=%s CHANNEL=%d LOAD=1 PRINTING=1 %s' %
                (module, channel, gcmd.get_raw_command_parameters()))

    # -- pause / resume (pause_resume.py + fluidd.cfg) -------------------------
    def cmd_PAUSE(self, gcmd):
        stats = self.sim.print_stats
        if stats.state not in ('printing', 'paused'):
            raise gcmd.error('!! Cannot pause while not printing: %s' % stats.state,
                             action='none', id=531, index=0, code=27, oneshot=1, level=1)
        if self.paused:
            return
        self.sim.gcode.respond_info('Pausing...')
        try:
            self.g.run_script_from_command('INNER_PAUSE %s' % gcmd.get_raw_command_parameters())
        except K.CommandError as exc:
            raise gcmd.error('Unable to pause print: %s' % exc)
        finally:
            self.g.run_script_from_command('SET_ACTION_CODE ACTION=IDLE')

    def cmd_PAUSE_BASE(self, gcmd):
        if self.paused:
            return
        if not self.pause_command_sent:
            if self.sim.sdcard.is_active():
                self.sd_paused = True
                self.sim.sdcard.do_pause()
            self.pause_command_sent = True
        self.g.run_script_from_command('SAVE_GCODE_STATE NAME=PAUSE_STATE')
        self.paused = True

    def cmd_INNER_PAUSE(self, gcmd):
        sim = self.sim
        toolhead = sim.toolhead
        variables = self.resume_macro.variables
        extruder = toolhead.get_extruder()
        variables['last_extruder_index'] = extruder.index
        variables['last_extruder_temp'] = extruder.heater.target_temp
        for i, ext in enumerate(sim.extruders):
            variables['extruder%d_temp' % i] = ext.heater.target_temp
        on_error = gcmd.get_int('ON_ERROR', 0)
        is_runout = gcmd.get_int('IS_RUNOUT', 0)
        self.g.run_script_from_command('PAUSE_BASE')
        self.g.run_script_from_command('_CLIENT_RETRACT')
        if not on_error:
            if 'xyz' in toolhead.homed_axes:
                self.g.run_script_from_command('MOVE_TO_DISCARD_FILAMENT_POSITION')
        if is_runout and 'xyz' in toolhead.homed_axes and extruder.heater.target_temp >= 170:
            self.g.run_script_from_command(
                'M109 S%d\nM83\nG1 E10 F150\n_CLIENT_RETRACT' % extruder.heater.target_temp)
        if sim.print_stats.state == 'printing':
            variables['pos_e_compensation'] = True
        for i, ext in enumerate(sim.extruders):
            if ext.heater.target_temp > 0:
                self.g.run_script_from_command('M104 S40 T%d A0' % i)
        if is_runout:
            variables['is_runout'] = 1
        elif on_error:
            variables['is_pause_on_err'] = True

    def cmd_RESUME(self, gcmd):
        sim = self.sim
        if sim.print_stats.state not in ('paused',):
            raise gcmd.error('!! Cannot resume while not paused: %s' % sim.print_stats.state,
                             action='none', id=531, index=0, code=26, oneshot=1, level=1)
        msm = sim.machine_state
        if str(msm.main_state) != 'PRINTING' or str(msm.action_code) != 'IDLE':
            raise gcmd.error('!! Cannot resume while machine main_state: %s or action_code: %s'
                             % (msm.main_state, msm.action_code))
        if not self.paused:
            raise gcmd.error('!! Not in paused state and cannot be resumed!')
        try:
            self.sim.gcode.respond_info('Resuming...')
            self.g.run_script_from_command('INNER_RESUME %s' % gcmd.get_raw_command_parameters())
        except Exception:
            self.sim.gcode.respond_info('!! Resumes error!')
            self.g.run_script_from_command('SET_ACTION_CODE ACTION=IDLE')
            for i, ext in enumerate(sim.extruders):
                if ext.heater.target_temp > 0:
                    self.g.run_script_from_command('M104 S40 T%d A0' % i)
            raise

    def cmd_RESUME_BASE(self, gcmd):
        if not self.paused:
            return
        self.g.run_script_from_command(
            'RESTORE_GCODE_STATE NAME=PAUSE_STATE MOVE=%d' % gcmd.get_int('MOVE', 1))
        if self.sd_paused:
            self.sim.sdcard.do_resume()
            self.sd_paused = False
        self.pause_command_sent = False
        self.paused = False

    def cmd_CLEAR_PAUSE(self, gcmd):
        self.paused = self.pause_command_sent = False

    def cmd_CANCEL_PRINT(self, gcmd):
        # fluidd.cfg: retract, INNER_PRINT_END, CANCEL_PRINT_BASE, heaters off
        self.g.run_script_from_command('_CLIENT_RETRACT')
        self.g.run_script_from_command('INNER_PRINT_END')
        self.g.run_script_from_command('CANCEL_PRINT_BASE')
        self.g.run_script_from_command('TURN_OFF_HEATERS')
        self.resume_macro.variables.update(is_pause_on_err=False, pos_e_compensation=False)

    def cmd_CANCEL_PRINT_BASE(self, gcmd):
        sim = self.sim
        if sim.sdcard.is_active() or self.sd_paused:
            sim.sdcard.do_cancel()
        self.paused = self.pause_command_sent = False

    def cmd_INNER_RESUME(self, gcmd):
        """lava/fluidd.cfg INNER_RESUME, step by step. Most unresumable bugs live here."""
        sim = self.sim
        v = self.resume_macro.variables
        is_replenish = gcmd.get_int('REPLENISH', 0)
        retract = 5.0
        unretract = 0.0
        if is_replenish:
            v['last_extruder_index'] = gcmd.get_int('REPLENISH_EXTRUDER')
        last = int(v['last_extruder_index'])
        e_pos = abs(sim.extruders[last].printing_e_pos)
        if v['pos_e_compensation']:
            e_pos = e_pos - retract if e_pos >= retract else 0
        if retract >= e_pos:
            unretract = unretract - e_pos if unretract >= e_pos else 0
        else:
            retract = e_pos + retract - unretract
            unretract = 0
        sensor_name = 'e%d_filament' % last
        homed = sim.toolhead.homed_axes
        homed_z = 'z' in homed
        cfg = sim.print_task_config.print_task_config
        is_filament_exist = bool(cfg['filament_exist'][last])
        g = self.g
        if not is_replenish and is_filament_exist:
            g.run_script_from_command(
                'INNER_CHECK_AND_RELOAD_FILAMENT_INFO EXTRUDER=%d IS_RUNOUT=%s' %
                (last, v['is_runout']))
        can_resume = is_filament_exist
        if not can_resume:
            raise gcmd.error('e%d filament runout' % last, id=523, index=last, code=0,
                             level=2)
        g.run_script_from_command(
            'SET_ACTION_CODE ACTION=%s' % ('PRINT_REPLENISHING' if is_replenish else 'PRINT_RESUMING'))
        if v['is_pause_on_err'] or 'x' not in sim.toolhead.homed_axes or 'y' not in sim.toolhead.homed_axes:
            g.run_script_from_command('G28 X Y')
        g.run_script_from_command('T%d A0' % last)
        g.run_script_from_command('SM_PRINT_AUTO_FEED EXTRUDER=%d' % last)
        g.run_script_from_command('MOVE_TO_DISCARD_FILAMENT_POSITION')
        if homed_z:
            g.run_script_from_command('G4 P200\nM400\nCHECK_FILAMENT_RUNOUT SENSOR=%s' % sensor_name)
            g.run_script_from_command('M107')
            for i in range(4):
                g.run_script_from_command('M104 S%s T%d A0' % (v['extruder%d_temp' % i], i))
            for i in range(4):
                if v['extruder%d_temp' % i] >= 170:
                    g.run_script_from_command('M109 S%s T%d A0' % (v['extruder%d_temp' % i], i))
            last_temp = v['last_extruder_temp']
            if last_temp > 170:
                g.run_script_from_command('M109 S%s T%d A0' % (last_temp, last))
                g.run_script_from_command('_CLIENT_EXTRUDE LENGTH=20 SPEED=2')
                self.cmd_client_retract_value(retract)
                g.run_script_from_command('M400\nM106 S255')
            else:
                g.run_script_from_command('M106 S255')
                if last_temp < 80:
                    g.run_script_from_command('M104 S%s T%d A0' % (last_temp, last))
                else:
                    g.run_script_from_command('M109 S%s T%d A0' % (last_temp, last))
                unretract = 0.0
            for name in ('INNER_CUTOFF_BASE_DISCARD', 'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD',
                         'INNER_DISCARD_FILAMENT_BASE_DISCARD'):
                g.run_script_from_command(name)
            g.run_script_from_command('G90\nG1 Y250 F12000')
            g.run_script_from_command('CHECK_FILAMENT_RUNOUT SENSOR=%s' % sensor_name)
            g.run_script_from_command('RESUME_BASE MOVE_ACCEL=10000 EXTRUDE=%s' % unretract)
        else:
            g.run_script_from_command('RESUME_BASE MOVE=0')
        v['is_pause_on_err'] = False
        v['pos_e_compensation'] = False
        v['is_runout'] = 0
        g.run_script_from_command('SET_ACTION_CODE ACTION=IDLE')

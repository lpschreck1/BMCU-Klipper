"""Fake Klipper core: printer, gcode, heaters, extruders, toolhead.

Only what BMCU-Klipper and the Snapmaker modules touch. Time is virtual: waiting
(`M109`, `G4`, extrusion at a feedrate) calls ``reactor.pause`` which advances the
world. See docs/dev/SIMULATOR_PLAN.md section 3.5.
"""
import re

from . import world as W



def _split_gcode_params(eargs):
    """Snapmaker Klipper's KEY=VALUE splitter (klippy/gcode.py)."""
    result, current = [], []
    in_quote, quote_char = False, None
    for ch in eargs:
        if in_quote:
            if ch == quote_char:
                in_quote = False
            else:
                current.append(ch)
        elif ch in ('"', "'"):
            in_quote, quote_char = True, ch
        elif ch in (' ', '\t'):
            if current:
                result.append(''.join(current))
                current = []
        else:
            current.append(ch)
    if current:
        result.append(''.join(current))
    return result

class CommandError(Exception):
    """Klipper's CommandError, with Snapmaker's coded-exception fields."""

    def __init__(self, message='', action=None, id=0, index=0, code=0,
                 oneshot=0, level=2):
        Exception.__init__(self, message)
        self.action = action
        self.id = id
        self.index = index
        self.code = code
        self.oneshot = oneshot
        self.level = level


class ConfigError(Exception):
    pass


_ARGS_R = re.compile(r'([A-Z_]+|[A-Z*/])')


class GCodeCommand(object):
    error = CommandError

    def __init__(self, gcode, command, commandline, params):
        self._gcode = gcode
        self._command = command
        self._commandline = commandline
        self._params = params
        self.respond_info = gcode.respond_info
        self.respond_raw = gcode.respond_raw

    def get_command(self):
        return self._command

    def get_commandline(self):
        return self._commandline

    def get_command_parameters(self):
        return self._params

    def get_raw_command_parameters(self):
        raw = self._commandline
        upper = raw.upper()
        index = upper.find(self._command)
        raw = raw[index + len(self._command):] if index >= 0 else ''
        return raw[1:] if raw.startswith(' ') else raw

    def get(self, name, default=None, parser=str, minval=None, maxval=None,
            above=None, below=None):
        value = self._params.get(name)
        if value is None:
            if default is None and False:
                raise self.error("Error on '%s': missing %s" %
                                 (self._commandline, name))
            return default
        try:
            value = parser(value)
        except Exception:
            raise self.error("Error on '%s': unable to parse %s" %
                             (self._commandline, value))
        if minval is not None and value < minval:
            raise self.error("Error on '%s': %s must have minimum of %s" %
                             (self._commandline, name, minval))
        if maxval is not None and value > maxval:
            raise self.error("Error on '%s': %s must have maximum of %s" %
                             (self._commandline, name, maxval))
        if above is not None and value <= above:
            raise self.error("Error on '%s': %s must be above %s" %
                             (self._commandline, name, above))
        if below is not None and value >= below:
            raise self.error("Error on '%s': %s must be below %s" %
                             (self._commandline, name, below))
        return value

    def get_int(self, name, default=None, minval=None, maxval=None):
        return self.get(name, default, parser=int, minval=minval, maxval=maxval)

    def get_float(self, name, default=None, minval=None, maxval=None,
                  above=None, below=None):
        return self.get(name, default, parser=float, minval=minval,
                        maxval=maxval, above=above, below=below)


class _Mutex(object):
    def test(self):
        return False


class FakeGcode(object):
    error = CommandError

    def __init__(self, printer):
        self.printer = printer
        self.ready_gcode_handlers = {}
        self.base_gcode_handlers = self.ready_gcode_handlers
        self.output = []              # every respond_info / respond_raw line
        self.history = []             # every command dispatched, in order
        self.on_dispatch = None       # hook(command, commandline)
        self._mutex = _Mutex()
        self.mux_commands = {}
        self.depth = 0
        self.running = []             # command lines being executed right now (any greenlet)

    def get_mutex(self):
        return self._mutex

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        old = self.ready_gcode_handlers.get(cmd)
        if func is None:
            self.ready_gcode_handlers.pop(cmd, None)
        else:
            self.ready_gcode_handlers[cmd] = func
        return old

    def register_mux_command(self, cmd, key, value, func, desc=None):
        """Klipper's mux commands: one handler per value of ``key``."""
        prev = self.mux_commands.get(cmd)
        if prev is None:
            prev = self.mux_commands[cmd] = (key, {})

            def handler(gcmd, cmd=cmd):
                key_name, table = self.mux_commands[cmd]
                selected = gcmd.get(key_name, None)
                if selected not in table:
                    raise gcmd.error('Unknown %s %s' % (key_name, selected))
                table[selected](gcmd)
            self.register_command(cmd, handler)
        prev[1][value] = func

    def respond_info(self, msg, log=True):
        for line in str(msg).split('\n'):
            self.output.append(line)

    def respond_raw(self, msg):
        self.output.append(str(msg))

    def parse(self, line):
        line = line.strip()
        if ';' in line:
            line = line[:line.index(';')].strip()
        if not line:
            return None
        parts = line.split()
        command = parts[0].upper()
        params = {}
        if any('=' in part for part in parts[1:]) or (
                len(command) > 1 and command[0] not in 'GMT'):
            # Snapmaker's klippy/gcode.py _split_gcode_params: quotes are removed
            # and spaces inside them kept (EXPECT_MATERIAL="PLA" reads PLA).
            for part in _split_gcode_params(line[len(parts[0]):].strip()):
                if '=' in part:
                    key, value = part.split('=', 1)
                    params[key.upper()] = value
                else:
                    params[part.upper()] = ''
        else:
            # Klipper's traditional parameters: letter followed by value
            args = _ARGS_R.split(' '.join(parts[1:]).upper())
            for i in range(1, len(args) - 1, 2):
                params[args[i]] = args[i + 1].strip()
        return command, line, params

    def run_script_from_command(self, script):
        for line in str(script).replace('\r', '').split('\n'):
            self._dispatch(line)

    run_script = run_script_from_command

    def _dispatch(self, line):
        parsed = self.parse(line)
        if parsed is None:
            return
        command, commandline, params = parsed
        self.history.append(commandline)
        if self.on_dispatch is not None:
            self.on_dispatch(command, commandline)
        handler = self.ready_gcode_handlers.get(command)
        if handler is None:
            raise CommandError('Unknown command:"%s"' % command)
        self.depth += 1
        token = (command, commandline)
        self.running.append(token)
        try:
            handler(GCodeCommand(self, command, commandline, params))
        finally:
            self.depth -= 1
            self.running.remove(token)


class FakeConfig(object):
    """Just enough of a Klipper config section for BMCUManager."""

    error = ConfigError

    def __init__(self, printer, values=None):
        self._printer = printer
        self.values = dict(values or {})

    def get_printer(self):
        return self._printer

    def get(self, name, default=None, note_valid=True):
        return self.values.get(name, default)

    def getint(self, name, default=None, minval=None, maxval=None,
               note_valid=True):
        value = int(self.values.get(name, default))
        if minval is not None and value < minval:
            raise ConfigError('%s below minimum' % name)
        if maxval is not None and value > maxval:
            raise ConfigError('%s above maximum' % name)
        return value

    def getfloat(self, name, default=None, minval=None, maxval=None,
                 above=None, below=None, note_valid=True):
        value = float(self.values.get(name, default))
        if minval is not None and value < minval:
            raise ConfigError('%s below minimum' % name)
        if maxval is not None and value > maxval:
            raise ConfigError('%s above maximum' % name)
        return value

    def getboolean(self, name, default=None, note_valid=True):
        value = self.values.get(name, default)
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)

    def get_name(self):
        return self.values.get('_name', 'bmcu')


class FakeConfigfile(object):
    def __init__(self):
        self.settings = {}

    def get_status(self, eventtime=None):
        return {'settings': self.settings, 'config': self.settings}


class FakePrinter(object):
    """Registry of objects, events and the reactor."""

    def __init__(self, reactor, state_dir=''):
        self.reactor = reactor
        self.objects = {}
        self.event_handlers = {}
        self.start_args = {'config_file': (state_dir + '/printer.cfg')
                           if state_dir else ''}
        self.events = []
        self.gcode = FakeGcode(self)
        self.objects['gcode'] = self.gcode
        self.objects['configfile'] = FakeConfigfile()

    def get_reactor(self):
        return self.reactor

    def get_start_args(self):
        return self.start_args

    def add_object(self, name, obj):
        self.objects[name] = obj
        return obj

    def lookup_object(self, name, *default):
        if name in self.objects:
            return self.objects[name]
        if default:
            return default[0]
        raise ConfigError("Unknown config object '%s'" % name)

    def lookup_objects(self, module=None):
        if module is None:
            return list(self.objects.items())
        prefix = module + ' '
        return [(name, obj) for name, obj in self.objects.items()
                if name == module or name.startswith(prefix)]

    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *params):
        self.events.append((event,) + params)
        results = []
        for callback in list(self.event_handlers.get(event, [])):
            results.append(callback(*params))
        return results

    def extract_encoded_message(self, text):
        return None

    def invoke_shutdown(self, msg):
        raise RuntimeError('printer shutdown: %s' % msg)


class FakeHeaters(object):
    def __init__(self, printer):
        self.printer = printer
        self.heaters = {}

    def set_temperature(self, heater, temp, wait=False):
        heater.set_target(temp)
        if wait and temp:
            heater.wait(temp)

    def lookup_heater(self, name):
        return self.heaters[name]


class FakeHeater(object):
    def __init__(self, reactor, state, name):
        self._reactor = reactor
        self._state = state          # world.Heater
        self.name = name
        self.min_extrude_temp = W.MIN_EXTRUDE_TEMP

    @property
    def target_temp(self):
        return self._state.target

    @property
    def smoothed_temp(self):
        return self._state.temp

    def set_target(self, temp):
        self._state.target = float(temp)

    def get_temp(self, eventtime=None):
        return self._state.temp, self._state.target

    def get_status(self, eventtime=None):
        return {'temperature': self._state.temp, 'target': self._state.target,
                'power': 0.0}

    def wait(self, minimum, timeout=600.0):
        end = self._reactor.monotonic() + timeout
        while self._state.temp < minimum - 1.0:
            if self._reactor.monotonic() > end:
                raise CommandError('heater %s did not reach %s' %
                                   (self.name, minimum))
            self._reactor.pause(self._reactor.monotonic() + 0.5)


class FakeExtruder(object):
    """One physical head's extruder. Moves feed the world's gears."""

    def __init__(self, sim_printer, index, world, heater):
        self.printer = sim_printer
        self.index = index
        self.world = world
        self.name = 'extruder%s' % ('' if index == 0 else index)
        self.heater = heater
        self.printing_e_pos = 0.0     # Snapmaker gcode_move accumulates this
        self.last_position = 0.0
        self.park_state = 'parked'

    def get_name(self):
        return self.name

    def get_heater(self):
        return self.heater

    def can_extrude(self):
        return self.heater.smoothed_temp >= self.heater.min_extrude_temp

    def get_status(self, eventtime=None):
        return {'temperature': self.heater.smoothed_temp,
                'target': self.heater.target_temp,
                'can_extrude': self.can_extrude(),
                'power': 0.0}

    def get_park_detector_status(self):
        return {'state': self.park_state.upper()}

    def get_extruder_activate_status(self):
        # Snapmaker returns [(name, state_code)]; 0 means the head is grabbed
        grabbed = (self.name == self.printer.lookup_object('toolhead').active_name and
                   self.park_state == 'active')
        return [(self.name, 0 if grabbed else 1)]

    def get_trapq(self):
        return None


class FakeToolhead(object):
    def __init__(self, printer, extruders):
        self.printer = printer
        self.extruders = extruders
        self.active = 0
        self.position = [0.0, 0.0, 0.0, 0.0]
        self.homed_axes = ''
        self.max_physical_extruder_num = len(extruders)
        self.print_file_line = None
        self.special_queuing_state = 'Main'
        self.step_generators = []

    @property
    def active_name(self):
        return self.extruders[self.active].name

    def get_extruder(self):
        return self.extruders[self.active]

    def get_status(self, eventtime=None):
        return {'homed_axes': self.homed_axes,
                'position': tuple(self.position),
                'axis_maximum': (270.0, 270.0, 270.5, 0.0),
                'extruder': self.active_name,
                'print_time': 0.0, 'estimated_print_time': 0.0}

    def get_position(self):
        return list(self.position)

    def wait_moves(self):
        return None

    def get_last_move_time(self):
        return 0.0

    def check_busy(self, eventtime):
        return 0.0, 0.0, True

    def flush_step_generation(self):
        return None

    def register_lookahead_callback(self, callback):
        callback(0.0)

    def is_calibrating_flow(self):
        return False

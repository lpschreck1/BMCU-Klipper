"""Faults to inject and the events that trigger them (plan section 5).

A fault is a small object with ``apply(sim)`` and ``clear(sim)``. A trigger watches the
G-code stream (``sim.gcode.on_dispatch``) and fires its fault just *before* the matching
line executes, so a fault placed ``at=ToolChange(tool=4)`` is already in the world when
BMCU starts that change.
"""

# -- triggers ----------------------------------------------------------------------


class Trigger(object):
    def matches(self, command, commandline, sim):
        raise NotImplementedError


class GcodeLine(Trigger):
    """The n-th line (1-based) whose text starts with ``prefix``."""

    def __init__(self, prefix, n=1):
        self.prefix = prefix.upper()
        self.n = n
        self.count = 0

    def matches(self, command, commandline, sim):
        if commandline.upper().startswith(self.prefix):
            self.count += 1
            return self.count == self.n
        return False


class ToolChange(GcodeLine):
    """The n-th ``BMCU_TOOL_CHANGE TOOL=<tool>`` (n counts changes to that tool)."""

    def __init__(self, tool, n=1):
        GcodeLine.__init__(self, 'BMCU_TOOL_CHANGE TOOL=%d' % tool, n)
        self.tool = tool


class PrintStart(GcodeLine):
    """Print-start auto feed of one head (``BMCU_AUTO_FEED EXTRUDER=<head>``)."""

    def __init__(self, head):
        GcodeLine.__init__(self, 'BMCU_AUTO_FEED EXTRUDER=%d' % head, 1)
        self.head = head


class Resume(Trigger):
    """The k-th time the user presses Resume (fires before RESUME runs)."""

    def __init__(self, k=1):
        self.k = k
        self.count = 0

    def matches(self, command, commandline, sim):
        if command == 'RESUME' and sim.gcode.depth == 0:
            self.count += 1
            return self.count == self.k
        return False


class AtTime(Trigger):
    """Fires on the first G-code line after virtual time ``t`` (seconds from start)."""

    def __init__(self, t):
        self.t = t
        self.fired = False

    def matches(self, command, commandline, sim):
        return (not self.fired) and sim.world.now >= self.t


# -- faults ------------------------------------------------------------------------------


class Fault(object):
    name = 'fault'

    def apply(self, sim):
        raise NotImplementedError

    def clear(self, sim):
        raise NotImplementedError


class Snag(Fault):
    """Filament stops advancing ``after_mm`` into the path; the wheel stalls."""
    name = 'snag'

    def __init__(self, source, after_mm):
        self.source = source
        self.after_mm = float(after_mm)

    def apply(self, sim):
        sim.world.sources[self.source].snag_at = self.after_mm

    def clear(self, sim):
        sim.world.sources[self.source].snag_at = None


class Clog(Fault):
    """Gears grip but nothing reaches the nozzle; the coil change stays small."""
    name = 'clog'

    def __init__(self, head):
        self.head = head

    def apply(self, sim):
        sim.world.heads[self.head].clog = True

    def clear(self, sim):
        # MODEL: clearing the clog means the user frees the nozzle and hand-feeds the
        # filament through, so the tip ends up in the nozzle zone
        sim.world.heads[self.head].clog = False
        for source in sim.world.heads[self.head].sources:
            if source.tip is not None and source.tip >= source.sensor_p:
                moved = sim.world._apply(source, source.nozzle_p - 2.0 - source.tip)


class DeformedTip(Fault):
    """The gears miss the grab (phase-3 retries)."""
    name = 'deformed_tip'

    def __init__(self, source):
        self.source = source

    def apply(self, sim):
        sim.world.sources[self.source].quality = 'deformed'

    def clear(self, sim):
        sim.world.sources[self.source].quality = 'good'


class Runout(Fault):
    """The spool empties ``at_mm`` into the path; ``present`` goes false."""
    name = 'runout'

    def __init__(self, source, at_mm=None):
        self.source = source
        self.at_mm = at_mm

    def apply(self, sim):
        source = sim.world.sources[self.source]
        source.runout_at = self.at_mm if self.at_mm is not None else (source.tip or 0.0)
        source.spool_mm = 0.0
        source.present = False

    def clear(self, sim):
        source = sim.world.sources[self.source]
        source.runout_at = None
        source.spool_mm = 50000.0
        source.present = True


class SensorLag(Fault):
    """The head sensor report lags ``mm`` of extruder motion after the switch opens."""
    name = 'sensor_lag'

    def __init__(self, head, mm):
        self.head = head
        self.mm = float(mm)

    def apply(self, sim):
        sim.world.heads[self.head].sensor_lag_mm = self.mm

    def clear(self, sim):
        sim.world.heads[self.head].sensor_lag_mm = None


class SensorStuck(Fault):
    """The head switch reads ``value`` whatever the filament does."""
    name = 'sensor_stuck'

    def __init__(self, head, value):
        self.head = head
        self.value = bool(value)

    def apply(self, sim):
        sim.world.heads[self.head].sensor_stuck = self.value

    def clear(self, sim):
        sim.world.heads[self.head].sensor_stuck = None


class ParkedInShared(Fault):
    """The channel's autoload runs longer than its tube: the tip sits past the splitter."""
    name = 'parked_in_shared'

    def __init__(self, source, extra_mm=100.0):
        self.source = source
        self.extra_mm = float(extra_mm)

    def apply(self, sim):
        source = sim.world.sources[self.source]
        world = sim.world
        world.insert(source, tip=source.tube_len + self.extra_mm)
        source.encoder_mm = source.tip

    def clear(self, sim):
        source = sim.world.sources[self.source]
        sim.world.pull(source, source.tip or 0.0)


class Unrecorded(Fault):
    """Filament is physically in the head while BMCU's state says empty."""
    name = 'unrecorded'

    def __init__(self, source):
        self.source = source

    def apply(self, sim):
        source = sim.world.sources[self.source]
        sim.world.insert(source, tip=source.nozzle_p - 2.0)
        source.encoder_mm = source.tip
        sim.world.heads[source.head].last_grip_at = None

    def clear(self, sim):
        source = sim.world.sources[self.source]
        sim.world.pull(source, source.tip or 0.0)


class BmcuDisconnect(Fault):
    """The USB link goes down; ``duration`` is how long (seconds) until it returns."""
    name = 'bmcu_disconnect'

    def __init__(self, duration=5.0):
        self.duration = duration

    def apply(self, sim):
        sim.bmcu.helper.set_link(False)
        sim.reactor.register_callback(
            lambda now: sim.bmcu.helper.set_link(True),
            sim.reactor.monotonic() + self.duration)

    def clear(self, sim):
        sim.bmcu.helper.set_link(True)


class HelperStuck(Fault):
    """The helper keeps the port open with no session."""
    name = 'helper_stuck'

    def apply(self, sim):
        sim.bmcu.helper.stuck = True

    def clear(self, sim):
        sim.bmcu.helper.stuck = False


class UserManual(Fault):
    """The user loads/unloads at the screen or pulls filament by hand.

    ``action`` is ``('pull', source)`` (pull the filament out of the head by hand)."""
    name = 'user_manual'

    def __init__(self, action, source):
        self.action = action
        self.source = source

    def apply(self, sim):
        source = sim.world.sources[self.source]
        if self.action == 'pull':
            sim.world.pull(source, source.tip or 0.0)

    def clear(self, sim):
        return None


ALL_PRINT_FAULTS = (Snag, Clog, DeformedTip)

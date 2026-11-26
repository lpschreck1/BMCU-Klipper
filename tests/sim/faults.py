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


# -- phase triggers: fire on simulated machine state, polled every world step -------------
#
# They look only at the world, the fake firmware and the fake Snapmaker feeder, never at the
# BMCU manager's internals. ``poll(sim)`` returns True once, when the phase is reached.

LOAD_OPS = ('feed', 'autoload', 'encoder_test')


class PhaseTrigger(Trigger):
    state = True

    def matches(self, command, commandline, sim):
        return False

    def poll(self, sim):
        raise NotImplementedError


def _channel_of(sim, device):
    """``'bmcu2'`` (a source name), ``2`` or ``(0, 2)`` -> the channel number."""
    if isinstance(device, str):
        return int(device[len('bmcu'):])
    if isinstance(device, tuple):
        return int(device[1])
    return int(device)


def load_activity(fw, ch):
    """The firmware is pushing channel ``ch`` towards the head."""
    from bmcu_core import protocol as P
    if fw.op is not None and fw.op.channel == ch:
        return fw.op.type != P.OP_CHANNEL_RETRACT
    return fw.motion[ch] == P.MOTION_SEND_OUT


def unload_activity(fw, ch):
    from bmcu_core import protocol as P
    if fw.op is not None and fw.op.channel == ch:
        return fw.op.type == P.OP_CHANNEL_RETRACT
    return fw.motion[ch] in (P.MOTION_PULL_BACK, P.MOTION_BEFORE_PULL_BACK)


class BmcuMoving(PhaseTrigger):
    """The fake firmware has moved ``device``'s channel ``after_mm`` in ``direction``
    (``'load'`` or ``'unload'``) since that move started. ``device`` is the source name
    (``'bmcu0'`` = channel 1)."""

    def __init__(self, device, direction='load', after_mm=0.0):
        self.channel = _channel_of(None, device)
        self.direction = direction
        self.after_mm = float(after_mm)
        self.base = None
        self.fired = False

    def poll(self, sim):
        fw = sim.bmcu.fw
        ch = self.channel
        active = (load_activity if self.direction == 'load' else unload_activity)(fw, ch)
        encoder = fw.source(ch).encoder_mm
        if not active:
            self.base = None
            return False
        if self.base is None:
            self.base = encoder
        moved = (encoder - self.base) if self.direction == 'load' else (self.base - encoder)
        return moved >= self.after_mm


class StockFeeding(PhaseTrigger):
    """The fake Snapmaker feeder of ``head`` is in ``filament_feed`` channel state ``phase``
    (``load_feeding``, ``load_extruding``, ``load_flushing``, ``unload_*`` ...). A trailing
    ``*`` matches a prefix."""

    def __init__(self, head, phase):
        self.head = head
        self.phase = phase
        self.fired = False

    def poll(self, sim):
        for feeder in sim.feeders.values():
            if self.head in feeder.filament_ch:
                state = feeder.channel_state[feeder.filament_ch.index(self.head)]
                if self.phase.endswith('*'):
                    return state.startswith(self.phase[:-1])
                return state == self.phase
        return False


class BeforeOnUse(PhaseTrigger):
    """Inside BMCU's BEFORE_ON_USE (the toolhead check) on a channel that feeds ``head``."""

    def __init__(self, head):
        self.head = head
        self.fired = False

    def poll(self, sim):
        from bmcu_core import protocol as P
        fw = sim.bmcu.fw
        return any(fw.motion[ch] == P.MOTION_BEFORE_ON_USE and
                   sim.channel_head[ch] == self.head for ch in range(4))


class Prestaging(PhaseTrigger):
    """BMCU is moving filament for ``tool``'s head in the background: the firmware is loading
    or unloading a channel of that head while the foreground G-code is not the change to
    ``tool`` itself (nor the print-start feed of that head, nor the print end)."""

    def __init__(self, tool):
        self.tool = int(tool)
        self.head = None
        self.fired = False

    def poll(self, sim):
        head = sim.channel_head[self.tool - 4]
        fw = sim.bmcu.fw
        if not any(load_activity(fw, ch) or unload_activity(fw, ch)
                   for ch in range(4) if sim.channel_head[ch] == head):
            return False
        for command, line in sim.gcode.running:
            upper = line.upper()
            if command == 'BMCU_TOOL_CHANGE' and ('TOOL=%d' % self.tool) in upper:
                return False
            if command == 'BMCU_AUTO_FEED' and ('EXTRUDER=%d' % head) in upper:
                return False
            if command == 'BMCU_PRINT_END':
                return False
        return True


class BetweenUnloadAndLoad(PhaseTrigger):
    """``head`` held a BMCU filament and now holds none (the old one is out of the shared
    tube, the next one is not in yet): both channels are "out"."""

    def __init__(self, head):
        self.head = head
        self.seen = False
        self.fired = False

    def poll(self, sim):
        sources = [s for s in sim.world.heads[self.head].sources if s.kind == 'bmcu']
        if any(s.past(s.gears_p) for s in sources):
            self.seen = True
            return False
        return self.seen and not any(s.in_shared for s in sources)


class WriteQueued(PhaseTrigger):
    """A durable state write has been queued but has not landed yet (needs
    ``sim.bmcu.write_lag`` > 0): the state file is older than the world."""

    def __init__(self, after=0.0):
        self.after = float(after)      # world time before it may fire
        self.fired = False

    def poll(self, sim):
        return (sim.world.now >= self.after and sim.bmcu is not None and
                bool(sim.bmcu.pending_writes))


class Idle(PhaseTrigger):
    """Mid-print, nothing of BMCU's is moving, ``after`` seconds after the first layer."""

    def __init__(self, after=60.0):
        self.after = float(after)
        self.start = None
        self.fired = False

    def poll(self, sim):
        if sim.print_stats.state != 'printing':
            return False
        if self.start is None:
            self.start = sim.world.now
        from bmcu_core import protocol as P
        fw = sim.bmcu.fw
        busy = fw.op is not None or any(m in (
            P.MOTION_SEND_OUT, P.MOTION_BEFORE_ON_USE, P.MOTION_PULL_BACK,
            P.MOTION_BEFORE_PULL_BACK) for m in fw.motion)
        return sim.world.now - self.start >= self.after and not busy


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
        # MODEL: the user trims the tip and hand-feeds the filament through, as for a clog
        source = sim.world.sources[self.source]
        source.quality = 'good'
        if source.tip is not None and source.tip >= source.sensor_p:
            sim.world._apply(source, source.nozzle_p - 2.0 - source.tip)


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
        self.duration = duration          # None: down until the user replugs (clear_fault)

    def apply(self, sim):
        sim.bmcu.helper.set_link(False)
        if self.duration is not None:
            sim.reactor.register_callback(
                lambda now: sim.bmcu.helper.set_link(True),
                sim.reactor.monotonic() + self.duration)

    def clear(self, sim):
        sim.bmcu.helper.set_link(True)


class Restart(Fault):
    """Klipper restarts (``'klipper'``) or the whole machine loses power (``'power'``)."""
    name = 'restart'

    def __init__(self, kind, corrupt_state=None):
        assert kind in ('klipper', 'power'), kind
        assert corrupt_state in (None, 'empty', 'truncated'), corrupt_state
        self.kind = kind
        self.corrupt_state = corrupt_state     # damage bmcu_state.json before the next boot

    def apply(self, sim):
        sim.corrupt_state = self.corrupt_state
        sim.request_restart(self.kind)

    def clear(self, sim):
        return None


class HelperExit(Fault):
    """The transport helper process dies mid-print. Its socket disappears. The supervisor
    (``bmcu_host_bootstrap.py --repair`` or the service manager) starts a new one
    ``restart_after`` seconds later; ``None`` means only ``clear`` (the user) does."""
    name = 'helper_exit'

    def __init__(self, restart_after=None):
        self.restart_after = restart_after

    def apply(self, sim):
        sim.bmcu.helper.exit()
        if self.restart_after is not None:
            sim.reactor.register_callback(
                lambda now: sim.bmcu.helper.restart(),
                sim.reactor.monotonic() + self.restart_after)

    def clear(self, sim):
        if not sim.bmcu.helper.running:
            sim.bmcu.helper.restart()


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

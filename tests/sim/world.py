"""Physical model: where every filament tip is.

All distances are millimetres. A *source* is a place filament comes from: a BMCU
channel or a stock feeder. Each head has up to two sources joined by a splitter.

Positions are per source, ``p``: distance of the tip from the source's outlet
(p == 0 means the tip is just inside the outlet, ``None`` means no filament)::

    outlet --- tube (tube_len) --- splitter --- shared --- entry sensor
          --- sensor_to_gears --- gears --- gears_to_nozzle --- nozzle

so the splitter is at ``p = tube_len`` and the nozzle at ``p = total``.

Motion is instantaneous per call (``push``/``pull``/``gear_move``); the callers
(fake BMCU firmware, fake feeder, fake extruder) split long moves into steps and
spend virtual time between them. Only heaters integrate with ``step(dt)``.

# MODEL: Snag and clog behaviour, the coil-frequency values and the splitter
# collision rule are inferred from the plan (sections 3.2 and 5), not measured.
"""

BASELINE_COIL = 3000.0          # MODEL: arbitrary idle level; only deltas matter
COIL_GOOD_DELTA = 1000.0        # a good grab (plan 3.2: ~1000 or more)
COIL_MISS_DELTA = 20.0          # a miss (plan 3.2: under 100)
FEED_WHEEL_CIRCUMFERENCE = 31.4159
FEED_WHEEL_PPR = 6
MIN_EXTRUDE_TEMP = 170.0
HEATER_SLEW = 5.0               # deg C per second towards the target
HEATER_COOL = 1.5               # deg C per second cooling to ambient
AMBIENT = 25.0

DEFAULT_PRELOAD_MM = 850.0
DEFAULT_BMCU_TUBE_MM = 1500.0
DEFAULT_STOCK_TUBE_MM = 900.0
DEFAULT_SHARED_MM = 300.0
DEFAULT_SENSOR_TO_GEARS = 20.0
DEFAULT_GEARS_TO_NOZZLE = 70.0


class WorldError(RuntimeError):
    pass


class Heater(object):
    def __init__(self, name):
        self.name = name
        self.temp = AMBIENT
        self.target = 0.0

    def step(self, dt):
        if self.target > self.temp:
            self.temp = min(self.target, self.temp + HEATER_SLEW * dt)
        else:
            floor = max(self.target, AMBIENT)
            self.temp = max(floor, self.temp - HEATER_COOL * dt)

    def can_extrude(self):
        return self.temp >= MIN_EXTRUDE_TEMP


class Source(object):
    """One filament source (a BMCU channel or a stock feeder)."""

    def __init__(self, world, name, kind, head, tube_len):
        self.world = world
        self.name = name
        self.kind = kind              # 'bmcu' or 'stock'
        self.head = head
        self.tube_len = float(tube_len)
        self.tip = None               # mm along the path, None = no filament
        self.quality = 'good'         # 'good' or 'deformed'
        self.spool_mm = 0.0           # filament left behind the outlet
        self.encoder_mm = 0.0         # integrates every motion (BMCU encoder)
        self.pushed_mm = 0.0          # motion made by this source's own wheel
        self.stalled = False          # last push hit an obstacle
        self.stall_reason = ''
        self.snag_at = None           # tip cannot pass this p (Snag fault)
        self.runout_at = None         # spool ends at this p (Runout fault)
        self.present = False          # filament detected at the outlet

    # positions along this source's path
    @property
    def splitter_p(self):
        return self.tube_len

    @property
    def sensor_p(self):
        return self.tube_len + self.world.heads[self.head].shared

    @property
    def gears_p(self):
        head = self.world.heads[self.head]
        return self.sensor_p + head.sensor_to_gears

    @property
    def nozzle_p(self):
        return self.gears_p + self.world.heads[self.head].gears_to_nozzle

    def past(self, point):
        return self.tip is not None and self.tip >= point

    @property
    def in_shared(self):
        return self.tip is not None and self.tip > self.splitter_p

    @property
    def in_gears(self):
        return self.past(self.gears_p)

    def counts(self):
        return int(round(self.pushed_mm * FEED_WHEEL_PPR * 2 /
                         FEED_WHEEL_CIRCUMFERENCE))


class Head(object):
    def __init__(self, index, shared, sensor_to_gears, gears_to_nozzle):
        self.index = index
        self.shared = float(shared)
        self.sensor_to_gears = float(sensor_to_gears)
        self.gears_to_nozzle = float(gears_to_nozzle)
        self.sources = []
        self.heater = Heater('extruder%s' % ('' if index == 0 else index))
        self.clog = False
        self.sensor_lag_mm = None      # SensorLag fault, see snapmaker.py
        self.sensor_stuck = None       # SensorStuck fault: forced value
        self.extruded_mm = 0.0         # total flow out of the nozzle
        self.last_grip_at = None


class World(object):
    def __init__(self):
        self.heads = {}
        self.sources = {}
        self.heaters = {}
        self.now = 0.0
        self.events = []

    # -- construction --------------------------------------------------------
    def add_head(self, index, shared=DEFAULT_SHARED_MM,
                 sensor_to_gears=DEFAULT_SENSOR_TO_GEARS,
                 gears_to_nozzle=DEFAULT_GEARS_TO_NOZZLE):
        head = Head(index, shared, sensor_to_gears, gears_to_nozzle)
        self.heads[index] = head
        self.heaters[head.heater.name] = head.heater
        return head

    def add_source(self, head, name, kind, tube_len=None):
        if tube_len is None:
            tube_len = (DEFAULT_BMCU_TUBE_MM if kind == 'bmcu'
                        else DEFAULT_STOCK_TUBE_MM)
        source = Source(self, name, kind, head, tube_len)
        self.heads[head].sources.append(source)
        self.sources[name] = source
        return source

    def log(self, text):
        self.events.append((round(self.now, 3), text))

    # -- user actions --------------------------------------------------------
    def insert(self, source, quality='good', spool_mm=50000.0, tip=0.0):
        """The user puts a spool on / filament into the outlet of a source."""
        source = self.sources[source] if isinstance(source, str) else source
        source.tip = float(tip)
        source.quality = quality
        source.spool_mm = float(spool_mm)
        source.present = True
        source.stalled = False

    def remove(self, source):
        source = self.sources[source] if isinstance(source, str) else source
        source.tip = None
        source.present = False
        source.spool_mm = 0.0

    # -- derived state -------------------------------------------------------
    def other_sources(self, source):
        return [s for s in self.heads[source.head].sources if s is not source]

    def head_filament(self, head):
        """The source whose filament reaches the gears or the shared tube."""
        for source in self.heads[head].sources:
            if source.in_shared:
                return source
        return None

    def blocked_at_splitter(self, source):
        """A tip past the splitter blocks the other source from entering."""
        return any(other.in_shared for other in self.other_sources(source))

    def sensor_physical(self, head):
        """Raw entry-switch state: any tip past the entry sensor."""
        return any(s.past(s.sensor_p) for s in self.heads[head].sources)

    def coil_freq(self, head):
        """Coil frequency at the gears. # MODEL: a good grab is one tip in the
        gears that is not deformed while the head has gripped; a clog or a
        deformed tip stays near baseline."""
        item = self.heads[head]
        if item.clog:
            return BASELINE_COIL + COIL_MISS_DELTA
        source = self.head_filament(head)
        if source is None or not source.in_gears:
            return BASELINE_COIL
        if source.quality != 'good':
            return BASELINE_COIL + COIL_MISS_DELTA
        if item.last_grip_at is None:
            return BASELINE_COIL
        return BASELINE_COIL + COIL_GOOD_DELTA

    # -- motion --------------------------------------------------------------
    def _limit(self, source, forward):
        """Furthest ``p`` the tip may reach in the given direction."""
        head = self.heads[source.head]
        if not forward:
            return 0.0
        limit = source.nozzle_p
        if source.snag_at is not None:
            limit = min(limit, source.snag_at)
        if source.runout_at is not None:
            limit = min(limit, source.runout_at)
        if head.clog:
            limit = min(limit, source.gears_p + 5.0)
        if self.blocked_at_splitter(source):
            limit = min(limit, source.splitter_p)
        return limit

    def _apply(self, source, delta):
        """Move the tip by ``delta`` (signed) under the limits. Returns moved."""
        if source.tip is None:
            return 0.0
        if delta >= 0:
            if source.spool_mm <= 0 and source.present:
                # nothing behind the outlet: the filament cannot be fed more
                limit = source.tip
            else:
                limit = self._limit(source, True)
            target = min(source.tip + delta, max(limit, source.tip))
            moved = target - source.tip
            if moved + 1e-9 < delta:
                source.stalled = True
                source.stall_reason = 'limit'
            used = min(moved, source.spool_mm)
            source.spool_mm = max(0.0, source.spool_mm - used)
        else:
            target = max(source.tip + delta, 0.0)
            moved = target - source.tip
            source.spool_mm -= moved
        source.tip = target
        source.encoder_mm += moved
        return moved

    def push(self, source, mm):
        """A source's own wheel pushes its filament ``mm`` forward."""
        source = self.sources[source] if isinstance(source, str) else source
        source.stalled = False
        source.stall_reason = ''
        moved = self._apply(source, float(mm))
        if moved > 0:
            source.pushed_mm += moved
        return moved

    def pull(self, source, mm):
        """A source's own wheel retracts its filament ``mm`` backwards."""
        source = self.sources[source] if isinstance(source, str) else source
        source.stalled = False
        moved = self._apply(source, -abs(float(mm)))
        return -moved

    def gear_move(self, head, mm):
        """The head's extruder gears move filament by ``mm`` (negative = back).

        Only a tip that is in the gears is moved. Returns ``(moved, flow)``:
        how far the filament moved and how much came out of the nozzle.
        """
        item = self.heads[head]
        mm = float(mm)
        if mm > 0 and not item.heater.can_extrude():
            raise WorldError('extrude below %.0f C' % MIN_EXTRUDE_TEMP)
        source = self.head_filament(head)
        if source is None or not source.in_gears:
            return 0.0, 0.0
        if mm >= 0:
            room = self._limit(source, True) - source.tip
            if source.spool_mm <= 0 and source.present:
                room = 0.0
            advance = min(mm, max(room, 0.0))
            moved = self._apply(source, advance)
            # filament already at the nozzle is extruded out of it
            at_nozzle = source.tip >= source.nozzle_p - 1e-9
            flow = 0.0
            if not item.clog and at_nozzle:
                flow = mm
                item.extruded_mm += flow
            elif not item.clog and moved > 0:
                pass
            if moved + 1e-9 < mm and not at_nozzle:
                source.stalled = True
                source.stall_reason = 'gears'
            return moved, flow
        moved = self._apply(source, mm)
        return moved, 0.0

    def feed_until(self, source, max_mm, stop_at_sensor=False, chunk=5.0,
                   on_chunk=None):
        """Push in ``chunk`` steps until ``max_mm`` is done, the tip stalls, or
        (optionally) the entry sensor of the head triggers. ``on_chunk(moved)``
        lets the caller spend virtual time. Returns ``(moved, reason)`` where
        reason is ``'done'``, ``'sensor'`` or ``'stalled'``."""
        source = self.sources[source] if isinstance(source, str) else source
        total = 0.0
        while total + 1e-9 < max_mm:
            if stop_at_sensor and self.sensor_physical(source.head):
                return total, 'sensor'
            step = min(chunk, max_mm - total)
            moved = self.push(source, step)
            total += moved
            if on_chunk is not None:
                on_chunk(moved)
            if moved + 1e-9 < step:
                return total, 'stalled'
        if stop_at_sensor and self.sensor_physical(source.head):
            return total, 'sensor'
        return total, 'done'

    # -- time ----------------------------------------------------------------
    def step(self, dt):
        self.now += dt
        for heater in self.heaters.values():
            heater.step(dt)

    # -- consistency ---------------------------------------------------------
    def consistent(self, expect=None):
        """Every filament is where the caller says. ``expect`` maps source name
        to ``'loaded'`` (tip at nozzle), ``'empty'`` (no tip in the head) or
        ``'parked'`` (tip in the tube). Returns a list of problems."""
        problems = []
        for name, want in sorted((expect or {}).items()):
            source = self.sources[name]
            if want == 'loaded' and not source.past(source.gears_p):
                problems.append('%s should be loaded, tip=%r' % (name, source.tip))
            if want == 'empty' and source.past(source.sensor_p):
                problems.append('%s should be clear of the head, tip=%r' %
                                (name, source.tip))
            if want == 'parked' and (source.tip is None or
                                     source.past(source.splitter_p)):
                problems.append('%s should be parked in its tube, tip=%r' %
                                (name, source.tip))
        for item in self.heads.values():
            inside = [s for s in item.sources if s.in_shared]
            if len(inside) > 1:
                problems.append('head %d has two filaments in the shared tube'
                                % item.index)
        return problems

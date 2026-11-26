"""Fake BMCU firmware (1.0.5) at the protocol level.

It consumes and produces the real wire messages of ``bmcu_core.protocol`` (the status,
op-result, HELLO and snapshot structs are packed with protocol's own struct formats), so
``device.py`` and ``protocol.py`` run for real. The control loops (PID, PWM, pressure) are
not simulated: motion is a simple kinematic model on top of ``world.py``.

# MODEL: everything about how fast the firmware moves filament and how the buffer
# percentage reacts is a guess shaped by firmware/src/bmcu_protocol.cpp
# (process_distance_op, set_motion, fill_status) and Motion_control.cpp.
"""
import struct

from bmcu_core import protocol as P

UID = bytes(bytearray(range(1, 13)))
PROTO = P.PROTO_VERSION
CAPS = P.CAP_CHANNEL_RETRACT | P.CAP_LOAD_PRESSURE_PCT | P.CAP_LED_PREVIEW | \
    P.CAP_LED_FILAMENT_PREVIEW
FREE_BUFFER_PCT = 50          # MODEL: idle / free-running buffer level
STALL_RISE_PCT_S = 60.0       # MODEL: buffer rises this fast when a push is blocked
DRAIN_PCT_S = 25.0            # MODEL: and falls back at this rate
AUTOLOAD_PAUSE_AT_MM = 1000.0   # Motion_control.cpp DM_AUTO_PAUSE_MS: pauses over 1 m
AUTOLOAD_PAUSE_S = 30.0


class Op(object):
    def __init__(self, op_id, op_type, channel, target_mm, now):
        self.op_id = op_id
        self.type = op_type
        self.channel = channel
        self.target_mm = target_mm
        self.start_mm = 0.0
        self.contact_pct = 90
        self.stop_on_contact = False
        self.started = now
        self.deadline = now + 300.0
        self.speed = 80.0


class FakeFirmware(object):
    """One BMCU. ``send_packet(msg_type, cmd_id, payload)`` delivers to the host."""

    def __init__(self, sim, uid=UID, firmware=(1, 0, 5), channels=None, routes=None):
        self.sim = sim
        self.world = sim.world
        self.reactor = sim.reactor
        self.uid = bytes(uid)
        self.firmware = firmware
        self.channel_sources = dict(channels or {
            ch: 'bmcu%d' % ch for ch in range(4)})
        self.send_packet = None          # set by the helper
        self.session_id = 0x1000
        self.seq = 1
        # MODEL: route states live in flash (Flash_AMS_state_write), so they survive a power
        # cycle; motion, the active op and the buffer do not. The encoder counter restarts.
        self.route_state = list(routes) if routes is not None else [P.ROUTE_EMPTY] * 4
        self.enc_base = [self.world.sources[self.channel_sources[ch]].encoder_mm
                         for ch in range(4)] if routes is not None else [0.0] * 4
        self.motion = [P.MOTION_IDLE] * 4
        self.buffer_pct = [FREE_BUFFER_PCT] * 4
        self.now_channel = 0xff
        self.event_counter = 1
        self.op = None
        self.op_history = []
        self.last_result = None
        self.config = {P.CONFIG_LOAD_PRESSURE_PCT: 82.0, P.CONFIG_LOAD_SPEED_MMS: 80.0,
                       P.CONFIG_PULL_SPEED_MMS: 80.0, P.CONFIG_PULL_SPEED_END_MMS: 12.0,
                       P.CONFIG_JAM_TIMEOUT_MS: 20000.0,
                       P.CONFIG_BEFORE_PULLBACK_TARGET_PCT: 40.0}
        self.retract_m = [0.200] * 4
        self.autoload_m = [0.120] * 4
        self.slots = [None] * 4
        self.runtime_syncs = []
        self.received = []               # (msg_type, payload) log, for tests
        self.autoload_state = {}
        self.pull_start = {}
        self.fail_next = {}              # msg_type -> error code (fault injection)
        self.silent = False              # fault: BmcuDisconnect, no reply
        self.encoder_ok = 0x0f
        self.reactor.add_stepper(self.step)

    def rebind(self, sim):
        """The Klipper side restarted: the firmware keeps running on the new reactor."""
        self.sim = sim
        self.reactor = sim.reactor
        self.reactor.add_stepper(self.step)

    # -- helpers --------------------------------------------------------------------
    def source(self, ch):
        return self.world.sources[self.channel_sources[ch]]

    def meters(self, ch):
        return (self.source(ch).encoder_mm - self.enc_base[ch]) / 1000.0

    def present(self, ch):
        return 1 if self.source(ch).present else 0

    def _emit(self, msg_type, cmd_id, payload=b''):
        if self.send_packet is None or self.silent:
            return
        self.send_packet(msg_type, cmd_id, bytes(payload))

    def _ack(self, cmd_id, ok=1, error=0):
        self._emit(P.MSG_ACK, cmd_id, struct.pack('<IBH', cmd_id, ok, error))

    def _error(self, cmd_id, channel, code, details=0):
        self._emit(P.MSG_ERROR, cmd_id, struct.pack('<BHI', channel, code, details))

    def status_payload(self):
        route = list(self.route_state)
        last = self.last_result
        if self.op is not None:
            op_id, op_type, op_ch = self.op.op_id, self.op.type, self.op.channel
            op_state, op_reason = P.OP_STATE_RUNNING, 0
        elif last is not None:
            op_id, op_type, op_ch, op_state, op_reason = (
                last['op_id'], last['type'], last['ch'], last['state'], last['reason'])
        else:
            op_id, op_type, op_ch, op_state, op_reason = 0, 0, 0xff, 0, 0
        error_flags = 0x0002 if any(r == P.ROUTE_UNCERTAIN for r in route) else 0
        values = [self.now_channel] + route + list(self.motion) + \
            [self.present(c) for c in range(4)] + \
            [int(round(min(100, max(0, b)))) for b in self.buffer_pct] + \
            [0x0f] + [0] * 4 + [self.encoder_ok, 0x0f, error_flags] + \
            [self.meters(c) for c in range(4)] + [1.65] * 4 + [0] * 4 + \
            [self.session_id, self.event_counter, op_id] + \
            [op_type, op_ch, op_state, op_reason] + [0] * 6
        return P._STATUS.pack(*values)

    def calibration_payload(self, ch):
        return P._CALIBRATION.pack(ch, 0, 1, 1, 1.65, 0.0, 0.5, 1.65, 2.8)

    def send_status(self, cmd_id=0):
        self._emit(P.MSG_STATUS, cmd_id, self.status_payload())

    def send_op_result(self, op_id, op_type, ch, state, reason, target_mm,
                       measured_mm, duration_ms=0):
        self.last_result = {'op_id': op_id, 'type': op_type, 'ch': ch, 'state': state,
                            'reason': reason}
        self.op_history = (self.op_history + [dict(self.last_result,
                           target_mm=target_mm, measured_mm=measured_mm,
                           duration_ms=duration_ms)])[-4:]
        self._emit(P.MSG_OP_RESULT, op_id, P._OP_RESULT.pack(
            op_id, op_type, ch, state, reason, float(target_mm), float(measured_mm),
            int(duration_ms)))

    # -- host -> firmware ---------------------------------------------------------------
    def handle(self, msg_type, cmd_id, payload):
        self.received.append((msg_type, bytes(payload)))
        if self.silent:
            return
        forced = self.fail_next.pop(msg_type, None)
        if forced is not None:
            self._error(cmd_id, 0xff, forced)
            return
        handler = getattr(self, '_h_%02x' % msg_type, None)
        if handler is None:
            self._error(cmd_id, 0xff, 1)
            return
        handler(cmd_id, payload)

    def _h_01(self, cmd_id, payload):                     # HELLO
        self._emit(P.MSG_HELLO_ACK, cmd_id, P._HELLO_ACK.pack(
            self.uid, self.firmware[0], self.firmware[1], self.firmware[2], PROTO, CAPS,
            self.session_id, 4, int(self.config[P.CONFIG_LOAD_PRESSURE_PCT])))

    def _h_08(self, cmd_id, payload):                     # SESSION_CONFIRM
        self._ack(cmd_id)

    def _h_02(self, cmd_id, payload):                     # PING
        self._emit(P.MSG_PONG, cmd_id, payload[:16])

    def _h_03(self, cmd_id, payload):                     # GET_STATUS
        self.send_status(cmd_id)

    def _h_04(self, cmd_id, payload):                     # GET_CAPS
        self._emit(P.MSG_CAPS, cmd_id, P._CAPS.pack(
            4, 0, 1, self.firmware[0], self.firmware[1], self.firmware[2], PROTO, 0,
            CAPS, self.uid, b'BMCU-Klipper'.ljust(16, b'\0')))

    def _h_05(self, cmd_id, payload):                     # GET_CALIBRATION
        self._emit(P.MSG_CALIBRATION, cmd_id, self.calibration_payload(payload[0]))

    def _h_06(self, cmd_id, payload):                     # GET_OP_RESULT
        (op_id,) = struct.unpack('<I', payload[:4])
        for item in reversed(self.op_history):
            if op_id == 0 or item['op_id'] == op_id:
                self._emit(P.MSG_OP_RESULT, cmd_id, P._OP_RESULT.pack(
                    item['op_id'], item['type'], item['ch'], item['state'],
                    item['reason'], item['target_mm'], item['measured_mm'],
                    item['duration_ms']))
                return
        self._error(cmd_id, 0xff, 22)

    def _h_07(self, cmd_id, payload):                     # GET_SNAPSHOT
        self._emit(P.MSG_SNAPSHOT, cmd_id, self.status_payload() + b''.join(
            self.calibration_payload(ch) for ch in range(4)))

    def _h_11(self, cmd_id, payload):                     # SET_ACTIVE_CH
        self.now_channel = payload[0]
        self._ack(cmd_id)
        self.send_status()

    def _h_10(self, cmd_id, payload):                     # SET_MOTION
        ch, motion = payload[0], payload[1]
        if ch >= 4 or (self.op is not None and self.op.channel == ch):
            self._error(cmd_id, 0xff, 3)
            return
        if motion in (P.MOTION_SEND_OUT, P.MOTION_BEFORE_ON_USE) and any(
                self.motion[o] in (P.MOTION_SEND_OUT, P.MOTION_BEFORE_ON_USE)
                for o in range(4) if o != ch):
            self._error(cmd_id, 0xff, 3)
            return
        self._set_motion(ch, motion)
        self._ack(cmd_id)
        self.send_status()

    def _set_motion(self, ch, motion):
        previous = self.motion[ch]
        if motion == P.MOTION_IDLE:
            self.motion[ch] = P.MOTION_IDLE
            if self.now_channel == ch:
                self.now_channel = 0xff
            self.event_counter += 1
            return
        if (motion == P.MOTION_SEND_OUT and self.route_state[ch] == P.ROUTE_EMPTY) or \
                motion in (P.MOTION_BEFORE_ON_USE, P.MOTION_BEFORE_PULL_BACK,
                           P.MOTION_PULL_BACK):
            self.route_state[ch] = P.ROUTE_UNCERTAIN       # ams_state_begin_change
        self.now_channel = ch
        self.motion[ch] = motion
        if motion == P.MOTION_ON_USE:
            self.route_state[ch] = P.ROUTE_LOADED          # ams_state_set_loaded
        if motion in (P.MOTION_PULL_BACK, P.MOTION_BEFORE_PULL_BACK) and \
                previous not in (P.MOTION_PULL_BACK, P.MOTION_BEFORE_PULL_BACK):
            self.pull_start[ch] = self.source(ch).encoder_mm
        self.event_counter += 1

    def _h_24(self, cmd_id, payload):                     # CHANNEL_PRELOAD
        self._ack(cmd_id)
        self.send_status()

    def _h_1a(self, cmd_id, payload):                     # MARK_UNLOADED
        ch = payload[0]
        self.motion[ch] = P.MOTION_IDLE
        self.route_state[ch] = P.ROUTE_EMPTY
        if self.now_channel == ch:
            self.now_channel = 0xff
        self.event_counter += 1
        self._ack(cmd_id)
        self.send_status()

    def _h_1b(self, cmd_id, payload):                     # MARK_LOADED
        ch = payload[0]
        self.motion[ch] = P.MOTION_IDLE
        self.route_state[ch] = P.ROUTE_LOADED
        if self.now_channel == ch:
            self.now_channel = 0xff
        self.event_counter += 1
        self._ack(cmd_id)
        self.send_status()

    def _distance_op(self, cmd_id, op_type, ch, mm, contact_pct, timeout_ms,
                     stop_on_contact, occupies_route):
        if (self.op is not None or ch >= 4 or not (5.0 <= mm <= 5000.0) or
                self.motion[ch] != P.MOTION_IDLE or not self.present(ch) or
                any(self.motion[o] in (P.MOTION_SEND_OUT, P.MOTION_BEFORE_ON_USE)
                    for o in range(4) if o != ch)):
            reason = 3 if (ch < 4 and not self.present(ch)) else 7
            self.send_op_result(cmd_id, op_type, ch, P.OP_STATE_FAILED, reason, mm, 0.0)
            return
        op = Op(cmd_id, op_type, ch, mm, self.reactor.monotonic())
        op.start_mm = self.source(ch).encoder_mm
        op.contact_pct = contact_pct
        op.stop_on_contact = stop_on_contact
        op.deadline = op.started + timeout_ms / 1000.0
        op.speed = self.config[P.CONFIG_LOAD_SPEED_MMS]
        if occupies_route:
            self.route_state[ch] = P.ROUTE_UNCERTAIN
        self.op = op
        self._set_motion(ch, P.MOTION_SEND_OUT)
        self._ack(cmd_id)
        self.send_status()

    def _h_15(self, cmd_id, payload):                     # CHANNEL_AUTOLOAD
        mm = struct.unpack('<f', payload[1:5])[0]
        self._distance_op(cmd_id, P.OP_CHANNEL_AUTOLOAD, payload[0], mm, 95,
                          int(max(6000, mm / 80.0 * 2000 + 1500)), False, False)

    def _h_14(self, cmd_id, payload):                     # TEST_ENCODER
        mm = struct.unpack('<f', payload[1:5])[0]
        self._distance_op(cmd_id, P.OP_ENCODER_TEST, payload[0], mm, 95,
                          int(max(3000, mm / 80.0 * 2000 + 1500)), False, False)

    def _feed(self, cmd_id, payload, contact):
        ch = payload[0]
        mm, = struct.unpack('<f', payload[1:5])
        contact_pct = payload[5]
        timeout_ms, = struct.unpack('<I', payload[6:10])
        self._distance_op(cmd_id, P.OP_FEED_TO_CONTACT if contact else P.OP_FEED_DISTANCE,
                          ch, mm, contact_pct, timeout_ms, contact, True)

    def _h_18(self, cmd_id, payload):                     # FEED_TO_CONTACT
        self._feed(cmd_id, payload, True)

    def _h_19(self, cmd_id, payload):                     # FEED_DISTANCE
        self._feed(cmd_id, payload, False)

    def _h_23(self, cmd_id, payload):                     # CHANNEL_RETRACT
        ch = payload[0]
        if (self.op is not None or ch >= 4 or self.route_state[ch] != P.ROUTE_EMPTY or
                self.motion[ch] != P.MOTION_IDLE or not self.present(ch)):
            self.send_op_result(cmd_id, P.OP_CHANNEL_RETRACT, ch, P.OP_STATE_FAILED,
                                7, 5000.0, 0.0)
            return
        op = Op(cmd_id, P.OP_CHANNEL_RETRACT, ch, self.retract_m[ch] * 1000.0,
                self.reactor.monotonic())
        op.start_mm = self.source(ch).encoder_mm
        op.speed = self.config[P.CONFIG_PULL_SPEED_MMS]
        self.op = op
        self._ack(cmd_id)
        self.send_status()

    def _h_16(self, cmd_id, payload):                     # ABORT_OP
        if self.op is not None:
            self._finish(P.OP_STATE_ABORTED, 6)
        self._ack(cmd_id)
        self.send_status()

    def _h_50(self, cmd_id, payload):                     # STOP_ALL
        self.op = None
        for ch in range(4):
            self.motion[ch] = P.MOTION_IDLE
        self.now_channel = 0xff
        self._ack(cmd_id)
        self.send_status()

    def _h_51(self, cmd_id, payload):                     # RESET_ERROR
        self._ack(cmd_id)

    def _h_34(self, cmd_id, payload):                     # RUNTIME_SYNC
        # validated like runtime_value_valid: autoload 0.01-2.0 m
        n = len(payload)
        if n == P._RUNTIME_SYNC_CHANNEL_AUTOLOAD.size:
            vals = P._RUNTIME_SYNC_CHANNEL_AUTOLOAD.unpack(payload)
            retract, autoload = vals[6:10], vals[10:14]
        elif n == P._RUNTIME_SYNC_LEGACY.size:
            vals = P._RUNTIME_SYNC_LEGACY.unpack(payload)
            retract, autoload = vals[6:10], self.autoload_m
        else:
            self._error(cmd_id, 0xff, 14)
            return
        if any(not (0.01 <= v <= 2.0) for v in list(retract) + list(autoload)):
            self._error(cmd_id, 0xff, 14)
            return
        for key, value in zip((P.CONFIG_LOAD_PRESSURE_PCT, P.CONFIG_LOAD_SPEED_MMS,
                               P.CONFIG_PULL_SPEED_MMS, P.CONFIG_PULL_SPEED_END_MMS,
                               P.CONFIG_JAM_TIMEOUT_MS,
                               P.CONFIG_BEFORE_PULLBACK_TARGET_PCT), vals[:6]):
            self.config[key] = value
        self.retract_m = list(retract)
        self.autoload_m = list(autoload)
        self.runtime_syncs.append(vals)
        self._ack(cmd_id)

    def _h_22(self, cmd_id, payload):                     # SET_SLOTS
        for ch in range(4):
            if payload[ch * 37] != ch:
                self._error(cmd_id, ch, 13)
                return
        self._ack(cmd_id)

    def _h_20(self, cmd_id, payload):                     # SET_SLOT_INFO
        self._emit(P.MSG_SLOT_INFO, cmd_id, payload[:37])

    def _h_21(self, cmd_id, payload):                     # GET_SLOT_INFO
        self._emit(P.MSG_SLOT_INFO, cmd_id,
                   P.pack_slot(payload[0], '#FFFFFF', 'Channel', 0, 0, ''))

    def _h_30(self, cmd_id, payload):                     # CONFIG_GET
        key, = struct.unpack('<H', payload[:2])
        self._emit(P.MSG_CONFIG_VAL, cmd_id, struct.pack('<Hf', key, self.config.get(key, 0.0)))

    def _h_31(self, cmd_id, payload):                     # CONFIG_SET
        key, value = struct.unpack('<Hf', payload[:6])
        self.config[key] = value
        self._ack(cmd_id)

    def _h_32(self, cmd_id, payload):                     # CONFIG_SAVE
        self._ack(cmd_id)

    def _ack_only(self, cmd_id, payload):
        self._ack(cmd_id)

    _h_33 = _h_35 = _h_36 = _ack_only                     # LED messages

    # -- time -------------------------------------------------------------------------
    def _finish(self, state, reason):
        op = self.op
        if op is None:
            return
        source = self.source(op.channel)
        measured = abs(source.encoder_mm - op.start_mm)
        self.op = None
        self.motion[op.channel] = P.MOTION_IDLE
        if self.now_channel == op.channel:
            self.now_channel = 0xff
        self.event_counter += 1
        self.send_op_result(op.op_id, op.type, op.channel, state, reason,
                            op.target_mm, measured,
                            int((self.reactor.monotonic() - op.started) * 1000))
        self.send_status()

    def step(self, dt):
        for ch in range(4):
            self.source(ch).assisting = (
                self.motion[ch] in (P.MOTION_SEND_OUT, P.MOTION_BEFORE_ON_USE,
                                    P.MOTION_ON_USE) or
                (self.op is not None and self.op.channel == ch and
                 self.op.type != P.OP_CHANNEL_RETRACT))
        op = self.op
        if op is not None and not self.silent:
            self._step_op(op, dt)
        for ch in range(4):
            self._step_motion(ch, dt)
        self._step_buffers(dt)

    def _step_op(self, op, dt):
        ch = op.channel
        source = self.source(ch)
        now = self.reactor.monotonic()
        if op.type == P.OP_CHANNEL_RETRACT:
            moved = self.world.pull(source, op.speed * dt)
            travelled = abs(source.encoder_mm - op.start_mm)
            if not source.present:
                self._finish(P.OP_STATE_FAILED, 3)
            elif travelled >= op.target_mm - 1e-6 or (source.tip is not None and source.tip <= 0):
                self._finish(P.OP_STATE_DONE, 1)
            elif now >= op.deadline:
                self._finish(P.OP_STATE_FAILED, 2)
            return
        if not source.present:
            self._finish(P.OP_STATE_FAILED, 3)
            return
        if self.motion[ch] == P.MOTION_IDLE:
            self._finish(P.OP_STATE_FAILED, 9)
            return
        before = source.encoder_mm
        moved = self.world.push(source, op.speed * dt)
        if source.stalled:
            self.buffer_pct[ch] = min(100.0, self.buffer_pct[ch] + STALL_RISE_PCT_S * dt)
        measured = abs(source.encoder_mm - op.start_mm)
        pct = self.buffer_pct[ch]
        if pct <= 3:
            self._finish(P.OP_STATE_FAILED, 5)
        elif op.stop_on_contact and pct >= op.contact_pct:
            self._finish(P.OP_STATE_DONE, 10)
        elif not op.stop_on_contact and pct >= op.contact_pct:
            self._finish(P.OP_STATE_FAILED, 5)
        elif measured >= op.target_mm:
            if op.stop_on_contact:
                self._finish(P.OP_STATE_FAILED, 11)
            else:
                self._finish(P.OP_STATE_DONE, 1)
        elif now >= op.deadline:
            self._finish(P.OP_STATE_FAILED, 2)

    def _step_motion(self, ch, dt):
        motion = self.motion[ch]
        if self.op is not None and self.op.channel == ch:
            return
        source = self.source(ch)
        if motion == P.MOTION_PULL_BACK:
            target = self.retract_m[ch] * 1000.0
            done = self.pull_start.get(ch, source.encoder_mm) - source.encoder_mm
            if done < target - 1e-6 and source.tip is not None and source.tip > 0:
                self.world.pull(source, self.config[P.CONFIG_PULL_SPEED_MMS] * dt)
            else:
                self.motion[ch] = P.MOTION_IDLE
                self.route_state[ch] = P.ROUTE_EMPTY
                if self.now_channel == ch:
                    self.now_channel = 0xff
                self.event_counter += 1
                self.send_status()
        elif motion == P.MOTION_SEND_OUT:
            # MODEL: a bare SEND_OUT keeps pushing until the buffer is full
            if self.buffer_pct[ch] < 90 and not source.stalled:
                self.world.push(source, self.config[P.CONFIG_LOAD_SPEED_MMS] * dt)
            if source.stalled:
                self.buffer_pct[ch] = min(100.0, self.buffer_pct[ch] + STALL_RISE_PCT_S * dt)

    def _step_buffers(self, dt):
        for ch in range(4):
            if self.op is not None and self.op.channel == ch:
                continue
            source = self.source(ch)
            if self.motion[ch] not in (P.MOTION_SEND_OUT,):
                if source.stalled and self.motion[ch] == P.MOTION_ON_USE:
                    continue
                if self.buffer_pct[ch] > FREE_BUFFER_PCT:
                    self.buffer_pct[ch] = max(FREE_BUFFER_PCT,
                                              self.buffer_pct[ch] - DRAIN_PCT_S * dt)
                elif self.buffer_pct[ch] < FREE_BUFFER_PCT:
                    self.buffer_pct[ch] = min(FREE_BUFFER_PCT,
                                              self.buffer_pct[ch] + DRAIN_PCT_S * dt)

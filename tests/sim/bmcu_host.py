"""Wires the real BMCUManager to a fake helper and fake firmware, with no seams.

``device.py`` talks to a Unix-socket sidecar with ``os.read``/``os.write`` on a real file
descriptor. Here the modules' ``os``, ``socket`` and ``time`` names are swapped for proxies
(patched from outside, nothing in ``bmcu_core`` changes):

* ``FakeSocketModule``: ``socket.socket`` returns a fake stream socket connected to the
  ``FakeHelper`` (or a datagram socket for the sidecar control path and the U1 planner);
* ``FakeOS``: ``read``/``write`` on fake descriptors go to the helper, ``urandom`` is seeded;
* ``FakeTime``: ``monotonic`` is the virtual clock;
* ``FakeWriter`` replaces ``DurableWriteWorker`` with a synchronous in-process writer.

The helper speaks ``transport.py`` framing, so ``MessageDecoder``/``encode_message`` run for
real. The firmware model is in bmcu_fw.py.
"""
import errno
import json
import os as _os
import queue as _queue
import random
import socket as _socket
import time as _time

import _support  # noqa: F401
from bmcu_core import device as device_mod
from bmcu_core import gcode_plan
from bmcu_core import manager as manager_mod
from bmcu_core import persistence
from bmcu_core import transport

from . import klipper as K

BASE_FD = 1000
STREAM, DGRAM = 1, 2


class _Conn(object):
    """One host-side connection to the helper."""

    def __init__(self, helper, fd):
        self.helper = helper
        self.fd = fd
        self.rx = bytearray()
        self.closed = False
        self.decoder = transport.MessageDecoder()

    def read(self, count):
        if self.rx:
            data = bytes(self.rx[:count])
            del self.rx[:count]
            return data
        if self.closed:
            return b''
        raise BlockingIOError(errno.EAGAIN, 'would block')

    def write(self, data):
        if self.closed:
            raise BrokenPipeError(errno.EPIPE, 'closed')
        for message in self.decoder.feed(data):
            self.helper.host_message(self, message)
        return len(data)

    def push(self, frame):
        self.rx.extend(frame)


class FakeHelper(object):
    """bmcu_transportd.py stand-in: owns the 'serial port', relays packets."""
    LATENCY = 0.004

    def __init__(self, bus, name, firmware):
        self.bus = bus
        self.name = name
        self.firmware = firmware
        self.running = True            # the socket exists
        self.stuck = False             # HelperStuck: accepts, never reports a session
        self.link_online = True        # physical link to the BMCU
        self.generation = 1
        self.conns = []
        self.ctl_log = []
        self.reactor = bus.sim.reactor
        self.seq = 1
        firmware.send_packet = self._packet_to_host
        self._keepalive = self.reactor.register_timer(
            self._keepalive_event, self.reactor.monotonic() + 1.0)

    def rebind(self, bus):
        """Klipper restarted but this helper process did not: new bus, new reactor, no
        clients. (The old reactor's timers and in-flight messages are gone with it.)"""
        self.bus = bus
        self.reactor = bus.sim.reactor
        self.conns = []
        self.firmware.send_packet = self._packet_to_host
        self._keepalive = self.reactor.register_timer(
            self._keepalive_event, self.reactor.monotonic() + 1.0)

    # -- connections ------------------------------------------------------------------
    def accept(self):
        if not self.running:
            raise FileNotFoundError(errno.ENOENT, 'no sidecar socket')
        conn = _Conn(self, self.bus.new_fd(None))
        self.bus.fds[conn.fd] = conn
        self.conns.append(conn)
        if not self.stuck:
            self._send_link(conn)
        return conn

    def _send_link(self, conn):
        conn.push(transport.encode_message(
            transport.OP_LINK, 1 if self.link_online else 0, self.generation, 0,
            b'' if self.link_online else b'BMCU serial port is not ready'))

    def set_link(self, online):
        self.link_online = bool(online)
        self.generation += 1
        for conn in list(self.conns):
            if not conn.closed:
                self._send_link(conn)

    def exit(self):
        """The helper process dies: its socket disappears and every client sees EOF."""
        self.running = False
        self.drop_clients()

    def restart(self):
        """The supervisor (bootstrap --repair) starts a fresh helper on the same port."""
        self.running = True
        self.stuck = False
        self.generation += 1

    def drop_clients(self):
        for conn in list(self.conns):
            conn.closed = True
        self.conns = []

    def _keepalive_event(self, now):
        for conn in self.conns:
            if not conn.closed and not self.stuck:
                conn.push(transport.encode_message(transport.OP_KEEPALIVE))
        # the sidecar polls the firmware for status once a second
        if self.link_online and not self.stuck and self.conns:
            self.firmware.send_status(0)
        return now + 1.0

    # -- relaying ------------------------------------------------------------------------
    def host_message(self, conn, message):
        op, arg1, arg2, arg3, payload = message
        if op != transport.OP_SEND:
            return
        if self.stuck or not self.link_online:
            conn.push(transport.encode_message(
                transport.OP_ERROR, arg2, 0, 0, b'BMCU serial port is not ready'))
            return
        msg_type, cmd_id = int(arg1) & 0xff, int(arg2) & 0xffffffff
        self.reactor.register_callback(
            lambda now: self.firmware.handle(msg_type, cmd_id, payload),
            self.reactor.monotonic() + self.LATENCY)

    def _packet_to_host(self, msg_type, cmd_id, payload):
        if not self.link_online or not self.running:
            return              # the cable is out (or the helper is dead): nothing arrives
        seq = self.seq
        self.seq += 1
        frame = transport.encode_message(transport.OP_PACKET, msg_type, seq, cmd_id,
                                         payload)
        self.reactor.register_callback(
            lambda now: self._deliver(frame), self.reactor.monotonic() + self.LATENCY)

    def _deliver(self, frame):
        if self.stuck:
            return
        for conn in self.conns:
            if not conn.closed:
                conn.push(frame)

    def control(self, command):
        self.ctl_log.append(bytes(command))


class _Dgram(object):
    def __init__(self, bus):
        self.bus = bus
        self.reply = []

    def bind(self, path):
        return None

    def setblocking(self, flag):
        return None

    def settimeout(self, value):
        return None

    def sendto(self, data, path):
        return self.bus.dgram(self, bytes(data), path)

    def recv(self, count):
        if self.reply:
            return self.reply.pop(0)
        raise BlockingIOError(errno.EAGAIN, 'would block')

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Stream(object):
    def __init__(self, bus):
        self.bus = bus
        self.conn = None
        self.fd = bus.new_fd(self)

    def settimeout(self, value):
        return None

    def setblocking(self, flag):
        return None

    def connect(self, path):
        helper = self.bus.helpers.get(str(path).replace(chr(92), '/'))
        if helper is None:
            raise FileNotFoundError(errno.ENOENT, 'no such socket', path)
        self.conn = helper.accept()
        self.fd = self.conn.fd

    def fileno(self):
        return self.fd

    def close(self):
        if self.conn is not None:
            self.conn.closed = True
            if self.conn in self.conn.helper.conns:
                self.conn.helper.conns.remove(self.conn)
        self.bus.fds.pop(self.fd, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class FakeSocketModule(object):
    AF_UNIX = 1
    SOCK_STREAM = STREAM
    SOCK_DGRAM = DGRAM
    timeout = _socket.timeout
    error = OSError

    def __init__(self, bus):
        self._bus = bus

    def socket(self, family=1, kind=STREAM, *args):
        if kind == DGRAM:
            return _Dgram(self._bus)
        return _Stream(self._bus)

    def __getattr__(self, name):
        return getattr(_socket, name)


class _PathProxy(object):
    """os.path that keeps '/...' paths as written (the manager requires them)."""

    def __getattr__(self, name):
        return getattr(_os.path, name)

    def abspath(self, path):
        text = str(path)
        if text.startswith('/'):
            return text
        return _os.path.abspath(text)


class FakeOS(object):
    def __init__(self, bus):
        self._bus = bus
        self.path = _PathProxy()
        self._random = random.Random(12345)

    def read(self, fd, count):
        conn = self._bus.fds.get(fd)
        if isinstance(conn, _Conn):
            return conn.read(count)
        return _os.read(fd, count)

    def write(self, fd, data):
        conn = self._bus.fds.get(fd)
        if isinstance(conn, _Conn):
            return conn.write(bytes(data))
        return _os.write(fd, data)

    def urandom(self, count):
        return bytes(bytearray(self._random.getrandbits(8) for _ in range(count)))

    def __getattr__(self, name):
        return getattr(_os, name)


class FakeTime(object):
    def __init__(self, reactor):
        self._reactor = reactor

    def monotonic(self):
        return self._reactor.monotonic()

    def time(self):
        return 1.7e9 + self._reactor.monotonic()

    def sleep(self, seconds):
        self._reactor.pause(self._reactor.monotonic() + seconds)

    def __getattr__(self, name):
        return getattr(_time, name)


class FakeWriter(object):
    """Replacement for DurableWriteWorker (no thread, no fchmod).

    Writes land ``rig.write_lag`` seconds after they are queued (0 = at once). A write that
    has not landed when the machine dies (power cut, Klipper crash) is lost, like the real
    worker thread's queue."""

    def __init__(self, reactor, rig, queue_limit=16):
        self.reactor = reactor
        self.rig = rig
        self.closed = False
        self.queue = _queue.Queue()
        self.stats = {'writes': 0, 'failures': 0, 'timeouts': 0,
                      'max_write_ms': 0.0, 'queue_overflows': 0}

    def write(self, path, payload, mode=0o600, timeout=30.0, sync=False):
        if sync:
            return True
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        rig = self.rig
        if not rig.alive:
            return False
        if rig.write_lag <= 0:
            self._land(path, payload)
            return True
        item = (path, payload)
        rig.pending_writes.append(item)
        self.reactor.register_callback(
            lambda now, item=item: self._land_pending(item),
            self.reactor.monotonic() + rig.write_lag)
        return True

    def _land_pending(self, item):
        rig = self.rig
        if rig.alive and item in rig.pending_writes:
            rig.pending_writes.remove(item)
            self._land(*item)

    def _land(self, path, payload):
        with open(path, 'wb') as stream:
            stream.write(payload)
        self.stats['writes'] += 1

    def sync(self, path, timeout=30.0):
        return True

    def close(self):
        self.closed = True


class Bus(object):
    """Owns the fake descriptors and routes sockets to helpers."""

    def __init__(self, sim, socket_dir):
        self.sim = sim
        self.socket_dir = socket_dir
        self.fds = {}
        self._next = BASE_FD
        self.helpers = {}
        self.planner_requests = 0

    def new_fd(self, obj):
        fd = self._next
        self._next += 1
        if obj is not None:
            self.fds[fd] = obj
        return fd

    def fd_ready(self, fd):
        conn = self.fds.get(fd)
        return bool(isinstance(conn, _Conn) and (conn.rx or conn.closed))

    def dgram(self, sock, data, path):
        path = str(path).replace(chr(92), '/')
        if path.endswith('.ctl'):
            name = _os.path.basename(path)[:-len('.sock.ctl')]
            helper = self.helpers.get(path[:-len('.ctl')])
            if helper is not None:
                helper.control(data)
                sock.reply.append(json.dumps(
                    {'control': data.decode('ascii'), 'ok': True}).encode('utf-8'))
                return len(data)
            raise FileNotFoundError(errno.ENOENT, 'no control socket', path)
        if path.endswith('u1-planner.sock'):
            return self._planner(data)
        raise FileNotFoundError(errno.ENOENT, 'unknown datagram path', path)

    def _planner(self, data):
        """bmcu_plannerd.py: scan the G-code and publish the result file."""
        self.planner_requests += 1
        request = json.loads(data.decode('utf-8'))
        request_id = str(request['id'])
        try:
            result = gcode_plan.scan_file(request['path'], request.get('identity'))
            payload = {'ok': True, 'request_id': request_id,
                       'schema': gcode_plan.PLAN_SCHEMA, 'result': result}
        except Exception as exc:        # same shape the daemon publishes
            payload = {'ok': False, 'request_id': request_id,
                       'schema': gcode_plan.PLAN_SCHEMA, 'error': str(exc)}
        directory = _os.path.join(self.socket_dir, 'u1-plans')
        if not _os.path.isdir(directory):
            _os.makedirs(directory)
        with open(_os.path.join(directory, request_id + '.json'), 'w') as stream:
            json.dump(payload, stream)
        return len(data)


class BmcuRig(object):
    """Everything the tests need to reach the BMCU side."""

    def __init__(self, sim, config_values=None, with_device=True, keep=None, routes=None):
        self.sim = sim
        self.with_device = with_device
        self.config_values = config_values
        self.alive = True              # False once the machine this rig belongs to is dead
        self.write_lag = 0.0           # seconds a queued durable write needs to land
        self.pending_writes = []       # queued, not yet on disk
        self.keep = keep               # (firmware, helper) that survive a Klipper restart
        self.routes = routes           # route states the firmware boots with (power cycle)
        self.socket_dir = '/tmp/bmcu-sim-%d' % id(sim)
        _os.makedirs(self.socket_dir + '/u1-plans', exist_ok=True)
        self.bus = Bus(sim, self.socket_dir)
        sim.reactor.fd_ready = self.bus.fd_ready
        self.patches = []
        self.firmware = {}
        self.helpers = {}
        self.start(config_values)

    # -- patching ----------------------------------------------------------------------------
    def _patch(self, obj, name, value):
        self.patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def start(self, config_values=None):
        sim = self.sim
        bus = self.bus
        self._patch(device_mod, 'os', FakeOS(bus))
        self._patch(device_mod, 'socket', FakeSocketModule(bus))
        self._patch(manager_mod, 'os', FakeOS(bus))
        self._patch(manager_mod, 'socket', FakeSocketModule(bus))
        self._patch(transport, 'socket', FakeSocketModule(bus))
        clock = FakeTime(sim.reactor)
        self._patch(transport, 'time', clock)
        self._patch(gcode_plan, 'time', clock)
        self._patch(manager_mod, 'DurableWriteWorker',
                    lambda reactor, **kwargs: FakeWriter(reactor, self, **kwargs))
        if self.with_device:
            from . import bmcu_fw
            # the helper's socket for one device named bmcu0
            sock_path = '%s/%s.sock' % (self.socket_dir, 'bmcu0')
            if self.keep is not None:
                fw, helper = self.keep
                fw.rebind(sim)
                helper.rebind(bus)
            else:
                fw = bmcu_fw.FakeFirmware(sim, routes=self.routes)
                helper = FakeHelper(bus, 'bmcu0', fw)
            bus.helpers[sock_path] = helper
            self.firmware['bmcu0'] = fw
            self.helpers['bmcu0'] = helper
        values = {
            'transport_socket_dir': self.socket_dir,
            'devices': 'bmcu0,/dev/ttyBMCU0' if self.with_device else '',
            'state_file': sim.tmp + '/bmcu_state.json',
            'u1_planner_timeout': 5.0,
        }
        values.update(config_values or {})
        config = K.FakeConfig(sim.printer, values)
        self.manager = manager_mod.BMCUManager(config)
        sim.manager = self.manager

    def stop(self):
        while self.patches:
            obj, name, value = self.patches.pop()
            setattr(obj, name, value)
        try:
            import shutil
            shutil.rmtree(self.socket_dir, ignore_errors=True)
        except Exception:
            pass

    @property
    def fw(self):
        return self.firmware['bmcu0']

    @property
    def helper(self):
        return self.helpers['bmcu0']


def attach(sim, config_values=None, with_device=True, keep=None, routes=None):
    sim.bmcu = BmcuRig(sim, config_values, with_device, keep=keep, routes=routes)
    return sim.bmcu

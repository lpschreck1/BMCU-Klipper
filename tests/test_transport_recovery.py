"""Transport helper recovery: a helper without a BMCU session must not be kept
or left sitting on an open port."""
import json
import logging
import os
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, 'scripts'), os.path.join(ROOT, 'klippy', 'extras'),
             os.path.join(ROOT, 'vendor', 'pyserial-3.5-py2.py3-none-any.whl')):
    if path not in sys.path:
        sys.path.insert(0, path)

import bmcu_host_bootstrap as bootstrap  # noqa: E402


class ReuseDecisionTest(unittest.TestCase):
    def status(self, **values):
        record = {'name': 'bmcu0', 'pid': 1259, 'online': False, 'serial_open': True,
                  'serial_released': False, 'uid': '', 'handshake_phase': 'wait_hello'}
        record.update(values)
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        json.dump(record, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return bootstrap._transport_reusable_state(handle.name, 1259, 'bmcu0', 'UID1')

    def test_online_helper_is_kept(self):
        self.assertTrue(self.status(online=True, uid='UID1')[0])

    def test_helper_released_for_update_is_kept(self):
        self.assertTrue(self.status(serial_open=False, serial_released=True)[0])

    def test_helper_without_session_is_restarted(self):
        keep, why = self.status()
        self.assertFalse(keep)
        self.assertIn('no BMCU session (phase wait_hello)', why)

    def test_wrong_device_or_process(self):
        self.assertFalse(self.status(online=True, uid='OTHER')[0])
        self.assertFalse(self.status(online=True, uid='UID1', pid=4242)[0])
        self.assertFalse(self.status(online=True, uid='UID1', name='bmcu1')[0])

    def test_missing_status_file(self):
        keep, why = bootstrap._transport_reusable_state(
            '/nonexistent/bmcu0.status.json', 1, 'bmcu0', '')
        self.assertFalse(keep)
        self.assertIn('unreadable', why)


@unittest.skipUnless(hasattr(os, 'openpty'), 'needs a pseudo-terminal')
class HelperBackstopTest(unittest.TestCase):
    """The helper never sits on an open port without a session."""
    def daemon(self, timeout):
        import bmcu_transportd as transportd
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        os.set_blocking(master, False)
        self.master = master
        directory = tempfile.mkdtemp()
        lock = open(os.path.join(directory, 'lock'), 'w+')
        self.addCleanup(lock.close)
        args = types.SimpleNamespace(
            name='bmcu0', port=os.ttyname(slave), socket=os.path.join(directory, 's.sock'),
            baud=115200, heartbeat=5.0, connection_timeout=timeout, reconnect=0.2,
            status_interval=1.0, connect_settle=0.05,
            status_file=os.path.join(directory, 'status.json'), settings_digest='',
            expected_uid='UID1', scan_fallback=False, exclude_ports=[],
            lifetime_lock=lock, process_record={})
        daemon = transportd.TransportDaemon(args)
        daemon.setup()
        self.addCleanup(daemon.cleanup)
        return daemon

    def drive(self, daemon, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            daemon._periodic(time.monotonic())
            if daemon.serial is not None:
                daemon._flush_serial()
            try:
                os.read(self.master, 4096)
            except (BlockingIOError, OSError):
                pass
            time.sleep(0.02)

    def test_silent_bmcu_times_out_and_logs(self):
        daemon = self.daemon(timeout=0.4)
        with self.assertLogs(level='INFO') as logs:
            self.drive(daemon, 1.0)
        text = '\n'.join(logs.output)
        self.assertIn('HELLO/session handshake timeout', text)
        self.assertGreater(daemon.hello_attempts, 0)

    def test_backstop_closes_when_handshake_timer_is_lost(self):
        daemon = self.daemon(timeout=0.4)
        daemon._not_ready_limit = lambda: 0.5
        self.drive(daemon, 0.2)
        daemon.handshake_started_at = 0.0      # the path the normal timeout misses
        with self.assertLogs(level='INFO') as logs:
            self.drive(daemon, 0.6)
        self.assertIn('no BMCU session', '\n'.join(logs.output))

    def test_status_file_reports_handshake_progress(self):
        daemon = self.daemon(timeout=5.0)
        self.drive(daemon, 0.3)
        daemon._write_status_file(force=True)
        with open(daemon.args.status_file) as stream:
            status = json.load(stream)
        self.assertEqual(status['handshake_phase'], 'wait_hello')
        self.assertFalse(status['online'])
        self.assertIn('hello_attempts', status)


if __name__ == '__main__':
    unittest.main()

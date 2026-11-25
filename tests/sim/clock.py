"""Virtual-time reactor: one clock, no real sleeps.

Extends tests/_support.FakeReactor. ``pause(t)`` advances virtual time to ``t``,
running due timers, one-shot callbacks, fd handlers and world steppers in order.
Everything is deterministic: ties run in registration order.
"""
import _support


class SimReactor(_support.FakeReactor):
    STEP = 0.05            # largest world integration step, seconds

    def __init__(self, start=100.0):
        _support.FakeReactor.__init__(self)
        self.now = float(start)
        self._seq = 0
        self._callbacks = []       # (waketime, seq, fn)
        self.steppers = []         # fn(dt), integrate physical state
        self.fd_handlers = {}      # handle -> dict(read=fn, write=fn, ready=fn)
        self._next_fd_handle = 1
        self._running = set()
        self.pause_depth = 0

    # -- Klipper reactor API -------------------------------------------------
    def register_timer(self, callback, waketime=_support.FakeReactor.NEVER):
        timer = {'callback': callback, 'waketime': waketime, 'active': True,
                 'seq': self._seq}
        self._seq += 1
        self.timers.append(timer)
        return timer

    def update_timer(self, timer, waketime):
        self.updates.append((timer, waketime))
        timer['waketime'] = waketime

    def register_callback(self, callback, waketime=_support.FakeReactor.NOW):
        self._seq += 1
        self._callbacks.append((waketime, self._seq, callback))

    def register_fd(self, fd, read_callback, write_callback=None):
        handle = self._next_fd_handle
        self._next_fd_handle += 1
        self.fd_handlers[handle] = {
            'fd': fd, 'read': read_callback, 'write': write_callback,
            'want_read': True, 'want_write': False}
        return handle

    def unregister_fd(self, handle):
        self.fd_handlers.pop(handle, None)

    def set_fd_wake(self, handle, is_readable=True, is_writeable=False):
        item = self.fd_handlers.get(handle)
        if item is not None:
            item['want_read'] = bool(is_readable)
            item['want_write'] = bool(is_writeable)

    # fd readiness is decided by whoever owns the fd (the fake helper)
    fd_ready = None            # fn(fd) -> bool, installed by the BMCU fake

    # -- time ---------------------------------------------------------------
    def add_stepper(self, fn):
        self.steppers.append(fn)

    def _advance_to(self, target):
        """Integrate the world up to ``target`` in steps of at most STEP."""
        while self.now < target:
            end = min(target, self.now + self.STEP)
            dt = end - self.now
            for fn in list(self.steppers):
                fn(dt)
            self.now = end

    def _next_event_time(self, limit):
        best = limit
        for timer in self.timers:
            if timer['active'] and id(timer) not in self._running:
                if timer['waketime'] < best:
                    best = timer['waketime']
        for waketime, _seq, _fn in self._callbacks:
            if waketime < best:
                best = waketime
        return best

    def _run_due(self):
        """Run everything due at self.now. Returns True if anything ran."""
        ran = False
        due = [c for c in self._callbacks if c[0] <= self.now]
        if due:
            due.sort(key=lambda c: (c[0], c[1]))
            for item in due:
                self._callbacks.remove(item)
            for _w, _s, fn in due:
                fn(self.now)
                ran = True
        for timer in sorted(self.timers, key=lambda t: (t['waketime'], t['seq'])):
            if (timer['active'] and timer['waketime'] <= self.now and
                    id(timer) not in self._running):
                self._running.add(id(timer))
                try:
                    timer['waketime'] = timer['callback'](self.now)
                finally:
                    self._running.discard(id(timer))
                ran = True
        if self.fd_ready is not None:
            for handle in sorted(self.fd_handlers):
                item = self.fd_handlers.get(handle)
                if item is None:
                    continue
                if item['want_read'] and self.fd_ready(item['fd']):
                    item['read'](self.now)
                    ran = True
                    continue
                if item['want_write'] and item['write'] is not None:
                    item['write'](self.now)
                    ran = True
        return ran

    def pause(self, waketime):
        self.pause_depth += 1
        try:
            guard = 0
            while True:
                guard += 1
                if guard > 1000000:
                    raise RuntimeError('SimReactor.pause did not converge')
                if self._run_due():
                    continue
                if self.now >= waketime:
                    break
                nxt = self._next_event_time(waketime)
                # step in small slices so fds and steppers stay live
                if nxt <= self.now:      # only a running timer is due: let time pass
                    nxt = waketime
                self._advance_to(min(nxt, self.now + self.STEP))
            return self.now
        finally:
            self.pause_depth -= 1

    def run_until(self, until):
        while self.now <= until:
            self.pause(self.now + 0.25)

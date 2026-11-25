"""Virtual-time reactor: one clock, no real sleeps, Klipper's greenlet concurrency.

Extends tests/_support.FakeReactor. Like Klipper's reactor, every timer callback, one-shot
callback and fd handler runs in its own greenlet; ``pause(t)`` registers a wake-up for the
calling greenlet and switches to the dispatch loop, which advances virtual time, integrates
the world (``add_stepper``) and resumes whatever is due, in a deterministic order (earliest
wake time first, ties by registration order). Background BMCU jobs therefore interleave with
the foreground G-code exactly as they do on the printer.
"""
import logging

import greenlet

import _support


class SimReactor(_support.FakeReactor):
    STEP = 0.05            # largest world integration step, seconds
    NEVER = _support.FakeReactor.NEVER

    def __init__(self, start=100.0):
        _support.FakeReactor.__init__(self)
        self.now = float(start)
        self._seq = 0
        self._callbacks = []       # [waketime, seq, fn]
        self._sleepers = []        # [waketime, seq, greenlet]
        self.steppers = []         # fn(dt), integrate physical state
        self.fd_handlers = {}      # handle -> dict
        self._next_fd_handle = 1
        self._running = set()      # ids of timers whose callback is running
        self._dispatch = None
        self.fd_ready = None       # fn(fd) -> bool, installed by the BMCU fake
        self.exceptions = []       # unhandled exceptions of timers/callbacks
        self.root = greenlet.getcurrent()

    # -- Klipper reactor API -------------------------------------------------
    def register_timer(self, callback, waketime=NEVER):
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
        self._callbacks.append([waketime, self._seq, callback])

    def register_fd(self, fd, read_callback, write_callback=None):
        handle = self._next_fd_handle
        self._next_fd_handle += 1
        self.fd_handlers[handle] = {
            'fd': fd, 'read': read_callback, 'write': write_callback,
            'want_read': True, 'want_write': False, 'running': False}
        return handle

    def unregister_fd(self, handle):
        self.fd_handlers.pop(handle, None)

    def set_fd_wake(self, handle, is_readable=True, is_writeable=False):
        item = self.fd_handlers.get(handle)
        if item is not None:
            item['want_read'] = bool(is_readable)
            item['want_write'] = bool(is_writeable)

    def add_stepper(self, fn):
        self.steppers.append(fn)

    # -- pausing ---------------------------------------------------------------
    def pause(self, waketime):
        current = greenlet.getcurrent()
        if current is self._dispatch:
            raise RuntimeError('pause() called from the reactor dispatch greenlet')
        if waketime <= self.now and not self._anything_due():
            return self.now
        self._seq += 1
        self._sleepers.append([waketime, self._seq, current])
        if self._dispatch is None or self._dispatch.dead:
            self._dispatch = greenlet.greenlet(self._loop)
        self._dispatch.switch()
        if self.exceptions and current is self.root:
            raise self.exceptions.pop(0)
        return self.now

    def run_until(self, until):
        while self.now <= until:
            self.pause(self.now + 0.25)

    # -- dispatch ----------------------------------------------------------------
    def _anything_due(self):
        for item in self._callbacks:
            if item[0] <= self.now:
                return True
        for timer in self.timers:
            if (timer['active'] and timer['waketime'] <= self.now and
                    id(timer) not in self._running):
                return True
        return False

    def _next_wake(self):
        best = self.NEVER
        for item in self._sleepers:
            best = min(best, item[0])
        for item in self._callbacks:
            best = min(best, item[0])
        for timer in self.timers:
            if timer['active'] and id(timer) not in self._running:
                best = min(best, timer['waketime'])
        return best

    def _advance_to(self, target):
        while self.now < target:
            end = min(target, self.now + self.STEP)
            dt = end - self.now
            for fn in list(self.steppers):
                fn(dt)
            self.now = end

    def _start(self, fn, *args):
        def guarded(*a):
            try:
                return fn(*a)
            except greenlet.GreenletExit:
                raise
            except Exception as exc:
                # Klipper would shut down; record it and surface it in the test
                logging.exception('unhandled exception in a reactor callback')
                self.exceptions.append(exc)
        g = greenlet.greenlet(guarded, parent=self._dispatch)
        g.switch(*args)

    def _run_timer(self, timer):
        self._running.add(id(timer))
        try:
            result = timer['callback'](self.now)
        finally:
            self._running.discard(id(timer))
        timer['waketime'] = result

    def _run_fd(self, item, kind):
        try:
            item[kind](self.now)
        finally:
            item['running'] = False

    def _run_due(self):
        """Start/resume everything due at ``self.now``. True if anything ran."""
        ran = False
        due = [c for c in self._callbacks if c[0] <= self.now]
        due.sort(key=lambda c: (c[0], c[1]))
        for item in due:
            if item in self._callbacks:
                self._callbacks.remove(item)
                self._start(item[2], self.now)
                ran = True
        for timer in sorted(self.timers, key=lambda t: (t['waketime'], t['seq'])):
            if (timer['active'] and timer['waketime'] <= self.now and
                    id(timer) not in self._running):
                self._start(self._run_timer, timer)
                ran = True
        if self.fd_ready is not None:
            for handle in sorted(self.fd_handlers):
                item = self.fd_handlers.get(handle)
                if item is None or item['running']:
                    continue
                if item['want_read'] and self.fd_ready(item['fd']):
                    item['running'] = True
                    self._start(self._run_fd, item, 'read')
                    ran = True
                elif (item['want_write'] and item['write'] is not None and
                        not item['running']):
                    item['running'] = True
                    self._start(self._run_fd, item, 'write')
                    ran = True
        due = [s for s in self._sleepers if s[0] <= self.now]
        due.sort(key=lambda s: (s[0], s[1]))
        for item in due:
            if item in self._sleepers:
                self._sleepers.remove(item)
                item[2].switch()
                ran = True
        return ran

    def _loop(self):
        guard = 0
        while True:
            if self._run_due():
                guard += 1
                if guard > 2000000:
                    raise RuntimeError('SimReactor made no progress')
                continue
            guard = 0
            nxt = self._next_wake()
            if nxt >= self.NEVER:
                # nothing scheduled: only a stepper (world) can change things
                nxt = self.now + self.STEP
            self._advance_to(min(max(nxt, self.now), self.now + self.STEP))

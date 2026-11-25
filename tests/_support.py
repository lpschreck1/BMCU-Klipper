"""Shared setup for the hardware-free tests.

The Klipper module is imported straight from klippy/extras, with stand-ins for the
Klipper C helpers it can import at load time. Nothing here talks to a printer.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRAS = os.path.join(ROOT, 'klippy', 'extras')
SCRIPTS = os.path.join(ROOT, 'scripts')

for _path in (EXTRAS, SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Klipper's C helper modules aren't installed outside a printer.
for _name in ('chelper', 'queuefile'):
    sys.modules.setdefault(_name, types.ModuleType(_name))


class FakeReactor(object):
    """Enough of Klipper's reactor for timers driven by hand."""
    NOW = 0.0
    NEVER = 9999999999999999.0

    def __init__(self):
        self.now = 100.0
        self.timers = []
        self.updates = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback, waketime=NOW):
        timer = {'callback': callback, 'waketime': waketime, 'active': True}
        self.timers.append(timer)
        return timer

    def pause(self, waketime):
        self.now = max(self.now, waketime)
        return self.now

    def unregister_timer(self, timer):
        timer['active'] = False

    def update_timer(self, timer, waketime):
        self.updates.append((timer, waketime))

    def run_until(self, until):
        """Advance time, calling due timers, until `until`."""
        while self.now <= until:
            for timer in self.timers:
                if timer['active'] and timer['waketime'] <= self.now:
                    timer['waketime'] = timer['callback'](self.now)
            self.now += 0.25

"""Resume sweep (M6): every fault type at every tool change of the reference print.

For each (trigger, fault) the reference print runs, the fault is injected just before the
trigger line, and the "user" fixes it and presses Resume (up to ``MAX_RESUMES`` times). The
outcome is one of:

* ``no_effect``  the print completed without pausing (the fault did not matter there)
* ``resumed``    it paused, and fix + Resume finished the print
* ``stuck``      it could not be finished (still paused / failed after the resumes)

Run ``python tests/sim/sweep.py`` (from the repo root) to print the table, or
``python tests/sim/sweep.py --write`` to refresh tests/sim/SWEEP.md.
"""
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import _support  # noqa: E402,F401
from sim import faults as F  # noqa: E402
from sim import checks  # noqa: E402
from sim.runner import Sim  # noqa: E402

PLAN = [[0], [4, 6], [3]]
SPOOLS = {0: 'PLA grey', 4: 'PLA red', 6: 'PLA black', 3: 'PLA white'}
MAX_RESUMES = 2

TRIGGERS = {
    'print start': 'PRINT_START',
    'auto feed T0': 'BMCU_AUTO_FEED EXTRUDER=0',
    'auto feed head 3': 'BMCU_AUTO_FEED EXTRUDER=2',
    'auto feed head 4': 'BMCU_AUTO_FEED EXTRUDER=3',
    'tool change T4': 'BMCU_TOOL_CHANGE TOOL=4',
    'tool change T6': 'BMCU_TOOL_CHANGE TOOL=6',
    'tool change T3': 'BMCU_TOOL_CHANGE TOOL=3',
    # earlier than the tool changes: BMCU prestages the next source in the background
    # while the previous tool prints, so a fault at the change line is often too late
    'layer 2 start': 'SET_PRINT_STATS_INFO TOTAL_LAYER=3 CURRENT_LAYER=2',
    'layer 3 start': 'SET_PRINT_STATS_INFO TOTAL_LAYER=3 CURRENT_LAYER=3',
}

FAULTS = {
    'snag T4 58mm': lambda: F.Snag('bmcu0', 58.0),
    'snag T6 58mm': lambda: F.Snag('bmcu2', 58.0),
    'clog head 3': lambda: F.Clog(2),
    'clog head 4': lambda: F.Clog(3),
    'deformed T4': lambda: F.DeformedTip('bmcu0'),
    'deformed T6': lambda: F.DeformedTip('bmcu2'),
    'deformed stock T0': lambda: F.DeformedTip('stock0'),
    'deformed stock T3': lambda: F.DeformedTip('stock3'),
}

CASES = (
    [('print start', f) for f in FAULTS] +
    [('auto feed T0', 'deformed stock T0')] +
    [('auto feed head 3', f) for f in ('snag T4 58mm', 'clog head 3', 'deformed T4')] +
    [('auto feed head 4', f) for f in ('clog head 4', 'deformed stock T3')] +
    [('tool change T4', f) for f in ('snag T4 58mm', 'clog head 3', 'deformed T4')] +
    [('tool change T6', f) for f in ('snag T6 58mm', 'clog head 4', 'deformed T6')] +
    [('tool change T3', f) for f in ('clog head 4', 'deformed stock T3')] +
    [('layer 2 start', f) for f in ('snag T4 58mm', 'clog head 3', 'deformed T4',
                                    'snag T6 58mm', 'clog head 4', 'deformed T6')] +
    [('layer 3 start', f) for f in ('clog head 4', 'deformed stock T3')]
)


def run_case(trigger_name, fault_name):
    sim = Sim.reference_machine()
    try:
        sim.boot(15.0)
        sim.setup_u1()
        sim.load_spools(SPOOLS)
        sim.slice(PLAN)
        sim.inject(F.GcodeLine(TRIGGERS[trigger_name]), FAULTS[fault_name]())
        state = sim.run_print()
        if state == 'complete':
            return 'no_effect', ''
        first = sim.print_stats.message
        for _ in range(MAX_RESUMES):
            sim.user.clear_fault()
            try:
                state = sim.user.resume()
            except Exception as exc:        # Snapmaker refused the Resume itself
                state = 'refused'
                first = '%s | resume: %s' % (first, exc)
                continue
            if state == 'complete':
                return 'resumed', first
        return 'stuck', '%s -> %s: %s' % (first, state, sim.print_stats.message)
    except Exception as exc:
        return 'stuck', 'sim error: %r' % (exc,)
    finally:
        sim.close()



# -- plan 2 sections (M8 onwards): any trigger, any fault, the shared checks -------------------

PLAN2 = [[0], [4, 6], [5], [6, 0]]
SPOOLS2 = {0: 'PLA grey', 4: 'PLA red', 5: 'PLA blue', 6: 'PLA black'}
FULL = bool(os.environ.get('BMCU_SIM_FULL'))


def run_spec(trigger, fault, actions=('clear_fault',), plan=None, spools=None, hook=None):
    """One case: the reference print, ``fault`` armed at ``trigger``, then the user's
    ``actions`` before each Resume. Returns ``(kind, detail)`` where ``kind`` is
    ``no_effect``, ``resumed``, ``stuck`` or ``dirty`` (finished, but a post-check failed)."""
    sim = Sim.reference_machine()
    try:
        sim.boot(15.0)
        sim.setup_u1()
        sim.load_spools(spools or SPOOLS2)
        sim.slice(plan or PLAN2)
        if trigger is not None:
            sim.inject(trigger, fault)
        if hook is not None:
            hook(sim)
        outcome = checks.recover(sim, actions)
        if outcome.state == 'complete':
            if sim.active_faults:                  # e.g. a cable still unplugged at the end
                checks._apply_actions(sim, actions)
            outcome.problems.extend(checks.verify(sim))
        detail = outcome.first_message
        if outcome.state != 'complete':
            return 'stuck', '; '.join(outcome.problems) or detail
        if outcome.problems:
            return 'dirty', '; '.join(outcome.problems)
        return outcome.kind, detail
    except Exception as exc:
        return 'stuck', 'sim error: %r' % (exc,)
    finally:
        sim.close()


def _disconnect_cases():
    points = [
        ('idle mid-layer', lambda: F.Idle(40.0)),
        ('load 50 mm', lambda: F.BmcuMoving('bmcu0', 'load', 50)),
        ('load 500 mm', lambda: F.BmcuMoving('bmcu0', 'load', 500)),
        ('unload 50 mm', lambda: F.BmcuMoving('bmcu0', 'unload', 50)),
        ('BEFORE_ON_USE', lambda: F.BeforeOnUse(2)),
        ('prestaging T5', lambda: F.Prestaging(5)),
    ]
    durations = [('3 s', 3.0), ('30 s', 30.0), ('until replug', None)]
    cases = []
    for point, trig in points:
        for label, duration in durations:
            cases.append((point, 'disconnect %s' % label, trig,
                          lambda d=duration: F.BmcuDisconnect(d), ('replug',)))
    return cases


def _staging_cases():
    cases = []
    for point, trig, source in (
            ('prestaging T5', lambda: F.Prestaging(5), 'bmcu1'),
            ('load T5 20 mm', lambda: F.BmcuMoving('bmcu1', 'load', 20), 'bmcu1'),
            ('load T4 20 mm', lambda: F.BmcuMoving('bmcu0', 'load', 20), 'bmcu0')):
        cases.append((point, 'snag %s 58 mm' % source, trig,
                      lambda s=source: F.Snag(s, 58.0), ('clear_fault',)))
        cases.append((point, 'deformed %s' % source, trig,
                      lambda s=source: F.DeformedTip(s), ('clear_fault',)))
        cases.append((point, 'runout %s' % source, trig,
                      lambda s=source: F.Runout(s), ('clear_fault',)))
    return cases


def _helper_cases():
    points = [
        ('idle mid-layer', lambda: F.Idle(40.0)),
        ('load 50 mm', lambda: F.BmcuMoving('bmcu0', 'load', 50)),
        ('unload 50 mm', lambda: F.BmcuMoving('bmcu0', 'unload', 50)),
        ('BEFORE_ON_USE', lambda: F.BeforeOnUse(2)),
    ]
    cases = []
    for point, trig in points:
        cases.append((point, 'helper exit, repaired by user', trig,
                      lambda: F.HelperExit(None), ('clear_fault',)))
    cases.append(('load 50 mm', 'helper exit, repaired after 30 s',
                  lambda: F.BmcuMoving('bmcu0', 'load', 50),
                  lambda: F.HelperExit(30.0), ('clear_fault',)))
    return cases


SECTIONS = [
    ('M8 disconnect sweep',
     'A BMCU cable glitch (3 s), an unplug and replug (30 s) and a cable that stays out '
     'until the user replugs it, injected at BMCU phases (faults.py phase triggers). The '
     'user replugs and presses Resume. The plan is `T0 | T4 T6 | T5 | T6 T0`.',
     _disconnect_cases),
    ('M8 staging faults',
     'Snag, deformed tip and runout injected through BMCU phases, so they hit the load '
     'itself and not the tool-change line (the open item of the M6 sweep).',
     _staging_cases),
    ('M8 helper death',
     'The transport helper process exits mid-print. Its socket vanishes; the supervisor '
     '(`bmcu_host_bootstrap.py --repair`) starts a new one when the user (or 30 s later) '
     'does.',
     _helper_cases),
]


def run_section(cases):
    rows = []
    for point, fault, trigger, fault_factory, actions in cases:
        kind, detail = run_spec(trigger(), fault_factory(), actions)
        rows.append((point, fault, kind, detail))
    return rows


_CACHE = []


def cached_sweep():
    """The sweep, run once per process (the tests share it)."""
    if not _CACHE:
        _CACHE.append(run_sweep())
    return _CACHE[0]


def run_sweep(full=None):
    """``[(title, intro, rows)]``: the M6 table first, then the plan 2 sections."""
    logging.disable(logging.CRITICAL)
    rows = []
    for trigger, fault in CASES:
        outcome, detail = run_case(trigger, fault)
        rows.append((trigger, fault, outcome, detail))
    sections = [('M6 resume sweep', None, rows)]
    for title, intro, factory in SECTIONS:
        sections.append((title, intro, run_section(factory())))
    return sections


def _table(rows, first_col, last_col):
    lines = ['| %s | Fault | Outcome | %s |' % (first_col, last_col), '|---|---|---|---|']
    for trigger, fault, outcome, detail in rows:
        lines.append('| %s | %s | %s | %s |' % (
            trigger, fault, outcome, detail.replace('|', '/')[:220]))
    return lines


def _totals(rows):
    names = ('no_effect', 'resumed', 'stuck', 'dirty')
    counts = [sum(r[2] == n for r in rows) for n in names]
    text = '**%d cases, %d no effect, %d resumed, %d stuck' % (
        len(rows), counts[0], counts[1], counts[2])
    if counts[3]:
        text += ', %d finished dirty' % counts[3]
    return text + '.**'


def render(sections):
    first = sections[0][2]
    lines = ['# Resume sweep', '',
             'Reference print `T0 | T4 T6 | T3`, each fault injected just before the trigger line, '
             'then the user clears it and presses Resume (up to %d times). Regenerate with '
             '`python tests/sim/sweep.py --write`.' % MAX_RESUMES, '',
             '`no_effect` for BMCU faults at a tool change or layer start means BMCU had already '
             'loaded that source in the background (it prestages while the previous tool prints), '
             'so the fault came too late; the faults that matter for BMCU loads are the '
             '`print start` and `auto feed` rows. `deformed` and `clog` clears model the user '
             'hand-feeding the filament through (faults.py).', '']
    lines += _table(first, 'Trigger', 'First pause message')
    lines += ['', _totals(first)]
    for title, intro, rows in sections[1:]:
        lines += ['', '## %s' % title, '', intro,
                  '', 'Outcomes: `no_effect` (finished without pausing), `resumed` (paused, fix '
                  '+ Resume finished it), `stuck` (no user action finished it), `dirty` '
                  '(finished, but the shared checks of `sim/checks.py` failed: BMCU and the '
                  'world disagree, a lock was left, or a head printed dry).', '']
        lines += _table(rows, 'Point', 'First pause message / problem')
        lines += ['', _totals(rows)]
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    started = time.time()
    table = render(run_sweep())
    print(table)
    print('(%.1f s)' % (time.time() - started))
    if '--write' in sys.argv:
        with open(os.path.join(HERE, 'SWEEP.md'), 'w') as stream:
            stream.write(table)

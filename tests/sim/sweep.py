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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import _support  # noqa: E402,F401
from sim import faults as F  # noqa: E402
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


def run_sweep():
    logging.disable(logging.CRITICAL)
    rows = []
    for trigger, fault in CASES:
        outcome, detail = run_case(trigger, fault)
        rows.append((trigger, fault, outcome, detail))
    return rows


def render(rows):
    lines = ['# Resume sweep', '',
             'Reference print `T0 | T4 T6 | T3`, each fault injected just before the trigger line, '
             'then the user clears it and presses Resume (up to %d times). Regenerate with '
             '`python tests/sim/sweep.py --write`.' % MAX_RESUMES, '',
             '`no_effect` for BMCU faults at a tool change or layer start means BMCU had already '
             'loaded that source in the background (it prestages while the previous tool prints), '
             'so the fault came too late; the faults that matter for BMCU loads are the '
             '`print start` and `auto feed` rows. `deformed` and `clog` clears model the user '
             'hand-feeding the filament through (faults.py).', '',
             '| Trigger | Fault | Outcome | First pause message |', '|---|---|---|---|']
    for trigger, fault, outcome, detail in rows:
        lines.append('| %s | %s | %s | %s |' % (
            trigger, fault, outcome, detail.replace('|', '/')[:220]))
    stuck = [r for r in rows if r[2] == 'stuck']
    lines += ['', '**%d cases, %d no effect, %d resumed, %d stuck.**' % (
        len(rows), sum(r[2] == 'no_effect' for r in rows),
        sum(r[2] == 'resumed' for r in rows), len(stuck))]
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    table = render(run_sweep())
    print(table)
    if '--write' in sys.argv:
        with open(os.path.join(HERE, 'SWEEP.md'), 'w') as stream:
            stream.write(table)

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Time the U1 print-start planner on every G-code file on this printer.

Each print starts with a scan of the whole file to plan BMCU tool changes.
Klipper waits for the scan before the print moves, so slow scans delay every
print start.  This runs the same scan the planner runs and lists the slowest
files first.

    python3 scripts/bmcu_planner_bench.py            # the usual G-code folders
    python3 scripts/bmcu_planner_bench.py DIR|FILE   # somewhere else

Read-only: it never changes a file.
"""

from __future__ import print_function

import argparse
import glob
import os
import sys
import time

_RUNTIME = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_EXTRAS = os.path.join(_RUNTIME, 'klippy', 'extras')
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)

from bmcu_core.gcode_plan import PlanError, scan_file  # noqa: E402

DEFAULT_DIRS = ['/userdata/gcodes', '/oem/printer_data/gcodes'] + sorted(
    glob.glob('/home/*/printer_data/gcodes'))
# BMCU's u1_planner_timeout default.  BMCU keeps waiting past it while the
# planner is still scanning, but a scan this long still delays the print start.
TIMEOUT_S = 15.0
SLOW_S = 10.0


def gcode_files(targets):
    seen = set()
    for target in targets:
        if os.path.isfile(target):
            candidates = [target]
        elif os.path.isdir(target):
            candidates = []
            for root, dirs, names in os.walk(target):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for name in names:
                    if name.lower().endswith('.gcode'):
                        candidates.append(os.path.join(root, name))
        else:
            continue
        for path in candidates:
            real = os.path.realpath(path)
            if real not in seen and os.path.isfile(real):
                seen.add(real)
                yield real


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('targets', nargs='*',
                        help='G-code files or folders (default: %s)' %
                        ', '.join(DEFAULT_DIRS))
    args = parser.parse_args()
    targets = args.targets or [d for d in DEFAULT_DIRS if os.path.isdir(d)]
    if not targets:
        print('No G-code folder found; pass one, e.g. %s /userdata/gcodes' %
              sys.argv[0])
        return 2

    rows = []
    files = list(gcode_files(targets))
    if not files:
        print('No .gcode files in: %s' % ', '.join(targets))
        return 2
    print('Scanning %d file(s) in %s ...' % (len(files), ', '.join(targets)))
    for index, path in enumerate(files, 1):
        size_mb = os.path.getsize(path) / 1e6
        started = time.monotonic()
        error = ''
        changes = 0
        try:
            changes = len(scan_file(path)['plan'])
        except (PlanError, OSError) as exc:
            error = str(exc)
        seconds = time.monotonic() - started
        rows.append((seconds, size_mb, changes, error, path))
        sys.stdout.write('\r  %d/%d' % (index, len(files)))
        sys.stdout.flush()
    print()

    rows.sort(reverse=True)
    print('%8s %9s %8s  %s' % ('scan s', 'size MB', 'changes', 'file'))
    for seconds, size_mb, changes, error, path in rows:
        flag = ('  << over %.0f s' % TIMEOUT_S if seconds > TIMEOUT_S else
                '  < slow' if seconds > SLOW_S else '')
        if error:
            flag = '  ERROR: %s' % error
        print('%8.1f %9.1f %8d  %s%s' % (
            seconds, size_mb, changes, os.path.basename(path), flag))

    slowest, size_mb = rows[0][0], rows[0][1]
    errors = [row for row in rows if row[3]]
    print()
    print('Slowest scan %.1f s (%.1f MB, %.1f s per 10 MB).' % (
        slowest, size_mb, slowest / max(size_mb, 0.1) * 10.0))
    if slowest > TIMEOUT_S:
        print('Over %.0f s: prints wait this long before moving. BMCU-Klipper '
              'with the planner-wait fix keeps waiting; older builds fail to '
              'start.' % TIMEOUT_S)
    if errors:
        print('%d file(s) could not be planned; those prints would fail to '
              'start.' % len(errors))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())

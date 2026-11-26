"""M6: the resume sweep. Fails if any fault/trigger combination can no longer be finished
by fix + Resume, and keeps tests/sim/SWEEP.md honest."""
import os
import unittest

import _support  # noqa: F401
from sim import sweep

# Combinations known to be unresumable (also listed in sim/FINDINGS.md). Empty today.
KNOWN_STUCK = set()


class ResumeSweepTest(unittest.TestCase):
    def test_every_pause_can_be_finished_by_resume(self):
        rows = sweep.cached_sweep()[0][2]
        stuck = {(t, f) for t, f, outcome, _ in rows if outcome == 'stuck'}
        self.assertEqual(stuck - KNOWN_STUCK, set(), 'new unresumable cases')
        self.assertEqual(KNOWN_STUCK - stuck, set(), 'fixed: remove from KNOWN_STUCK')
        self.assertGreaterEqual(sum(r[2] == 'resumed' for r in rows), 10)

    def test_committed_table_matches(self):
        rows = sweep.cached_sweep()
        path = os.path.join(os.path.dirname(sweep.__file__), 'SWEEP.md')
        with open(path) as stream:
            self.assertEqual(stream.read().replace('\r\n', '\n'), sweep.render(rows),
                             'run: python tests/sim/sweep.py --write')


if __name__ == '__main__':
    unittest.main()

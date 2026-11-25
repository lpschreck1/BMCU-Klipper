"""State file loading: damaged or foreign files fall back to defaults."""
import json
import logging
import os
import shutil
import tempfile
import unittest

import _support  # noqa: F401  (path setup)
from bmcu_core import state


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'bmcu_state.json')
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data):
        with open(self.path, 'w') as stream:
            stream.write(data if isinstance(data, str) else json.dumps(data))

    def test_missing_file_uses_defaults(self):
        self.assertEqual(state.StateStore(self.path).data, state.default_state())

    def test_corrupt_json_uses_defaults(self):
        self.write('{not json')
        self.assertEqual(state.StateStore(self.path).data, state.default_state())

    def test_non_object_root_uses_defaults(self):
        self.write('[1, 2, 3]')
        self.assertEqual(state.StateStore(self.path).data, state.default_state())

    def test_nan_constants_are_rejected(self):
        self.write('{"schema": NaN}')
        self.assertEqual(state.StateStore(self.path).data, state.default_state())

    def test_unsupported_schema_uses_defaults(self):
        base = state.default_state()
        base['schema'] = 'something-else'
        self.write(base)
        self.assertEqual(state.StateStore(self.path).data, state.default_state())

    def test_invalid_sections_fall_back_per_section(self):
        base = state.default_state()
        base['endpoints'] = ['not', 'a', 'dict']
        base['tools'] = 'nope'
        self.write(base)
        data = state.StateStore(self.path).data
        default = state.default_state()
        self.assertEqual(data['endpoints'], default['endpoints'])
        self.assertEqual(data['tools'], default['tools'])

    def test_unknown_endpoint_keys_survive(self):
        # per-head settings such as u1_preload_clear are kept across loads
        base = state.default_state()
        endpoint = dict(next(iter(base['endpoints'].values()))) if base['endpoints'] else {}
        if not endpoint:
            self.skipTest('default state has no endpoints to copy')
        name = next(iter(base['endpoints']))
        endpoint['u1_preload_clear'] = True
        base['endpoints'][name] = endpoint
        self.write(base)
        loaded = state.StateStore(self.path).data['endpoints'][name]
        self.assertTrue(loaded.get('u1_preload_clear'))



class ChannelAutoloadStateTest(unittest.TestCase):
    """Saved autoload lengths up to 2 m survive a reload instead of being cut to 1 m."""
    def test_two_metres_kept(self):
        self.assertEqual(state._clean_channel_state({'autoload_mm': 2000.0}, 0)['autoload_mm'], 2000.0)
        self.assertEqual(state._clean_channel_state({'autoload_mm': 1500.0}, 0)['autoload_mm'], 1500.0)

    def test_clamped_to_range(self):
        self.assertEqual(state._clean_channel_state({'autoload_mm': 5000.0}, 0)['autoload_mm'], 2000.0)
        self.assertEqual(state._clean_channel_state({'autoload_mm': 1.0}, 0)['autoload_mm'], 10.0)


if __name__ == '__main__':
    unittest.main()

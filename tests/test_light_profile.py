"""Guard the checked-in light profile's coverage and generated problem sizes."""
import json
from pathlib import Path
import unittest

from solver_benchmark.problems import generate_case
from solver_benchmark.profiles import validate_profile

ROOT = Path(__file__).resolve().parents[1]


class LightProfileTests(unittest.TestCase):
    def test_catalog_coverage_and_actual_orders(self):
        def read(name):
            return json.loads((ROOT / 'examples' / name).read_text())

        profile = validate_profile(read('light-profile.json'))
        expected = {case['id'] for name in ('benchmark-profile.json', 'comparison-profile.json')
                    for case in read(name)['cases']}
        self.assertEqual({case['id'] for case in profile['cases']}, expected)
        self.assertEqual(len(profile['passes']), 1)
        timing = profile['passes'][0]
        self.assertEqual(timing['instrumentation']['kind'], 'baseline')
        self.assertEqual((timing['warmup_runs'], timing['measured_runs']), (1, 5))
        for case in profile['cases']:
            with self.subTest(case=case['id']):
                self.assertEqual(case['rhs']['count'], 1)
                problem = generate_case(case)
                self.assertGreaterEqual(problem['matrix']['order'], 9000)
                self.assertLessEqual(problem['matrix']['order'], 11000)
                self.assertEqual(problem['dense']['columns'], 1)

"""Check the heavy cloud recipe without materializing its large matrices."""
import json
import math
from pathlib import Path
import unittest

from solver_benchmark.profiles import validate_profile

ROOT = Path(__file__).resolve().parents[1]


class HeavyProfileTests(unittest.TestCase):
    def test_heavy_recipe_and_cloud_resources(self):
        cloud = json.loads((ROOT / 'cloud/profiles/heavy.json').read_text())
        profile = validate_profile(json.loads((ROOT / cloud['benchmark_profile']).read_text()))
        self.assertEqual({case['id'] for case in profile['cases']}, {
            'laplacian-1d', 'laplacian-2d', 'laplacian-3d', 'advection', 'wave',
            'transport-3d', 'wave-3d-indefinite',
        })
        self.assertEqual({solver['id'] for solver in profile['solvers']},
                         {'eigen-sparse-lu', 'superlu'})
        for case in profile['cases']:
            params = case['generator']['parameters']
            if case['generator']['id'].startswith('laplacian_'):
                dims = [params[key] for key in ('mx', 'my', 'mz') if key in params]
                order = math.prod(dims)
                bound = order + 2 * sum((n - 1) * (order // n) for n in dims)
            else:
                order = math.prod(params[key] for key in ('nx', 'ny', 'nz') if key in params)
                bound = 9 * order  # extended_problems' conservative preflight
            self.assertEqual(order, 200_000, case['id'])
            self.assertLessEqual(bound, cloud['max_entries'], case['id'])
            self.assertEqual(case['rhs']['count'], 1)

        self.assertEqual({p['id'] for p in profile['passes']}, {'timing', 'resources'})
        self.assertTrue(all(p['case_timeout_seconds'] <= 1800 for p in profile['passes']))
        # The whole task, including setup and generation, is bounded separately
        # from the per-pass limits; it can end before all passes complete.
        self.assertEqual(cloud['max_run_seconds'], 7200)
        self.assertEqual(cloud['upload_grace_seconds'], 120)
        self.assertGreaterEqual(cloud['boot_disk_gb'], 100)
        self.assertEqual({target['id'] for target in cloud['targets']}, {'intel', 'amd', 'arm'})
        for target in (target for target in cloud['targets'] if target['id'] != 'arm'):
            self.assertEqual(target['cpu_milli'], 32000)
            self.assertGreaterEqual(target['memory_mib'], 240000)
        light = json.loads((ROOT / 'cloud/profiles/light.json').read_text())
        self.assertEqual(next(t for t in cloud['targets'] if t['id'] == 'arm'),
                         next(t for t in light['targets'] if t['id'] == 'arm'))


if __name__ == '__main__':
    unittest.main()

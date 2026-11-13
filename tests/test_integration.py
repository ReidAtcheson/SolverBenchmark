import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from solver_benchmark.cli import execute
from solver_benchmark.profiles import canonical_hash

ROOT=Path(__file__).resolve().parents[1]
WORKER=os.environ.get('SB_EIGEN_WORKER','')

@unittest.skipUnless(WORKER and Path(WORKER).is_file(), 'Eigen worker not built')
class EndToEnd(unittest.TestCase):
    def test_profile_to_artifacts(self):
        profile=json.loads((ROOT/'examples/benchmark-profile.json').read_text())
        profile['solvers'][0]['options']['thread_limit'] = 'available_cores'
        for case in profile['cases']:
            for key in case['generator']['parameters']:
                if key.startswith('m'): case['generator']['parameters'][key]=3
        complex_case=copy.deepcopy(profile['cases'][1]); complex_case['id']='complex-2d'
        complex_case['encoding']['kind']='complex_interleaved'; profile['cases'].append(complex_case)
        for measure in profile['passes']: measure['measured_runs']=1
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            recipe=base/'profile.json'; recipe.write_text(json.dumps(profile))
            deployment=base/'deployment.json'
            deployment.write_text(json.dumps({'schema_version':'1.0','solvers':{'eigen-sparse-lu':{
                'executable':str(Path(WORKER).resolve()),'arguments':[],'environment':{}}}}))
            output=base/'results'
            with patch('solver_benchmark.cli.available_physical_cores', return_value=2):
                self.assertEqual(execute(recipe,deployment,output),0)
            summary=json.loads((output/'summary.json').read_text())
            self.assertEqual(summary['outcomes'],{'success':8})
            self.assertEqual(summary['profile_hash'],canonical_hash(profile))
            rows=[json.loads(line) for line in (output/'results.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),8)
            for row in rows:
                self.assertEqual(row['options']['thread_limit'], 2)
                self.assertEqual(row['effective_thread_environment']['OMP_NUM_THREADS'], '2')
                self.assertEqual(len(row['summary']['accepted_measured_samples']),1)
                self.assertEqual(len(row['accuracy']),2)
                self.assertGreater(row['process']['max_rss_bytes'],0)
                self.assertEqual(row['process']['exit_code'],0)
                self.assertTrue(all(a['accuracy']['accepted'] for a in row['accuracy']))
                self.assertEqual(len(row['solution_artifacts']),2)
                if row['instrumentation']['kind']=='trace':
                    self.assertTrue(row['trace_complete'])
                    self.assertTrue(row['trace_artifact_hashes'])
            for case in profile['cases']:
                identity=json.loads((output/'inputs'/case['id']/'identity.json').read_text())
                self.assertTrue(identity['reference_hash'].startswith('sha256:'))

    def test_generated_physics_profile(self):
        profile=json.loads((ROOT/'examples/generated-physics-profile.json').read_text())
        for measure in profile['passes']:
            measure['warmup_runs']=0
            measure['measured_runs']=1
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            recipe=base/'profile.json'; recipe.write_text(json.dumps(profile))
            deployment=base/'deployment.json'
            deployment.write_text(json.dumps({'schema_version':'1.0','solvers':{'eigen-sparse-lu':{
                'executable':str(Path(WORKER).resolve()),'arguments':[],'environment':{}}}}))
            output=base/'results'
            self.assertEqual(execute(recipe,deployment,output),0)
            summary=json.loads((output/'summary.json').read_text())
            self.assertEqual(summary['outcomes'],{'success':len(profile['cases'])*len(profile['passes'])})
            rows=[json.loads(line) for line in (output/'results.jsonl').read_text().splitlines()]
            for row in rows:
                with self.subTest(case=row['case_id'], mode=row['pass_id']):
                    self.assertEqual(row['outcome'], 'success')
                    self.assertEqual(len(row['summary']['accepted_measured_samples']),1)
                    self.assertTrue(all(a['accuracy']['accepted'] for a in row['accuracy']))
                    if row['instrumentation']['kind']=='trace': self.assertTrue(row['trace_complete'])

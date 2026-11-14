"""Exercise the cloud worker's bounded partial-result upload locally."""
import json
import importlib.util
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_gcp_grace', ROOT / 'cloud/run_gcp.py')
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CloudGraceTests(unittest.TestCase):
    def test_grace_validation_and_job_environment(self):
        profile = cloud.load_profile(ROOT / 'cloud/profiles/light.json')
        for grace in (True, -1, 1, 29, profile['max_run_seconds'], 1.5):
            with self.subTest(grace=grace), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'cloud.json'
                path.write_text(json.dumps({**profile, 'upload_grace_seconds': grace}))
                with self.assertRaisesRegex(ValueError, 'invalid upload_grace_seconds'):
                    cloud.load_profile(path)
        for grace in (0, 120):
            job = cloud.job_config(profile['targets'][0], {**profile, 'upload_grace_seconds': grace},
                                   'test-bucket', 'a@b.example', 'us-central1-c')
            script = job['taskGroups'][0]['taskSpec']['runnables'][0]['script']['text']
            self.assertIn(f'export UPLOAD_GRACE_SECONDS={grace}\n', script)
            self.assertIn(f'export MAX_RUN_SECONDS={profile["max_run_seconds"]}\n', script)

    def test_interrupt_uploads_completed_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bin_dir = root / 'sb-venv/bin'
            bin_dir.mkdir(parents=True)
            (bin_dir / 'python').symlink_to('/bin/true')  # metadata helper
            benchmark = bin_dir / 'solver-benchmark'
            benchmark.write_text('''#!/usr/bin/python3
import pathlib, sys, time
out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
out.mkdir()
for name in ('execution.json', 'profile.json', 'version-manifest.json'):
    (out / name).write_text('{}')
(out / 'results.jsonl').write_text('{"case_id":"completed"}\\n')
time.sleep(60)
''')
            benchmark.chmod(0o755)
            source = (ROOT / 'cloud/gcp-worker.sh').read_text()
            tail = source[source.index('set +e\nif [[ "$SINGLE_CORE"'):]
            tail = tail.replace('/tmp/sb-venv', str(root / 'sb-venv')).replace('/tmp/sb-result', str(root / 'sb-result'))
            shell = f'''#!/bin/bash
set -euo pipefail
SECONDS=0
SINGLE_CORE=0 MAX_RUN_SECONDS=31 UPLOAD_GRACE_SECONDS=30 MAX_ENTRIES=1000
BENCHMARK_PROFILE=dummy MAX_RESULT_BYTES=10485760 RUN_BUCKET=test RUN_KEY=intel
token() {{ echo fake; }}
curl() {{ cp '{root}/sb-result.tar.gz' '{root}/uploaded.tar.gz'; }}
{tail}
'''
            result = subprocess.run(['bash'], input=shell, text=True, capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 124, result.stderr)
            with tarfile.open(root / 'uploaded.tar.gz') as archive:
                rows = archive.extractfile('results.jsonl').read().decode().splitlines()
            self.assertEqual(json.loads(rows[0])['case_id'], 'completed')

    def test_expensive_setup_skips_benchmark(self):
        with tempfile.TemporaryDirectory() as folder:
            source = (ROOT / 'cloud/gcp-worker.sh').read_text()
            tail = source[source.index('set +e\nif [[ "$SINGLE_CORE"'):]
            tail = tail.replace('/tmp/sb-result', str(Path(folder) / 'no-result'))
            shell = f'''#!/bin/bash
set -euo pipefail
SECONDS=31
SINGLE_CORE=0 MAX_RUN_SECONDS=31 UPLOAD_GRACE_SECONDS=30 MAX_ENTRIES=1000
BENCHMARK_PROFILE=dummy MAX_RESULT_BYTES=10485760 RUN_BUCKET=test RUN_KEY=intel
{tail}
'''
            result = subprocess.run(['bash'], input=shell, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertIn('Setup exhausted the benchmark budget', result.stderr)


if __name__ == '__main__':
    unittest.main()

import importlib.util
import io
import argparse
import itertools
import subprocess
from types import SimpleNamespace
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_gcp', ROOT / 'cloud/run_gcp.py')
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CloudGcpTests(unittest.TestCase):
    def test_profile_jobs_use_native_architectures_and_bounded_resources(self):
        profile = cloud.load_profile(ROOT / 'cloud/profiles/light.json')
        self.assertEqual({t['id'] for t in profile['targets']}, {'intel', 'amd', 'arm'})
        jobs = {t['id']: cloud.job_config(t, profile, 'test-bucket', 'a@b.example', 'us-central1-c')
                for t in profile['targets']}
        policy = jobs['arm']['allocationPolicy']['instances'][0]['policy']
        self.assertEqual(policy['bootDisk']['type'], 'hyperdisk-balanced')
        self.assertTrue(policy['bootDisk']['image'].endswith('debian-12-arm64'))
        self.assertEqual(jobs['intel']['allocationPolicy']['instances'][0]['policy']['minCpuPlatform'], 'Intel Ice Lake')
        for job in jobs.values():
            task = job['taskGroups'][0]['taskSpec']
            self.assertEqual(task['maxRunDuration'], str(profile['max_run_seconds']) + 's')
            self.assertEqual(task['maxRetryCount'], 0)
            self.assertIn('MAX_RESULT_BYTES', task['runnables'][0]['script']['text'])

    def test_extract_only_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'result.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for name in cloud.REPORT_FILES:
                    data = b'{}\n'
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            cloud.extract_result(archive, Path(tmp) / 'out', 10000)
            self.assertEqual({p.name for p in (Path(tmp) / 'out').iterdir()}, cloud.REPORT_FILES)
            with self.assertRaises(ValueError):
                cloud.extract_result(archive, Path(tmp) / 'another', 1)

    def test_extract_rejects_extra_files_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                info = tarfile.TarInfo('../escape')
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                cloud.extract_result(archive, Path(tmp) / 'out', 10000)

    def test_source_excludes_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'source.tar.gz'
            cloud.make_source(archive, 'examples/light-profile.json')
            with tarfile.open(archive) as tar:
                names = set(tar.getnames())
            self.assertIn('examples/light-profile.json', names)
            self.assertIn('cloud/gcp-worker.sh', names)
            self.assertNotIn('.git/config', names)
            self.assertFalse(any(name.startswith(('build/', '.venv/')) for name in names))

    def run_mocked(self, tmp, fail=None, state='SUCCEEDED'):
        args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'), project='example-project',
            service_account='test@example.iam.gserviceaccount.com', region='us-central1', zone='us-central1-c',
            output=str(Path(tmp) / 'output'), dry_run=False)
        calls = []
        def fake_call(*argv, capture=False, check=True):
            calls.append(argv)
            command = ' '.join(argv)
            if fail == 'create' and 'buckets create' in command:
                raise subprocess.CalledProcessError(1, argv)
            if fail == 'submit' and 'jobs submit' in command and '-amd ' in command:
                raise subprocess.CalledProcessError(1, argv)
            if fail == 'cleanup' and 'storage rm' in command:
                return SimpleNamespace(returncode=1, stdout='')
            if 'jobs describe' in command:
                if fail == 'describe-amd' and '-amd ' in command:
                    raise subprocess.CalledProcessError(1, argv)
                job_state = state
                if isinstance(state, dict):
                    job_state = next(value for key, value in state.items() if '-' + key + ' ' in command)
                return SimpleNamespace(returncode=0, stdout=json.dumps({'status': {'state': job_state}}))
            if 'storage cp gs://' in command:
                destination = Path(argv[4])
                if destination.suffix == '.gz':
                    with tarfile.open(destination, 'w:gz') as archive:
                        for name in cloud.REPORT_FILES:
                            info = tarfile.TarInfo(name)
                            info.size = 2
                            archive.addfile(info, io.BytesIO(b'{}'))
                return SimpleNamespace(returncode=0, stdout='')
            return SimpleNamespace(returncode=0, stdout='')
        def fake_source(path, benchmark):
            path.write_bytes(b'source')
        with mock.patch.object(cloud, 'call', side_effect=fake_call), mock.patch.object(cloud, 'make_source', side_effect=fake_source):
            try:
                result = cloud.run(args)
            except (subprocess.CalledProcessError, RuntimeError) as exc:
                result = exc
        return result, [' '.join(c) for c in calls]

    def test_resource_lifecycle_success_and_cancellation(self):
        for state, expected in [('SUCCEEDED', 0), ('CANCELLED', 1)]:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                result, calls = self.run_mocked(tmp, state=state)
                self.assertEqual(result, expected)
                self.assertEqual(sum('jobs delete' in c for c in calls), 3)
                self.assertEqual(sum('storage rm' in c for c in calls), 1)

    def test_create_failure_does_not_delete_unowned_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, fail='create')
            self.assertIsInstance(result, subprocess.CalledProcessError)
            self.assertFalse(any('storage rm' in c for c in calls))

    def test_submit_failure_cleans_only_confirmed_jobs_and_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, fail='submit')
            self.assertIsInstance(result, subprocess.CalledProcessError)
            self.assertEqual(sum('jobs delete' in c for c in calls), 1)
            self.assertTrue(any('storage rm' in c for c in calls))

    def test_cleanup_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_mocked(tmp, fail='cleanup')
            self.assertIsInstance(result, RuntimeError)
            self.assertIn('Cleanup failed', str(result))

    def test_completed_results_survive_a_later_poll_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, fail='describe-amd')
            self.assertIsInstance(result, subprocess.CalledProcessError)
            output = Path(tmp) / 'output'
            self.assertEqual({p.name for p in (output / 'intel').iterdir()}, cloud.REPORT_FILES)
            self.assertEqual(json.loads((output / 'cloud-run.json').read_text())['states']['intel'], 'SUCCEEDED')
            self.assertTrue(any('storage rm' in c for c in calls))
            self.assertEqual(sum('jobs delete' in c for c in calls), 3)

    def test_jobs_still_queued_after_an_hour_are_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0, 0, 0, 0], itertools.repeat(3601))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, state='SCHEDULED')
            self.assertEqual(result, 1)
            self.assertEqual(sum('jobs cancel' in c for c in calls), 3)
            states = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())['states']
            self.assertEqual(set(states.values()), {'QUEUE_TIMEOUT'})
            self.assertTrue(any('storage rm' in c for c in calls))

    def test_successful_results_are_retained_when_another_job_cannot_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0, 0, 0, 0], itertools.repeat(3601))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, state={'intel': 'SUCCEEDED', 'amd': 'SUCCEEDED', 'arm': 'SCHEDULED'})
            self.assertEqual(result, 1)
            output = Path(tmp) / 'output'
            self.assertEqual({p.name for p in (output / 'intel').iterdir()}, cloud.REPORT_FILES)
            self.assertEqual({p.name for p in (output / 'amd').iterdir()}, cloud.REPORT_FILES)
            states = json.loads((output / 'cloud-run.json').read_text())['states']
            self.assertEqual(states, {'intel': 'SUCCEEDED', 'amd': 'SUCCEEDED', 'arm': 'QUEUE_TIMEOUT'})
            self.assertEqual(sum('jobs cancel' in c for c in calls), 1)

    def test_state_write_failure_does_not_prevent_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_text = Path.write_text
            def fail_state(path, *args, **kwargs):
                if path.name == 'cloud-run.json':
                    raise OSError('disk full')
                return write_text(path, *args, **kwargs)
            with mock.patch.object(Path, 'write_text', fail_state):
                result, calls = self.run_mocked(tmp)
            self.assertIsInstance(result, RuntimeError)
            self.assertIn('Could not save cloud-run.json', str(result))
            self.assertEqual(sum('jobs delete' in c for c in calls), 3)
            self.assertTrue(any('storage rm' in c for c in calls))


if __name__ == '__main__':
    unittest.main()

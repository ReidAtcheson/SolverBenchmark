import importlib.util
import io
import argparse
import itertools
import os
import subprocess
from types import SimpleNamespace
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from urllib import error
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_gcp', ROOT / 'cloud/run_gcp.py')
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CloudGcpTests(unittest.TestCase):
    def test_worker_manifest_records_and_verifies_loaded_openblas(self):
        # Exercise the worker's manifest generation without needing a VM or a
        # full SuperLU build. A different loaded backend must fail closed.
        script = (ROOT / 'cloud/gcp-worker.sh').read_text().split("python3 - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            superlu, expected, other = (folder / name for name in ('libsuperlu.so', 'libopenblas.so', 'other-openblas.so'))
            for path in (superlu, expected, other):
                path.touch()
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                with mock.patch.dict(os.environ, {'SB_OPENBLAS_LIBRARY': str(expected)}):
                    for loaded in (expected, other):
                        ldd = f'libsuperlu.so => {superlu} (0x1)\nlibopenblas.so.0 => {loaded} (0x2)\n'
                        with mock.patch('subprocess.check_output', return_value=ldd):
                            if loaded == other:
                                with self.assertRaisesRegex(RuntimeError, 'did not load'):
                                    exec(script, {})
                            else:
                                exec(script, {})
                                manifest = json.loads((folder / 'cloud-deployment.json').read_text())
                                self.assertEqual(manifest['solvers']['superlu']['runtime_dependencies']['blas'], str(expected))
            finally:
                os.chdir(previous)

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
            self.assertIn('export SINGLE_CORE=0\n', task['runnables'][0]['script']['text'])

    def test_single_core_job_sets_mode(self):
        profile = cloud.load_profile(ROOT / 'cloud/profiles/light.json')
        job = cloud.job_config(profile['targets'][0], profile, 'test-bucket',
                               'a@b.example', 'us-central1-c', single_core=True)
        script = job['taskGroups'][0]['taskSpec']['runnables'][0]['script']['text']
        self.assertIn('export SINGLE_CORE=1\n', script)

    def test_profile_resource_overrides_and_defaults(self):
        profile = cloud.load_profile(ROOT / 'cloud/profiles/light.json')
        for overrides, expected_disk, expected_entries in (({}, 30, 5_000_000),
                                                            ({'boot_disk_gb': 200, 'max_entries': 80_000_000}, 200, 80_000_000)):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'profile.json'
                path.write_text(json.dumps({**profile, **overrides}))
                loaded = cloud.load_profile(path)
                for single_core in (False, True):
                    job = cloud.job_config(loaded['targets'][0], loaded, 'test-bucket',
                                           'a@b.example', 'us-central1-c', single_core=single_core)
                    self.assertEqual(job['allocationPolicy']['instances'][0]['policy']['bootDisk']['sizeGb'], expected_disk)
                    script = job['taskGroups'][0]['taskSpec']['runnables'][0]['script']['text']
                    self.assertIn(f'export MAX_ENTRIES={expected_entries}\n', script)
                    self.assertEqual(script.count('--max-entries "$MAX_ENTRIES"'), 2)

    def test_heavy_profile_job_resources(self):
        profile = cloud.load_profile(ROOT / 'cloud/profiles/heavy.json')
        light = cloud.load_profile(ROOT / 'cloud/profiles/light.json')
        self.assertEqual(next(t for t in profile['targets'] if t['id'] == 'arm'),
                         next(t for t in light['targets'] if t['id'] == 'arm'))
        for target in profile['targets']:
            with self.subTest(target=target['id']):
                job = cloud.job_config(target, profile, 'test-bucket', 'a@b.example', 'us-central1-c')
                self.assertEqual(job['allocationPolicy']['instances'][0]['policy']['bootDisk']['sizeGb'], 100)
                task = job['taskGroups'][0]['taskSpec']
                self.assertEqual(task['maxRunDuration'], '7200s')
                self.assertIn('export MAX_ENTRIES=10000000\n', task['runnables'][0]['script']['text'])

    def test_profile_rejects_invalid_resource_overrides(self):
        profile = cloud.load_profile(ROOT / 'cloud/profiles/light.json')
        for field, values in (('boot_disk_gb', (True, 0, 9, 65537, 1.5)),
                              ('max_entries', (True, 0, -1, 1 << 63, 1.5))):
            for value in values:
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'profile.json'
                    path.write_text(json.dumps({**profile, field: value}))
                    with self.assertRaisesRegex(ValueError, 'invalid ' + field):
                        cloud.load_profile(path)

    @unittest.skipUnless(hasattr(os, 'sched_getaffinity'), 'requires Linux CPU affinity')
    def test_single_core_profile_and_inherited_affinity(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, derived = Path(tmp) / 'source.json', Path(tmp) / 'derived.json'
            provenance, execution = Path(tmp) / 'affinity.json', Path(tmp) / 'execution.json'
            profile = {'solvers': [{'id': 'a', 'options': {'thread_limit': 'available_cores'}},
                                   {'id': 'b', 'options': {'thread_limit': 8}}], 'cases': []}
            source.write_text(json.dumps(profile))
            probe = ('import json,os,subprocess,sys; '
                     'print(json.dumps([sorted(os.sched_getaffinity(0)), '
                     'json.loads(subprocess.check_output([sys.executable, "-c", '
                     '"import json,os; print(json.dumps(sorted(os.sched_getaffinity(0))))"]))]))')
            output = subprocess.check_output([sys.executable, str(ROOT / 'cloud/single_core.py'),
                                              str(source), str(derived), sys.executable, '-c', probe], text=True,
                                             env={**os.environ, 'SB_SINGLE_CORE_AFFINITY_PATH': str(provenance)})
            parent_cpus, child_cpus = json.loads(output)
            self.assertEqual(len(parent_cpus), 1)
            self.assertEqual(parent_cpus, child_cpus)
            self.assertIn(parent_cpus[0], os.sched_getaffinity(0))
            actual = json.loads(derived.read_text())
            self.assertEqual([solver['options']['thread_limit'] for solver in actual['solvers']], [1, 1])
            self.assertEqual(actual['cases'], profile['cases'])
            self.assertEqual(json.loads(source.read_text()), profile)
            expected = {'mode': 'single_core', 'original_affinity': sorted(os.sched_getaffinity(0)),
                        'selected_cpu': parent_cpus[0]}
            self.assertEqual(json.loads(provenance.read_text()), expected)
            execution.write_text(json.dumps({'host': {'affinity': parent_cpus}, 'run_id': 'test'}))
            subprocess.run([sys.executable, str(ROOT / 'cloud/single_core.py'), '--attach',
                            str(provenance), str(execution)], check=True)
            attached = json.loads(execution.read_text())
            self.assertEqual(attached['affinity_control'], expected)
            self.assertEqual(attached['host']['affinity'], parent_cpus)
            self.assertEqual(attached['run_id'], 'test')

    def test_single_core_affinity_attachment_rejects_mismatch_without_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance, execution = Path(tmp) / 'affinity.json', Path(tmp) / 'execution.json'
            provenance.write_text(json.dumps({'mode': 'single_core', 'original_affinity': [0, 1],
                                               'selected_cpu': 0}))
            original = json.dumps({'host': {'affinity': [1]}, 'run_id': 'test'})
            execution.write_text(original)
            result = subprocess.run([sys.executable, str(ROOT / 'cloud/single_core.py'), '--attach',
                                     str(provenance), str(execution)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('does not match', result.stderr)
            self.assertEqual(execution.read_text(), original)

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
            self.assertIn('cloud/single_core.py', names)
            self.assertNotIn('.git/config', names)
            self.assertFalse(any(name.startswith(('build/', '.venv/')) for name in names))

    def run_mocked(self, tmp, fail=None, state='SUCCEEDED'):
        args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'), project='example-project',
            service_account='test@example.iam.gserviceaccount.com', region='us-central1', zone='us-central1-c',
            output=str(Path(tmp) / 'output'), dry_run=False)
        calls = []
        context = {'owner': None, 'describes': {}, 'downloads': {}, 'cancelled': set()}
        def fake_call(*argv, capture=False, check=True):
            calls.append(argv)
            command = ' '.join(argv)
            if fail in ('submit', 'submit-accepted', 'submit-unrelated', 'submit-interrupt') and 'jobs submit' in command and '-amd ' in command:
                if fail == 'submit-interrupt':
                    raise KeyboardInterrupt()
                raise subprocess.CalledProcessError(1, argv)
            if fail == 'cleanup' and 'storage rm' in command:
                return SimpleNamespace(returncode=1, stdout='')
            if 'jobs cancel' in command:
                context['cancelled'].add(argv[4])
                return SimpleNamespace(returncode=0, stdout='')
            if 'jobs describe' in command:
                if fail in ('submit', 'submit-accepted', 'submit-unrelated', 'submit-interrupt') and '-amd ' in command:
                    label = context['owner'] if fail in ('submit-accepted', 'submit-interrupt') else 'someone-else'
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'labels': {'sb-run': label}}))
                if fail in ('describe-amd', 'describe-once') and '-amd ' in command:
                    context['describes']['amd'] = context['describes'].get('amd', 0) + 1
                    if fail == 'describe-amd' or context['describes']['amd'] == 1:
                        return SimpleNamespace(returncode=1, stdout='')
                canceled_state = 'CANCELLATION_IN_PROGRESS' if fail == 'cancel-stuck' else 'CANCELLED'
                job_state = canceled_state if argv[4] in context['cancelled'] else state
                if isinstance(state, dict):
                    job_state = (canceled_state if argv[4] in context['cancelled'] else
                                 next(value for key, value in state.items() if '-' + key + ' ' in command))
                return SimpleNamespace(returncode=0, stdout=json.dumps({'status': {'state': job_state}}))
            if 'buckets describe' in command:
                label = context['owner'] if fail == 'create-accepted' else 'someone-else'
                return SimpleNamespace(returncode=0, stdout=json.dumps({'labels': {'sb-run': label}}))
            if 'storage cp gs://' in command:
                destination = Path(argv[4])
                if 'result-intel.tar.gz' in command and fail in ('download-once', 'download-always'):
                    context['downloads']['intel'] = context['downloads'].get('intel', 0) + 1
                    if fail == 'download-always' or context['downloads']['intel'] == 1:
                        return SimpleNamespace(returncode=1, stdout='')
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
        def fake_bucket(bucket, project, region, owner):
            context['owner'] = owner
            if fail in ('create', 'create-accepted'):
                raise subprocess.CalledProcessError(1, 'create')
        with mock.patch.object(cloud, 'call', side_effect=fake_call), mock.patch.object(cloud, 'make_source', side_effect=fake_source), \
             mock.patch.object(cloud, 'create_bucket', side_effect=fake_bucket), mock.patch.object(cloud.time, 'sleep'), \
             mock.patch.object(cloud, 'object_exists', return_value=None):
            try:
                result = cloud.run(args)
            except (subprocess.CalledProcessError, RuntimeError, KeyboardInterrupt) as exc:
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

    def test_ambiguous_bucket_create_deletes_only_matching_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, fail='create-accepted')
            self.assertIsInstance(result, subprocess.CalledProcessError)
            self.assertTrue(any('buckets describe' in c for c in calls))
            self.assertTrue(any('storage rm' in c for c in calls))

    def test_ambiguous_submission_reconciles_owner_and_interrupt(self):
        for failure, expected in [('submit-accepted', 2), ('submit-interrupt', 2), ('submit-unrelated', 1)]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                result, calls = self.run_mocked(tmp, fail=failure)
                self.assertIsInstance(result, (subprocess.CalledProcessError, KeyboardInterrupt))
                self.assertEqual(sum('jobs delete' in c for c in calls), expected)
                self.assertFalse(any('storage rm' in c for c in calls))

    def test_submit_failure_cleans_only_confirmed_jobs_and_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, fail='submit')
            self.assertIsInstance(result, subprocess.CalledProcessError)
            self.assertEqual(sum('jobs delete' in c for c in calls), 1)
            # A previously submitted worker may still have results to recover.
            self.assertFalse(any('storage rm' in c for c in calls))

    def test_cleanup_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_mocked(tmp, fail='cleanup')
            self.assertIsInstance(result, RuntimeError)
            self.assertIn('Cleanup failed', str(result))

    def test_completed_results_survive_a_later_poll_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, fail='describe-amd')
            self.assertEqual(result, 1)
            output = Path(tmp) / 'output'
            self.assertEqual({p.name for p in (output / 'intel').iterdir()}, cloud.REPORT_FILES)
            self.assertEqual(json.loads((output / 'cloud-run.json').read_text())['states']['intel'], 'SUCCEEDED')
            self.assertFalse(any('storage rm' in c for c in calls))
            self.assertTrue((output / 'arm').exists())
            self.assertTrue(json.loads((output / 'cloud-run.json').read_text())['bucket_retained'])
            self.assertEqual(sum('jobs delete' in c for c in calls), 3)

    def test_transient_read_and_download_errors_are_retried(self):
        for failure in ('describe-once', 'download-once'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                result, calls = self.run_mocked(tmp, fail=failure)
                self.assertEqual(result, 0)
                self.assertTrue((Path(tmp) / 'output/intel/results.jsonl').exists())
                self.assertTrue(any('storage rm' in c for c in calls))

    def test_persistent_download_error_retains_bucket_and_other_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, fail='download-always')
            self.assertEqual(result, 1)
            output = Path(tmp) / 'output'
            state = json.loads((output / 'cloud-run.json').read_text())
            self.assertEqual(state['states']['intel'], 'DOWNLOAD_FAILED')
            self.assertTrue(state['bucket_retained'])
            self.assertTrue((output / 'amd/results.jsonl').exists())
            self.assertTrue((output / 'arm/results.jsonl').exists())
            self.assertFalse(any('storage rm' in c for c in calls))

    def test_confirmed_absent_objects_are_safe_to_clean_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, scratch = Path(tmp) / 'out', Path(tmp) / 'tmp'
            out.mkdir(); scratch.mkdir()
            with mock.patch.object(cloud, 'retry_call', return_value=SimpleNamespace(returncode=1)), \
                 mock.patch.object(cloud, 'object_exists', return_value=False):
                state, safe = cloud.collect_result('arm', 'CANCELLED', 'bucket', out, scratch, [], 1000)
            self.assertEqual(state, 'CANCELLED')
            self.assertTrue(safe)

    def test_bucket_creation_sets_ownership_and_lifecycle_atomically(self):
        token = SimpleNamespace(stdout='token', returncode=0)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with mock.patch.object(cloud, 'call', return_value=token), mock.patch.object(cloud.request, 'urlopen', return_value=response) as urlopen:
            cloud.create_bucket('example-bucket', 'example-project', 'us-central1', 'abc123')
        req = urlopen.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(body['labels'], {'sb-run': 'abc123'})
        self.assertEqual(body['softDeletePolicy']['retentionDurationSeconds'], '0')
        self.assertEqual(body['lifecycle']['rule'][0]['condition']['age'], 1)

    def test_only_http_404_is_a_confirmed_missing_object(self):
        token = SimpleNamespace(stdout='token', returncode=0)
        with mock.patch.object(cloud, 'call', return_value=token), mock.patch.object(cloud.time, 'sleep'):
            for status, expected in ((404, False), (403, None), (503, None)):
                with self.subTest(status=status), mock.patch.object(cloud.request, 'urlopen', side_effect=error.HTTPError('https://example.invalid', status, 'error', {}, None)):
                    self.assertIs(cloud.object_exists('bucket', 'result.tar.gz'), expected)

    def test_jobs_still_queued_after_an_hour_are_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, itertools.repeat(3601))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, state='SCHEDULED')
            self.assertEqual(result, 1)
            self.assertEqual(sum('jobs cancel' in c for c in calls), 3)
            states = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())['states']
            self.assertEqual(set(states.values()), {'QUEUE_TIMEOUT'})
            self.assertTrue(any('storage rm' in c for c in calls))

    def test_successful_results_are_retained_when_another_job_cannot_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, itertools.repeat(3601))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, state={'intel': 'SUCCEEDED', 'amd': 'SUCCEEDED', 'arm': 'SCHEDULED'})
            self.assertEqual(result, 1)
            output = Path(tmp) / 'output'
            self.assertEqual({p.name for p in (output / 'intel').iterdir()}, cloud.REPORT_FILES)
            self.assertEqual({p.name for p in (output / 'amd').iterdir()}, cloud.REPORT_FILES)
            states = json.loads((output / 'cloud-run.json').read_text())['states']
            self.assertEqual(states, {'intel': 'SUCCEEDED', 'amd': 'SUCCEEDED', 'arm': 'QUEUE_TIMEOUT'})
            self.assertEqual(sum('jobs cancel' in c for c in calls), 1)

    def test_stuck_queue_cancellation_is_bounded_and_preserves_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, [3601] * 7, itertools.repeat(3725))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, fail='cancel-stuck', state='SCHEDULED')
            self.assertEqual(result, 1)
            self.assertEqual(sum('jobs cancel' in c for c in calls), 3)
            self.assertEqual(sum('jobs delete' in c for c in calls), 3)
            self.assertFalse(any('storage rm' in c for c in calls))
            states = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
            self.assertEqual(set(states['states'].values()), {'CANCEL_TIMEOUT'})
            self.assertTrue(states['bucket_retained'])

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
            self.assertFalse(any('storage rm' in c for c in calls))

    def test_deleting_job_retains_bucket_even_if_artifacts_are_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, state='DELETION_IN_PROGRESS')
            self.assertEqual(result, 1)
            self.assertFalse(any('storage rm' in c for c in calls))


if __name__ == '__main__':
    unittest.main()

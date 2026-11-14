import importlib.util
import io
import argparse
import hashlib
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
    def test_cloud_subprocess_timeout_is_bounded_and_redacted(self):
        with mock.patch.object(cloud.subprocess, 'run',
                side_effect=subprocess.TimeoutExpired(['secret-token'], 120)) as run:
            with self.assertRaisesRegex(OSError, 'cloud subprocess timed out after 120 seconds') as error:
                cloud.call('secret-token', capture=True)
            self.assertNotIn('secret-token', str(error.exception))
            self.assertEqual(run.call_args.kwargs['timeout'], 120)

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

    def snapshot(self, destination, extra=(), worker=b'echo snapshot-worker\n'):
        with tarfile.open(destination, 'w:gz') as tar:
            entries = [('examples/light-profile.json', b'{}', tarfile.REGTYPE),
                       ('cloud/gcp-worker.sh', worker, tarfile.REGTYPE),
                       ('cloud/single_core.py', b'# helper\n', tarfile.REGTYPE), *extra]
            for name, data, member_type in entries:
                info = tarfile.TarInfo(name)
                info.type = member_type
                info.size = len(data) if member_type == tarfile.REGTYPE else 0
                info.linkname = 'target' if member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                tar.addfile(info, io.BytesIO(data) if info.size else None)

    def test_snapshot_worker_script_is_used_in_all_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / 'source.tar.gz'
            self.snapshot(snapshot)
            args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'),
                project='example-project', service_account='test@example.iam.gserviceaccount.com',
                region='us-central1', zone='us-central1-c', output=str(Path(tmp) / 'output'),
                dry_run=True, source_archive=str(snapshot), source_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest())
            stdout = io.StringIO()
            with mock.patch('sys.stdout', stdout), mock.patch.object(cloud, 'make_source') as build:
                self.assertEqual(cloud.run(args), 0)
                build.assert_not_called()
            for job in json.loads(stdout.getvalue())['jobs'].values():
                script = job['taskGroups'][0]['taskSpec']['runnables'][0]['script']['text']
                self.assertIn('echo snapshot-worker\n', script)
                self.assertNotIn('save_failure_log()', script)
            args.source_sha256 = '0' * 64
            with self.assertRaisesRegex(ValueError, 'does not match'):
                cloud.run(args)
            self.assertFalse(Path(args.output).exists())

    def test_snapshot_path_and_digest_are_required_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'),
                project='example-project', service_account='test@example.iam.gserviceaccount.com',
                region='us-central1', zone='us-central1-c', output=str(Path(tmp) / 'output'),
                dry_run=True, source_archive=None, source_sha256='0' * 64)
            with self.assertRaisesRegex(ValueError, 'must be provided together'):
                cloud.run(args)
            args.source_archive, args.source_sha256 = 'missing', None
            with self.assertRaisesRegex(ValueError, 'must be provided together'):
                cloud.run(args)
            args.source_sha256 = 'not-a-sha256'
            with self.assertRaisesRegex(ValueError, 'must be provided together'):
                cloud.run(args)

    def test_snapshot_rejects_unsafe_duplicate_and_missing_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.tar.gz'
            bad = [('traversal', '../escape', tarfile.REGTYPE),
                   ('absolute', '/escape', tarfile.REGTYPE),
                   ('noncanonical', 'dir/./file', tarfile.REGTYPE),
                   ('backslash', 'dir\\file', tarfile.REGTYPE),
                   ('duplicate', 'cloud/gcp-worker.sh', tarfile.REGTYPE),
                   ('symlink', 'link', tarfile.SYMTYPE),
                   ('hardlink', 'link', tarfile.LNKTYPE),
                   ('directory', 'folder', tarfile.DIRTYPE)]
            for label, name, member_type in bad:
                with self.subTest(label=label):
                    self.snapshot(path, [(name, b'bad', member_type)])
                    with self.assertRaisesRegex(ValueError, 'unsafe or duplicate'):
                        cloud.validate_source_archive(path, 'examples/light-profile.json')
            with tarfile.open(path, 'w:gz') as tar:
                info = tarfile.TarInfo('cloud/gcp-worker.sh')
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
            with self.assertRaisesRegex(ValueError, 'missing required'):
                cloud.validate_source_archive(path, 'examples/light-profile.json')
            with path.open('wb') as stream:
                stream.truncate(cloud.MAX_SOURCE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, 'exceeds 20 MB'):
                cloud.validate_source_archive(path, 'examples/light-profile.json')

    def run_mocked(self, tmp, fail=None, state='SUCCEEDED', queue_timeout=3600):
        args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'), project='example-project',
            service_account='test@example.iam.gserviceaccount.com', region='us-central1', zone='us-central1-c',
            output=str(Path(tmp) / 'output'), dry_run=False, queue_timeout_seconds=queue_timeout)
        calls = []
        context = {'owner': None, 'describes': {}, 'downloads': {}, 'cancelled': set(), 'cancel_calls': {},
                   'remote_jobs': {}}
        def fake_call(*argv, capture=False, check=True):
            calls.append(argv)
            command = ' '.join(argv)
            if fail in ('submit', 'submit-accepted', 'submit-unrelated', 'submit-interrupt', 'submit-timeout') and 'jobs submit' in command and '-amd ' in command:
                if fail in ('submit-accepted', 'submit-interrupt', 'submit-timeout', 'submit-unrelated'):
                    label = 'someone-else' if fail == 'submit-unrelated' else context['owner']
                    context['remote_jobs'][argv[4]] = {'name': argv[4], 'uid': 'uid-' + argv[4],
                                                       'labels': {'sb-run': label}}
                if fail == 'submit-interrupt':
                    raise KeyboardInterrupt()
                if fail == 'submit-timeout':
                    raise OSError('cloud subprocess timed out')
                raise subprocess.CalledProcessError(1, argv)
            if 'jobs submit' in command:
                manifest = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
                for key in ('intel', 'amd', 'arm'):
                    if manifest['states'][key] == 'QUEUED':
                        self.assertEqual(manifest['job_uids'][key], 'uid-' + manifest['jobs'][key])
                context['remote_jobs'][argv[4]] = {'name': argv[4], 'uid': 'uid-' + argv[4],
                                                   'labels': {'sb-run': context['owner']}}
                return SimpleNamespace(returncode=0, stdout=json.dumps({'uid': 'uid-' + argv[4]}))
            if 'jobs list' in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps(list(context['remote_jobs'].values())))
            if 'jobs delete' in command:
                context['remote_jobs'].pop(argv[4], None)
                return SimpleNamespace(returncode=0, stdout='')
            if fail == 'cleanup' and 'storage rm' in command:
                return SimpleNamespace(returncode=1, stdout='')
            if 'jobs cancel' in command:
                key = argv[4]
                context['cancel_calls'][key] = context['cancel_calls'].get(key, 0) + 1
                if '-amd ' in command and (fail == 'cancel-always' or
                                           (fail == 'cancel-once' and context['cancel_calls'][key] == 1)):
                    raise OSError('transient cancellation failure')
                context['cancelled'].add(argv[4])
                return SimpleNamespace(returncode=0, stdout='')
            if 'jobs describe' in command:
                if fail in ('submit', 'submit-accepted', 'submit-unrelated', 'submit-interrupt', 'submit-timeout') and '-amd ' in command:
                    label = context['owner'] if fail in ('submit-accepted', 'submit-interrupt', 'submit-timeout') else 'someone-else'
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'labels': {'sb-run': label}}))
                if fail in ('describe-amd', 'describe-once') and '-amd ' in command:
                    context['describes']['amd'] = context['describes'].get('amd', 0) + 1
                    if fail == 'describe-amd' or context['describes']['amd'] == 1:
                        return SimpleNamespace(returncode=1, stdout='')
                canceled_state = 'CANCELLATION_IN_PROGRESS' if fail == 'cancel-stuck' else 'CANCELLED'
                job_state = canceled_state if argv[4] in context['cancelled'] else state
                if isinstance(state, dict):
                    key = next(key for key in state if '-' + key + ' ' in command)
                    value = state[key]
                    if isinstance(value, list):
                        index = context['describes'].get(key, 0)
                        context['describes'][key] = index + 1
                        value = value[min(index, len(value) - 1)]
                    job_state = canceled_state if argv[4] in context['cancelled'] else value
                events = []
                if job_state == 'CANCELLED':
                    events = [{'type': 'STATUS_CHANGED', 'description':
                               f'Job state is set from {before} to {after} for job x.'}
                              for before, after in [('QUEUED', 'SCHEDULED'), ('SCHEDULED', 'CANCELLED')]]
                return SimpleNamespace(returncode=0, stdout=json.dumps(
                    {'uid': 'uid-' + argv[4], 'status': {'state': job_state, 'statusEvents': events}}))
            if 'buckets describe' in command:
                if fail == 'bucket-ownership-unknown':
                    return SimpleNamespace(returncode=1, stdout='')
                label = 'someone-else' if fail in ('create', 'bucket-replaced') else context['owner']
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
            # Recovery information must exist even if the create succeeds but
            # the process dies before receiving its response.
            manifest = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
            self.assertEqual(manifest['project'], project)
            self.assertEqual(manifest['region'], region)
            self.assertEqual(manifest['zone'], 'us-central1-c')
            self.assertEqual(manifest['scratch_bucket'], bucket)
            self.assertEqual(manifest['ownership_label'], {'sb-run': owner})
            self.assertEqual(manifest['jobs'], {key: manifest['job_prefix'] + '-' + key
                                                for key in ('intel', 'amd', 'arm')})
            self.assertEqual(set(manifest['states'].values()), {'NOT_SUBMITTED'})
            self.assertEqual(set(manifest['job_uids'].values()), {None})
            self.assertEqual(set(manifest['ever_running'].values()), {None})
            if fail in ('create', 'create-accepted'):
                raise subprocess.CalledProcessError(1, 'create')
        with mock.patch.object(cloud, 'call', side_effect=fake_call), mock.patch.object(cloud, 'make_source', side_effect=fake_source), \
             mock.patch.object(cloud, 'create_bucket', side_effect=fake_bucket), mock.patch.object(cloud.time, 'sleep'), \
             mock.patch.object(cloud, 'object_exists', return_value=None):
            try:
                result = cloud.run(args)
            except (subprocess.CalledProcessError, RuntimeError, OSError, KeyboardInterrupt) as exc:
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
        for failure, expected in [('submit-accepted', 2), ('submit-interrupt', 2),
                                  ('submit-unrelated', 1), ('submit-timeout', 2)]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                result, calls = self.run_mocked(tmp, fail=failure)
                self.assertIsInstance(result, (subprocess.CalledProcessError, OSError, KeyboardInterrupt))
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

    def test_replaced_or_unverifiable_bucket_is_not_deleted(self):
        for failure in ('bucket-replaced', 'bucket-ownership-unknown'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
                result, calls = self.run_mocked(tmp, fail=failure)
                self.assertIsInstance(result, RuntimeError)
                self.assertIn('unverified bucket', str(result))
                self.assertIn('only resources owned by this run', str(result))
                self.assertNotIn('storage rm', stderr.getvalue())
                self.assertTrue(any('buckets describe' in c for c in calls))
                self.assertFalse(any('storage rm' in c for c in calls))
                manifest = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
                self.assertTrue(manifest['bucket_retained'])

    def test_owned_job_deletion_retries_only_when_identity_is_confirmed(self):
        common = ['--project', 'example-project', '--quiet']
        success = SimpleNamespace(returncode=0)
        for observation, expected, calls in (('owned', True, 2), ('absent', True, 0),
                                             ('deleting', True, 0), ('unknown', False, 0)):
            with self.subTest(observation=observation), \
                 mock.patch.object(cloud, 'call', side_effect=[OSError('timed out'), success]) as call, \
                 mock.patch.object(cloud, 'owned_job_state', return_value=observation), \
                 mock.patch.object(cloud.time, 'sleep'):
                self.assertEqual(cloud.delete_owned_job('job', 'us-central1', common, 'owner', 'uid'), expected)
                self.assertEqual(call.call_count, calls)

    def test_unrelated_or_replaced_job_is_never_deleted(self):
        common = ['--project', 'example-project', '--quiet']
        for job in ({'name': 'job', 'labels': {'sb-run': 'other'}, 'uid': 'uid'},
                    {'name': 'job', 'labels': {'sb-run': 'owner'}, 'uid': 'replacement'}):
            with self.subTest(job=job), \
                 mock.patch.object(cloud, 'retry_call', return_value=SimpleNamespace(
                     returncode=0, stdout=json.dumps([job]))), \
                 mock.patch.object(cloud, 'call') as delete:
                self.assertFalse(cloud.delete_owned_job('job', 'us-central1', common, 'owner', 'uid'))
                delete.assert_not_called()

    def test_job_absence_requires_successful_list_and_reused_identity_is_not_owned(self):
        common = ['--project', 'example-project', '--quiet']
        def state(payload, code=0):
            result = SimpleNamespace(returncode=code, stdout=json.dumps(payload))
            with mock.patch.object(cloud, 'retry_call', return_value=result):
                return cloud.owned_job_state('job', 'us-central1', common, 'owner', 'uid')
        self.assertEqual(state([]), 'absent')
        self.assertEqual(state([], code=1), 'unknown')
        self.assertEqual(state([{'name': 'job', 'labels': {'sb-run': 'other'}, 'uid': 'uid'}]), 'unknown')
        self.assertEqual(state([{'name': 'job', 'labels': {'sb-run': 'owner'}, 'uid': 'other'}]), 'unknown')
        self.assertEqual(state([{'name': 'job', 'labels': {'sb-run': 'owner'}, 'uid': 'uid'}]), 'owned')
        self.assertEqual(state([{'name': 'job', 'labels': {'sb-run': 'owner'}, 'uid': 'uid',
                                 'status': {'state': 'DELETION_IN_PROGRESS'}}]), 'deleting')

    def test_job_list_success_with_diagnostics_is_never_treated_as_absent(self):
        common = ['--project', 'example-project', '--quiet']
        warning = SimpleNamespace(returncode=0, stdout='[]', stderr='WARNING: partial results; sensitive')
        with mock.patch.object(cloud, 'call', return_value=warning) as call, \
             mock.patch.object(cloud.time, 'sleep'), mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertFalse(cloud.delete_owned_job('job', 'us-central1', common, 'owner', 'uid'))
        self.assertEqual(call.call_count, cloud.RETRIES)
        self.assertTrue(all('list' in args.args for args in call.call_args_list))
        self.assertEqual(stderr.getvalue(), '')

    def test_job_list_retries_diagnostics_and_accepts_a_clean_result(self):
        common = ['--project', 'example-project', '--quiet']
        warning = SimpleNamespace(returncode=0, stdout='[]', stderr='WARNING: partial results')
        clean = SimpleNamespace(returncode=0, stdout='[]', stderr='')
        with mock.patch.object(cloud, 'call', side_effect=[warning, clean]) as call, \
             mock.patch.object(cloud.time, 'sleep'):
            self.assertEqual(cloud.owned_job_state('job', 'us-central1', common, 'owner', 'uid'), 'absent')
        self.assertEqual(call.call_count, 2)

    def test_ownership_describe_rejects_success_with_diagnostics(self):
        warning = SimpleNamespace(returncode=0, stdout=json.dumps({'labels': {'sb-run': 'owner'}}),
                                  stderr='WARNING: incomplete response; sensitive')
        clean = SimpleNamespace(returncode=0, stdout=warning.stdout, stderr='')
        with mock.patch.object(cloud, 'call', return_value=warning) as call, \
             mock.patch.object(cloud.time, 'sleep'), mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertIsNone(cloud.owned_resource('gcloud', 'storage', 'buckets', 'describe', 'bucket', owner='owner'))
        self.assertEqual(call.call_count, cloud.RETRIES)
        self.assertEqual(stderr.getvalue(), '')
        with mock.patch.object(cloud, 'call', side_effect=[warning, clean]) as call, \
             mock.patch.object(cloud.time, 'sleep'):
            self.assertTrue(cloud.owned_resource('gcloud', 'storage', 'buckets', 'describe', 'bucket', owner='owner'))
        self.assertEqual(call.call_count, 2)

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

    def test_custom_queue_deadline_and_recorded_provisioning_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, itertools.repeat(61))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, state='SCHEDULED', queue_timeout=60)
            self.assertEqual(result, 1)
            self.assertEqual(sum('jobs cancel' in c for c in calls), 3)
            state = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
            self.assertEqual(state['queue_timeout_seconds'], 60)
            self.assertEqual(set(state['states'].values()), {'QUEUE_TIMEOUT'})
            self.assertEqual(set(state['ever_running'].values()), {False})
            self.assertEqual(state['job_uids'], {key: 'uid-' + name for key, name in state['jobs'].items()})

    def test_terminal_poll_after_nominal_deadline_gets_bounded_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A task can begin just before the one-hour queue deadline and
            # finish just after the nominal task-plus-queue boundary.
            times = itertools.chain([0] * 7, [3599, 25201], itertools.repeat(25201))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, _ = self.run_mocked(tmp, state={
                    'intel': ['RUNNING', 'SUCCEEDED'], 'amd': 'SUCCEEDED', 'arm': 'SUCCEEDED'})
            self.assertEqual(result, 0)
            manifest = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
            self.assertEqual(set(manifest['states'].values()), {'SUCCEEDED'})
            self.assertTrue((Path(tmp) / 'output/intel/results.jsonl').exists())

    def test_global_deadline_still_expires_after_collection_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, itertools.repeat(25381))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, _ = self.run_mocked(tmp, state='SCHEDULED')
            self.assertIsInstance(result, RuntimeError)
            self.assertIn('180-second collection grace', str(result))

    def test_transient_cancel_error_does_not_stop_other_running_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, itertools.repeat(3601))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, fail='cancel-once',
                    state={'intel': ['RUNNING', 'SUCCEEDED'], 'amd': 'SCHEDULED', 'arm': 'SUCCEEDED'})
            self.assertEqual(result, 1)
            manifest = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
            self.assertEqual(manifest['states'], {'intel': 'SUCCEEDED', 'amd': 'QUEUE_TIMEOUT', 'arm': 'SUCCEEDED'})
            self.assertEqual(manifest['cancellations']['amd']['attempts'], 2)
            self.assertTrue(manifest['cancellations']['amd']['ambiguous'])
            self.assertTrue(manifest['cancellations']['amd']['accepted'])
            self.assertTrue((Path(tmp) / 'output/intel/results.jsonl').exists())
            self.assertEqual(sum('jobs cancel' in call and '-amd ' in call for call in calls), 2)

    def test_job_starting_after_ambiguous_cancel_is_not_cancelled_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, itertools.repeat(3601))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, fail='cancel-once',
                    state={'intel': 'SUCCEEDED', 'amd': ['SCHEDULED', 'RUNNING', 'SUCCEEDED'], 'arm': 'SUCCEEDED'})
            self.assertEqual(result, 0)
            manifest = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
            self.assertEqual(manifest['states']['amd'], 'SUCCEEDED')
            self.assertTrue(manifest['ever_running']['amd'])
            self.assertTrue(manifest['cancellations']['amd']['observed_running'])
            self.assertEqual(sum('jobs cancel' in call and '-amd ' in call for call in calls), 1)

    def test_unconfirmed_cancellation_is_bounded_and_retains_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = itertools.chain([0] * 7, [3601] * 20, itertools.repeat(3725))
            with mock.patch.object(cloud.time, 'monotonic', side_effect=times):
                result, calls = self.run_mocked(tmp, fail='cancel-always',
                    state={'intel': 'SUCCEEDED', 'amd': 'SCHEDULED', 'arm': 'SUCCEEDED'})
            self.assertEqual(result, 1)
            manifest = json.loads((Path(tmp) / 'output/cloud-run.json').read_text())
            self.assertEqual(manifest['states']['amd'], 'CANCEL_TIMEOUT')
            self.assertEqual(manifest['cancellations']['amd']['attempts'], 3)
            self.assertTrue(manifest['bucket_retained'])
            self.assertTrue((Path(tmp) / 'output/intel/results.jsonl').exists())
            self.assertFalse(any('storage rm' in call for call in calls))

    def test_queue_timeout_validation_happens_before_remote_actions(self):
        for value in (0, -1, 86401, True, 0.5):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'),
                    project='example-project', service_account='test@example.iam.gserviceaccount.com',
                    region='us-central1', zone='us-central1-c', output=str(Path(tmp) / 'output'),
                    dry_run=False, queue_timeout_seconds=value)
                with self.assertRaisesRegex(ValueError, 'queue_timeout_seconds'):
                    cloud.run(args)
                self.assertFalse(Path(args.output).exists())

    def test_running_history_is_conservative(self):
        def status(*transitions):
            return {'statusEvents': [{'type': 'STATUS_CHANGED', 'description':
                     f'Job state is set from {a} to {b} for job x.'} for a, b in transitions]}
        self.assertTrue(cloud.running_observation('RUNNING', {}, None))
        self.assertTrue(cloud.running_observation('CANCELLED', {}, True))
        self.assertTrue(cloud.running_observation('CANCELLED', status(('QUEUED', 'RUNNING'),
                                                                       ('RUNNING', 'CANCELLED')), None))
        self.assertFalse(cloud.running_observation('CANCELLED', status(('QUEUED', 'SCHEDULED'),
                                                                        ('SCHEDULED', 'CANCELLED')), None))
        self.assertIsNone(cloud.running_observation('CANCELLED', {}, None))
        self.assertIsNone(cloud.running_observation('CANCELLED', status(('SCHEDULED', 'CANCELLED')), None))
        self.assertIsNone(cloud.running_observation('CANCELLED', status(('QUEUED', 'SCHEDULED'),
                                                                         ('PENDING', 'CANCELLED')), None))

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
            write_state = cloud.write_state_atomic
            writes = 0
            def fail_state(path, value):
                nonlocal writes
                writes += 1
                if writes > 4:  # Initial manifest and all three confirmed submissions succeed.
                    raise OSError('disk full')
                return write_state(path, value)
            with mock.patch.object(cloud, 'write_state_atomic', side_effect=fail_state):
                result, calls = self.run_mocked(tmp)
            self.assertIsInstance(result, RuntimeError)
            self.assertIn('Could not save cloud-run.json', str(result))
            self.assertEqual(sum('jobs delete' in c for c in calls), 3)
            self.assertFalse(any('storage rm' in c for c in calls))

    def test_initial_manifest_failure_prevents_remote_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'),
                project='example-project', service_account='test@example.iam.gserviceaccount.com',
                region='us-central1', zone='us-central1-c', output=str(Path(tmp) / 'output'), dry_run=False)
            with mock.patch.object(cloud, 'make_source', side_effect=lambda path, _: path.write_bytes(b'source')), \
                 mock.patch.object(cloud, 'write_state_atomic', side_effect=OSError('disk full')), \
                 mock.patch.object(cloud, 'create_bucket') as create:
                with self.assertRaisesRegex(OSError, 'disk full'):
                    cloud.run(args)
                create.assert_not_called()

    def test_manifest_replacement_failure_preserves_prior_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            state = folder / 'cloud-run.json'
            state.write_text('{"old": true}\n')
            with mock.patch.object(cloud.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    cloud.write_state_atomic(state, {'new': True})
            self.assertEqual(json.loads(state.read_text()), {'old': True})
            self.assertEqual([p.name for p in folder.iterdir()], ['cloud-run.json'])

    def test_deleting_job_retains_bucket_even_if_artifacts_are_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_mocked(tmp, state='DELETION_IN_PROGRESS')
            self.assertEqual(result, 1)
            self.assertFalse(any('storage rm' in c for c in calls))


if __name__ == '__main__':
    unittest.main()

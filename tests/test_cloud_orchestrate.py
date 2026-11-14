"""Exercise the unattended orchestration without creating cloud resources."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_orchestrate', ROOT / 'cloud/orchestrate_gcp.py')
orchestrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orchestrate)


class CloudOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'run'
        self.args = argparse.Namespace(profile=str(ROOT / 'cloud/profiles/light.json'),
            project='example-project', service_account='runner@example-project.iam.gserviceaccount.com',
            region='us-central1', zones=['us-central1-c', 'us-central1-f'],
            queue_timeout_seconds=300, output=str(self.output), single_core=False, dry_run=False)
        self.source = mock.patch.object(orchestrate, 'source_state', return_value={
            'commit': 'abc', 'tracked_dirty': False, 'tracked_status': '', 'omitted_untracked': [],
            'fingerprint': 'sha256:original'})
        self.source.start()
        self.addCleanup(self.source.stop)
        def snapshot(path, benchmark):
            with tarfile.open(path, 'w:gz') as archive:
                archive.add(ROOT / benchmark, arcname=benchmark)
        self.snapshot = mock.patch.object(orchestrate.run_gcp, 'make_source', side_effect=snapshot)
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def test_retries_only_confirmed_capacity_after_verified_cleanup(self):
        calls = []
        def run(command, log, timeout, cleanup_timeout):
            dest = Path(command[command.index('--output') + 1])
            dest.mkdir(parents=True)
            profile = json.loads(Path(command[command.index('--profile') + 1]).read_text())
            ids = [t['id'] for t in profile['targets']]
            self.assertEqual(cleanup_timeout, orchestrate.cleanup_wait_seconds(len(ids)))
            calls.append(ids)
            states = ({'intel': 'SUCCEEDED', 'amd': 'QUEUE_TIMEOUT', 'arm': 'FAILED'}
                      if len(ids) > 1 else {'amd': 'SUCCEEDED'})
            for key in ids:
                if states[key] != 'QUEUE_TIMEOUT':
                    (dest / key).mkdir()
                    (dest / key / 'results.jsonl').write_text(json.dumps({
                        'case_id': 'laplacian-1d', 'solver_id': 'superlu', 'pass_id': 'timing',
                        'outcome': 'success', 'provenance': {'input_hash': 'sha256:abc'}}) + '\n')
            (dest / 'cloud-run.json').write_text(json.dumps({'states': states,
                'ever_running': {'amd': False}, 'job_uids': {'amd': 'sb-123456789abc-am-1234'}}))
            return (1 if len(ids) > 1 else 0), None, False, False
        with mock.patch.object(orchestrate, 'execute_attempt', side_effect=run), \
             mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': True}), \
             mock.patch.object(orchestrate, 'stockout', return_value=True) as stock, \
             mock.patch.object(orchestrate, 'create_report') as report:
            self.assertEqual(orchestrate.orchestrate(self.args), 1)
        self.assertEqual(calls, [['intel', 'amd', 'arm'], ['amd']])
        stock.assert_called_once_with('example-project', 'us-central1-c', 'sb-123456789abc-am-1234')
        inputs = report.call_args.args[0]
        self.assertEqual([Path(x).name for x in inputs], ['intel', 'amd', 'arm'])
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['targets']['amd']['attempt'], 'retry-amd')
        self.assertEqual(summary['targets']['arm']['state'], 'FAILED')

    def test_unknown_running_or_unverified_cleanup_prevents_retry(self):
        for ever_running, verified in ((None, True), (False, False), (True, True)):
            with self.subTest(ever_running=ever_running, verified=verified), tempfile.TemporaryDirectory() as tmp:
                self.args.output = str(Path(tmp) / 'out')
                def run(command, log, timeout, cleanup_timeout):
                    dest = Path(command[command.index('--output') + 1]); dest.mkdir(parents=True)
                    (dest / 'cloud-run.json').write_text(json.dumps({'states': {'amd': 'QUEUE_TIMEOUT'},
                        'ever_running': {'amd': ever_running}, 'job_uids': {'amd': 'uid'}}))
                    return 1, None, False, False
                with mock.patch.object(orchestrate, 'execute_attempt', side_effect=run) as execute, \
                     mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': verified}), \
                     mock.patch.object(orchestrate, 'stockout', return_value=True) as stock:
                    self.assertEqual(orchestrate.orchestrate(self.args), 1)
                self.assertEqual(execute.call_count, 1)
                stock.assert_not_called()

    def test_capacity_evidence_requires_exact_uid_zone_and_error(self):
        uid = 'sb-123456789abc-ar-abcdef-1234'
        item = {'operationType': 'insert', 'status': 'DONE',
                'targetLink': f'https://compute/projects/p/zones/us-central1-c/instances/{uid}-group0-0-1',
                'error': {'errors': [{'code': 'ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS'}]}}
        def check(items):
            result = subprocess.CompletedProcess([], 0, json.dumps(items), '')
            with mock.patch.object(orchestrate.subprocess, 'run', return_value=result):
                return orchestrate.stockout('p', 'us-central1-c', uid)
        self.assertTrue(check([item]))
        for replacement in ({'targetLink': item['targetLink'].replace(uid, uid + 'x')},
                            {'targetLink': item['targetLink'].replace('us-central1-c', 'us-central1-f')},
                            {'error': {'errors': [{'code': 'QUOTA_EXCEEDED'}]}},
                            {'status': 'PENDING'}):
            self.assertFalse(check([{**item, **replacement}]))

    def test_changed_source_blocks_retry(self):
        initial = {'commit': 'abc', 'tracked_dirty': False, 'tracked_status': '', 'omitted_untracked': [],
                   'fingerprint': 'sha256:initial'}
        changed = {**initial, 'fingerprint': 'sha256:changed'}
        def run(command, log, timeout, cleanup_timeout):
            dest = Path(command[command.index('--output') + 1]); dest.mkdir(parents=True)
            (dest / 'cloud-run.json').write_text(json.dumps({'states': {'amd': 'QUEUE_TIMEOUT'},
                'ever_running': {'amd': False}, 'job_uids': {'amd': 'uid'}}))
            return 1, None, False, False
        with mock.patch.object(orchestrate, 'source_state', side_effect=[initial, initial, changed]), \
             mock.patch.object(orchestrate, 'execute_attempt', side_effect=run) as execute, \
             mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': True}), \
             mock.patch.object(orchestrate, 'stockout', return_value=True):
            self.assertEqual(orchestrate.orchestrate(self.args), 1)
        self.assertEqual(execute.call_count, 1)
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertIn('source changed', summary['retry_skipped']['amd'])

    def test_snapshot_change_blocks_retry(self):
        def run(command, log, timeout, cleanup_timeout):
            dest = Path(command[command.index('--output') + 1]); dest.mkdir(parents=True)
            (dest / 'cloud-run.json').write_text(json.dumps({'states': {'amd': 'QUEUE_TIMEOUT'},
                'ever_running': {'amd': False}, 'job_uids': {'amd': 'uid'}}))
            Path(command[command.index('--source-archive') + 1]).write_bytes(b'changed')
            return 1, None, False, False
        with mock.patch.object(orchestrate, 'execute_attempt', side_effect=run) as execute, \
             mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': True}), \
             mock.patch.object(orchestrate, 'stockout', return_value=True):
            self.assertEqual(orchestrate.orchestrate(self.args), 1)
        self.assertEqual(execute.call_count, 1)
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(summary['attempts'][1]['state'], 'snapshot_error')

    def test_signal_before_popen_returns_is_forwarded_once(self):
        process = mock.Mock(pid=12345)
        def wait(**kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return 130
        process.wait.side_effect = wait
        def spawn(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return process
        with mock.patch.object(orchestrate.subprocess, 'Popen', side_effect=spawn), \
             mock.patch.object(orchestrate.os, 'killpg') as kill:
            self.assertEqual(orchestrate.execute_attempt(['fake'], self.output, 10, 40),
                             (130, signal.SIGTERM, False, False))
        kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_watchdog_escalates_and_marks_forced_kill(self):
        process = mock.Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired('fake', 40), -9]
        with mock.patch.object(orchestrate.subprocess, 'Popen', return_value=process), \
             mock.patch.object(orchestrate.os, 'killpg') as kill, \
             mock.patch.object(orchestrate.time, 'monotonic', side_effect=[0, 5, 5, 5]):
            self.assertEqual(orchestrate.execute_attempt(['fake'], self.output, 5, 40), (-9, None, True, True))
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=40), mock.call(timeout=30)])
        self.assertEqual(kill.call_args_list, [mock.call(12345, signal.SIGTERM), mock.call(12345, signal.SIGKILL)])

    def test_signal_switches_immediately_to_cleanup_timeout(self):
        process = mock.Mock(pid=12345)
        def wait(*, timeout):
            if timeout == 1:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                raise subprocess.TimeoutExpired('fake', timeout)
            if timeout == 40:
                raise subprocess.TimeoutExpired('fake', timeout)
            self.assertEqual(timeout, 30)
            return -9
        process.wait.side_effect = wait
        with mock.patch.object(orchestrate.subprocess, 'Popen', return_value=process), \
             mock.patch.object(orchestrate.os, 'killpg') as kill, \
             mock.patch.object(orchestrate.time, 'monotonic', side_effect=[0, 0, 1, 1]):
            self.assertEqual(orchestrate.execute_attempt(['fake'], self.output, 7200, 40),
                             (-9, signal.SIGTERM, False, True))
        self.assertEqual(process.wait.call_args_list,
                         [mock.call(timeout=1), mock.call(timeout=40), mock.call(timeout=30)])
        self.assertEqual(kill.call_args_list, [mock.call(12345, signal.SIGTERM), mock.call(12345, signal.SIGKILL)])

    def test_cleanup_budget_includes_each_job_and_bucket_checks(self):
        retries = orchestrate.run_gcp.RETRIES
        timeout = orchestrate.run_gcp.COMMAND_TIMEOUT_SECONDS
        read = retries * timeout + retries - 1
        per_job = retries * (read + timeout) + retries - 1 + read
        fixed = 3 * read + timeout + 2 * timeout
        self.assertEqual(orchestrate.cleanup_wait_seconds(3), 3 * per_job + fixed)
        self.assertEqual(orchestrate.cleanup_wait_seconds(3) - orchestrate.cleanup_wait_seconds(1),
                         2 * per_job)

    def test_result_summary_surfaces_missing_and_hash_differences(self):
        profile = {'cases': [{'id': 'case'}], 'solvers': [{'id': 'solver'}],
                   'passes': [{'id': 'timing'}, {'id': 'resources'}]}
        paths = {}
        for target, hash_value in (('intel', 'hash-a'), ('amd', 'hash-b')):
            path = Path(self.tmp.name) / target; path.mkdir()
            (path / 'results.jsonl').write_text(json.dumps({'case_id': 'case', 'solver_id': 'solver',
                'pass_id': 'timing', 'outcome': 'success' if target == 'intel' else 'unsupported',
                'provenance': {'input_hash': hash_value}}) + '\n')
            paths[target] = path
        (paths['intel'] / 'execution.json').write_text(json.dumps({'deployment': {'solvers': {
            'solver': {}, 'extra-solver': {}}}}))
        result = orchestrate.result_summary(paths, profile)
        self.assertEqual(result['targets']['intel']['recorded'], 1)
        self.assertEqual(result['targets']['intel']['missing'][0]['pass_id'], 'resources')
        self.assertEqual(result['input_hash_differences']['case']['amd'], ['hash-b'])
        self.assertEqual(result['requested_solvers'], ['solver'])
        self.assertEqual(result['targets']['intel']['observed_solvers'], ['solver'])
        self.assertEqual(result['targets']['intel']['solver_outcomes'], {'solver': {'success': 1}})
        self.assertEqual(result['targets']['intel']['deployment_solvers'], ['extra-solver', 'solver'])
        self.assertEqual(result['targets']['intel']['omitted_deployment_solvers'], ['extra-solver'])
        self.assertIsNone(result['targets']['amd']['deployment_solvers'])
        self.assertIsNone(result['targets']['amd']['omitted_deployment_solvers'])
        self.assertEqual(result['targets']['amd']['solver_outcomes'], {'solver': {'unsupported': 1}})

    def test_launcher_error_cannot_be_hidden_by_successful_states(self):
        def run(command, log, timeout, cleanup_timeout):
            dest = Path(command[command.index('--output') + 1]); dest.mkdir(parents=True)
            states = {key: 'SUCCEEDED' for key in ('intel', 'amd', 'arm')}
            (dest / 'cloud-run.json').write_text(json.dumps({'states': states}))
            for key in states:
                (dest / key).mkdir()
                (dest / key / 'results.jsonl').write_text('{}\n')
            return 2, None, False, False
        complete_results = {'targets': {key: {'missing': [], 'outcomes': {'success': 1}}
                                        for key in ('intel', 'amd', 'arm')}}
        with mock.patch.object(orchestrate, 'execute_attempt', side_effect=run), \
             mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': True}), \
             mock.patch.object(orchestrate, 'result_summary', return_value=complete_results), \
             mock.patch.object(orchestrate, 'create_report'):
            self.assertEqual(orchestrate.orchestrate(self.args), 1)

    def test_recovered_capacity_retry_can_complete(self):
        def run(command, log, timeout, cleanup_timeout):
            dest = Path(command[command.index('--output') + 1]); dest.mkdir(parents=True)
            selected = json.loads(Path(command[command.index('--profile') + 1]).read_text())
            ids = [t['id'] for t in selected['targets']]
            states = ({'intel': 'SUCCEEDED', 'amd': 'QUEUE_TIMEOUT', 'arm': 'SUCCEEDED'}
                      if len(ids) == 3 else {'amd': 'SUCCEEDED'})
            (dest / 'cloud-run.json').write_text(json.dumps({'states': states,
                'ever_running': {'amd': False}, 'job_uids': {'amd': 'uid'}}))
            for key in ids:
                if states[key] == 'SUCCEEDED':
                    (dest / key).mkdir()
                    (dest / key / 'results.jsonl').write_text('{}\n')
            return (1 if len(ids) == 3 else 0), None, False, False
        complete_results = {'targets': {key: {'missing': [], 'outcomes': {'success': 1}}
                                        for key in ('intel', 'amd', 'arm')}}
        with mock.patch.object(orchestrate, 'execute_attempt', side_effect=run), \
             mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': True}), \
             mock.patch.object(orchestrate, 'stockout', return_value=True), \
             mock.patch.object(orchestrate, 'result_summary', return_value=complete_results), \
             mock.patch.object(orchestrate, 'create_report'):
            self.assertEqual(orchestrate.orchestrate(self.args), 0)

    def test_forced_kill_marks_cleanup_unverified_even_after_clean_observation(self):
        def run(command, log, timeout, cleanup_timeout):
            dest = Path(command[command.index('--output') + 1]); dest.mkdir(parents=True)
            (dest / 'cloud-run.json').write_text(json.dumps({'states': {'amd': 'QUEUE_TIMEOUT'},
                'ever_running': {'amd': False}, 'job_uids': {'amd': 'uid'}}))
            return -9, None, True, True
        with mock.patch.object(orchestrate, 'execute_attempt', side_effect=run) as execute, \
             mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': True}), \
             mock.patch.object(orchestrate, 'stockout', return_value=True):
            self.assertEqual(orchestrate.orchestrate(self.args), 1)
        self.assertEqual(execute.call_count, 1)
        cleanup = json.loads((self.output / 'summary.json').read_text())['attempts'][0]['cleanup']
        self.assertFalse(cleanup['verified'])
        self.assertIn('forced kill', cleanup['recovery'])

    def test_arbitrary_untracked_source_is_rejected_but_outputs_are_ignored(self):
        self.source.stop()
        def run(command, **kwargs):
            if '--others' in command:
                output = 'scripts/new-helper.sh\0reports/report.html\0tests/test_new.py\0'
            elif '--porcelain' in command:
                output = ''
            elif 'rev-parse' in command:
                output = 'abc\n'
            else:
                output = 'README.md\0'
            return subprocess.CompletedProcess(command, 0, output, '')
        with mock.patch.object(orchestrate.subprocess, 'run', side_effect=run):
            result = orchestrate.source_state('examples/light-profile.json')
        self.assertEqual(result['omitted_untracked'], ['scripts/new-helper.sh'])

    def test_missing_report_dependency_blocks_before_cloud_creation(self):
        with mock.patch.object(orchestrate, 'report_preflight', side_effect=ImportError('missing dep')), \
             mock.patch.object(orchestrate, 'execute_attempt') as execute:
            with self.assertRaisesRegex(ImportError, 'missing dep'):
                orchestrate.orchestrate(self.args)
        execute.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_final_output_mentions_status_paths_and_unverified_cleanup(self):
        def run(command, log, timeout, cleanup_timeout):
            dest = Path(command[command.index('--output') + 1]); dest.mkdir(parents=True)
            (dest / 'cloud-run.json').write_text(json.dumps({'states': {'amd': 'QUEUE_TIMEOUT'},
                'ever_running': {'amd': False}, 'job_uids': {'amd': 'uid'}}))
            (dest / 'intel').mkdir()
            (dest / 'intel' / 'results.jsonl').write_text('{}\n')
            return 1, None, False, False
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(orchestrate, 'execute_attempt', side_effect=run), \
             mock.patch.object(orchestrate, 'verify_attempt', return_value={'verified': False}), \
             mock.patch.object(orchestrate, 'result_summary', return_value={'targets': {}}), \
             mock.patch.object(orchestrate, 'create_report'), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(orchestrate.orchestrate(self.args), 1)
        self.assertIn('Run status: partial', stdout.getvalue())
        self.assertIn('Summary: ' + str(self.output / 'summary.json'), stdout.getvalue())
        self.assertIn('HTML report: ' + str(self.output / 'report.html'), stdout.getvalue())
        self.assertIn('JSON report: ' + str(self.output / 'report.json'), stdout.getvalue())
        self.assertIn('Cleanup not verified for initial', stderr.getvalue())
        self.assertIn('Retry skipped for amd: initial cleanup was not verified', stderr.getvalue())

    def test_omitted_source_blocks_launch_and_dry_run_creates_nothing(self):
        with mock.patch.object(orchestrate, 'source_state', return_value={'omitted_untracked': ['src/new.cpp']}), \
             mock.patch.object(orchestrate, 'execute_attempt') as execute:
            with self.assertRaisesRegex(ValueError, 'src/new.cpp'):
                orchestrate.orchestrate(self.args)
        execute.assert_not_called()
        self.assertFalse(self.output.exists())
        self.args.dry_run = True
        with mock.patch.object(orchestrate, 'execute_attempt') as execute:
            self.assertEqual(orchestrate.orchestrate(self.args), 0)
        execute.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()

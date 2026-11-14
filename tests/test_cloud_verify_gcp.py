"""Exercise cleanup verification without making any cloud requests."""
import json
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_verify_gcp', ROOT / 'cloud/verify_gcp.py')
cloud_verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_verify)
verify_attempt = cloud_verify.verify_attempt


OWNER = '0123456789abcdef0123456789abcdef'
PREFIX = 'sb-' + OWNER[:12]
BUCKET = 'sb-temp-' + OWNER[:16]
JOBS = {'intel': PREFIX + '-intel', 'arm': PREFIX + '-arm'}


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class FakeGcloud:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        snapshot = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        if argv[1:4] == ['batch', 'jobs', 'list']:
            kind = 'jobs'
        elif argv[1:5] == ['compute', 'instance-groups', 'managed', 'list']:
            kind = 'groups'
        elif argv[1:4] == ['compute', 'instances', 'list']:
            kind = 'vms'
        elif argv[1:4] == ['compute', 'disks', 'list']:
            kind = 'disks'
        elif argv[1:4] == ['storage', 'buckets', 'describe']:
            kind = 'bucket'
            self.index += 1
        else:
            raise AssertionError(f'unexpected command: {argv}')
        value = snapshot.get(kind, [] if kind != 'bucket' else None)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return SimpleNamespace(returncode=0, stdout=json.dumps(value[0]), stderr=value[1])
        if isinstance(value, str):
            return SimpleNamespace(returncode=1, stdout='', stderr=value)
        if kind == 'bucket' and value is None:
            return SimpleNamespace(returncode=1, stdout='', stderr=(
                f'ERROR: (gcloud.storage.buckets.describe) gs://{BUCKET} not found: 404.'))
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')


class VerifyGcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cloud-run.json'
        self.data = {'project': 'example-project', 'region': 'us-central1', 'zone': 'us-central1-c',
                     'job_prefix': PREFIX, 'jobs': JOBS, 'ownership_label': {'sb-run': OWNER},
                     'scratch_bucket': BUCKET}
        self.write_manifest()

    def write_manifest(self):
        self.path.write_text(json.dumps(self.data))

    def verify(self, fake, *, duration=10):
        clock = Clock()
        return verify_attempt('example-project', 'us-central1', 'us-central1-c', self.path,
                              timeout_seconds=duration, interval_seconds=1, runner=fake,
                              monotonic=clock.monotonic, sleep=clock.sleep)

    def test_requires_two_clean_observations_and_only_reads(self):
        fake = FakeGcloud([{}])
        result = self.verify(fake)
        self.assertTrue(result['verified'])
        self.assertEqual(result['details']['observations'], 2)
        self.assertEqual(len(fake.calls), 10)
        self.assertFalse(any('delete' in argv or 'rm' in argv for argv, _ in fake.calls))
        self.assertTrue(all(options['timeout'] > 0 for _, options in fake.calls))
        self.assertTrue(all(options['env']['CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR'] == 'false'
                            for _, options in fake.calls))

    def test_partial_list_warning_on_success_is_unknown(self):
        for kind in ('jobs', 'groups', 'vms', 'disks'):
            with self.subTest(kind=kind), mock.patch.dict('os.environ',
                                                          {'CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR': 'true',
                                                           'SB_VERIFY_SENTINEL': 'preserved'}):
                fake = FakeGcloud([{kind: ([], 'WARNING: Some requests did not succeed.')}])
                result = self.verify(fake, duration=2)
                self.assertFalse(result['verified'])
                self.assertIn(kind, result['details']['errors'])
                self.assertTrue(all(options['env']['CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR'] == 'false'
                                    for _, options in fake.calls))
                self.assertTrue(all(options['env']['SB_VERIFY_SENTINEL'] == 'preserved'
                                    for _, options in fake.calls))

    def test_delayed_cleanup_and_external_boot_disk_are_tracked(self):
        vm_name = PREFIX + '-in-abcdef-group0-0-test'
        vm = {'name': vm_name, 'labels': {'batch-job-id': JOBS['intel']},
              'disks': [{'boot': True, 'source': 'projects/p/zones/z/disks/custom-boot'}]}
        fake = FakeGcloud([
            {'jobs': [{'name': 'projects/p/locations/r/jobs/' + JOBS['intel']}],
             'vms': [vm], 'disks': [{'name': 'custom-boot'}], 'bucket': {'name': BUCKET}},
            {'disks': [{'name': 'custom-boot'}]}, {}, {},
        ])
        result = self.verify(fake)
        self.assertTrue(result['verified'])
        self.assertEqual(result['details']['observations'], 4)

    def test_unlabeled_disk_and_job_prefix_block_success(self):
        fake = FakeGcloud([{'disks': [{'name': PREFIX + '-in-abcdef-group0-0-test'}]}])
        result = self.verify(fake, duration=2)
        self.assertFalse(result['verified'])
        self.assertEqual(len(result['details']['resources']['disks']), 1)

    def test_unresolved_boot_disk_identity_remains_unknown(self):
        fake = FakeGcloud([{'vms': [{'name': PREFIX + '-in-abcdef-group0-0-test'}]}, {}, {}, {}])
        result = self.verify(fake, duration=3)
        self.assertFalse(result['verified'])
        self.assertIn('boot disk', result['details']['errors']['identity'])

    def test_permission_error_and_timeout_are_not_absence(self):
        for failed in ('ERROR: 403 Permission denied', subprocess.TimeoutExpired('gcloud', 1)):
            with self.subTest(failed=failed):
                fake = FakeGcloud([{'bucket': failed}])
                result = self.verify(fake, duration=2)
                self.assertFalse(result['verified'])
                self.assertIn('bucket', result['details']['errors'])

    def test_list_failure_and_invalid_response_are_not_absence(self):
        for failed in ('ERROR: permission denied', {'unexpected': 'shape'}):
            with self.subTest(failed=failed):
                result = self.verify(FakeGcloud([{'vms': failed}]), duration=2)
                self.assertFalse(result['verified'])
                self.assertIn('vms', result['details']['errors'])

    def test_empty_then_reappearing_resources_resets_stability(self):
        fake = FakeGcloud([{}, {'jobs': [{'name': JOBS['arm']}]}, {}, {}])
        result = self.verify(fake)
        self.assertTrue(result['verified'])
        self.assertEqual(result['details']['observations'], 4)

    def test_managed_group_must_disappear_before_success(self):
        group = {'name': PREFIX + '-ar-abcdef-group0-0'}
        fake = FakeGcloud([{'groups': [group]}, {'groups': [group]}, {}, {}])
        result = self.verify(fake)
        self.assertTrue(result['verified'])
        self.assertEqual(result['details']['observations'], 4)

    def test_group_list_error_is_not_absence(self):
        result = self.verify(FakeGcloud([{'groups': 'ERROR: permission denied'}]), duration=2)
        self.assertFalse(result['verified'])
        self.assertIn('groups', result['details']['errors'])

    def test_invalid_manifest_and_mismatched_project_fail_before_query(self):
        for field, value in (('project', 'wrong-project'), ('job_prefix', 'sb-other'),
                             ('scratch_bucket', 'other-bucket'), ('jobs', {'arm': 'unrelated'})):
            with self.subTest(field=field):
                old = self.data[field]
                self.data[field] = value
                self.write_manifest()
                fake = FakeGcloud([{}])
                self.assertFalse(self.verify(fake)['verified'])
                self.assertEqual(fake.calls, [])
                self.data[field] = old


if __name__ == '__main__':
    unittest.main()

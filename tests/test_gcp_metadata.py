import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

from solver_benchmark import gcp_metadata


class GcpMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'execution.json'
        self.execution = {'host': {'machine': 'aarch64', 'cpu': ''},
                          'profile_hash': 'sha256:profile',
                          'version_manifest_hash': 'sha256:manifest'}
        self.path.write_text(json.dumps(self.execution))

    def test_attaches_actual_machine_identity_without_changing_provenance(self):
        metadata = {
            'machine-type': 'projects/123/machineTypes/c4a-standard-8',
            'cpu-platform': 'Google Axion',
            'zone': 'projects/123/zones/us-central1-f',
        }
        with mock.patch.object(gcp_metadata, 'read_metadata', side_effect=metadata.get):
            gcp_metadata.attach_metadata(self.path)
        result = json.loads(self.path.read_text())
        self.assertEqual(result['host']['cloud'], {
            'provider': 'gcp', 'machine_type': 'c4a-standard-8',
            'cpu_platform': 'Google Axion', 'zone': 'us-central1-f'})
        self.assertEqual(result['host']['cpu'], '')
        self.assertEqual(result['profile_hash'], self.execution['profile_hash'])
        self.assertEqual(result['version_manifest_hash'], self.execution['version_manifest_hash'])

    def test_incomplete_metadata_does_not_modify_execution(self):
        before = self.path.read_bytes()
        with mock.patch.object(gcp_metadata, 'read_metadata', side_effect=[
                'projects/123/machineTypes/c4a-standard-8', '', 'projects/123/zones/us-central1-f']):
            with self.assertRaises(ValueError):
                gcp_metadata.attach_metadata(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_metadata_requests_retry_transient_failures(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'Google Axion\n'
        opener = mock.Mock()
        opener.open.side_effect = [urllib.error.URLError('temporary'), response]
        with mock.patch.object(gcp_metadata.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(gcp_metadata.time, 'sleep'):
            self.assertEqual(gcp_metadata.read_metadata('cpu-platform'), 'Google Axion')
        self.assertEqual(opener.open.call_count, 2)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, gcp_metadata.METADATA_ROOT + 'cpu-platform')
        self.assertEqual(request.get_header('Metadata-flavor'), 'Google')


if __name__ == '__main__':
    unittest.main()

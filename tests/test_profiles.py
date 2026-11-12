import copy
import json
from pathlib import Path
import tempfile
import unittest

from solver_benchmark.profiles import canonical_hash, load_json, validate_profile, input_hash
from solver_benchmark.problems import generate_case

ROOT=Path(__file__).resolve().parents[1]

class Profiles(unittest.TestCase):
    def setUp(self):
        self.profile=json.loads((ROOT/'examples/benchmark-profile.json').read_text())
    def test_schema_copy_matches(self):
        self.assertEqual((ROOT/'schemas/benchmark-profile.schema.json').read_bytes(),
                         (ROOT/'python/solver_benchmark/benchmark-profile.schema.json').read_bytes())
    def test_identity(self):
        validate_profile(self.profile)
        reordered=json.loads(json.dumps(self.profile,sort_keys=True))
        self.assertEqual(canonical_hash(self.profile),canonical_hash(reordered))
        reordered['cases'][0]['generator']['parameters']['dx'] *= 2
        self.assertNotEqual(canonical_hash(self.profile),canonical_hash(reordered))
    def test_input_identity(self):
        a=generate_case(self.profile['cases'][1]); b=generate_case(self.profile['cases'][1])
        self.assertEqual(input_hash(a),input_hash(b))
        b['arrays']['rhs']=bytes(len(b['arrays']['rhs']))
        self.assertNotEqual(input_hash(a),input_hash(b))
    def test_duplicates_and_overflow(self):
        self.profile['cases'].append(copy.deepcopy(self.profile['cases'][0]))
        with self.assertRaises(ValueError): validate_profile(self.profile)
        self.profile['cases'].pop()
        self.profile['cases'][0]['generator']['seed']=str(2**64)
        with self.assertRaises(ValueError): validate_profile(self.profile)
    def test_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.json'; path.write_text('{"x":1,"x":2}')
            with self.assertRaises(ValueError): load_json(path)
    def test_runtime_dependency_identity(self):
        from solver_benchmark.profiles import version_manifest, file_hash
        with tempfile.TemporaryDirectory() as directory:
            library=Path(directory)/'backend.so'; library.write_bytes(b'backend-v1')
            worker={'identity':{'name':'test'},'executable_hash':'sha256:fixed',
                    'runtime_dependency_hashes':{'backend':file_hash(library)}}
            old=canonical_hash(version_manifest({'test':worker}))
            library.write_bytes(b'backend-v2')
            worker['runtime_dependency_hashes']['backend']=file_hash(library)
            self.assertNotEqual(old,canonical_hash(version_manifest({'test':worker})))

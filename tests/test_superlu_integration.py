"""End-to-end numerical coverage for the optional SuperLU worker."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from solver_benchmark.cli import execute


ROOT = Path(__file__).resolve().parents[1]
WORKER = os.environ.get("SB_SUPERLU_WORKER", "")


@unittest.skipUnless(WORKER and Path(WORKER).is_file(), "SuperLU worker not built")
class SuperLUIntegration(unittest.TestCase):
    def test_exactly_singular_factorization_is_reported_as_failure(self):
        profile = json.loads((ROOT / "examples/generated-physics-profile.json").read_text())
        case = copy.deepcopy(profile["cases"][1])
        # The 2x2-grid Laplacian has smallest eigenvalue 36, so this
        # integer shift makes the matrix exactly singular in binary64.
        case["generator"]["parameters"] = {"nx": 2, "ny": 2, "wavenumber": 6}
        profile["cases"] = [case]
        profile["solvers"][0]["id"] = "superlu"
        profile["passes"] = profile["passes"][:1]
        profile["passes"][0]["warmup_runs"] = 0
        profile["passes"][0]["measured_runs"] = 1
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            recipe = base / "profile.json"
            recipe.write_text(json.dumps(profile))
            deployment = base / "deployment.json"
            deployment.write_text(json.dumps({"schema_version": "1.0", "solvers": {
                "superlu": {"executable": str(Path(WORKER).resolve()),
                            "arguments": [], "environment": {}}}}))
            output = base / "results"
            self.assertEqual(execute(recipe, deployment, output), 1)
            rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "solver_failed")
            self.assertEqual(rows[0]["phases"][-1]["phase"], "numeric")

    def test_generated_cases_and_complex_expansion(self):
        profile = json.loads((ROOT / "examples/generated-physics-profile.json").read_text())
        # Include a nonsymmetric complex-encoded case as well as all the real
        # SPD, indefinite and nonsymmetric examples. The complex coefficients
        # in these generators are real; the transport expansion is still tested.
        complex_case = copy.deepcopy(profile["cases"][0])
        complex_case["id"] = "advection-complex-encoding"
        complex_case["encoding"]["kind"] = "complex_interleaved"
        profile["cases"].append(complex_case)
        profile["solvers"][0]["id"] = "superlu"
        profile["passes"] = profile["passes"][:1]
        profile["passes"][0]["warmup_runs"] = 0
        profile["passes"][0]["measured_runs"] = 1

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            recipe = base / "profile.json"
            recipe.write_text(json.dumps(profile))
            deployment = base / "deployment.json"
            deployment.write_text(json.dumps({"schema_version": "1.0", "solvers": {
                "superlu": {"executable": str(Path(WORKER).resolve()),
                            "arguments": [], "environment": {}}}}))
            output = base / "results"
            self.assertEqual(execute(recipe, deployment, output), 0)
            rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), len(profile["cases"]))
            for row in rows:
                with self.subTest(case=row["case_id"]):
                    self.assertEqual(row["outcome"], "success")
                    self.assertEqual(len(row["accuracy"]), 1)
                    accuracy = row["accuracy"][0]["accuracy"]
                    self.assertTrue(accuracy["accepted"])
                    self.assertEqual(len(accuracy["columns"]), 3)
                    for column in accuracy["columns"]:
                        self.assertLess(column["componentwise_backward_error"], 1e-12)


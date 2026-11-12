import copy
import json
import math
import unittest

from solver_benchmark.problems import generate_case, decode_array
from solver_benchmark.accuracy import validate_solution


OPTIONS = {"relative_tolerance": 1e-10, "absolute_tolerance": 0}


def recipe(dimensions=1, sizes=(3,), spacings=(1.0,), columns=1, encoding="real"):
    axes = "xyz"[:dimensions]
    parameters = {"m" + axis: size for axis, size in zip(axes, sizes)}
    parameters.update({"d" + axis: spacing for axis, spacing in zip(axes, spacings)})
    return {
        "id": "test", "data_format": "csr-f64-i32-i64-le", "encoding": {"kind": encoding},
        "generator": {"id": f"laplacian_{dimensions}d", "parameters": parameters, "seed": "0"},
        "rhs": {"count": columns, "generator": {"id": "manufactured_constant_columns",
                "parameters": {"first_value": 1, "column_increment": 1}, "seed": "0"}},
    }


def unpack(problem):
    arrays = problem["arrays"]
    return (decode_array("q", arrays["row_offsets"]), decode_array("i", arrays["column_indices"]),
            decode_array("d", arrays["values"]), decode_array("d", arrays["rhs"]))


def entries(problem):
    offsets, columns, values, _ = unpack(problem)
    return {(row, columns[p]): values[p] for row in range(problem["matrix"]["order"])
            for p in range(offsets[row], offsets[row + 1])}


class LaplacianTests(unittest.TestCase):
    def test_one_dimension_and_spacing(self):
        problem = generate_case(recipe(spacings=(0.5,)))
        offsets, columns, values, rhs = unpack(problem)
        self.assertEqual(list(offsets), [0, 2, 5, 7])
        self.assertEqual(list(columns), [0, 1, 0, 1, 2, 1, 2])
        self.assertEqual(list(values), [8, -4, -4, 8, -4, -4, 8])
        self.assertEqual(list(rhs), [4, 0, 4])
        self.assertEqual(problem["arrays"]["row_offsets"][:8], b"\0" * 8)
        self.assertEqual(problem["arrays"]["row_offsets"][8:16], b"\x02" + b"\0" * 7)

    def test_two_dimensions_anisotropic_and_no_wrap(self):
        problem = generate_case(recipe(2, (2, 3), (1, 0.5)))
        a = entries(problem)
        self.assertEqual(problem["matrix"]["order"], 6)
        self.assertEqual(problem["matrix"]["nonzeros"], "20")
        self.assertEqual({j: value for (i, j), value in a.items() if i == 0}, {0: 10, 1: -1, 2: -4})
        self.assertNotIn((1, 2), a)  # Consecutive flat IDs across an x-boundary are not neighbors.
        self.assertEqual(a[(3, 5)], -4)
        self.assertTrue(all(a[(j, i)] == value for (i, j), value in a.items()))

    def test_three_dimensions_anisotropic(self):
        problem = generate_case(recipe(3, (2, 2, 2), (1, 2, 0.5)))
        a = entries(problem)
        self.assertEqual({j: value for (i, j), value in a.items() if i == 0},
                         {0: 10.5, 1: -1, 2: -0.25, 4: -4})
        self.assertNotIn((3, 4), a)
        self.assertEqual(a[(7, 7)], 10.5)
        self.assertEqual(a[(7, 3)], -4)
        rectangular = generate_case(recipe(3, (2, 3, 4), (1, 1, 1)))
        self.assertEqual(rectangular["matrix"]["nonzeros"], "116")

    def test_single_point_dimensions_keep_dirichlet_diagonal(self):
        for dimension in (1, 2, 3):
            with self.subTest(dimension=dimension):
                problem = generate_case(recipe(dimension, (1,) * dimension, (1,) * dimension))
                self.assertEqual(entries(problem), {(0, 0): 2 * dimension})
                self.assertEqual(problem["matrix"]["properties"], {
                    "symmetric": "yes", "structurally_symmetric": "yes", "positive_definite": "yes"})

    def test_invalid_and_unbounded_recipes(self):
        original = recipe()
        invalid = []
        for field, value in [("mx", 0), ("mx", True), ("mx", 2.5), ("dx", 0), ("dx", -1),
                             ("dx", math.inf), ("dx", math.nan), ("dx", 1e-300), ("dx", 1e300)]:
            case = copy.deepcopy(original); case["generator"]["parameters"][field] = value; invalid.append(case)
        case = copy.deepcopy(original); case["generator"]["parameters"]["wavenumber"] = 1; invalid.append(case)
        case = copy.deepcopy(original); del case["generator"]["parameters"]["dx"]; invalid.append(case)
        for seed in ["01", "-1", "18446744073709551616", 0]:
            case = copy.deepcopy(original); case["generator"]["seed"] = seed; invalid.append(case)
        for case in invalid:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    generate_case(case)
        with self.assertRaises(ValueError):
            generate_case(original, max_entries=6)
        with self.assertRaises(ValueError):
            generate_case(recipe(columns=100), max_entries=100)

    def test_complex_expansion_and_multiple_rhs(self):
        problem = generate_case(recipe(columns=2, encoding="complex_interleaved"))
        self.assertEqual(problem["matrix"]["order"], 6)
        self.assertEqual(problem["matrix"]["complex_order"], 3)
        self.assertEqual(problem["matrix"]["nonzeros"], "14")
        self.assertEqual(problem["dense"], {"rows": 6, "columns": 2, "row_stride": "1", "column_stride": "6"})
        a = entries(problem)
        self.assertEqual(a[(0, 2)], -1)
        self.assertEqual(a[(1, 3)], -1)
        self.assertNotIn((0, 1), a)
        self.assertEqual(problem["reference"], [1] * 6 + [2] * 6)
        self.assertTrue(validate_solution(problem, problem["reference"], OPTIONS)["accepted"])


class AccuracyTests(unittest.TestCase):
    def test_every_dimension_reference_is_accepted(self):
        for dimension in (1, 2, 3):
            problem = generate_case(recipe(dimension, (3,) * dimension, (0.3,) * dimension, columns=3))
            result = validate_solution(problem, problem["reference"], OPTIONS)
            self.assertTrue(result["accepted"])
            self.assertEqual(len(result["columns"]), 3)
            json.dumps(result, allow_nan=False)

    def test_one_bad_rhs_fails_block(self):
        problem = generate_case(recipe(columns=3))
        solution = problem["reference"].copy(); solution[4] += 0.1
        result = validate_solution(problem, solution, OPTIONS)
        self.assertFalse(result["accepted"])
        self.assertEqual([c["accepted"] for c in result["columns"]], [True, False, True])
        self.assertGreater(result["worst_relative_residual_l2"], 0)

    def test_nonfinite_solution_and_overflow_fail_without_invalid_json(self):
        problem = generate_case(recipe())
        for value in [math.nan, math.inf, 1e308]:
            solution = problem["reference"].copy(); solution[1] = value
            result = validate_solution(problem, solution, OPTIONS)
            self.assertFalse(result["accepted"])
            self.assertFalse(result["finite"])
            json.dumps(result, allow_nan=False)

    def test_zero_rhs_convention_and_absolute_tolerance(self):
        case = recipe(); case["rhs"]["generator"]["parameters"]["first_value"] = 0
        problem = generate_case(case)
        exact = validate_solution(problem, [0, 0, 0], OPTIONS)
        self.assertTrue(exact["accepted"])
        self.assertEqual(exact["columns"][0]["relative_residual_l2"], 0)
        approximate = validate_solution(problem, [1e-8] * 3,
                                        {"relative_tolerance": 0, "absolute_tolerance": 1e-6})
        self.assertTrue(approximate["accepted"])
        self.assertEqual(approximate["columns"][0]["relative_residual_l2"], {"nonfinite": "+inf"})
        self.assertFalse(validate_solution(problem, [1e-8] * 3, OPTIONS)["accepted"])

    def test_wrong_shape_and_invalid_tolerances(self):
        problem = generate_case(recipe())
        with self.assertRaises(ValueError):
            validate_solution(problem, [0], OPTIONS)
        for tolerance in [-1, math.inf, math.nan, True]:
            with self.assertRaises(ValueError):
                validate_solution(problem, [0] * 3,
                                  {"relative_tolerance": tolerance, "absolute_tolerance": 0})


if __name__ == "__main__":
    unittest.main()

"""Independent, per-RHS validation using original parent-owned inputs."""

import math
from .problems import decode_array


def _metric(value):
    if math.isfinite(value):
        return value
    return {"nonfinite": "nan" if math.isnan(value) else ("+inf" if value > 0 else "-inf")}


def _ratio(numerator, denominator):
    if denominator == 0:
        return 0.0 if numerator == 0 else math.inf
    return numerator / denominator


def _norm2(values):
    # Scaling avoids squaring unscaled values, which can overflow or underflow.
    scale = max(map(abs, values), default=0.0)
    if scale == 0:
        return 0.0
    if not math.isfinite(scale):
        return math.inf
    return scale * math.sqrt(math.fsum((value / scale) ** 2 for value in values))


def _failed_column(reason):
    return {
        "finite": False, "accepted": False, "error": reason,
        "residual_l2": {"nonfinite": "+inf"},
        "relative_residual_l2": {"nonfinite": "+inf"},
        "normwise_backward_error_inf": {"nonfinite": "+inf"},
        "componentwise_backward_error": {"nonfinite": "+inf"},
        "relative_reference_error_l2": None,
    }


def validate_solution(problem, solution, options):
    """A solver success is accepted only if every column meets the common target.

    Products are binary64; math.fsum accumulates them independently from backend
    arithmetic. Any nonfinite solution or invalid verification arithmetic fails.
    Infinite zero-denominator diagnostics use JSON-safe tagged values.
    """
    relative = options["relative_tolerance"]
    absolute = options["absolute_tolerance"]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in (relative, absolute)):
        raise ValueError("validation tolerances must be finite and nonnegative")
    rows, count = problem["dense"]["rows"], problem["dense"]["columns"]
    if len(solution) != rows * count:
        raise ValueError("solution shape does not match original input")
    arrays = problem["arrays"]
    offsets = decode_array("q", arrays["row_offsets"])
    indices = decode_array("i", arrays["column_indices"])
    values = decode_array("d", arrays["values"])
    rhs = decode_array("d", arrays["rhs"])
    reference = problem.get("reference")
    records, worst = [], 0.0
    for column in range(count):
        begin, end = column * rows, (column + 1) * rows
        x, b = solution[begin:end], rhs[begin:end]
        if any(not math.isfinite(value) for value in x):
            records.append(_failed_column("nonfinite solution")); worst = math.inf
            continue
        try:
            residual, denominator, row_norms = [], [], []
            for row in range(rows):
                entries = range(offsets[row], offsets[row + 1])
                products = [values[p] * x[indices[p]] for p in entries]
                residual.append(math.fsum([b[row], *(-value for value in products)]))
                denominator.append(math.fsum([abs(b[row]), *(abs(value) for value in products)]))
                row_norms.append(math.fsum(abs(values[p]) for p in entries))
            residual_norm, rhs_norm = _norm2(residual), _norm2(b)
            x_inf = max(map(abs, x), default=0.0)
            b_inf = max(map(abs, b), default=0.0)
            r_inf = max(map(abs, residual), default=0.0)
            normwise_denominator = max(row_norms, default=0.0) * x_inf + b_inf
            if not all(math.isfinite(value) for value in [residual_norm, rhs_norm, normwise_denominator,
                                                          *residual, *denominator, *row_norms]):
                raise ArithmeticError("nonfinite verification arithmetic")
            # Equivalent to ||r|| <= atol + rtol*||b||, without overflowing target.
            accepted = residual_norm <= absolute or (
                rhs_norm > 0 and (residual_norm - absolute) / rhs_norm <= relative)
            relative_residual = _ratio(residual_norm, rhs_norm)
            componentwise = max((_ratio(abs(r), d) for r, d in zip(residual, denominator)), default=0.0)
            reference_error = None
            if reference is not None:
                expected = reference[begin:end]
                reference_norm = _norm2(expected)
                difference_norm = _norm2([a - e for a, e in zip(x, expected)])
                if not math.isfinite(reference_norm) or not math.isfinite(difference_norm):
                    raise ArithmeticError("nonfinite reference arithmetic")
                reference_error = _metric(_ratio(difference_norm, reference_norm))
            records.append({
                "finite": True, "accepted": accepted,
                "residual_l2": _metric(residual_norm), "relative_residual_l2": _metric(relative_residual),
                "normwise_backward_error_inf": _metric(_ratio(r_inf, normwise_denominator)),
                "componentwise_backward_error": _metric(componentwise),
                "relative_reference_error_l2": reference_error,
            })
            worst = max(worst, relative_residual)
        except (OverflowError, ValueError, ArithmeticError) as error:
            records.append(_failed_column(str(error))); worst = math.inf
    return {
        "finite": all(record["finite"] for record in records),
        "accepted": all(record["accepted"] for record in records),
        "worst_relative_residual_l2": _metric(worst), "columns": records,
        "verification_arithmetic": "binary64_products_math.fsum_scaled_l2",
    }

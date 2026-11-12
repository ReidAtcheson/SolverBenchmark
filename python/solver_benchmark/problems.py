"""Deterministic, solver-independent matrix and manufactured RHS recipes."""

from array import array
import math
import re
import sys

DATA_FORMAT = "csr-f64-i32-i64-le"
GENERATOR_VERSION = "4"
MAX_INDEX = (1 << 31) - 1
MAX_OFFSET = (1 << 63) - 1
DEFAULT_MAX_ENTRIES = 5_000_000


def encode_array(typecode, values):
    """Encode profile arrays explicitly little-endian, including on big-endian hosts."""
    result = array(typecode, values)
    if result.itemsize != {"q": 8, "i": 4, "d": 8}[typecode]:
        raise RuntimeError("unsupported native array representation")
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


def decode_array(typecode, payload):
    result = array(typecode)
    if result.itemsize != {"q": 8, "i": 4, "d": 8}[typecode]:
        raise RuntimeError("unsupported native array representation")
    result.frombytes(payload)
    if sys.byteorder != "little":
        result.byteswap()
    return result


def _exact_keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{name} requires exactly {', '.join(keys)}")


def _positive_integer(value, name, maximum=MAX_INDEX):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [1, {maximum}]")
    return value


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must fit binary64") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _recipe(recipe, name):
    _exact_keys(recipe, ("id", "parameters", "seed"), name)
    seed = recipe["seed"]
    if not isinstance(seed, str) or not re.fullmatch(r"0|[1-9][0-9]*", seed):
        raise ValueError(f"{name}.seed must be a canonical uint64 decimal string")
    if len(seed) > 20 or int(seed) >= 1 << 64:
        raise ValueError(f"{name}.seed exceeds uint64")
    return recipe["id"], recipe["parameters"]


def _laplacian_stencil(sizes, spacings):
    weights = []
    for spacing in spacings:
        reciprocal = 1.0 / spacing
        weight = reciprocal * reciprocal
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("inverse squared spacings must be positive finite binary64 values")
        weights.append(weight)
    try:
        diagonal = 2.0 * math.fsum(weights)
    except OverflowError as error:
        raise ValueError("Laplacian diagonal overflows binary64") from error
    if not math.isfinite(diagonal):
        raise ValueError("Laplacian diagonal overflows binary64")

    strides, stride = [], 1
    for size in sizes:
        strides.append(stride)
        stride *= size
    offsets, columns, values = array("q", [0]), array("i"), array("d")
    for row in range(stride):
        entries = [(row, diagonal)]
        for size, step, weight in zip(sizes, strides, weights):
            coordinate = (row // step) % size
            if coordinate:
                entries.append((row - step, -weight))
            if coordinate + 1 < size:
                entries.append((row + step, -weight))
        entries.sort()
        columns.extend(column for column, _ in entries)
        values.extend(value for _, value in entries)
        offsets.append(len(columns))
    return offsets, columns, values


def _complex_expand(offsets, columns, values):
    """Real A is interpreted as a complex operator with zero imaginary part."""
    expanded_offsets, expanded_columns, expanded_values = array("q", [0]), array("i"), array("d")
    for row in range(len(offsets) - 1):
        for component in range(2):
            for position in range(offsets[row], offsets[row + 1]):
                expanded_columns.append(2 * columns[position] + component)
                expanded_values.append(values[position])
            expanded_offsets.append(len(expanded_columns))
    return expanded_offsets, expanded_columns, expanded_values


def _splitmix64(seed):
    """Specified integer PRNG, mapping the high 53 bits to [0, 1)."""
    mask = (1 << 64) - 1
    while True:
        seed = (seed + 0x9E3779B97F4A7C15) & mask
        z = seed
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z ^= z >> 31
        yield (z >> 11) / (1 << 53)


def generate_case(case, *, max_entries=DEFAULT_MAX_ENTRIES):
    """Materialize a case as immutable wire bytes and a parent-only reference.

    max_entries bounds both stored CSR entries and dense elements before any
    large allocation. It is a reported local resource limit, not a recipe change.
    """
    _positive_integer(max_entries, "max_entries", MAX_OFFSET)
    if case.get("data_format") != DATA_FORMAT:
        raise ValueError("unsupported data_format")
    _exact_keys(case.get("encoding"), ("kind",), "encoding")
    encoding = case["encoding"]["kind"]
    if encoding not in ("real", "complex_interleaved"):
        raise ValueError("unsupported encoding")
    generator, parameters = _recipe(case.get("generator"), "generator")
    dimensions = {"laplacian_1d": 1, "laplacian_2d": 2, "laplacian_3d": 3}.get(generator)
    factor = 2 if encoding == "complex_interleaved" else 1
    _exact_keys(case.get("rhs"), ("generator", "count"), "rhs")
    count = _positive_integer(case["rhs"]["count"], "rhs.count", (1 << 32) - 1)
    rhs_id, rhs_parameters = _recipe(case["rhs"]["generator"], "rhs.generator")
    if rhs_id == "manufactured_constant_columns":
        _exact_keys(rhs_parameters, ("first_value", "column_increment"), rhs_id)
        first = _finite_number(rhs_parameters["first_value"], "first_value")
        increment = _finite_number(rhs_parameters["column_increment"], "column_increment")
    elif rhs_id == "manufactured_seeded_columns":
        _exact_keys(rhs_parameters, ("offset", "amplitude"), rhs_id)
        first = _finite_number(rhs_parameters["offset"], "offset")
        amplitude = _finite_number(rhs_parameters["amplitude"], "amplitude")
        if amplitude <= 0:
            raise ValueError("amplitude must be positive")
        rng = _splitmix64(int(case["rhs"]["generator"]["seed"]))
    else:
        raise ValueError(f"unknown RHS generator: {rhs_id}")
    if dimensions is None:
        if generator == "volume_diffusion_torus":
            from .volume_torus import build
        elif generator in ("fem_diffusion_square", "fem_diffusion_cube", "surface_diffusion_torus"):
            from .mesh_problems import build
        elif generator in ("stokes_mac_2d", "darcy_mixed_2d", "elasticity_2d"):
            from .mixed_problems import build
        elif generator in ("geographic_grid_dc", "geographic_grid_ac", "grid_dc_opf_kkt"):
            from .grid_problems import build
        else:
            from .extended_problems import build
        try:
            offsets, columns, values, properties = build(
                generator, parameters, max_entries // factor, seed=case["generator"]["seed"],
                max_order=min(MAX_INDEX // factor, max_entries // (factor * count),
                              sys.maxsize // (8 * factor * count)))
        except (OverflowError, ZeroDivisionError) as error:
            raise ValueError("generator arithmetic outside binary64 range") from error
        logical_order = len(offsets) - 1
        order = factor * logical_order
        nonzeros = factor * len(columns)
    else:
        axes = "xyz"[:dimensions]
        keys = tuple("m" + axis for axis in axes) + tuple("d" + axis for axis in axes)
        _exact_keys(parameters, keys, generator)
        sizes = [_positive_integer(parameters["m" + axis], "m" + axis) for axis in axes]
        spacings = [_finite_number(parameters["d" + axis], "d" + axis) for axis in axes]
        if any(spacing <= 0 for spacing in spacings):
            raise ValueError("grid spacings must be positive")
        logical_order = math.prod(sizes)
        order = factor * logical_order
        nonzeros = factor * (logical_order + 2 * sum((size - 1) * (logical_order // size) for size in sizes))
        properties = {"symmetric": "yes", "structurally_symmetric": "yes", "positive_definite": "yes"}
    if order > MAX_INDEX or nonzeros > MAX_OFFSET or order > sys.maxsize:
        raise ValueError("matrix dimensions exceed the wire/address-space limits")

    dense_count = order * count
    if nonzeros > max_entries or dense_count > max_entries or dense_count > sys.maxsize // 8:
        raise ValueError(f"case exceeds local max_entries={max_entries}")

    if dimensions is not None:
        offsets, columns, values = _laplacian_stencil(sizes, spacings)
    if factor == 2:
        offsets, columns, values = _complex_expand(offsets, columns, values)
    reference, rhs = array("d"), array("d")
    for column in range(count):
        if rhs_id == "manufactured_constant_columns":
            constant = first + column * increment
            reference_column = [constant] * order
        else:
            reference_column = [first + amplitude * (2 * next(rng) - 1) for _ in range(order)]
        if not all(math.isfinite(v) for v in reference_column):
            raise ValueError("manufactured reference overflows binary64")
        reference.extend(reference_column)
        for row in range(order):
            try:
                value = math.fsum(values[p] * reference_column[columns[p]]
                                  for p in range(offsets[row], offsets[row + 1]))
            except (OverflowError, ValueError) as error:
                raise ValueError("manufactured RHS arithmetic is nonfinite") from error
            if not math.isfinite(value):
                raise ValueError("manufactured RHS arithmetic is nonfinite")
            rhs.append(value)
    matrix = {
        "order": order, "nonzeros": str(nonzeros), "encoding": encoding,
        "properties": properties,
    }
    if factor == 2:
        matrix["complex_order"] = logical_order
    return {
        "data_format": DATA_FORMAT,
        "matrix": matrix,
        "dense": {"rows": order, "columns": count, "row_stride": "1", "column_stride": str(order)},
        "arrays": {
            "row_offsets": encode_array("q", offsets), "column_indices": encode_array("i", columns),
            "values": encode_array("d", values), "rhs": encode_array("d", rhs),
        },
        "reference": list(reference),
        "generation": {"implementation_version": GENERATOR_VERSION,
                       "boundary": "homogeneous_dirichlet_interior" if dimensions is not None else "generator_specific", "indexing": "x_fastest" if dimensions is not None else "generator_specific",
                       "rhs_arithmetic": "binary64_products_math.fsum_binary64_output"},
    }

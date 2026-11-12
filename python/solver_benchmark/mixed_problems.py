"""Staggered incompressible flow, mixed Darcy, and P1 plane-strain elasticity.

All domains are the unit square. See docs/mixed-generators.md for equations,
ordering, gauges, boundary treatment, and parameter contracts.
"""
from array import array
import math


PARAMETERS = {
    'stokes_mac_2d': ('nx', 'ny', 'viscosity'),
    'darcy_mixed_2d': ('nx', 'ny', 'permeability', 'contrast'),
    'elasticity_2d': ('nx', 'ny', 'lame_lambda', 'lame_mu'),
}


def _integer(value, name, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value


def _coefficient(value, name, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f'{name} must be a finite number')
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f'{name} must fit binary64') from error
    if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0):
        raise ValueError(f'{name} must be finite and {"nonnegative" if allow_zero else "positive"}')
    return result


def _check_positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} is outside positive finite binary64 range')
    return value


def build(generator, p, limit, seed='0', *, max_order=(1 << 31)-1):
    """Return CSR and properties. Deterministic generators do not consume seed."""
    if generator not in PARAMETERS:
        raise ValueError(f'unknown matrix generator: {generator}')
    if not isinstance(p, dict) or set(p) != set(PARAMETERS[generator]):
        raise ValueError(f'{generator} requires exactly {", ".join(PARAMETERS[generator])}')
    _integer(limit, 'limit', 1)
    elasticity = generator == 'elasticity_2d'
    nx = _integer(p['nx'], 'nx', 1 if elasticity else 2)
    ny = _integer(p['ny'], 'ny', 1 if elasticity else 2)
    if elasticity:
        order = 2 * nx * ny
        # A node touches at most nine nodes, each with two displacement DOFs.
        bound = 18 * order
    else:
        faces = (nx - 1) * ny + nx * (ny - 1)
        order = faces + nx * ny - 1
        bound = (9 if generator == 'stokes_mac_2d' else 5) * faces
    if order > max_order or max(order, bound) > limit:
        raise ValueError(f'case exceeds local max_entries={limit}')
    rows = [dict() for _ in range(order)]

    def add(i, j, value):
        if value:
            rows[i][j] = rows[i].get(j, 0.0) + value

    try:
        if elasticity:
            lam = _coefficient(p['lame_lambda'], 'lame_lambda', allow_zero=True)
            mu = _coefficient(p['lame_mu'], 'lame_mu')
            _elasticity(nx, ny, lam, mu, add)
        else:
            _mixed(generator, nx, ny, p, add)
    except (OverflowError, ZeroDivisionError) as error:
        raise ValueError('generator arithmetic outside binary64 range') from error
    offsets, columns, values = array('q', [0]), array('i'), array('d')
    for row in rows:
        for j, value in sorted(row.items()):
            if not math.isfinite(value):
                raise ValueError('matrix contains nonfinite values')
            if value:
                columns.append(j)
                values.append(value)
        offsets.append(len(columns))
    properties = {'symmetric': 'yes', 'structurally_symmetric': 'yes',
                  'positive_definite': 'yes' if elasticity else 'no'}
    return offsets, columns, values, properties


def _mixed(generator, nx, ny, p, add):
    nu = (nx - 1) * ny
    nv = nx * (ny - 1)
    velocity_count = nu + nv

    def u(i, j):
        return j * (nx - 1) + i - 1

    def v(i, j):
        return nu + (j - 1) * nx + i

    def gradient(face, left_cell, right_cell, inverse_spacing):
        # B = -div, so B.T is grad. Omit pressure p(0,0) and its redundant
        # continuity equation to remove the constant pressure nullspace.
        for cell, sign in ((left_cell, -1), (right_cell, 1)):
            if cell:
                pressure = velocity_count + cell - 1
                add(face, pressure, sign * inverse_spacing)
                add(pressure, face, sign * inverse_spacing)

    if generator == 'stokes_mac_2d':
        viscosity = _coefficient(p['viscosity'], 'viscosity')
        wx = _check_positive(viscosity * nx * nx, 'x diffusion weight')
        wy = _check_positive(viscosity * ny * ny, 'y diffusion weight')
        for j in range(ny):
            for i in range(1, nx):
                row = u(i, j)
                # Tangential no-slip data use odd reflection across the wall.
                diagonal = 2 * wx + (2 + (j == 0) + (j == ny - 1)) * wy
                add(row, row, _check_positive(diagonal, 'velocity diagonal'))
                for ii in (i - 1, i + 1):
                    if 1 <= ii < nx:
                        add(row, u(ii, j), -wx)
                for jj in (j - 1, j + 1):
                    if 0 <= jj < ny:
                        add(row, u(i, jj), -wy)
        for j in range(1, ny):
            for i in range(nx):
                row = v(i, j)
                diagonal = (2 + (i == 0) + (i == nx - 1)) * wx + 2 * wy
                add(row, row, _check_positive(diagonal, 'velocity diagonal'))
                for ii in (i - 1, i + 1):
                    if 0 <= ii < nx:
                        add(row, v(ii, j), -wx)
                for jj in (j - 1, j + 1):
                    if 1 <= jj < ny:
                        add(row, v(i, jj), -wy)
    else:
        permeability = _coefficient(p['permeability'], 'permeability')
        contrast = _coefficient(p['contrast'], 'contrast')
        right = _check_positive(permeability * contrast, 'right-half permeability')
        inverse_left = _check_positive(1 / permeability, 'inverse permeability')
        inverse_right = _check_positive(1 / right, 'inverse right-half permeability')

        def inverse_k(i):
            return inverse_left if 2 * i + 1 < nx else inverse_right

        for j in range(ny):
            for i in range(1, nx):
                # Average inverse permeability = inverse harmonic mean.
                add(u(i, j), u(i, j), _check_positive(
                    0.5 * inverse_k(i - 1) + 0.5 * inverse_k(i), 'face resistance'))
        for j in range(1, ny):
            for i in range(nx):
                add(v(i, j), v(i, j), inverse_k(i))

    for j in range(ny):
        for i in range(1, nx):
            gradient(u(i, j), j * nx + i - 1, j * nx + i, nx)
    for j in range(1, ny):
        for i in range(nx):
            gradient(v(i, j), (j - 1) * nx + i, j * nx + i, ny)


def _elasticity(nx, ny, lam, mu, add):
    hx, hy = 1 / (nx + 1), 1 / (ny + 1)
    area = hx * hy / 2
    # Guard representable shear and longitudinal stiffness before assembly.
    _check_positive(mu * hx / hy, 'shear stiffness')
    _check_positive(mu * hy / hx, 'shear stiffness')
    _check_positive((lam + 2 * mu) * max(hx / hy, hy / hx), 'longitudinal stiffness')

    def dofs(vertices):
        result = []
        for node, (x, y) in enumerate(vertices):
            if 1 <= x <= nx and 1 <= y <= ny:
                result.extend((2 * ((y - 1) * nx + x - 1) + a, node, a) for a in (0, 1))
        return result

    for y in range(ny + 1):
        for x in range(nx + 1):
            # SW--NE diagonal; gradients correspond to (SW, SE, NE) and
            # (SW, NE, NW). No numerical inversion of element coordinates.
            triangles = (
                (((x, y), (x + 1, y), (x + 1, y + 1)),
                 ((-1 / hx, 0), (1 / hx, -1 / hy), (0, 1 / hy))),
                (((x, y), (x + 1, y + 1), (x, y + 1)),
                 ((0, -1 / hy), (1 / hx, 0), (-1 / hx, 1 / hy))),
            )
            for vertices, gradients in triangles:
                active = dofs(vertices)
                for pos, (row, i, a) in enumerate(active):
                    gi = gradients[i]
                    for col, j, b in active[pos:]:
                        gj = gradients[j]
                        value = area * (mu * ((sum(gi[t] * gj[t] for t in (0, 1)) if a == b else 0)
                                              + gi[b] * gj[a]) + lam * gi[a] * gj[b])
                        add(row, col, value)
                        if row != col:
                            add(col, row, value)

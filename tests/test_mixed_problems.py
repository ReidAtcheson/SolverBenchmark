"""Independent block, inertia, and strain-energy checks for model generators."""
import math
import unittest

from solver_benchmark.mixed_problems import build


def dense(name, parameters, limit=100_000):
    offsets, columns, values, properties = build(name, parameters, limit)
    n = len(offsets) - 1
    result = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for k in range(offsets[i], offsets[i + 1]):
            result[i][columns[k]] = values[k]
    return result, properties


def ldlt_diagonal(a):
    """Unpivoted LDL: valid here since SPD leading block and -SPD Schur block."""
    n = len(a)
    l = [[float(i == j) for j in range(n)] for i in range(n)]
    d = []
    for i in range(n):
        d.append(a[i][i] - math.fsum(l[i][k] ** 2 * d[k] for k in range(i)))
        if abs(d[i]) < 1e-12:
            raise AssertionError('unexpected zero pivot')
        for j in range(i + 1, n):
            l[j][i] = (a[j][i] - math.fsum(l[j][k] * l[i][k] * d[k] for k in range(i))) / d[i]
    return d


class MixedPhysicsTests(unittest.TestCase):
    def test_mac_boundary_stencil_and_linear_pressure(self):
        nx, ny = 4, 3
        a, props = dense('stokes_mac_2d', {'nx': nx, 'ny': ny, 'viscosity': 0.5})
        nu = (nx - 1) * ny
        velocities = nu + nx * (ny - 1)
        self.assertEqual(props['positive_definite'], 'no')
        self.assertEqual(a[0][0], 0.5 * (2 * nx**2 + 3 * ny**2))
        self.assertEqual(a[0][nx - 1], -0.5 * ny**2)
        self.assertEqual(a[nu][nu], 0.5 * (3 * nx**2 + 2 * ny**2))
        # Pressure p=x+2y shifted by its first-cell value gives grad=(1,2).
        pressures = [(i % nx) / nx + 2 * (i // nx) / ny for i in range(1, nx * ny)]
        for row in range(velocities):
            derivative = math.fsum(a[row][velocities + j] * val for j, val in enumerate(pressures))
            self.assertAlmostEqual(derivative, 1 if row < nu else 2)
        self.assertTrue(all(a[i][j] == 0 for i in range(velocities, len(a))
                        for j in range(velocities, len(a))))

    def test_mixed_inertia_pressure_gauge_and_symmetry(self):
        for nx, ny in ((2, 2), (2, 3), (4, 2)):
            for name, coefficients in (
                ('stokes_mac_2d', {'viscosity': 0.03}),
                ('darcy_mixed_2d', {'permeability': 0.2, 'contrast': 1000}),
            ):
                with self.subTest(name=name, nx=nx, ny=ny):
                    a, properties = dense(name, {'nx': nx, 'ny': ny, **coefficients})
                    self.assertEqual(a, list(map(list, zip(*a))))
                    self.assertEqual(properties['symmetric'], 'yes')
                    d = ldlt_diagonal(a)
                    velocities = (nx - 1) * ny + nx * (ny - 1)
                    self.assertEqual(sum(v > 0 for v in d), velocities)
                    self.assertEqual(sum(v < 0 for v in d), nx * ny - 1)

    def test_darcy_schur_is_harmonic_finite_volume_diffusion(self):
        a, _ = dense('darcy_mixed_2d', {'nx': 2, 'ny': 2, 'permeability': 2, 'contrast': 4})
        # Eliminating flux gives -B M^-1 B^T. Opposite sign below is the
        # grounded diffusion Laplacian. Horizontal k_h=2/(1/2+1/8)=3.2.
        schur = [[sum(a[i + 4][k] * a[k][j + 4] / a[k][k] for k in range(4))
                  for j in range(3)] for i in range(3)]
        expected = [[44.8, 0, -32], [0, 20.8, -12.8], [-32, -12.8, 44.8]]
        for row, expected_row in zip(schur, expected):
            for value, target in zip(row, expected_row):
                self.assertAlmostEqual(value, target)

    def test_elasticity_matches_integrated_strain_energy(self):
        nx, ny, lam, mu = 3, 2, 5.0, 2.0
        a, props = dense('elasticity_2d', {'nx': nx, 'ny': ny, 'lame_lambda': lam, 'lame_mu': mu})
        self.assertEqual(a, list(map(list, zip(*a))))
        self.assertEqual(props['positive_definite'], 'yes')
        self.assertTrue(all(d > 0 for d in ldlt_diagonal(a)))
        displacement = [math.sin(i + 0.25) for i in range(len(a))]
        hx, hy = 1 / (nx + 1), 1 / (ny + 1)

        def value(i, j):
            if i in (0, nx + 1) or j in (0, ny + 1):
                return (0.0, 0.0)
            k = 2 * ((j - 1) * nx + i - 1)
            return displacement[k:k + 2]

        energy = 0.0
        for j in range(ny + 1):
            for i in range(nx + 1):
                sw, se, ne, nw = value(i, j), value(i + 1, j), value(i + 1, j + 1), value(i, j + 1)
                for bottom, right, top, left in ((sw, se, ne, se), (nw, ne, nw, sw)):
                    # Each P1 triangle has constant gradient, evaluated from
                    # horizontal and vertical edge differences.
                    dx = [(right[c] - bottom[c]) / hx for c in (0, 1)]
                    dy = [(top[c] - left[c]) / hy for c in (0, 1)]
                    strain_xy = 0.5 * (dy[0] + dx[1])
                    energy += hx * hy / 2 * (lam * (dx[0] + dy[1])**2
                              + 2 * mu * (dx[0]**2 + dy[1]**2 + 2 * strain_xy**2))
        quadratic = math.fsum(displacement[i] * a[i][j] * displacement[j]
                              for i in range(len(a)) for j in range(len(a)))
        self.assertAlmostEqual(quadratic, energy, places=11)

    def test_elasticity_small_meshes_and_lambda_zero(self):
        for nx, ny in ((1, 1), (1, 3), (3, 1), (3, 3)):
            a, _ = dense('elasticity_2d', {'nx': nx, 'ny': ny, 'lame_lambda': 0, 'lame_mu': 1})
            self.assertTrue(all(d > 0 for d in ldlt_diagonal(a)))

    def test_preflight_and_coefficient_validation(self):
        recipes = [
            ('stokes_mac_2d', {'nx': 3, 'ny': 3, 'viscosity': 1}),
            ('darcy_mixed_2d', {'nx': 3, 'ny': 3, 'permeability': 1, 'contrast': 2}),
            ('elasticity_2d', {'nx': 3, 'ny': 3, 'lame_lambda': 2, 'lame_mu': 1}),
        ]
        for name, params in recipes:
            with self.subTest(name=name):
                self.assertEqual(build(name, params, 10000), build(name, params, 10000))
                for dimensions in ({'nx': 10**100}, {'nx': True}, {'ny': 0}):
                    with self.assertRaises(ValueError):
                        build(name, {**params, **dimensions}, 10000)
                with self.assertRaises(ValueError):
                    build(name, params, 1)
                for key in set(params) - {'nx', 'ny'}:
                    for invalid in (True, -1, math.inf, math.nan, 10**400):
                        with self.assertRaises(ValueError):
                            build(name, {**params, key: invalid}, 10000)
        for name, params in (
            ('stokes_mac_2d', {'nx': 3, 'ny': 3, 'viscosity': 1e308}),
            ('darcy_mixed_2d', {'nx': 3, 'ny': 3, 'permeability': 1e308, 'contrast': 1e308}),
            ('elasticity_2d', {'nx': 3, 'ny': 3, 'lame_lambda': 1, 'lame_mu': 1e308}),
        ):
            with self.assertRaises(ValueError):
                build(name, params, 10000)


if __name__ == '__main__':
    unittest.main()

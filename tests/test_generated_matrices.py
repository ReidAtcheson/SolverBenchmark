import math
import unittest

from solver_benchmark.problems import generate_case, decode_array
from solver_benchmark.accuracy import validate_solution


def case(name, parameters):
    return {'id':'test', 'data_format':'csr-f64-i32-i64-le', 'encoding':{'kind':'real'},
            'generator':{'id':name, 'parameters':parameters, 'seed':'0'},
            'rhs':{'count':1, 'generator':{'id':'manufactured_constant_columns',
                'parameters':{'first_value':1, 'column_increment':1}, 'seed':'0'}}}


def matrix(problem):
    a = problem['arrays']; offsets = decode_array('q', a['row_offsets'])
    cols, vals = decode_array('i', a['column_indices']), decode_array('d', a['values'])
    return {(i, cols[k]): vals[k] for i in range(problem['matrix']['order']) for k in range(offsets[i], offsets[i+1])}


class GeneratedTests(unittest.TestCase):
    def test_convection_and_helmholtz(self):
        a = matrix(generate_case(case('convection_diffusion_2d', {'nx':2,'ny':2,'diffusion':1,'velocity_x':3,'velocity_y':0})))
        self.assertEqual(a[0,1], -9)
        self.assertEqual(a[1,0], -18)
        h = matrix(generate_case(case('helmholtz_2d', {'nx':2,'ny':2,'wavenumber':6})))
        self.assertEqual(h.get((0,0), 0), 0)
        self.assertEqual(h[0,1], -9)

    def test_fem_is_symmetric_and_has_diagonals(self):
        a = matrix(generate_case(case('triangular_fem_poisson', {'nx':4,'ny':4})))
        self.assertTrue(any(abs(i-j) in (3,5) for i,j in a))
        for (i,j), value in a.items(): self.assertAlmostEqual(value, a[j,i])
        for i in range(16): self.assertGreater(a[i,i], 0)

    def test_dc_and_ac_flat_start(self):
        dc = matrix(generate_case(case('grid_dc_power_flow', {'buses':3,'reactance':0.5,'chord_stride':2})))
        self.assertEqual(dc, {(0,0):4,(0,1):-2,(1,0):-2,(1,1):4})
        ac = matrix(generate_case(case('grid_ac_jacobian', {'buses':3,'reactance':2,'resistance':1,'chord_stride':2})))
        # Finite-difference complex power injections at the flat operating point.
        def power(z):
            volt = [1+0j] + [z[i]*complex(math.cos(z[i+2]), math.sin(z[i+2])) for i in range(2)]
            admittance = 1/(1+2j)
            currents = [sum((volt[i]-volt[j])*admittance for j in range(3) if i != j) for i in range(1,3)]
            s = [volt[i+1]*currents[i].conjugate() for i in range(2)]
            return [q.real for q in s] + [q.imag for q in s]
        # Generated ordering is angles then voltage magnitudes.
        for j in range(4):
            def eval_step(delta):
                angles = [0.,0.]; mags = [1.,1.]
                (angles if j<2 else mags)[j%2] += delta
                return power(mags+angles)
            plus, minus = eval_step(1e-6), eval_step(-1e-6)
            for i in range(4): self.assertAlmostEqual(ac.get((i,j),0), (plus[i]-minus[i])/2e-6, places=8)

    def test_symmetry_metadata_uses_rounded_entries(self):
        for dimension in (2, 3):
            parameters = dict(nx=2, ny=2, diffusion=1, velocity_x=1e-16, velocity_y=0)
            if dimension == 3: parameters.update(nz=2, velocity_z=0)
            recipe = case(f'convection_diffusion_{dimension}d', parameters)
            rounded = generate_case(recipe)
            a = matrix(rounded)
            self.assertTrue(all(value == a.get((j, i), 0) for (i, j), value in a.items()))
            self.assertEqual(rounded['matrix']['properties']['symmetric'], 'yes')
            parameters['velocity_x'] = 1
            resolved = generate_case(recipe)
            self.assertNotEqual(matrix(resolved)[0, 1], matrix(resolved)[1, 0])
            self.assertEqual(resolved['matrix']['properties']['symmetric'], 'no')
        # A positive resistance may have an unrepresentably small conductance.
        p = generate_case(case('grid_ac_jacobian', dict(
            buses=3, resistance=1e-308, reactance=1e308, chord_stride=2)))
        self.assertEqual(p['matrix']['properties']['symmetric'], 'yes')
        self.assertGreater(int(p['matrix']['nonzeros']), 0)

    def test_ac_admittance_scaling_and_range(self):
        for impedance in (1e154, 1e308, 1e-200):
            with self.subTest(impedance=impedance):
                p = generate_case(case('grid_ac_jacobian', dict(
                    buses=3, resistance=impedance, reactance=impedance, chord_stride=2)))
                a = matrix(p)
                # Equal r and x give g=b=1/(2*r); bus degree two doubles
                # each diagonal. Dividing by the expected value tests relative
                # error, unlike an absolute tolerance that accepts zero here.
                weight = 0.5/impedance
                self.assertEqual(len(a), 16)
                self.assertAlmostEqual(a[0, 0]/(2*weight), 1)
                self.assertAlmostEqual(a[0, 1]/(-weight), 1)
                self.assertAlmostEqual(a[0, 2]/(2*weight), 1)
                self.assertAlmostEqual(a[2, 0]/(-2*weight), 1)
                self.assertTrue(all(math.isfinite(v) and v != 0 for v in a.values()))
        with self.assertRaisesRegex(ValueError, 'admittance'):
            generate_case(case('grid_ac_jacobian', dict(
                buses=3, resistance=1e-320, reactance=1e-320, chord_stride=2)))

    def test_recipes_reproducible_bounded_and_solvable_reference(self):
        recipes = [('convection_diffusion_2d', {'nx':3,'ny':4,'diffusion':1,'velocity_x':2,'velocity_y':-1}),
                   ('helmholtz_2d', {'nx':3,'ny':4,'wavenumber':2}),
                   ('triangular_fem_poisson', {'nx':3,'ny':4}),
                   ('grid_dc_power_flow', {'buses':6,'reactance':0.2,'chord_stride':2}),
                   ('grid_ac_jacobian', {'buses':6,'reactance':0.2,'resistance':0.1,'chord_stride':2})]
        for name, params in recipes:
            with self.subTest(name=name):
                p = generate_case(case(name,params)); self.assertEqual(p, generate_case(case(name,params)))
                self.assertTrue(validate_solution(p, p['reference'], {'relative_tolerance':1e-10,'absolute_tolerance':1e-12})['accepted'])
                with self.assertRaises(ValueError): generate_case(case(name,params), max_entries=2)
                with self.assertRaises(ValueError): generate_case(case(name,{**params, 'extra':1}))

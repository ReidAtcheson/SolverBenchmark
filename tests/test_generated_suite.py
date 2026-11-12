"""Cross-family input-contract checks and independent structured-grid spectra."""
import copy
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from solver_benchmark.problems import generate_case, decode_array
from solver_benchmark.profiles import input_hash, validate_profile
from test_generated_matrices import case, matrix

ROOT = Path(__file__).resolve().parents[1]


class GeneratedSuiteTests(unittest.TestCase):
    def test_seeded_rhs_multiple_columns_and_encoding(self):
        recipe = case('laplacian_1d', {'mx':3, 'dx':1})
        recipe['rhs'] = {'count':2, 'generator':{'id':'manufactured_seeded_columns',
                         'parameters':{'offset':0, 'amplitude':1}, 'seed':'0'}}
        a = generate_case(recipe)
        # SplitMix64's first output for seed zero is 0xe220a8397b1dcdaf.
        self.assertEqual(a['reference'][0], 2 * (0xe220a8397b1dcdaf >> 11) / 2**53 - 1)
        self.assertEqual(input_hash(a), input_hash(generate_case(recipe)))
        vals = a['reference']; rhs = list(decode_array('d', a['arrays']['rhs']))
        for k in (0, 3):
            self.assertEqual(rhs[k:k+3], [2*vals[k]-vals[k+1],
                math.fsum([-vals[k],2*vals[k+1],-vals[k+2]]), 2*vals[k+2]-vals[k+1]])
        recipe['rhs']['generator']['seed'] = '1'
        b = generate_case(recipe)
        self.assertEqual(a['arrays']['values'], b['arrays']['values'])
        self.assertNotEqual(a['arrays']['rhs'], b['arrays']['rhs'])
        recipe['encoding']['kind'] = 'complex_interleaved'
        c = generate_case(recipe)
        self.assertEqual(c['matrix']['order'], 6)
        self.assertEqual(len(c['reference']), 12)
        recipe['rhs']['generator']['parameters']['amplitude'] = 0
        with self.assertRaises(ValueError): generate_case(recipe)

    def test_helmholtz_three_dimensional_spectrum(self):
        n, k = 3, 8
        a = matrix(generate_case(case('helmholtz_3d', {'nx':n,'ny':n,'nz':n,'wavenumber':k})))
        eigenvalues = []
        for modes in ((1,1,1), (3,3,3)):
            v = [math.prod(math.sin(mode*math.pi*(c+1)/(n+1)) for mode,c in zip(modes,(x,y,z)))
                 for z in range(n) for y in range(n) for x in range(n)]
            eig = sum(4*(n+1)**2 * math.sin(mode*math.pi/(2*(n+1)))**2 for mode in modes)-k*k
            eigenvalues.append(eig)
            for i in range(n**3):
                av = math.fsum(a.get((i,j),0)*v[j] for j in range(n**3))
                self.assertAlmostEqual(av, eig*v[i], places=11)
        self.assertLess(eigenvalues[0], 0)
        self.assertGreater(eigenvalues[1], 0)

    def test_transport_three_dimensional_stencil(self):
        problem = generate_case(case('convection_diffusion_3d', {'nx':2,'ny':2,'nz':2,
            'diffusion':1,'velocity_x':0,'velocity_y':0,'velocity_z':-3}))
        a = matrix(problem)
        self.assertEqual(a[0,0], 63)
        self.assertEqual(a[0,4], -18)
        self.assertEqual(a[4,0], -9)
        self.assertNotIn((3,4), a)
        self.assertEqual(problem['matrix']['properties']['symmetric'], 'no')

    def test_expanded_wire_and_dense_limits_precede_matrix_allocation(self):
        params = dict(buses=1_500_000_000, mesh_fraction=0.5, reactance=0.1,
                      impedance_spread=0.2, tap_spread=0.01)
        recipe = case('geographic_grid_dc', params)
        recipe['encoding']['kind'] = 'complex_interleaved'
        with patch('solver_benchmark.grid_problems.network') as network:
            with self.assertRaises(ValueError): generate_case(recipe, max_entries=200_000_000_000)
            network.assert_not_called()
        recipe['encoding']['kind'] = 'real'
        params['buses'] = 100
        recipe['rhs']['count'] = 1_000_000
        with patch('solver_benchmark.grid_problems.network') as network:
            with self.assertRaises(ValueError): generate_case(recipe, max_entries=10000)
            network.assert_not_called()

    def test_catalog_profile_canonical_csr_and_resource_limits(self):
        profile = json.loads((ROOT/'examples/generated-physics-profile.json').read_text())
        validate_profile(profile)
        for recipe in profile['cases']:
            with self.subTest(case=recipe['id']):
                p = generate_case(recipe)
                offsets = decode_array('q', p['arrays']['row_offsets'])
                cols = decode_array('i', p['arrays']['column_indices'])
                vals = decode_array('d', p['arrays']['values'])
                self.assertEqual(offsets[-1], int(p['matrix']['nonzeros']))
                self.assertTrue(all(math.isfinite(x) for x in vals))
                for i in range(p['matrix']['order']):
                    row = list(cols[offsets[i]:offsets[i+1]])
                    self.assertEqual(row, sorted(set(row)))
                    self.assertTrue(all(0 <= j < p['matrix']['order'] for j in row))
                # Exact structural and numerical assertions advertised to workers.
                a = matrix(p)
                props = p['matrix']['properties']
                if props['structurally_symmetric'] == 'yes':
                    self.assertEqual(set(a), {(j,i) for i,j in a})
                if props['symmetric'] == 'yes':
                    for (i,j), value in a.items():
                        self.assertAlmostEqual(value, a.get((j,i),0), delta=1e-12*max(1,abs(value)))
                with self.assertRaises(ValueError): generate_case(recipe, max_entries=1)
                wide = copy.deepcopy(recipe); wide['rhs']['count'] = 2**32-1
                with self.assertRaises(ValueError): generate_case(wide, max_entries=100000)

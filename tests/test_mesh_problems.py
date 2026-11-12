import math
import unittest
from collections import Counter
from itertools import combinations
from unittest.mock import patch

from solver_benchmark.mesh_problems import (assemble, box_mesh, build, refine,
                                            simplex_matrices, torus_mesh)


def quadratic_form(csr, vector):
    offsets, columns, values = csr[:3]
    return math.fsum(vector[i]*values[p]*vector[columns[p]] for i in range(len(vector))
                     for p in range(offsets[i],offsets[i+1]))


def assert_spd(test, csr):
    offsets, columns, values = csr[:3]
    n = len(offsets)-1
    a = [[0.0]*n for _ in range(n)]
    for i in range(n):
        for p in range(offsets[i],offsets[i+1]): a[i][columns[p]] = values[p]
    l = [[0.0]*n for _ in range(n)]
    for i in range(n):
        for j in range(i+1):
            test.assertEqual(a[i][j], a[j][i])
            value = a[i][j] - math.fsum(l[i][k]*l[j][k] for k in range(j))
            if i == j:
                test.assertGreater(value,0)
                l[i][j] = math.sqrt(value)
            else: l[i][j] = value/l[j][j]


class MeshTests(unittest.TestCase):
    def test_reference_elements(self):
        for points,volume in [([(0,0),(1,0),(0,1)],0.5),
                              ([(0,0,0),(1,0,0),(0,1,0),(0,0,1)],1/6)]:
            k,m = simplex_matrices(points)
            d = len(points)-1
            for i in range(d+1):
                self.assertAlmostEqual(sum(k[i]),0)
                for j in range(d+1):
                    expected = volume*(d if i == j == 0 else 1 if i == j else -1 if i == 0 or j == 0 else 0)
                    self.assertAlmostEqual(k[i][j],expected)
                    self.assertAlmostEqual(m[i][j],volume*(2 if i == j else 1)/((d+1)*(d+2)))
            self.assertAlmostEqual(sum(map(sum,m)),volume)

    def test_rotated_surface_triangle(self):
        a = simplex_matrices([(0,0),(1,0),(0,1)])
        b = simplex_matrices([(0,0,0),(1,0,0),(0,math.sqrt(.5),math.sqrt(.5))])
        for x,y in zip(a,b):
            for row_a,row_b in zip(x,y):
                for u,v in zip(row_a,row_b): self.assertAlmostEqual(u,v)

    def test_thin_triangle_and_degenerate_elements(self):
        # Direct area remains resolved when subtracting entries of E.T E
        # would round away the triangle's height entirely.
        for height in (1e-4,1e-8):
            points = [(0.,0.,0.),(1.,0.,0.),(.5,height,0.)]
            stiffness,mass = simplex_matrices(points)
            self.assertAlmostEqual(math.fsum(x for row in mass for x in row)/(height/2),1)
            self.assertAlmostEqual(stiffness[2][2]/(0.5/height),1)
        for points in ([(0,0),(1,0),(2,0)],
                       [(0,0,0),(2,3,5),(14,21,35)],
                       [(0,0,0),(1,0,0),(0,1,0),(1,1,0)],
                       [(0,0,0),(1,2,3),(4,5,6),(5,7,9)],
                       [(0,0,0),(2,3,5),(7,11,13),(9,14,18)],
                       [(0,0,0)]*4,
                       [(0,0),(math.inf,0),(0,1)]):
            with self.subTest(points=points),self.assertRaises(ValueError):
                simplex_matrices(points)

    def test_refinement_conformity_volume_and_affine_energy(self):
        for dimension in (2,3):
            vertices,elements = box_mesh(2,dimension,.14,123)
            for level in (0,1,2):
                if level: vertices,elements = refine(vertices,elements)
                self.assertEqual(len(elements), math.factorial(dimension)*2**dimension*(2**dimension)**level)
                facets = Counter(tuple(sorted(f)) for e in elements for f in combinations(e,dimension))
                self.assertTrue(all(count in (1,2) for count in facets.values()))
                # Every unpaired face lies on the box boundary: no interior cracks.
                for face,count in facets.items():
                    if count == 1:
                        self.assertTrue(any(all(vertices[i][axis] == side for i in face)
                                            for axis in range(dimension) for side in (0,1)))
                k = assemble(vertices,elements,1,0,False)
                m = assemble(vertices,elements,0,1,False)
                self.assertAlmostEqual(sum(m[2]),1,places=11)
                self.assertAlmostEqual(quadratic_form(k,[p[0] for p in vertices]),1,places=10)

    def test_torus_topology_and_area_convergence(self):
        major,minor = 2,0.5
        vertices,elements,midpoint = torus_mesh(8,6,major,minor)
        errors = []
        for level in (0,1,2):
            if level: vertices,elements = refine(vertices,elements,midpoint)
            edges = Counter(tuple(sorted(e)) for t in elements for e in combinations(t,2))
            self.assertTrue(all(count == 2 for count in edges.values()))
            self.assertEqual(len(vertices)-len(edges)+len(elements),0)  # genus one
            for x,y,z in vertices:
                self.assertAlmostEqual((math.hypot(x,y)-major)**2+z*z,minor**2)
            k = assemble(vertices,elements,1,0,False)
            offsets,_,values = k
            for i in range(len(vertices)):
                self.assertAlmostEqual(math.fsum(values[offsets[i]:offsets[i+1]]),0,places=11)
            m = assemble(vertices,elements,0,1,False)
            errors.append(abs(math.fsum(m[2])-4*math.pi**2*major*minor))
        self.assertLess(errors[1],errors[0]/3)
        self.assertLess(errors[2],errors[1]/3)

    def test_spd_seed_and_refinement_recipe(self):
        for generator in ('fem_diffusion_square','fem_diffusion_cube'):
            p = dict(cells=3,refinement=0,jitter=.13,diffusion=2,reaction=.1)
            a = build(generator,p,100000,seed='8')
            assert_spd(self,a)
            self.assertEqual(a,build(generator,p,100000,seed='8'))
            self.assertNotEqual(a[2],build(generator,p,100000,seed='9')[2])
            b = build(generator,{**p,'refinement':1},100000,seed='8')
            self.assertGreater(len(b[0]),len(a[0]))
        p = dict(major_segments=4,minor_segments=4,major_radius=2,minor_radius=.5,
                 refinement=0,diffusion=1,reaction=1)
        assert_spd(self,build('surface_diffusion_torus',p,100000))

    def test_validation_and_preflight(self):
        p = dict(cells=2,refinement=0,jitter=.1,diffusion=1,reaction=0)
        for patch in ({'cells':True},{'refinement':-1},{'refinement':10**100},
                      {'cells':10**100},{'diffusion':float('inf')},{'jitter':.9},
                      {'reaction':-1},{'extra':0}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                build('fem_diffusion_cube',{**p,**patch},10000)
        with self.assertRaises(ValueError): build('fem_diffusion_cube',p,10)

    def test_wire_limit_rejected_before_mesh_allocation(self):
        p = dict(cells=1000,refinement=0,jitter=.1,diffusion=1,reaction=0)
        with patch('solver_benchmark.mesh_problems.box_mesh', side_effect=AssertionError('allocated')):
            with self.assertRaisesRegex(ValueError,'wire index'):
                build('fem_diffusion_cube',p,10**20)

    def test_torus_reaction_must_survive_roundoff(self):
        p = dict(major_segments=4,minor_segments=4,major_radius=2,minor_radius=.5,
                 refinement=0,diffusion=1,reaction=1)
        for reaction in (1e-320,1e-300,1e-20):
            with self.subTest(reaction=reaction), self.assertRaisesRegex(ValueError,'reaction'):
                build('surface_diffusion_torus',{**p,'reaction':reaction},100000)

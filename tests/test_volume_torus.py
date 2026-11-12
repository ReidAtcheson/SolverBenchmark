import math
import unittest
from collections import Counter
from itertools import combinations
from unittest.mock import patch

from solver_benchmark.mesh_problems import assemble, refine, simplex_matrices
from solver_benchmark.volume_torus import (assemble_volume, boundary_vertices,
                                         build, torus_volume_mesh)
from test_mesh_problems import assert_spd, quadratic_form


def connected(nodes, edges):
    neighbors = {node:set() for node in nodes}
    for a,b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    visited = set()
    todo = [next(iter(nodes))]
    while todo:
        node = todo.pop()
        if node not in visited:
            visited.add(node)
            todo.extend(neighbors[node]-visited)
    return visited == set(nodes)


class VolumeTorusTests(unittest.TestCase):
    def test_filled_cross_section_and_no_seam_cracks(self):
        major,minor,radial = 6,5,2
        vertices,elements = torus_volume_mesh(major,minor,radial,2,.5)
        self.assertEqual(len(vertices),major*(1+minor*radial))
        self.assertEqual(len(elements),3*major*minor*(2*radial-1))
        exterior = boundary_vertices(elements)
        # Only outer ring vertices are boundary, including at the periodic seam.
        cross = 1+minor*radial
        expected = {i*cross+1+(radial-1)*minor+j for i in range(major) for j in range(minor)}
        self.assertEqual(exterior,expected)
        self.assertTrue(all(i*cross not in exterior for i in range(major)))
        # Each cross section really contains the center of its filled disk.
        for i in range(major):
            x,y,z = vertices[i*cross]
            self.assertAlmostEqual(math.hypot(x,y),2)
            self.assertEqual(z,0)
        for level in range(2):
            if level:
                vertices,elements = refine(vertices,elements)
            faces = Counter(tuple(sorted(face)) for e in elements for face in combinations(e,3))
            edges = {tuple(sorted(edge)) for e in elements for edge in combinations(e,2)}
            self.assertTrue(all(n in (1,2) for n in faces.values()))
            self.assertEqual(len(vertices)-len(edges)+len(faces)-len(elements),0)
            self.assertTrue(connected(set(range(len(vertices))),edges))
            boundary = [face for face,count in faces.items() if count == 1]
            self.assertEqual(len(boundary),2*major*minor*4**level)
            boundary_edges = Counter(tuple(sorted(edge)) for face in boundary for edge in combinations(face,2))
            boundary_nodes = {node for face in boundary for node in face}
            self.assertTrue(all(n == 2 for n in boundary_edges.values()))
            self.assertEqual(len(boundary_nodes)-len(boundary_edges)+len(boundary),0)
            self.assertTrue(connected(boundary_nodes,boundary_edges))
            # Every tetrahedron has strictly positive measure, including near the centerline.
            for e in elements:
                _,mass = simplex_matrices([vertices[i] for i in e])
                self.assertGreater(math.fsum(value for row in mass for value in row),0)

    def test_exact_polyhedral_volume_affine_energy_and_refinement(self):
        n,m = 6,5
        vertices,elements = torus_volume_mesh(n,m,2,2,.5)
        volume = n*math.sin(2*math.pi/n)*2 * m*.5**2*math.sin(2*math.pi/m)/2
        for level in range(2):
            if level:
                vertices,elements = refine(vertices,elements)
            mass = assemble(vertices,elements,0,1,False)
            stiffness = assemble(vertices,elements,1,0,False)
            self.assertAlmostEqual(math.fsum(mass[2]),volume,places=11)
            for axis in range(3):
                # For u=x, y, or z, integral |grad(u)|² is the domain volume.
                energy = quadratic_form(stiffness,[p[axis] for p in vertices])
                self.assertAlmostEqual(energy,volume,places=10)

    def test_volume_converges_with_angular_resolution(self):
        exact = 2*math.pi**2*2*.5**2
        errors = []
        for segments in (4,8,16):
            vertices,elements = torus_volume_mesh(segments,segments,1,2,.5)
            mass = assemble(vertices,elements,0,1,False)
            errors.append(abs(math.fsum(mass[2])-exact))
        self.assertLess(errors[1],errors[0]/3)
        self.assertLess(errors[2],errors[1]/3)

    def test_dirichlet_spd_radial_resolution_and_refinement(self):
        p = dict(major_segments=4,minor_segments=4,radial_layers=1,major_radius=2,
                 minor_radius=.5,refinement=0,diffusion=1,reaction=0)
        a = build('volume_diffusion_torus',p,100000)
        self.assertEqual(len(a[0])-1,4)
        self.assertEqual(a,build('volume_diffusion_torus',p,100000,seed='999'))
        for changes in ({},{'radial_layers':2},{'refinement':1},{'reaction':.4}):
            with self.subTest(changes=changes):
                b = build('volume_diffusion_torus',{**p,**changes},100000)
                assert_spd(self,b)
                if 'radial_layers' in changes or 'refinement' in changes:
                    self.assertGreater(len(b[0]),len(a[0]))
        vertices,elements = torus_volume_mesh(4,4,2,2,.5)
        self.assertEqual(len(assemble_volume(vertices,elements,1,0)[0])-1,
                         len(vertices)-len(boundary_vertices(elements)))

    def test_parameters_and_preallocation_limits(self):
        p = dict(major_segments=4,minor_segments=4,radial_layers=1,major_radius=2,
                 minor_radius=.5,refinement=0,diffusion=1,reaction=0)
        bad = ({'major_segments':3},{'minor_segments':True},{'radial_layers':0},
               {'radial_layers':1.5},{'refinement':-1},{'refinement':10**100},
               {'major_segments':10**100},{'major_radius':.5},{'minor_radius':0},
               {'minor_radius':math.inf},{'diffusion':0},{'reaction':-1},{'extra':1})
        for changes in bad:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                build('volume_diffusion_torus',{**p,**changes},100000)
        with patch('solver_benchmark.volume_torus.torus_volume_mesh',side_effect=AssertionError('allocated')):
            with self.assertRaisesRegex(ValueError,'max_entries'):
                build('volume_diffusion_torus',p,100)
            with self.assertRaisesRegex(ValueError,'wire index'):
                build('volume_diffusion_torus',{**p,'major_segments':10**8},10**20)
            with self.assertRaisesRegex(ValueError,'dense order'):
                build('volume_diffusion_torus',p,100000,max_order=1)

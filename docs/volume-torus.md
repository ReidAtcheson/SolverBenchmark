# Solid-torus volume generator

`volume_diffusion_torus` generates a **three-dimensional filled solid torus**
with tetrahedral P1 finite elements. It is separate from
`surface_diffusion_torus`, which remains a two-dimensional closed surface mesh.
Both are generated locally from scalar parameters without external data or
meshing dependencies.

| Parameter | Range | Meaning |
| --- | --- | --- |
| `major_segments` | Integer >= 4 | Angular sectors around the central hole |
| `minor_segments` | Integer >= 4 | Angular sectors in each circular cross-section |
| `radial_layers` | Integer >= 1 | Concentric layers filling each disk cross-section |
| `refinement` | Integer >= 0 | Uniform tetrahedral midpoint-refinement levels |
| `major_radius` | Finite, greater than `minor_radius` | Distance from symmetry axis to tube centerline |
| `minor_radius` | Finite, positive | Tube radius |
| `diffusion` | Finite, positive | Scalar diffusion coefficient |
| `reaction` | Finite, nonnegative | Reaction coefficient; zero is allowed |

All parameters are required. The seed is validated by the profile contract but
has no numerical effect: this is a deterministic swept mesh, not a random or
Delaunay tetrahedralization. Increase `major_segments`, `minor_segments`, and
`radial_layers` to resolve the geometry and tube interior; increase `refinement`
to subdivide a fixed polyhedral domain. Each refinement level multiplies the
tetrahedron count by eight.

Example profile matrix recipe:

```json
{"id":"volume_diffusion_torus","parameters":{"major_segments":8,"minor_segments":8,"radial_layers":2,"refinement":1,"major_radius":2,"minor_radius":0.5,"diffusion":1,"reaction":0},"seed":"0"}
```

## Mesh and boundary

Each cross-section is a filled polygonal disk: one center node, concentric rings,
a triangular fan touching the center, and two triangles per annular quadrilateral.
The center is not represented by a collapsed angular ring, so no tetrahedra
collapse on the tube centerline. Sweep this disk around the major angular
coordinate using

```
((major_radius + s)*cos(theta), (major_radius + s)*sin(theta), z),
```

where `(s,z)` are disk coordinates. The final major-angle sector connects back
to the first cross-section using the same nodes; there is no duplicated seam.

Each swept triangle is split into three tetrahedra. For cross-section vertex IDs
`a < b < c`, with corresponding next-section IDs `A,B,C`, the tetrahedra are
`(a,b,c,C)`, `(a,b,B,C)`, and `(a,A,B,C)`. The consistent cross-section ordering
makes neighboring prism faces choose the same diagonal, including across the
periodic seam. With at least four major sectors and `major_radius > minor_radius`,
all three tetrahedra have positive geometric measure. This construction gives a
structured swept volume mesh with genuine radial interior degrees of freedom;
it does not claim irregular or unstructured connectivity.

The exterior consists of tetrahedral faces with one incident element. Its nodes
are eliminated to impose homogeneous Dirichlet data on the entire tube wall.
All interior nodes, including the centerline, remain free. Thus pure diffusion
is already SPD; unlike the closed surface case, a positive reaction is not
required to remove a constant nullspace. The weak form is
`diffusion * integral grad(u).grad(v) + reaction * integral u*v`, assembled with
the same exact P1 tetrahedral stiffness and consistent mass formulas as the
[other simplex generators](mesh-generators.md).

Refinement uses shared **straight spatial edge midpoints**, with eight children
per tetrahedron. It preserves the initial polyhedral domain exactly. It does
not project boundary nodes onto the analytic torus, change the domain, or
silently improve curved-boundary accuracy. Angular segment counts control that
geometry approximation. On coarse thin-tube meshes, planar elements need not
lie entirely inside the analytic curved torus; they approximate a polygonal
solid torus. Their interiors remain disjoint angular sectors and the central
hole remains open. Increase angular resolution when curved geometry accuracy
matters.

Initial ordering is major-angle cross-sections, then center and concentric rings
inside each section; refinement appends shared-edge midpoint nodes. Dirichlet
elimination preserves the relative order of the remaining nodes. Before
refinement there are
`major_segments * (1 + radial_layers * minor_segments)` vertices and
`3 * major_segments * minor_segments * (2*radial_layers - 1)` tetrahedra.

## Verification and limits

The generated polyhedral volume is exactly

```
major_segments * sin(2*pi/major_segments) * major_radius
  * minor_segments * minor_radius^2 * sin(2*pi/minor_segments) / 2.
```

To see this, each prism contributes its cross-section triangle area times
`sin(delta_theta) * (major_radius + triangle_centroid_s)`. The symmetric disk
has zero integral of `s`. As both angular segment counts grow, this converges
to the analytic solid-torus volume `2*pi^2*major_radius*minor_radius^2`.
Radial subdivision and straight midpoint refinement preserve the polyhedral
volume rather than changing that geometry error.

Tests check this exact volume, convergence under angular resolution, and the
energy of the three affine coordinate fields. They also check that all internal
faces have two incident tetrahedra, every tetrahedron has positive volume, the
volume complex has Euler characteristic zero, and its connected exterior is a
closed genus-one surface. The centerline and radial interior unknowns remain
free after boundary elimination. Small matrices pass independent dense Cholesky
checks with zero and positive reaction.

Thin-tube regressions use major radius one, minor radii `0.001`, `0.0003`, and
`0.0001`, eight segments in each angular direction, and two radial layers. They
exercise the public generator and compare the complete assembled stiffness
matrix against independent 80-digit solves of each element's affine interpolation
matrix. The previous Gram-matrix calculation lost accuracy on these finite,
nondegenerate elements; the direct, scaled and pivoted Jacobian calculation
avoids that squaring of the geometric condition number. Additional element tests
cover rotations, every vertex ordering, and large/small uniform spatial scales.
Numerically unresolved geometry is rejected using the
[simplex rank safeguard](mesh-generators.md), rather than returned as a matrix.

Before allocation, generation requires `16 * final_tetrahedron_count <=
max_entries` and a conservative vertex bound `4 * final_tetrahedron_count` to
fit the effective order limit, including complex expansion and dense RHS width.
The parent additionally verifies final CSR and dense sizes. These conservative
assembly bounds may reject a recipe whose merged CSR would fit. Degenerate or
nonfinite geometry, nonfinite matrix arithmetic, and underflowed diagonal
stiffness are rejected explicitly.

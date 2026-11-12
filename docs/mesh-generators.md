# Refined simplex and surface matrices

These dependency-free generators assemble scalar diffusion–reaction operators
from continuous, piecewise-linear (P1) finite elements. They use conforming
triangles or tetrahedra, with coefficients computed from element geometry.
No stored coordinates, connectivity, meshes, or matrices are required.

| Generator | Geometry and boundary | Required parameters |
| --- | --- | --- |
| `fem_diffusion_square` | Unit square, triangular mesh; all boundary nodes eliminated for homogeneous Dirichlet data | `cells`, `refinement`, `jitter`, `diffusion`, `reaction` |
| `fem_diffusion_cube` | Unit cube, tetrahedral mesh; all boundary nodes eliminated for homogeneous Dirichlet data | `cells`, `refinement`, `jitter`, `diffusion`, `reaction` |
| `surface_diffusion_torus` | Closed torus **surface** in 3D, triangular mesh; positive reaction removes the constant nullspace | `major_segments`, `minor_segments`, `major_radius`, `minor_radius`, `refinement`, `diffusion`, `reaction` |

`cells` is an integer at least two and counts coarse intervals per coordinate.
`refinement` is a nonnegative integer. `jitter` is in `[0, 0.15]` and specifies
the maximum coarse interior vertex displacement in each coordinate, as a
fraction of the coarse cell width. The recipe's uint64 seed controls independent
coordinate displacements using a fixed SplitMix64 integer mapping; boundary
coordinates remain fixed. A nonzero jitter gives irregular element geometry and
non-stencil coefficients. With zero jitter some stiffness couplings may vanish.

The torus has `major_segments` and `minor_segments` at least four and finite
radii satisfying `major_radius > minor_radius > 0`. The seed has no effect on
this deterministic surface geometry. This is a periodic structured 2D
triangulation. For a filled 3D domain, use the separate
[solid torus volume generator](volume-torus.md).
`diffusion` is finite and positive. `reaction` is finite and nonnegative for the
box domains and strictly positive on the torus. All effective parameters must be
present, including zero reaction for pure Dirichlet Poisson cases.

Examples of matrix-generator recipes (to place in the profile's `generator`):

```json
{"id":"fem_diffusion_cube","parameters":{"cells":3,"refinement":1,"jitter":0.12,"diffusion":1,"reaction":0},"seed":"42"}
```

```json
{"id":"surface_diffusion_torus","parameters":{"major_segments":8,"minor_segments":6,"major_radius":2,"minor_radius":0.5,"refinement":1,"diffusion":1,"reaction":0.1},"seed":"0"}
```

## Discretization and refinement

On the boxes the weak form is
`a(u,v) = diffusion ∫ grad(u)·grad(v) + reaction ∫ u v`.
This is the standard conforming Dirichlet diffusion form described in the
[DOLFINx Poisson demonstration](https://docs.fenicsproject.org/dolfinx/main/python/demos/demo_poisson.html),
with an added reaction term. On the torus the same form uses tangential gradients
on each flat triangle; this follows the piecewise-planar surface finite-element
construction reviewed by [Dziuk and Elliott, *Finite element methods for surface PDEs*](https://wrap.warwick.ac.uk/id/eprint/53966/1/WRAP_Elliott_DziEll13a.pdf).
These references describe the underlying discretizations; no implementation
from them is copied and neither library is a runtime dependency.

The coarse box mesh splits each Cartesian cell into `dimension!` simplices via
coordinate permutations (a Freudenthal triangulation), then perturbs interior
vertices. This is **not** a Delaunay mesh. Each triangle is refined into four
triangles using shared edge midpoints. Each tetrahedron is refined into eight:
four corner tetrahedra and four from splitting the central octahedron along the
`ab`–`cd` midpoint diagonal in the parent's stored vertex ordering. All faces
use the same midpoint subdivision, so neighboring elements remain conforming.
This is uniform refinement, not error-driven adaptive refinement; the specific
central split does not assert optimal shape quality at arbitrarily many levels.

The torus is initially triangulated from a periodic angular grid without duplicate
seam nodes. Refinement bisects edges in periodic angular coordinates and places
new vertices on the analytic torus. This avoids projecting a coarse spatial
chord onto the wrong side of a thin torus. Element matrices still use the actual
flat triangles joining these vertices, not the curved analytic surface.

For an element with edge matrix `E`, the Gram matrix `G = EᵀE` gives the dot
products of basis gradients through `G⁻¹`. The element measure is
`sqrt(det(G))/dimension!`. The consistent mass matrix is
`measure × (1 + δij) / ((dimension+1)(dimension+2))`.
Both terms are assembled before removing constrained box nodes. This yields
symmetric positive-definite systems for the accepted coefficients. The code
rejects degenerate or nonfinite geometry and arithmetic. On the torus it also
requires each assembled reaction row sum and the resulting matrix row sum to
exceed 64 binary64 ulps of the accumulated absolute stiffness contributions.
A positive reaction that underflows or disappears beneath stiffness roundoff
therefore fails explicitly instead of being mislabeled as a usable SPD matrix.

Element counts multiply by four per triangle refinement and eight per tetrahedral
refinement. Before constructing a mesh, generation requires
`element_count × (dimension+1)² <= max_entries`; this conservative contribution
bound controls assembly work and storage as well as final CSR entries. It can
reject a recipe whose merged CSR would fit. Before allocation, a conservative vertex bound `(dimension+1) × element_count`
must fit the effective order limit, including complex expansion and dense RHS
storage. Vertex ordering is the initial lexicographic box
or angular grid order, followed by edge midpoint insertion order, with box
boundary nodes removed.

## Validation

Tests compare reference triangle and tetrahedron matrices with analytic values,
check invariance under rotating a surface triangle, verify box volume and affine
function energy under refinement, and ensure each unpaired box face is on the
boundary. Torus tests check every edge has two incident triangles, Euler
characteristic zero, exact analytic-surface vertex placement, vanishing constant
stiffness action, and second-order area convergence. Small assembled matrices
also pass an independent dense Cholesky factorization. Seed repeatability, preallocation rejection for local and wire limits, and
rejection of numerically unresolved torus reaction terms are tested.

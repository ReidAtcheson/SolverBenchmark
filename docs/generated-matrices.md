# Generated physics and electric-grid matrices

The [example profile](../examples/generated-physics-profile.json) contains 24
cases from 17 generator families. Together with the original 1D/2D/3D Laplacians,
these cover scalar transport and waves, refined volume and surface finite
elements, incompressible flow, porous flow, elasticity, and electrical networks.
All inputs are generated locally from explicit parameters and seeds. No mesh
files, object storage, matrix downloads, or additional Python dependencies are
required.

The [light profile](../examples/light-profile.json) includes these 24 cases and
the three Laplacians, each with 9,261–10,648 equations and one manufactured
right-hand side. It runs one warmup and five measured rounds in a single
baseline timing pass for each of the Eigen and SuperLU workers. Each
case/solver execution has a 300-second deadline covering all six rounds. A
deployment must provide both workers, as in the comparison example. Validate
or run it with:

```sh
solver-benchmark validate examples/light-profile.json
solver-benchmark run examples/light-profile.json \
  --deployment examples/comparison-deployment.json --output results/light
```

The comparison deployment is a template and its worker and library paths must
be adjusted for the host. Generation happens before solver timing; the deadline
is a limit, not a prediction of run time.

```sh
solver-benchmark validate examples/generated-physics-profile.json
solver-benchmark run examples/generated-physics-profile.json \
  --deployment examples/deployment.json --output results/generated-physics
```

The profile includes coarse/refined square, cube, and torus cases; radial and
meshed electrical networks; high permeability contrast; and a large Lamé ratio.
Every case uses three independently varying, seeded manufactured solution
columns with `b = A x`. The parent validates residuals against the delivered
matrix. These are linear solver benchmarks: manufactured RHSs are not physical
loads or a substitute for discretization verification.

## Coverage

| Generator | Physical model / discretization | Matrix class |
| --- | --- | --- |
| `convection_diffusion_2d`, `convection_diffusion_3d` | Centered diffusion plus first-order upwind transport on a unit box | Generally nonsymmetric |
| `helmholtz_2d`, `helmholtz_3d` | `-Δ - k²`, unit box, homogeneous Dirichlet | Symmetric; indefinite for the example wavenumbers |
| `triangular_fem_poisson` | P1 Poisson on a perturbed triangular square mesh | SPD |
| `fem_diffusion_square` | Seeded perturbed triangular mesh, midpoint refinement, diffusion/reaction | SPD |
| `fem_diffusion_cube` | Seeded perturbed tetrahedral mesh, midpoint refinement, diffusion/reaction | SPD |
| `surface_diffusion_torus` | Intrinsic surface diffusion plus positive mass term on a closed torus | SPD |
| `volume_diffusion_torus` | P1 volume diffusion/reaction on a tetrahedral solid torus with Dirichlet exterior | SPD |
| `stokes_mac_2d` | Staggered velocity/pressure, no-slip boundary, pressure gauge removed | Symmetric indefinite |
| `darcy_mixed_2d` | Mixed face-flux/cell-pressure system, heterogeneous permeability | Symmetric indefinite |
| `elasticity_2d` | P1 plane-strain displacement FEM with clamped boundary | SPD, including a stiff nearly incompressible case |
| `grid_dc_power_flow` | Uniform path/chord network, grounded DC susceptance | SPD |
| `grid_ac_jacobian` | Uniform path/chord network, flat-start all-PQ polar Jacobian | Generally nonsymmetric |
| `geographic_grid_dc` | Seeded local geographic tree plus loops, heterogeneous lines and taps | SPD |
| `geographic_grid_ac` | Non-flat polar Newton Jacobian, PV/PQ buses, taps, phase shifts, charging | Generally nonsymmetric |
| `grid_dc_opf_kkt` | Equality-constrained quadratic DC dispatch optimality system | Symmetric indefinite |

Full equations, node/unknown ordering, boundary conditions, exact parameters,
resource bounds, and sources are in:

- [Refined mesh generators](mesh-generators.md)
- [Solid torus volume generator](volume-torus.md)
- [Mixed physics and elasticity generators](mixed-generators.md)
- [Geographic electric-grid generators](grid-generators.md)

## Structured and initial generators

All parameters below are required. `nx`, `ny`, and `nz` are positive integer
interior-node counts. Physical coefficients are positive finite binary64;
velocities are signed finite numbers. `convection_diffusion_*` uses grid spacing
`1/(n+1)` along each axis and x-fastest ordering. Its off-diagonal neighbor weights
are `-diffusion/h² - max(±velocity,0)/h`; the diagonal includes the eliminated
Dirichlet boundary contributions. Helmholtz uses the same unit-box centered
Laplacian minus `wavenumber² I`.

| Generator | Parameters |
| --- | --- |
| `convection_diffusion_2d` | `nx`, `ny`, `diffusion`, `velocity_x`, `velocity_y` |
| `convection_diffusion_3d` | `nx`, `ny`, `nz`, `diffusion`, `velocity_x`, `velocity_y`, `velocity_z` |
| `helmholtz_2d` | `nx`, `ny`, `wavenumber` |
| `helmholtz_3d` | `nx`, `ny`, `nz`, `wavenumber` |
| `triangular_fem_poisson` | `nx`, `ny` |
| `grid_dc_power_flow` | `buses`, `reactance`, `chord_stride` |
| `grid_ac_jacobian` | `buses`, `resistance`, `reactance`, `chord_stride` |

The initial triangular generator uses alternating cell diagonals, fixed interior
coordinate perturbations, exact P1 stiffness, and eliminated Dirichlet nodes.
Use `fem_diffusion_square` for seeded meshes and actual refinement.

The initial grid models use a path `0..buses-1` plus edges `(i,i+chord_stride)`,
where `buses >= 3` and `2 <= chord_stride < buses`. Bus 0 is slack. DC weights
are `1/reactance`. AC uses uniform positive resistance and reactance, no shunts
or transformers, flat voltage, and all non-slack buses PQ. In angle/magnitude
ordering its Jacobian is `[B G; -G B]`, where the two grounded Laplacians have
weights `x/(r²+x²)` and `r/(r²+x²)`. These initial generators ignore the seed.
Use the geographic generators for heterogeneous and non-flat cases.
[Numerical corner cases](numerical-corner-cases.md) documents stable admittance
arithmetic and symmetry assertions after floating-point assembly.

## Scaling, reproducibility, and interpretation

Increase mesh refinement to grow a fixed coarse geometry; increase grid cell
counts or bus counts to grow other problems. Change viscosity, advection speed,
wavenumber, permeability contrast, Lamé ratio, or electrical parameters to vary
numerical difficulty. P1 displacement elasticity can lock at large Lamé ratios;
this is a conditioning stress case, not a claim of robust incompressible accuracy.
Helmholtz can be singular at discrete resonances; its metadata therefore leaves
positive definiteness unknown. Indefinite saddle-point cases explicitly report
`positive_definite: no`.

`max_entries` bounds CSR entries, dense elements, and generator-specific
conservative assembly estimates before large allocation. Mesh contribution
bounds can reject a recipe even if the final CSR would fit. This prevents a
small matrix hiding very expensive intermediate mesh assembly. It is a local
execution limit, never a silent change to the recipe. The generators are intended
for moderate local benchmarks; Python assembly itself is outside worker timings.

The matrix seed and RHS seed are independent. Seeded RHSs use the specified
SplitMix64 stream in the [profile contract](benchmark-profile.md); mesh and grid
seed behavior is documented in their own catalogs. Input hashes identify actual
wire bytes, and manifests hash every generator module. Floating-point geometry
may differ in its last bits across platforms. `complex_interleaved` duplicates
these real operators into real/imaginary components; none of these recipes
introduces nonzero imaginary matrix coefficients.

## Research basis and limits

[MFEM's example catalog](https://mfem.org/examples/) demonstrates why elasticity,
Darcy, and scalar diffusion deserve separate solver tests: their unknown spaces
and block structures differ. Our discretizations are compact implementations
of the equations documented here, rather than wrappers around MFEM. The mixed
and mesh notes link their particular discretization references and independent
checks.

[MATPOWER Technical Note 2](https://matpower.org/docs/TN2-OPF-Derivatives.pdf)
provides the AC power-injection derivative framework; the grid tests check the
result against derivatives of independently assembled branch currents. The
[SynGrid manual](https://matpower.org/docs/SynGrid-manual-1.0.pdf) describes a more
ambitious statistically informed grid-generation process. Our geographic tree
and loop model supplies varied sparse topology and standard electrical equations,
but does not claim those statistical or operational realism guarantees.

The mesh cases use conforming simplicial partitions, with seeded perturbations
available on square/cube meshes; they do not use Delaunay triangulation. The cube and solid torus are 3D volumes. The separate torus surface
generator remains a 2D closed surface embedded in 3D. The surface uses a
structured periodic angular triangulation; the solid torus uses a swept disk
triangulation filled with tetrahedra. Neither is an unstructured Delaunay mesh. The grid Jacobians are evaluated at explicitly generated voltage
states, not at a solved realistic load-flow scenario. Dispatch currently includes
quadratic costs and balance equalities, without line/generation inequalities.
Maxwell edge elements, nonlinear mechanics, and complete AC OPF
remain distinct future additions rather than implied coverage.

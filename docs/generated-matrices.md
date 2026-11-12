# Generated physics matrices

`examples/generated-physics-profile.json` is a runnable profile using these
self-contained generators. Recipes specify every parameter, use no files or
object storage, and are deterministic. Seeds are currently validated but not
used. Each matrix uses a manufactured constant solution and RHS `b = A x`;
this tests linear algebra, not the fidelity of a physical simulation. All
parameters are required; sizes and physical coefficients must be positive
except velocities, which are finite signed numbers.

| Generator | Parameters | Model and classification |
| --- | --- | --- |
| `convection_diffusion_2d` | `nx`, `ny`, `diffusion`, `velocity_x`, `velocity_y` | Unit square, zero Dirichlet boundary, five-point diffusion and first-order upwind advection. Generally nonsymmetric. |
| `helmholtz_2d` | `nx`, `ny`, `wavenumber` | Unit square, zero Dirichlet boundary, `-Δ - k²`. Symmetric and potentially indefinite or singular, depending on the chosen wavenumber and mesh. |
| `triangular_fem_poisson` | `nx`, `ny` | Linear triangular finite elements on a deterministically perturbed square mesh with alternating diagonals; homogeneous Dirichlet boundary. Symmetric positive definite. Sizes count interior nodes. |
| `grid_dc_power_flow` | `buses`, `reactance`, `chord_stride` | Reduced DC power-flow susceptance matrix with bus 0 as slack, unit taps, and uniform positive line reactance. Symmetric positive definite. |
| `grid_ac_jacobian` | `buses`, `resistance`, `reactance`, `chord_stride` | Polar AC power-injection Jacobian at flat voltage, bus 0 slack, all remaining buses PQ, no shunts or transformers. Generally nonsymmetric. |

Both grid models use a connected path on buses `0..buses-1` plus edges
`(i, i + chord_stride)`, with `2 <= chord_stride < buses`. They are synthetic
network models, not a reconstruction of a real grid or statistically realistic
synthetic network. The AC operating point is a flat-start linearization, not a
solved power-flow case; no load, generation, or voltage-limit data are modeled.
Its block form is `[B G; -G B]`, with grounded graph Laplacians weighted by
`x/(r²+x²)` and `r/(r²+x²)`, respectively. This is a special case of the
standard AC power-balance Jacobian. DC uses branch weights `1/x`.

`complex_interleaved` expands these real matrices into independent real and
imaginary components; it does not introduce complex coefficients. Properties
are conservative: Helmholtz definiteness is unknown, and nonsymmetric operators
are not classified as positive definite. For comparability, input hashes include
actual matrix bytes, and the version manifest identifies generator source.

## Research and scope

The choices cover distinct numerical mechanisms: transport, wave propagation,
unstructured finite-element connectivity, and electrical networks. The
[MATPOWER DC formulation](https://matpower.org/docs/ref/matpower7.1/lib/makeBdc.html)
defines the susceptance matrix and its `1/x` branch weights; its
[AC power-flow documentation](https://matpower.app/manual/matpower/ACPowerFlow.html)
describes the reduced Newton Jacobian and bus types. The
[SynGrid manual](https://matpower.org/docs/SynGrid-manual-1.0.pdf) describes
more realistic synthetic grid construction, which this small topology generator
does not attempt. The [FEniCS Stokes example](https://docs.fenicsproject.org/dolfinx/v0.7.2/python/demos/demo_stokes.html)
illustrates another valuable class, mixed saddle-point systems, which is not
yet included. Future extensions could add stable Stokes or elasticity elements,
three-dimensional and curved meshes, realistic synthetic grids, transformer and
PV bus models, and non-flat AC operating points.

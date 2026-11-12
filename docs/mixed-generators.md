# Incompressible flow, porous flow, and elasticity

These generators need only the listed scalar parameters. They assemble real CSR
matrices using the Python standard library, with no input meshes, downloaded
matrices, or solver library dependencies. All parameters are mandatory. The
recipe seed is accepted but unused: there is no randomness in these models.

| Generator | Required parameters | Unknowns / matrix class |
| --- | --- | --- |
| `stokes_mac_2d` | integer `nx, ny >= 2`; finite `viscosity > 0` | Face velocities and cell pressures; symmetric indefinite |
| `darcy_mixed_2d` | integer `nx, ny >= 2`; finite `permeability, contrast > 0` | Face fluxes and cell pressures; symmetric indefinite |
| `elasticity_2d` | integer `nx, ny >= 1`; finite `lame_lambda >= 0`, `lame_mu > 0` | Two displacement components per interior node; symmetric positive definite |

Every domain is the unit square. `nx, ny` count **cells** for the mixed models
and **interior nodes** for elasticity. Refinement is obtained by increasing these
counts. Material coefficients whose generated stiffnesses or inverse
permeabilities overflow or vanish in binary64 are rejected. Sparse entry and
order bounds are checked before allocating matrices.

## Stokes: a stable velocity/pressure discretization

`stokes_mac_2d` discretizes

```
-viscosity * Delta(u) + grad(p) = f,
                         -div(u) = g.
```

The MAC staggered grid stores the horizontal component at interior vertical
faces, the vertical component at interior horizontal faces, and pressure at cell
centers. Normal velocities are zero on the boundary and are eliminated.
Tangential no-slip conditions use odd reflection of ghost values across the
wall. For example, the first horizontal velocity layer has a y-direction
diffusion diagonal `3*viscosity/hy²` and an inward neighbor `-viscosity/hy²`.
This avoids the pressure checkerboard modes of collocated central differences.
The locations, difference operators, and reflection treatment follow the
[MAC discretization notes by Long Chen](https://www.math.uci.edu/~chenlong/226/MACStokes.pdf).

The resulting matrix is `[A B^T; B 0]`, where `A` is the positive-definite
vector Laplacian and `B` is negative divergence. Pressure in the lower-left
cell is fixed to zero; its redundant continuity row is removed together with
that pressure column. This is a gauge choice, not a pressure penalty. No
artificial diagonal regularization is added. Since the cell adjacency graph
is connected, the reduced divergence has full row rank. The matrix has exactly
as many positive eigenvalues as free velocity DOFs and as many negative
eigenvalues as free pressure DOFs.

Ordering is all horizontal faces (x fastest), then all vertical faces
(x fastest), then all cell pressures except cell 0 (x fastest). The order is
`(nx-1)*ny + nx*(ny-1) + nx*ny - 1`. A useful starter recipe is
`{"nx":8,"ny":8,"viscosity":0.01}`.

The benchmark's manufactured solution is not necessarily incompressible:
its generated right-hand side includes a nonzero continuity source `g`.
This exercises the same linear operator. A physical incompressible solve would
instead set `g=0` and supply momentum forcing and compatible boundary data.

## Darcy: permeability contrast and a flux/pressure saddle point

`darcy_mixed_2d` uses the same face and pressure locations, gauge, and ordering,
with impermeable exterior walls. Its equations are

```
K^-1 q + grad(p) = f,
          -div(q) = g.
```

The velocity diffusion block is replaced by a diagonal face resistance. Cell
permeability is `permeability` when the cell center has `x < 0.5` and
`permeability * contrast` otherwise. Resistance at an interior face is the
average of the inverse permeabilities in its adjacent cells. Eliminating flux
therefore yields the usual harmonic-mean two-point finite-volume diffusion
operator, with a negative sign in the pressure Schur complement. Contrast is
not restricted to values above one: either side may be less permeable.

This is a two-point finite-volume mixed system, not an implementation of the
Raviart–Thomas finite element. The distinction matters on nonorthogonal meshes;
this generator uses an orthogonal rectangular grid. The broader two-field
formulation and the symmetric indefinite block structure are described in the
[FEniCS mixed Poisson documentation](https://docs.fenicsproject.org/dolfinx/v0.7.1/python/demos/demo_mixed-poisson.html).
A starter recipe is `{"nx":8,"ny":8,"permeability":1,"contrast":1000}`.

## Elasticity: coupled displacement and near incompressibility

`elasticity_2d` assembles linear triangular finite elements for plane-strain
isotropic elasticity. Each rectangular cell is split along its southwest to
northeast diagonal. Both displacement components vanish on the entire exterior
boundary, removing translations and rigid rotation. The bilinear form is

```
a(u,v) = integral [2*mu*epsilon(u):epsilon(v)
                   + lambda*div(u)*div(v)] dx,
epsilon(u) = (grad(u) + grad(u)^T)/2.
```

The constitutive law is the standard Lamé formulation used in the
[FEniCS elasticity demonstration](https://docs.fenicsproject.org/dolfinx/v0.7.3/python/demos/demo_elasticity.html).
The two-dimensional restriction here is plane strain, not plane stress.
Displacement components are interleaved `(ux,uy)` at each interior node,
x fastest. Matrix order is `2*nx*ny`.

For `mu > 0` and `lambda >= 0`, full clamping makes the stiffness positive
definite. Increasing `lambda/mu` tests increasingly stiff volumetric response
and more difficult conditioning. These P1 displacement elements exhibit
[volumetric locking near incompressibility](https://solidmechanics.org/Text/Chapter8_6/Chapter8_6.php);
this is a documented property of the chosen discretization, not a claim that it
is an accurate incompressible elasticity solver. A starter recipe is
`{"nx":7,"ny":7,"lame_lambda":1000,"lame_mu":1}`.

## Verification

The tests check exact wall stencils and affine pressure gradients for MAC;
positive/negative inertia counts after gauge removal for both mixed models;
the Darcy Schur complement against harmonic finite-volume diffusion; and the
elasticity quadratic form against independently evaluated element strain
energy. Small and rectangular meshes, zero Lamé lambda, material overflow,
invalid parameters, and allocation limits are covered. These checks go beyond
solving a right-hand side formed by multiplying the same matrix.

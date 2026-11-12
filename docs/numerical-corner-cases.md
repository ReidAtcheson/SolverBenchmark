# Binary64 corner cases in generated matrices

Matrix properties describe the actual delivered binary64 entries. For the
structured transport and initial grid generators, numerical and structural
symmetry are checked after assembly. A nonzero input velocity can round away
when its advection contribution is much smaller than diffusion. For example,
`nx=ny=2`, `diffusion=1`, `velocity_x=1e-16`, `velocity_y=0` produces a symmetric
matrix and is now reported as such. Select coefficients whose advection
contribution survives rounding when a nonsymmetric benchmark is intended.
Positive definiteness remains conservatively unknown in this rounded-advection
case; a symmetry assertion alone is not a definiteness assertion.

The initial `grid_ac_jacobian` generator computes reciprocal line impedance by
normalizing resistance and reactance before squaring. This avoids overflow or
underflow in `r²+x²` when the desired admittance is representable. In particular,
`r=x=1e154` no longer silently generates an all-zero matrix; its conductance and
positive susceptance weight are both approximately `5e-155`. The normalized
calculation also handles `r=x=1e308` and `r=x=1e-200`.

Admittance or assembled entries outside finite binary64 range cause a clear
`ValueError`. A component of admittance may underflow to zero when the ratio of
resistance to reactance is extreme; symmetry is determined from the resulting
stored entries. Such extreme scaling is a numerical range test rather than a
representative per-unit electrical network. The ordinary example profile uses
moderate physical coefficients and does not depend on these corner cases.

Regression tests check the rounded and resolved transport cases, scaled AC
admittances using relative comparisons (so a zero answer cannot pass), and
explicit rejection when the reciprocal impedance is too large to represent.

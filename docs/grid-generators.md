# Parameterized electrical networks

Three new generators extend the original path-and-chord examples. They create
all topology, line parameters, and voltage states locally from scalar parameters
and the recipe's uint64 seed. They require neither downloaded cases nor NumPy,
SciPy, or a power-system simulator. All listed parameters are required.

| Generator | Order | Matrix class |
| --- | --- | --- |
| `geographic_grid_dc` | `buses - 1` | Symmetric positive definite grounded DC susceptance matrix |
| `geographic_grid_ac` | `buses - 1 + number_of_PQ_buses` | Generally nonsymmetric real polar power-flow Jacobian |
| `grid_dc_opf_kkt` | `3*buses - 1` | Symmetric indefinite, nonsingular equality-constrained DC dispatch KKT system |

## Shared topology and parameters

| Parameter | Range | Meaning |
| --- | --- | --- |
| `buses` | Integer ≥ 3 | Bus count; bus 0 is the angle reference |
| `mesh_fraction` | [0, 1] | Probability of retaining each candidate edge outside the spanning tree; zero produces a radial feeder |
| `reactance` | > 0 | Nominal series reactance per normalized line length, in per unit |
| `impedance_spread` | [0, 1) | Uniform relative perturbation of each line's impedance |
| `tap_spread` | [0, 1) | Uniform relative deviation of transformer tap magnitudes from one |

Bus coordinates occupy a square lattice of width `ceil(sqrt(buses))`, with
independent bounded positional jitter. Candidate lines connect horizontal,
vertical, and both diagonal neighboring cells. A randomized distance-weighted
Kruskal spanning tree guarantees connectivity; `mesh_fraction` controls the
additional loops. The result is an irregular sparse local graph with at most
four candidate lines per bus. Line impedance is proportional to geometric
length (normalized by lattice spacing) times an independent factor in
`[1-impedance_spread, 1+impedance_spread)`. Tap magnitudes vary independently.
The topology generator costs O(n log n) time and O(n) storage.

This is a controlled geographic network model, **not** a statistically calibrated
synthetic transmission grid or a reconstruction of real infrastructure. It
varies connectivity, geometry, line weights, radiality, and meshing while keeping
input data minimal. The seed drives explicitly specified SplitMix64 integer
arithmetic, followed by 53-bit uniform floating-point conversion; it changes
positions, topology, and electrical parameters. The same recipe and software
reproduce the inputs. Floating-point trigonometry can differ across platforms,
so the benchmark's actual input-byte hashes remain authoritative.

Before allocating topology or matrix data, all three generators enforce a
conservative budget of `40*buses` entries and their wire dimension limits.
This can reject a recipe whose final CSR would fit the limit. Increase the
local `max_entries` limit without changing the recipe when necessary.

## DC power flow

For each branch, the reduced bus susceptance matrix uses the positive weight
`w = 1/(x*tap)`. Assemble its graph Laplacian and eliminate the reference angle
at bus 0. Connectivity and positive weights make the reduced matrix SPD.
Resistance and charging are absent under the DC approximation. The tap
convention follows [MATPOWER makeBdc](https://matpower.org/docs/ref/matpower7.1/lib/makeBdc.html).
Phase shifts affect a DC injection offset, not this coefficient matrix, so they
are not parameters of the DC generator.

Example parameters:

```json
{"buses":200,"mesh_fraction":0.3,"reactance":0.1,"impedance_spread":0.6,"tap_spread":0.05}
```

Set `mesh_fraction` to zero for a radial network. Nonzero values introduce loops
without sacrificing connectivity.

## Non-flat AC power-flow Jacobian

Additional parameters:

| Parameter | Range | Meaning |
| --- | --- | --- |
| `resistance` | ≥ 0 | Nominal series resistance per normalized length, in per unit |
| `pv_fraction` | [0, 1] | Fraction of non-slack buses classified as PV (rounded down) |
| `angle_scale` | [0, π] | Voltage angle gradient across the domain, in radians |
| `voltage_spread` | [0, 1) | Amplitude of a sinusoidal voltage-magnitude field about one |
| `phase_shift` | [0, π] | Maximum absolute line transformer phase shift, in radians |
| `charging` | ≥ 0 | Total line charging susceptance per normalized length |

For a line from f to t, let `y = 1/(r + i*x)` and
`a = tap*exp(i*shift)`. Its terminal admittances are
`Yff=(y+i*bc/2)/tap²`, `Ytt=y+i*bc/2`, `Yft=-y/conj(a)`,
`Ytf=-y/a`. These represent a π line plus an ideal from-side transformer,
using the conventions in [MATPOWER's branch model](https://matpower.app/manual/matpower/Branches.html).
The reciprocal `y` uses the same scaled real arithmetic as the initial AC
generator, rather than direct complex division. Thus a representable admittance
is not lost when `r²+x²` or a complex-division intermediate would overflow.
Unrepresentable admittance or assembled values fail explicitly; see
[numerical corner cases](numerical-corner-cases.md) for the extreme-range
regressions and limits.

Voltage angles vary smoothly with x; magnitudes use
`1 + voltage_spread*sin(2πx)*sin(2πy)`. Bus 0 is fixed at `1+0i`.
Exactly `floor(pv_fraction*(buses-1))` evenly spaced non-slack buses are PV.
The remaining non-slack buses are PQ. Unknowns are all non-slack angles in bus
order, followed by PQ magnitudes in bus order. Rows are active injections at
all non-slack buses, followed by reactive injections at PQ buses.

The assembled matrix is the derivative of `S=diag(V)*conj(Ybus*V)`, with respect
to those polar unknowns. This follows the formulation documented in
[MATPOWER dSbus_dV](https://matpower.org/docs/ref/matpower7.1/lib/dSbus_dV.html).
The tests independently evaluate terminal currents through each branch circuit
and finite-difference every Jacobian column, including non-unit taps, nonzero
phase shifts, line charging, and all-PQ/mixed/all-PV cases.

Example parameters:

```json
{"buses":200,"mesh_fraction":0.3,"reactance":0.1,"impedance_spread":0.6,"tap_spread":0.05,"resistance":0.02,"pv_fraction":0.2,"angle_scale":0.15,"voltage_spread":0.05,"phase_shift":0.03,"charging":0.01}
```

This is a linearization at a prescribed non-flat state. One can choose net
injections from that state, but no independently specified load-flow problem is
solved, and no operational feasibility or stability is asserted. Large angle,
voltage, or transformer variations may produce difficult or singular Jacobians.
The generator reports definiteness as unknown and measures numerical and
structural symmetry from the assembled entries. It does not relabel every AC
case nonsymmetric: special lossless flat cases can be symmetric.

## DC optimal-power-flow KKT system

`grid_dc_opf_kkt` adds a required positive `generation_cost` parameter. It
models an unconstrained generator at each bus with quadratic cost
`0.5*sum_i c_i*p_i²`, where `c_i=generation_cost*(1+i/buses)`.
Keep every bus power-balance equation, including the reference bus, and fix
only its angle. If `B` is the full Laplacian with its reference-angle column
removed, the constraints are `p - B*theta = demand`. In the ordering
`[p (n), theta (n-1), multipliers (n)]`, the coefficient matrix is

```text
[ C    0     I  ]
[ 0    0    -Bᵀ ]
[ I   -B     0  ]
```

This is the equality-constrained quadratic core of DC OPF; the standard DC OPF
also includes generation and line-flow limits, which are intentionally absent
here. See the [MATPOWER manual, section 6.2](https://matpower.org/manual.pdf).
The matrix is symmetric indefinite. It is nonsingular because `C` is positive
definite and the connected network's reference-column-deleted `B` has full
column rank. Keeping the reference bus balance prevents an unpriced implicit
slack generator from supplying all demand.

Example parameters:

```json
{"buses":200,"mesh_fraction":0.3,"reactance":0.1,"impedance_spread":0.6,"tap_spread":0.05,"generation_cost":2.0}
```

Tests solve a small system with nonuniform demand, verify total generation
balance and equal marginal costs, and check positive and negative quadratic
form directions. The benchmark itself continues to manufacture its linear RHS
from a known reference solution, independently of a particular demand profile.

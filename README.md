# SolverBenchmark — interface mockup

This repository currently contains a **design draft**, not a working benchmark
runner. The solver-facing header is self-contained; the worker and orchestrator
are syntax-checkable control-flow sketches backed by unimplemented runtime
declarations. No solver backend, IPC implementation, benchmark CLI, or results
are included yet.

The design takes inspiration from `RatchesSparse/libraries/solver_bench`, especially
borrowed matrix views, explicit unsupported results, phase accounting, independent
accuracy validation, and retaining partial results after a worker dies. The
benchmark suite and problem generation live here; solver adapters can live in
their own repositories and build environments.

## Three boundaries

```text
Benchmark repository                          External solver repository
┌──────────────────────────┐                  ┌─────────────────────────────┐
│ Parent orchestrator      │  control socket  │ Standard worker executable  │
│ prepares A, b, properties ├─────────────────►│ negotiates, maps, validates │
│ owns reference solution  │◄─────────────────┤ runs phases, reports timing │
│ checks every returned x  │                  │              │              │
│ supervises exit/deadline  │  shared memory   │       solver_t interface    │
│ records process max RSS  ├──────────────────┤              │              │
└──────────────────────────┘  immutable A, b   │       backend adapter       │
                             writable x       └─────────────────────────────┘
```

- [Solver interface](include/solver_benchmark/solver.h): the only header an adapter
  implements. It has no process, serialization, or benchmark orchestration API.
- [Standard worker sketch](mockup/worker.cpp): built with the adapter and its
  backend using that solver's toolchain, libc, C++ library, and threading runtime.
- [Parent sketch](mockup/orchestrator.cpp): publishes the problem, starts an
  external executable, consumes results, and determines the case outcome.
- [Runtime service declarations](mockup/runtime.h): the missing platform services,
  deliberately left unimplemented while reviewing the boundaries.
- [Benchmark profile](docs/benchmark-profile.md), [JSON Schema](schemas/benchmark-profile.schema.json),
  and [example JSON](examples/benchmark-profile.json): serializable experiment
  recipes, measurement passes, and separate profile/version/input identities.
- [Wire protocol draft](docs/protocol.md): independent of C++ source/ABI versions;
  sufficient to discuss a worker or parent written in another language.

The C++ header and worker must agree locally, but the parent and worker need only
agree on the wire protocol and data profile. The parent never loads a solver's
shared libraries. A fresh executable image provides runtime separation; this
does not imply a different kernel, a security sandbox, or isolated hardware
caches. The first proposed transport targets local Linux processes. Other
platforms need equivalent handle-passing, mapping, and supervision backends.

## Solver contract

```cpp
using scalar_t = double;
using index_t = std::int32_t;
using offset_t = std::int64_t;

class solver_t {
public:
    virtual ~solver_t() = default;
    virtual phase_result_t setup(const csr_matrix_view_t&, const solver_options_t&) = 0;
    virtual phase_result_t pre_analyze() = 0;
    virtual phase_result_t analyze() = 0;
    virtual phase_result_t numeric() = 0;
    virtual phase_result_t solve(dense_view_t<const scalar_t> rhs,
        dense_view_t<scalar_t> solution) = 0;
};
```

An adapter may define all overrides inline in its own header. Its backend can
still be a compiled library; "header-only interface" does not constrain how that
library is implemented. The standard worker entry point takes a local factory:

```cpp
// Illustrative glue after the worker runtime is implemented:
#include "my_solver.h"
#include "runtime.h"  // Draft location of the standard worker entry point.

int main(int argc, char** argv) {
    return solver_benchmark::mockup::worker_main(argc, argv,
        +[]() -> std::unique_ptr<solver_benchmark::solver_t> {
            return std::make_unique<my_solver_t>();
        });
}
```

Setup binds an instance to exactly one immutable matrix, including its entries,
dimensions and properties. No subsequent method takes a matrix;
replacing it requires a new instance. Its backing arrays, RHS, and output buffer
remain valid until after destruction. An adapter retains a **copy of the view**,
not a reference to a temporary view object. Setup configures the backend;
`pre_analyze` copies/adapts data for an in-place or differently formatted backend.

Any phase can return `unsupported` with a reason. For an SPD-only backend, a
conservative setup can require both symmetry and positive definiteness:

```cpp
const auto& info = matrix.info();
if (info.properties.get(matrix_property_t::symmetric) != property_state_t::yes ||
    info.properties.get(matrix_property_t::positive_definite) != property_state_t::yes) {
    return {phase_status_t::unsupported, "requires known SPD properties"};
}
```

Alternatively it can proceed to analysis and establish missing properties itself.
That deduction is solver work and is timed. `unsupported` means declining the
problem/configuration; `failed` means an attempted operation failed. A numerical
breakdown or failure to converge must not be disguised as unsupported.

Setup, pre-analysis, analysis, or numeric may return `not_applicable` when the backend has no
separate operation. A combined analysis/factorization belongs in numeric, with
analysis marked not applicable; adapters document their mapping. Solve must run.
All backend work must finish before a callback returns, including asynchronous
work. No factors, converted matrices, or solved answers may be cached across
fresh instances; normal process-level code/allocator/thread-runtime warming is
allowed. Each adapter documents any unavoidable persistent backend caches.

## Matrix choices

The first data format is **square, real binary64, full canonical CSR, zero based**.
This is distinct from a benchmark profile, which specifies generators and their
parameters, RHS construction, solvers, and measurement passes:

- `1 <= order <= INT32_MAX`; row/column IDs are signed 32-bit.
- Stored-entry counts and row offsets are signed 64-bit, independently of IDs.
  Counts must also fit the receiving process's address space and buffer limits.
- `row_offsets` has `order + 1` entries, begins at zero, is nondecreasing, and ends
  at `nonzeros`. `column_indices` and `values` each have `nonzeros` entries.
- Columns are in range and strictly increasing in each row. Duplicate entries
  are combined during parent preparation. Explicit stored zeros are allowed and
  remain part of the structural pattern. Empty rows and a zero-entry matrix are
  valid inputs, though a solver may subsequently reject/fail on them.
- Values and RHS entries must be finite. RHS and solution have shape
  `(order, rhs_count)`, with `rhs_count >= 1`.

CSR-only is a useful initial interchange rule: every worker receives identical
input, and validation needs only one representation. CSC or symmetric-triangle
backends convert during `pre_analyze`, including matrix value conversion costs.
Report preparation separately, then both core time (`analysis + numeric + solve`)
and total solver time (`setup + pre_analyze + core`). Cleanup is an additional
separate measurement. These sums exclude inter-phase IPC and are not enclosing
stopwatch intervals. End-to-end process time is reported separately.

`pre_analyze` covers format/index conversion, triangle extraction, and complex
reconstruction. Symbolic analysis and fill-reducing ordering belong in `analyze`;
factorization/preconditioning belongs in `numeric`. Data adaptation must not hide
algorithmic work outside core time. A backend with inseparable preparation and
analysis reports the combined operation in analysis and documents this limitation.

The aliases keep source changes localized. Changing an alias does **not** change
the protocol implicitly: another scalar/index/offset layout needs a distinct
negotiated data format. The initial wire format remains fixed.

Both dense arguments are rank-2 `mdspan` views with explicit element strides.
The wire format uses packed column-major blocks: row stride 1, column stride
`order`. The view type uses `layout_stride`, allowing adapters to inspect layout
without assuming contiguous rows. RHS and output never alias. Native dense
packing/unpacking, including real/complex vector conversion, is charged to solve.
The initial schedule solves one multi-RHS block per fresh solver instance;
repeated solve calls reusing factors can be a future explicit schedule.

Properties are three-state assertions about the delivered values, not storage
instructions. All properties default to `unknown`; absence never means false.

| Property | Meaning when `yes` |
| --- | --- |
| `symmetric` | The full numerical matrix equals its transpose. |
| `structurally_symmetric` | The stored sparsity pattern equals its transpose, including explicit zeros. |
| `positive_definite` | `xᵀAx > 0` for every nonzero real x. Symmetry is a separate assertion. |

`no` asserts that the property does not hold; `unknown` makes no assertion.
Assertions must describe the final matrix after canonicalization/rounding, and
their provenance should be recorded by the parent. The transport validates metadata
structure without requiring an expensive proof of every mathematical assertion. Solvers may trust
assertions, infer additional properties, or decline unknown cases. Numerical
symmetry alone need not imply stored structural symmetry when explicit zeros
are present. None of these properties permits omitting a triangle.

Diagonal dominance is not exposed as metadata, either as an assertion or as a
numerical margin. Solvers can cheaply determine it during `pre_analyze` if useful;
that preparation remains timed separately from the analysis-through-solve total.
Positive definiteness remains a three-state assertion: it is expensive to establish
in general and often known from the problem's construction. A quantitative
property API can be added later for information worth transporting.

## Complex systems using real storage

Encoding metadata can assert `complex_interleaved`, with logical complex order
`m` and delivered real order `2*m`. Every complex entry `a+ib` corresponds to the
real block `[a,-b; b,a]`. Dense columns interleave `[Re0, Im0, Re1, Im1, ...]`;
RHS count does not double. Zero entries in those blocks may be omitted from CSR.
The identity must hold numerically over all blocks, treating absent entries as
zero. An arbitrary even-sized real matrix does not qualify.

Real backends solve the delivered system directly; complex backends reconstruct
their preferred matrix during pre-analysis and translate dense blocks during
solve. All properties describe the delivered **real**
matrix. Any assertion about the logical complex matrix needs an explicitly
scoped future property. The parent validates against the delivered real system;
Euclidean residual norms match the complex interpretation. Infinity-norm and
componentwise diagnostics retain their real-system definitions. Reports retain
both logical complex order and delivered real order/entry count.

## Repetitions and measurement

One fresh process handles one `(problem, solver, configuration, measurement pass)` execution.
It executes configurable full warm-ups, then configurable measured repetitions:

```text
launch → negotiate → map/validate
  warm-up × W: new solver → setup → pre_analyze → analyze → numeric → solve → destroy → validate/ACK
  measured × R: new solver → setup → pre_analyze → analyze → numeric → solve → destroy → validate/ACK
complete → exit → parent reaps and records resources
```

`W >= 0`, `R >= 1`; the JSON profile specifies both explicitly for every pass. Warm-ups run exactly the same callbacks,
but do not produce phase timing samples. They still report progress/failure and
receive independent accuracy checks. All repetitions start with a zero solution block,
so neither factors nor iterative initial guesses carry over. The output buffer
is reused only after the parent acknowledges consumption of the preceding result.

Setup includes factory construction. Each measured phase reports monotonic wall
time and process CPU time across all worker threads, including user and system
CPU. Threaded CPU time can exceed wall time. These measurements exclude IPC,
matrix generation/publication/validation, output initialization, parent accuracy
checks, and process launch. Cleanup has its own wall/CPU sample, separate from
the five solver phases. A destructor crash/hang remains a case failure.

The parent records whole-process wall time, user/system CPU time, exit code or
signal, and **lifetime max RSS in bytes**, including startup, mappings, warm-ups,
all repetitions, and teardown. RSS is not solver-only allocation, a per-phase
peak, or a sum over a process tree. It cannot be reset after warm-up using a
process lifetime high-water counter. Missing resource data is null, never zero.
Backend subprocesses are outside this initial measurement scope.

Reports preserve every measured sample and compute summaries from successful,
accuracy-accepted measured repetitions, with failure and exclusion counts beside
them. Warm-ups and failures never disappear from the raw case report. One case
deadline covers launch through exit, including negotiation, warm-up, ACK waits,
and teardown. Failed/unsupported cases do not prevent subsequent suite cases.

A pass selects instrumentation; a repetition supplies another sample within it.
Baseline passes disable tracing. Trace passes start fresh workers, replay the same
inputs/options and their declared warm-ups, and retain their timings separately.
The standard worker emits phase/teardown markers on the trace clock with case,
pass, trial, phase, PID, and TID identifiers. Parent IPC receipt timestamps are
not tracing boundaries. OS-level collectors can correlate mappings, page faults,
scheduling, and thread lifetimes across the worker's threads. Available collectors
and permissions are negotiated before trials; missing instrumentation is explicitly
`unsupported_instrumentation`, not a silent baseline run. Collector failure or
lost/incomplete trace data is `instrumentation_failed` with partial data retained.

Per-phase event counts, snapshots, sampled RSS peaks, and lifetime high-water RSS
have different meanings. In particular, OS mapping events are not individual
heap allocations, and differences of lifetime max RSS are not phase memory peaks.
Allocation lifetime can span phases; report event attribution and retained memory
separately. Attach collectors before warm-ups and detach/reap on every exit path.

Thread limits, launch environment, executable/build identity, machine/CPU details,
affinity policy, matrix identity, property assertions, requested options, and
documented native settings belong in the run record. Solvers must honor the thread limit or decline
it; launch-time threading environment must be set before executable startup.
Run compared cases sequentially by default to avoid competition between workers.
The initial design compares CPU solvers; accelerators need additional device
identity/memory metrics and an explicit measurement policy.

## Accuracy and outcomes

The parent checks each `r = b - A*x` against its original A and b, outside solver timing,
after **every successful solve**, including warm-ups, separately for every RHS column. The worker never sees an
optional reference solution. Verification should use wider accumulation where
available and stable norm/reduction algorithms, recording the arithmetic used.
The draft's acceptance rule for each column is:

```text
finite(x, r, norms) AND ||r||₂ <= absolute_tolerance + relative_tolerance * ||b||₂
```

Every column must pass; reports retain per-column diagnostics and the worst
relative residual. A block aggregate must not hide a failed column.

Tolerance values must be finite and nonnegative. A zero RHS works with this same
rule; the absolute tolerance can allow a nonzero residual. Iterative solvers
receive this target and an iteration limit; direct solvers may ignore the
iteration limit. Backend success is separate from accuracy acceptance.

Report absolute and relative L2 residual, normwise infinity backward error
`||r||∞ / (||A||∞ ||x||∞ + ||b||∞)`, and componentwise backward error
`max_i |r_i| / (|A||x| + |b|)_i`. Relative residual uses `||r||₂ / ||b||₂`.
Define `0/0 = 0` and positive/zero as infinity for these diagnostics. Nonfinite
solutions or invalid verification arithmetic fail acceptance; encode nonfinite
diagnostics as a tagged value in reports, not invalid JSON numbers. If a reference
solution exists, report relative forward/reference error too, using the same
zero-denominator convention. A manufactured reference is diagnostic: rounding
when forming b can mean it is not the exact solution of the stored problem.

Keep `success`, `unsupported`, `solver_failed`, `accuracy_failed`, `worker_crashed`,
`timed_out`, `launch_failed`, `protocol_error`, `unsupported_instrumentation`,
and `instrumentation_failed` distinct. A complete result
message and consistent process exit are both required. Preserve completed phase
samples and the last active phase when a worker dies. Failure to meet accuracy
does not erase timing data or prevent later repetitions; it makes the case an
accuracy failure even when the backend reported success.

## Serializable profiles and provenance

The [profile schema](schemas/benchmark-profile.schema.json) and
[example](examples/benchmark-profile.json) describe generators and parameters,
seeds, multi-RHS construction, solver settings, and measurement passes entirely
as JSON. Generator code and deployment paths stay outside the recipe; logical IDs
are resolved by the parent. The parent prepares immutable A/B once and reuses
identical inputs across compared solvers and passes.

Keep three identities: the hash of the canonical profile, the hash of the resolved
software version manifest, and the hash of each materialized input. Record all
three with case/solver/pass IDs and actual execution environment. A generator or
solver version change can leave the profile hash unchanged while the software
manifest changes; an input hash establishes whether generated bytes also changed.
See the [profile specification](docs/benchmark-profile.md) for canonical JSON,
explicit-default rules, hash preimages, and the distinction between a software
manifest and deployment/environment metadata. Parsing, hashing, generation, and
profile execution are declared in the parent sketch, not implemented yet.

## Reviewing the draft

With a standard library providing C++23 `<mdspan>`:

```sh
c++ -std=c++23 -Wall -Wextra -Wpedantic -Werror -Iinclude -Imockup \
    -fsyntax-only mockup/worker.cpp mockup/orchestrator.cpp
```

For toolchains without `<mdspan>`, select the header-only
[Kokkos mdspan backport](https://github.com/kokkos/mdspan) explicitly. No Kokkos
runtime is needed. The whole worker must select the same mdspan implementation.
The parent and wire format are independent of this choice. For example, with a
checkout at `/tmp/solverbenchmark-mdspan`:

```sh
c++ -std=c++20 -DSOLVER_BENCHMARK_USE_KOKKOS_MDSPAN \
    -Wall -Wextra -Wpedantic -Werror -Iinclude -Imockup \
    -isystem /tmp/solverbenchmark-mdspan/include \
    -fsyntax-only mockup/worker.cpp mockup/orchestrator.cpp
```

Both GCC 13 and Clang 18 were checked using backport revision
`b15adf7aa6626b231a1aa01a4d2f8ba558a24527`. The installed standard libraries do not
provide `<mdspan>`, so the standard-library path has not been validated here.

The protocol and profile schema are draft 1.0, not yet external compatibility
promises. No IPC, profiler, generator, validator, or executable runner is shipped.

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
    virtual phase_result_t setup(const matrix_info_t&, const solver_options_t&) = 0;
    virtual phase_result_t analyze(const csr_matrix_view_t&) = 0;
    virtual phase_result_t numeric(const csr_matrix_view_t&) = 0;
    virtual phase_result_t solve(const csr_matrix_view_t&,
        std::span<const scalar_t> rhs, std::span<scalar_t> solution) = 0;
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

Only setup lacks access to matrix entries; it gets dimensions, properties, and
options. Later phases get the same immutable matrix. Its backing arrays, RHS,
and output buffer remain valid until after solver destruction. An adapter may
store a **copy of a view**, not references to temporary view objects. An in-place
backend copies its input into owned storage during a measured phase.

Any phase can return `unsupported` with a reason. For an SPD-only backend, a
conservative setup can require both symmetry and positive definiteness:

```cpp
if (info.properties.get(matrix_property_t::symmetric) != property_state_t::yes ||
    info.properties.get(matrix_property_t::positive_definite) != property_state_t::yes) {
    return {phase_status_t::unsupported, "requires known SPD properties"};
}
```

Alternatively it can proceed to analysis and establish missing properties itself.
That deduction is solver work and is timed. `unsupported` means declining the
problem/configuration; `failed` means an attempted operation failed. A numerical
breakdown or failure to converge must not be disguised as unsupported.

Setup, analysis, or numeric may return `not_applicable` when the backend has no
separate operation. A combined analysis/factorization belongs in numeric, with
analysis marked not applicable; adapters document their mapping. Solve must run.
All backend work must finish before a callback returns, including asynchronous
work. No factors, converted matrices, or solved answers may be cached across
fresh instances; normal process-level code/allocator/thread-runtime warming is
allowed. Each adapter documents any unavoidable persistent backend caches.

## Matrix choices

The first profile is **square, real binary64, full canonical CSR, zero based**:

- `1 <= order <= INT32_MAX`; row/column IDs are signed 32-bit.
- Stored-entry counts and row offsets are signed 64-bit, independently of IDs.
  Counts must also fit the receiving process's address space and buffer limits.
- `row_offsets` has `order + 1` entries, begins at zero, is nondecreasing, and ends
  at `nonzeros`. `column_indices` and `values` each have `nonzeros` entries.
- Columns are in range and strictly increasing in each row. Duplicate entries
  are combined during parent preparation. Explicit stored zeros are allowed and
  remain part of the structural pattern. Empty rows and a zero-entry matrix are
  valid inputs, though a solver may subsequently reject/fail on them.
- Values and RHS entries must be finite. The first draft has one contiguous RHS
  and solution vector, each of length `order`.

CSR-only is a useful initial interchange rule: every worker receives identical
input, and validation needs only one representation. CSC or symmetric-triangle
backends convert during analysis, including value conversion costs there if
needed. Consequently, end-to-end comparisons include adaptation cost and favor
backends that accept the interchange format directly. That is an explicit
benchmark scope. A future native-format benchmark should be a separately named
policy with its preparation costs reported, rather than silently excluding them.

The aliases keep source changes localized. Changing an alias does **not** change
the protocol implicitly: a different scalar/index/offset layout needs a distinct
negotiated data profile. The initial wire profile remains fixed.

Properties are three-state assertions about the delivered values, not storage
instructions. All properties default to `unknown`; absence never means false.

| Property | Meaning when `yes` |
| --- | --- |
| `symmetric` | The full numerical matrix equals its transpose. |
| `structurally_symmetric` | The stored sparsity pattern equals its transpose, including explicit zeros. |
| `positive_definite` | `xᵀAx > 0` for every nonzero real x. Symmetry is a separate assertion. |
| `diagonally_dominant` | Every row satisfies `abs(a_ii) >= sum_{j != i} abs(a_ij)`. |
| `strictly_diagonally_dominant` | Every row satisfies the strict version of that inequality. |

`no` asserts that the property does not hold; `unknown` makes no assertion.
Assertions must describe the final matrix after canonicalization/rounding, and
their provenance should be recorded by the parent. The transport validates known
logical contradictions (for example strict dominance true but weak dominance
false), not an expensive proof of every mathematical assertion. Solvers may trust
assertions, infer additional properties, or decline unknown cases. Numerical
symmetry alone need not imply stored structural symmetry when explicit zeros
are present. None of these properties permits omitting a triangle.

## Repetitions and measurement

One fresh process handles one `(problem, solver executable, configuration)` case.
It executes configurable full warm-ups, then configurable measured repetitions:

```text
launch → negotiate → map/validate
  warm-up × W: new solver → setup → analyze → numeric → solve → destroy → validate/ACK
  measured × R: new solver → setup → analyze → numeric → solve → destroy → validate/ACK
complete → exit → parent reaps and records resources
```

`W >= 0`, `R >= 1`; both default to one. Warm-ups run exactly the same callbacks,
but do not produce phase timing samples. They still report progress/failure and
receive independent accuracy checks. All repetitions start with a zero solution,
so neither factors nor iterative initial guesses carry over. The output buffer
is reused only after the parent acknowledges consumption of the preceding result.

Setup includes factory construction. Each measured phase reports monotonic wall
time and process CPU time across all worker threads, including user and system
CPU. Threaded CPU time can exceed wall time. These measurements exclude IPC,
matrix generation/publication/validation, output initialization, parent accuracy
checks, and process launch. Cleanup has its own wall/CPU sample, separate from
the four solver phases. A destructor crash/hang remains a case failure.

The parent records whole-process wall time, user/system CPU time, exit code or
signal, and **lifetime max RSS in bytes**, including startup, mappings, warm-ups,
all repetitions, and teardown. RSS is not solver-only allocation, a per-phase
peak, or a sum over a process tree. It cannot be reset after warm-up using a
process lifetime high-water counter. Missing resource data is null, never zero.
Backend subprocesses are outside this initial measurement profile.

Reports preserve every measured sample and compute summaries from successful,
accuracy-accepted measured repetitions, with failure and exclusion counts beside
them. Warm-ups and failures never disappear from the raw case report. One case
deadline covers launch through exit, including negotiation, warm-up, ACK waits,
and teardown. Failed/unsupported cases do not prevent subsequent suite cases.

Thread limits, launch environment, executable/build identity, machine/CPU details,
affinity policy, matrix identity, property assertions, requested options, and
documented native settings belong in the run record. Solvers must honor the thread limit or decline
it; launch-time threading environment must be set before executable startup.
Run compared cases sequentially by default to avoid competition between workers.
The initial design compares CPU solvers; accelerators need additional device
identity/memory metrics and an explicit measurement policy.

## Accuracy and outcomes

The parent checks `r = b - A*x` against its original A and b, outside solver timing,
after **every successful solve**, including warm-ups. The worker never sees an
optional reference solution. Verification should use wider accumulation where
available and stable norm/reduction algorithms, recording the arithmetic used.
The draft's common acceptance rule is:

```text
finite(x, r, norms) AND ||r||₂ <= absolute_tolerance + relative_tolerance * ||b||₂
```

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
`timed_out`, `launch_failed`, and `protocol_error` distinct. A complete result
message and consistent process exit are both required. Preserve completed phase
samples and the last active phase when a worker dies. Failure to meet accuracy
does not erase timing data or prevent later repetitions; it makes the case an
accuracy failure even when the backend reported success.

## Reviewing the draft

Check the C++ declarations and sketches without building or linking a runtime:

```sh
c++ -std=c++20 -Wall -Wextra -Wpedantic -Werror -Iinclude -Imockup \
    -fsyntax-only mockup/worker.cpp mockup/orchestrator.cpp
```

The main choices to settle before implementation are CSR as the common input
format, fresh solver instances within a warm process, the one-RHS initial scope,
and the common residual acceptance rule. The protocol is explicitly draft 1.0;
it is not yet a compatibility promise to external implementations.

# SolverBenchmark

A Linux sparse linear solver benchmark with a Python parent and independently
built C++ worker executables. The parent generates identical inputs, publishes
sealed shared memory, supervises each worker, validates every returned RHS, and
writes reproducible result artifacts. Solver adapters implement only a small
header-only interface; they do not implement processes or messaging.

The baseline backend is **Eigen 3.4 SparseLU**, built as `sb-eigen`. It is a portable,
sequential baseline with no vendor BLAS dependency or architecture-specific build
flags. Eigen supports x86 and ARM; Intel/AMD/ARM binaries are built from the same
source for each target. This repository is MIT licensed, while Eigen retains its
MPL-2.0 terms. `EIGEN_MPL2_ONLY` excludes incompatible optional Eigen modules.
See [dependency notices](THIRD_PARTY_NOTICES.md). An ARM CI job is configured;
local x86 testing alone is not a claim that ARM execution has been verified.

The optional **SuperLU** worker provides another general sparse LU solver for
comparison, including nonsymmetric and symmetric-indefinite matrices. It runs
the real CSR input, including the real expansion of complex-encoded systems.
Both backends are intended for nonsingular systems; numerical failures are
reported rather than counted as successful solves.

## Build and run

Requires Linux, Python 3.11+, CMake 3.24+, and a C++20 compiler. CMake fetches
pinned Eigen, nlohmann/json, and header-only mdspan sources. No Kokkos runtime is
used. A network connection is needed for the first dependency download. On Debian-like
systems, Python virtual environments may require the `python3-venv` package.
If uv is already installed, `uv venv .venv` followed by
`uv pip install --python .venv/bin/python -e .` is another supported setup.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
ctest --test-dir build --output-on-failure
solver-benchmark validate examples/benchmark-profile.json
solver-benchmark run examples/benchmark-profile.json \
    --deployment examples/deployment.json --output results/local-001
```

Output directories must be new, so previous measurements are never overwritten.
The example runs three Laplacians with three RHS columns, warm-ups, baseline
measurements, and a separate phase-resource pass. Exit 0 means every execution
succeeded or was explicitly unsupported, 1 means a measured execution failed,
and 2 means a profile/configuration error. Read the outcome counts: unsupported
instrumentation is not a completed trace. `--max-entries` bounds each materialized
sparse/dense buffer before allocation (default 5,000,000 elements).

With both workers built, the comparison profile exercises the generated physics
suite and produces a report containing both solvers. The deployment is a template:
before running it, copy it and replace its SuperLU and BLAS dependency placeholders
with the actual library paths on the target host.

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    -DSOLVER_BENCHMARK_BUILD_SUPERLU=ON
cmake --build build -j2
ctest --test-dir build --output-on-failure
ldd build/sb-superlu
cp examples/comparison-deployment.json build/comparison-deployment.json
# Edit build/comparison-deployment.json as described below.
solver-benchmark run examples/comparison-profile.json \
    --deployment build/comparison-deployment.json --output results/comparison-001
solver-benchmark report results/comparison-001 --output reports/comparison.html
```

Install the SuperLU 6 development package and its BLAS dependency first, or set
`CMAKE_PREFIX_PATH` to a SuperLU installation. The worker is optional and is
not built by default. Use a sequential BLAS or set the thread limit appropriately.
The installed worker retains non-system library search paths from the build;
moving those libraries, or their transitive dependencies, may require loader
configuration or relinking.

The template intentionally fails until its `runtime_dependencies` entries name
existing files. Use `ldd` in the same runtime environment as the benchmark to
identify the actual SuperLU and BLAS files; use absolute paths for clarity. Add
entries for other relevant dynamically linked math libraries and runtimes, such
as a BLAS implementation loaded transitively or an OpenMP runtime. For statically
linked components, remove the corresponding placeholder: their code is covered
by the executable hash. Libraries loaded dynamically after startup may not appear
in `ldd` and must be recorded separately. Paths are resolved relative to the
deployment file, so check worker paths when moving the template elsewhere.
The declared file contents are hashed for comparison provenance; the benchmark
does not verify that those files are the ones ultimately loaded. Run `ldd` only
on a trusted executable.

The example deployment points into `build/` and limits common BLAS/OpenMP
thread pools for SuperLU. Adjust the paths and thread settings for the selected
build and runtime libraries. The profile's residual tolerances govern acceptance;
the report also shows achieved componentwise backward errors. A small residual
alone does not guarantee a small forward error for an ill-conditioned system.

For an offline build, CMake's `FETCHCONTENT_SOURCE_DIR_MDSPAN`,
`FETCHCONTENT_SOURCE_DIR_JSON`, and `FETCHCONTENT_SOURCE_DIR_EIGEN` can point to
matching pinned source trees. Use the same Python environment for CTest and the
CLI; select it with `-DPython3_EXECUTABLE=/absolute/path/to/python` if necessary.

## HTML and JSON comparison reports

Collect any number of benchmark output directories into matching reports: a
self-contained HTML file for people and a structured JSON summary for agents and
scripts. No web server, JavaScript, or external assets are needed to view the HTML:

```sh
solver-benchmark report results/local-001 results/local-002 \
    --output reports/comparison.html
```

This writes `reports/comparison.html` and `reports/comparison.json` from the same
aggregated data, including the same column baselines and relative timing ratios.
The JSON preserves full numeric precision, matrix/platform/solver identities,
configuration details, worst backward errors, accepted sample counts, execution
outcomes, and source references. Missing values are `null`; infinite values use
`{"nonfinite": "+inf"}` rather than nonstandard JSON numbers. The versioned
[JSON report format](docs/report-format.md) describes the fields and aggregation
rules. Changing the HTML filename also changes the sibling JSON filename.
An existing JSON file is replaced only if it is a comparison report in this
format; collisions with benchmark artifacts or unrelated JSON files are errors.

Inputs may also be `results.jsonl` paths, with `execution.json`, `profile.json`,
and `version-manifest.json` alongside them. The report reads these artifacts;
workers and the original input arrays are not needed. `--title` customizes the
heading, and `--metric cpu` selects CPU seconds instead of the default wall
seconds. Repeating the same input path does not count it twice.

Each matrix comparison has an **absolute timing table** (seconds) and a
**relative timing table**, with one row per platform/solver configuration and
columns for setup, pre-analysis, analysis, numeric, solve, teardown, and critical
path. Relative timings divide by the best timing in that column across all rows:
the fastest is **1**, and **1.2** means 20% slower. Tied best values are highlighted.
Missing values appear as a dash and do not enter the baseline; when the best
timing is zero, zero ties are 1 and positive timings have an infinite ratio.

Each solver row also shows its **worst componentwise backward error**: the maximum
over all RHS columns and measured baseline repetitions across its source outputs,
including rejected solutions. Warmups and trace passes are excluded. This
dimensionless error stays absolute in both tables. A dash means no error was
recorded; infinity preserves infinite errors or failed-verification diagnostics.

Only accuracy-accepted **measured** trials from baseline passes contribute timings.
Warm-ups and instrumentation passes are excluded. Every phase independently uses
its **fastest** eligible repetition across matching outputs. The critical path
is the minimum of **analysis + numeric + solve within a single repetition**,
not the sum of independently selected phase minima. It excludes setup,
pre-analysis, teardown, IPC, and parent validation. Accepted measured trials
remain eligible even if another trial fails; all execution outcomes are visible
in the report, and unsuccessful solvers remain present with missing timings.

Comparisons are separated by delivered input hash (including RHS and encoding),
so identically named cases with different inputs are not mixed. Tolerance settings
remain part of each solver configuration, allowing their achieved errors and
timings to be compared within the same matrix table. Matching platform and solver
configurations pool their repetitions; different CPU/OS/host/affinity metadata,
solver IDs/options/arguments, worker builds or declared runtime dependency hashes,
and recorded runtime environments keep separate rows.
Expandable details and source references retain the context for each comparison.

## Matrix generators

The initial generators discretize **negative Laplacian `-Δ`**, with homogeneous
Dirichlet boundary conditions and second-order centered differences. Counts are
interior grid points; boundary values are eliminated as zero. Ordering is x
fastest, then y, then z. Each active direction contributes `2/h²` to the diagonal
and `-1/h²` to existing adjacent interior points. Boundary rows retain the full
diagonal, including when a direction has just one interior point.

| Generator ID | Required parameters | Delivered order |
| --- | --- | --- |
| `laplacian_1d` | `mx`, `dx` | `mx` |
| `laplacian_2d` | `mx`, `my`, `dx`, `dy` | `mx * my` |
| `laplacian_3d` | `mx`, `my`, `mz`, `dx`, `dy`, `dz` | `mx * my * mz` |

Counts must be positive integers; spacings must be finite and positive. All
parameters are explicit, and unknown parameters are errors. These generators
provide symmetric, structurally symmetric, and positive-definite assertions.
They expose no diagonal-dominance metadata. Extremely unrepresentable coefficients
are rejected rather than yielding nonfinite matrices.

Additional generators cover Helmholtz, transport, mixed systems, elasticity,
meshes, and electric grids. Their matrix properties and parameter sets are
described in the [generated matrix catalog](docs/generated-matrices.md).

`manufactured_constant_columns` takes `first_value` and `column_increment` and
constructs each parent-only reference column before forming its RHS. Seeds are
required for provenance but these generators are deterministic. Different RHS
widths are separate cases. There is one block solve per fresh instance, without
factor reuse between repetitions.

## Solver API and external repositories

[solver.h](include/solver_benchmark/solver.h) defines the adapter contract:

```cpp
setup(matrix, options);  // Bind one immutable matrix for this instance's lifetime.
pre_analyze();           // Convert input to the backend's representation.
analyze();               // Symbolic analysis and ordering.
numeric();               // Factorization or preconditioner construction.
solve(rhs, solution);    // Rank-two mdspan blocks: (rows, RHS count).
```

Each instance owns its workspace and backend handles. Borrowed inputs last through
destruction and cannot be modified. Each phase must synchronize all its backend
work before returning. Any phase may decline an unsupported input/configuration;
algorithmic failure is a separate result. An absent preparation/analysis/numeric
operation may return `not_applicable`; solve must actually produce an answer.

Inputs are full canonical CSR with `double` values, int32 column IDs and matrix
order, and int64 offsets/entry counts. Dense blocks are packed column-major on the
wire. `layout_stride` mdspan views expose their strides locally. No C++ object,
pointer, allocator, or synchronization primitive crosses the process boundary.
Properties use `yes`, `no`, or `unknown`; positive definiteness and symmetry are
separate assertions. The framework does not prove expensive supplied assertions.

`complex_interleaved` encodes each complex `a+ib` as `[a,-b; b,a]` with vectors
`[Re0,Im0,Re1,Im1,...]`. The worker validates the numeric block identity. Real
backends may solve it directly; native complex adapters can reconstruct it during
pre-analysis and pack/unpack dense vectors during solve. The built-in Laplacian
complex expansion has zero imaginary coefficients; arbitrary valid complex
operators are accepted by the transport, but no non-real generator ships yet.

Build and install the standard worker SDK with:

```sh
cmake --install build --prefix /absolute/path/to/sdk
cmake -S examples/external_worker -B build-external \
    -DCMAKE_PREFIX_PATH=/absolute/path/to/sdk
cmake --build build-external
```

External adapters use `find_package(SolverBenchmark CONFIG REQUIRED)` and link
`SolverBenchmark::worker`. They supply a factory and identity to
`solver_benchmark::worker_main`; see the [external worker example](examples/external_worker).
The provided SDK uses the mdspan backport consistently across worker and adapter.
Backend libraries are linked only into the worker. An independently implemented
worker may use another language/runtime if it follows the [wire protocol](docs/protocol.md).
Only local Linux process transport is currently implemented.

## Profiles, provenance, and artifacts

The [JSON Schema](schemas/benchmark-profile.schema.json) and
[profile specification](docs/benchmark-profile.md) define the complete recipe:
generators/parameters/seeds, RHS blocks, solver options, and ordered measurement
passes. The [deployment JSON](examples/deployment.json) maps solver IDs to an
executable, argument array, and environment overrides. An optional
`runtime_dependencies` object maps library labels to files whose content hashes
are included in the software manifest. Relative executable paths
are resolved against that deployment file. No shell command interpolation occurs.

Artifacts include the original profile, RFC 8785 canonical profile bytes,
software version manifest, host/execution metadata, actual input arrays, reference
solutions, per-execution results, streamed `results.jsonl`, and summary counts.
The profile, software manifest, and delivered inputs have separate SHA-256 hashes.
Names/options/pass ordering are part of the recipe; software versions and host
paths are not. A changed generator implementation can therefore retain the recipe
hash while changing its version and possibly its input hash.

The version manifest records Python source and dependency versions, schema digest,
worker identity and executable digest. It does not yet fingerprint every shared
library loaded by external backends. List external backend/runtime libraries in deployment `runtime_dependencies`
and use controlled runtime environments when comparing external workers. Results retain
threading settings and affinity, but do not dump all inherited environment values.
Profiles with different acceptance tolerances should not be pooled as equivalent
accuracy comparisons. GCP Batch submission is deliberately deferred; the CLI and
self-contained result directory are suitable entry/output boundaries for that work.

## Timing, correctness, and failure handling

Each `(case, solver, pass)` uses a fresh process. Warm-ups and measured repetitions
create fresh solver instances. Output starts at zero; no factors or initial guesses
carry over. Setup includes construction, and teardown is reported separately.
Core time is `analysis + numeric + solve`; total solver time adds setup and
pre-analysis. These sums exclude IPC, parent validation, and output initialization.
Whole-process wall/CPU statistics are separate. CPU timing includes all worker
threads. Max RSS is a lifetime high-water mark including warm-up, not a phase peak.

The parent blocks on events while the worker runs, with a deadline covering
handshake, collector setup, trials, acknowledgment waits, and teardown. It drains
bounded logs, handles crash/timeout/protocol failure, and reaps workers before
continuing. A matching terminal message and exit status are both required.

Every successful solve is checked per RHS against original A/B using binary64
products, `math.fsum` accumulation, and scaled L2 norms. Acceptance requires
`||b-Ax||₂ <= atol + rtol*||b||₂` for **every** column, with finite verification
arithmetic. Reports retain backward/reference errors and tagged nonfinite
diagnostics. The worker never receives reference solutions. Raw failed/warm-up
samples are retained; accepted measured samples are summarized separately.

## Optional instrumentation

A baseline pass performs ordinary timing/resource accounting. A trace pass runs
separately with its own warm-ups and never contributes to baseline summaries.
`linux.phase_resources` works without perf/BPF privileges: worker-side phase and
teardown markers, minor/major faults and context-switch deltas, and process
RSS/thread snapshots. These are boundary observations, not allocation traces or
exact within-phase memory peaks. Collector overhead affects the trace pass.

Optional `linux.vm_events`, `linux.thread_events`, and `linux.page_faults` use
perf recording when the host has a suitable perf executable, event support, and
permission. The parent arms collection before releasing the worker's start
barrier. Raw artifacts and worker monotonic timestamps permit correlation;
unsupported configurations are explicit results, never silent fallback. There is
no BPF collector implementation yet. Event-level collection has not been verified
on a host where perf is unavailable. Partial/lost capture must not be interpreted
as complete telemetry. See the protocol for collector semantics and limitations.

## Development checks

CTest runs numerical, profile, worker-protocol, supervisor, and integration tests.
Fixtures exercise unsupported input, wrong answers, exceptions, process crashes,
timeouts, invalid protocol messages, and separate tracing passes. GitHub Actions
builds on Linux x86_64 and ARM64; CI results establish coverage beyond the local
machine. Functional worker code is in `src/`; parent orchestration is in
`python/solver_benchmark/`. The original mockup has been replaced.

Additional self-contained physics and synthetic electric-grid generators are
listed in [Generated physics matrices](docs/generated-matrices.md), with a
ready-to-run profile in [examples/generated-physics-profile.json](examples/generated-physics-profile.json).

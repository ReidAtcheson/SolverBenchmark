# Serializable benchmark profiles — version 1.0

A benchmark profile is a portable JSON experiment recipe. It specifies what to
generate and measure without embedding generator code, backend libraries, host
paths, or software versions. The [JSON Schema](../schemas/benchmark-profile.schema.json)
and [example profile](../examples/benchmark-profile.json) define the implemented
local Linux runner. The schema's `.invalid` identifier is a stable identifier,
not a service to contact; validators load the packaged schema. Earlier PR drafts
were not released compatibility contracts.

The installed CLI validates/materializes a profile or executes it with a separate
deployment mapping:

```sh
solver-benchmark validate examples/benchmark-profile.json
solver-benchmark run examples/benchmark-profile.json \
    --deployment examples/deployment.json --output results/laplacians
```

`validate` checks the schema, semantic rules, generator parameters, and generated
inputs, then prints the canonical profile hash. `run` writes a new output directory
and refuses to overwrite one that exists. `--max-entries` limits each case's CSR
entry count and dense element count before allocation; its default is 5,000,000.
This local limit is recorded in the execution record and does not alter the recipe.

## Recipe and execution

All top-level fields are required, and unknown fields are rejected. Version
`"1.0"` identifies this profile schema, independently of the worker protocol and
C++ adapter API versions. A breaking change needs a new schema version. Every
effective setting is explicit; neither the parent nor a generator may silently
insert defaults after the profile is hashed. A future convenience authoring tool
may expand defaults or parameter sweeps, but it must save and hash the resulting
complete profile before executing it.

| Field | Meaning |
| --- | --- |
| `name` | Human-readable experiment name, included in the profile hash. |
| `cases` | Ordered materialization recipes, each with a unique `id`. |
| Case `generator` | Stable generator `id`, full `parameters` object, and explicit decimal-string `seed`. |
| Case `rhs` | RHS generator recipe and positive `count`; dense shape is `(delivered matrix order, count)`. |
| Case `data_format` | Wire/storage format; initially `csr-f64-i32-i64-le`. |
| Case `encoding.kind` | `real` or `complex_interleaved`; a complex generator must produce the specified real expansion. |
| `solvers` | Ordered logical solver IDs and explicit common options; executable locations are resolved separately. |
| `passes` | Ordered measurement passes, each with a unique `id`, instrumentation, warm-up and measured repetition counts, and per-worker deadline. |

The parent converts recipe `encoding.kind` to the run descriptor's
`matrix.encoding` string and records the actual `complex_order` for an
interleaved matrix. A recipe requests an encoding; only validated materialized
metadata asserts the resulting matrix obeys it.

Expand the profile in case order, then solver order, then pass order. Launch a
fresh worker for every `(case, solver, pass)` tuple. Each worker runs its own
warm-ups followed by measured repetitions, using fresh solver instances bound to
one matrix. All solvers and passes for a case receive identical materialized
matrix/RHS bytes and mathematical metadata. A case is generated once and retained
or cached for replay, rather than regenerated independently for each pass. There
is no parallel execution in the initial profile semantics.

Different RHS widths are distinct cases in this first profile, as the example
demonstrates. Each trial solves its entire dense block once after factorization;
this schedule does not imply factor reuse across width cases. Future schedules for
repeated solves or numeric refactorization require explicit lifecycle semantics.

`baseline` instrumentation has an empty collector list and records standard
wall/CPU and process resource metrics. `trace` names one or more required
collectors. Unsupported collectors produce an explicit `unsupported_instrumentation`
result; they are never silently omitted or treated as solver unsupported. A failure
does not prevent independent passes from running. Failures or lost data after
activation are `instrumentation_failed`. Baseline timing summaries never combine
traced samples with baseline samples.

The implemented collectors are:

| Collector | Measurements |
| --- | --- |
| `linux.phase_resources` | Worker phase/teardown boundary timestamps, `getrusage` page-fault/context-switch deltas, and procfs RSS/thread-count snapshots. No perf installation is required. |
| `linux.vm_events` | Optional perf recording of `sys_enter_mmap`, `sys_enter_munmap`, and `sys_enter_brk` tracepoints. |
| `linux.thread_events` | Optional perf recording of `sched_process_fork`, `sched_process_exit`, and `sched_switch` tracepoints. |
| `linux.page_faults` | Optional perf recording of the `page-faults` software event. |

The perf collectors require an installed perf with acknowledged control-pipe
support, matching kernel events, and permission to record them. Availability is
checked for the requested pass. No BPF collector is implemented. All trace passes
also receive worker phase observations. Optional perf data and decoded event text
share `CLOCK_MONOTONIC` with phase intervals and can be correlated offline; the
runner does not reconstruct allocation lifetimes or produce a merged event timeline.

## Matrix and RHS generators

The baseline Laplacian generators have strict parameter sets:

| ID | Required parameters |
| --- | --- |
| `laplacian_1d` | `mx`, `dx` |
| `laplacian_2d` | `mx`, `my`, `dx`, `dy` |
| `laplacian_3d` | `mx`, `my`, `mz`, `dx`, `dy`, `dz` |

Counts are positive integers and represent **interior unknowns**. Spacings are
positive finite binary64 values whose inverse squares and resulting diagonal
remain positive/finite. Coordinates are flattened x-fastest: `i + mx*(j + my*k)`.
The matrix represents the standard centered finite-difference **negative Laplacian**
with homogeneous Dirichlet boundary values. Each active axis contributes `2/d²`
to the diagonal and `-1/d²` to each existing neighbor. An absent boundary neighbor
is eliminated as zero; its diagonal contribution remains, including when an axis
has only one interior point. The result is full canonical CSR and asserts numerical
symmetry, structural symmetry, and positive definiteness. It does not advertise
diagonal dominance.

For example, `mx=3, dx=0.5` gives diagonal 8 and adjacent entries -4. Anisotropic
2D/3D grids use separate weights for each spacing. Additional transport, Helmholtz, refined mesh, mixed-physics and electric-grid
generators are specified in the [generated matrix catalog](generated-matrices.md).
Each generator has its own strict parameter set and matrix-property assertions;
Laplacian recipes still reject wavenumbers and other extra parameters.

`encoding.kind: complex_interleaved` expands the generated real operator as a
complex operator with zero imaginary part. It doubles delivered order and stored
entry count but preserves RHS count. The logical complex order remains the original
grid size. Native complex matrices with nonzero imaginary coefficients are supported
by the wire contract, but are not produced by these three generators.

`manufactured_constant_columns` has exactly `first_value` and `column_increment`
parameters. Parent-only reference column j has every delivered real entry equal to
`first_value + j * column_increment`; RHS column j is formed by the delivered matrix.
For complex encoding this sets both the real and imaginary component to that value.
Products use binary64 arithmetic and each row is accumulated with Python `math.fsum`,
then stored as binary64. No reference solution is sent to a worker.

`manufactured_seeded_columns` requires `offset` and positive `amplitude`.
It fills each reference column with `offset + amplitude * (2*u - 1)` from one
SplitMix64 stream, in column-major order. The stream starts from the RHS seed,
adds `0x9E3779B97F4A7C15` before each output, applies the standard xor-shift/
multiply mixing steps, and maps its upper 53 bits to `[0,1)`. Matrix and RHS
seeds are independent. Different columns have different entries; these inputs
exercise more modes than proportional constant columns. RHS formation uses the
same binary64 products and `math.fsum` rule as the constant recipe.

Every recipe requires a canonical uint64 decimal-string seed. Laplacians and
constant RHS recipes ignore it numerically; mesh and grid recipes document their
seed behavior. Changing a seed always changes the recipe hash, even for a
seed-independent generator. Floating-point geometry can differ in its last bits
across platforms; input hashes, rather than seeds alone, identify identical bytes.

## Validation before execution

The JSON Schema checks shape, required fields, basic ranges, instrumentation
variants. A profile may request only tracing; it then has no baseline timing
summary. Additional semantic checks
are mandatory before hashing or execution:

1. Reject duplicate JSON member names, invalid UTF-8/Unicode, NaN/Infinity, and
   values not representable by the agreed binary64 JSON number model. Integer
   quantities requiring exactness beyond `2^53 - 1` use decimal strings, including
   all seeds and iteration limits. Check their uint64 bounds explicitly.
2. Require unique case IDs, solver IDs, and pass IDs within their respective
   arrays. Distinct IDs may intentionally refer to the same generator recipe.
3. Resolve each generator ID to a parameter schema and reject missing/defaulted
   parameters, unknown parameters, and invalid types/ranges. Unknown generators
   are a profile resolution error before any worker starts.
4. Check generated CSR/dense sizes, overflow, address-space limits, property
   consistency, and the declared real/complex encoding. An interleaved
   complex matrix has even delivered order and the specified 2-by-2 block
   relation; RHS and solutions use the same interleaving for each column.
5. Resolve the solver IDs, options, and collector IDs; record all software and
   execution settings. Validate that pass counts and deadlines can be executed
   within implementation resource limits. A local limit is reported, not applied
   as an invisible truncation of the recipe.

JSON numbers follow binary64 parsing and JCS serialization. Parameters needing
an exact decimal mathematical value instead of a binary64 value must use a
generator-defined string representation. No Unicode normalization, parameter
coercion, array sorting, or numeric rounding beyond that agreed JSON number model
is performed as an extra normalization step. Large generator integer parameters
must use documented decimal strings; generic JSON Schema validation alone cannot
detect every generator-specific precision requirement.

## Independent identities for recipe, software, and data

Use SHA-256 with the representation `sha256:` followed by 64 lowercase hexadecimal
digits. A profile hash is computed over the UTF-8 bytes of the **entire validated
profile object** canonicalized by [RFC 8785 JSON Canonicalization Scheme (JCS)](https://www.rfc-editor.org/rfc/rfc8785).
JCS fixes object-member order and number/string serialization while preserving
array order. Whitespace, indentation, and object-key ordering do not change the
hash. Changing a name, parameter, seed, tolerance, requested pass, or array order
does. Use a conforming JCS library; ordinary sorted-key JSON dumping is not a
substitute, particularly for floating-point formatting and Unicode key ordering.

```text
profile_hash = "sha256:" + hex(SHA256(UTF8(JCS(profile))))
version_manifest_hash = "sha256:" + hex(SHA256(UTF8(JCS(version_manifest))))
```

The profile contains no self-referential hash. Save the original JSON and its
canonical bytes alongside the digest. Keep documentation outside the hashed
object unless intentionally represented by a schema field.

The implemented version manifest records orchestrator Python source-file hashes,
the packaged profile-schema hash, generator contract ID, Python version, jsonschema
and RFC8785 package versions, and each worker's `--describe` identity plus executable
SHA-256. Updating an implementation may change this manifest hash while preserving
the profile hash. This permits version-to-version comparisons without assuming that
the generated bytes stayed unchanged.

The separate deployment document has `schema_version: "1.0"` and a `solvers` object
mapping every requested solver ID to `executable`, `arguments`, and `environment`.
Executable paths resolve relative to the deployment file; environment is a string
to string object. The parent inherits its environment, applies these overrides,
and sets OMP/OpenBLAS/MKL/BLIS thread-count variables from `thread_limit` before launch.
The adapter still must honor the requested compute-thread limit.

The execution record captures a run UUID, UTC start time, deployment mapping,
resolved executable paths, local entry limit, host/CPU/kernel/libc information,
available CPU affinity, and profile/version hashes. Each result records effective
threading overrides. The current manifest does not enumerate every dynamically
loaded library or preserve every inherited environment variable; record additional
external runtime configuration when comparing different machines or deployments.
No credentials should be placed in the saved deployment environment.

Each materialized case has an `input_hash` that identifies the delivered inputs
independently of shared-memory addresses, offsets, padding, and software version.
Define its bytes as:

```text
ASCII("SolverBenchmark.input.v1\0")
|| uint64_le(metadata_utf8_byte_count) || UTF8(JCS(input_metadata))
|| uint64_le(row_offsets_byte_count)  || row_offsets_le_bytes
|| uint64_le(column_ids_byte_count)   || column_ids_le_bytes
|| uint64_le(values_byte_count)       || binary64_values_le_bytes
|| uint64_le(rhs_byte_count)          || binary64_rhs_le_bytes
```

`input_hash` is SHA-256 of that byte sequence, using the same `sha256:` text
representation. All byte counts exclude mapping padding. Input metadata is a
canonical object with required keys `data_format`, `matrix`, and `dense` copied
from the validated run descriptor: matrix metadata includes order, nonzeros,
encoding and assertions; dense metadata describes the
packed column-major RHS shape/strides. In this normalized metadata copy, insert
the v1 property keys `symmetric`, `structurally_symmetric`, and `positive_definite`,
using `"unknown"` for any omitted key, without adding a quantitative attribute
object. Preserve supplied unknown property keys and their values
for hashing even if the receiver cannot use them as mathematical assertions.
Do not insert defaults for unknown names. Changing these normalization rules
requires a new input-hash domain version, not receiver-dependent normalization.
`complex_order` is absent for real encoding and required for complex encoding,
as in the run descriptor. Omit case/pass names, transport fields,
handles, options, and provenance from input metadata. Two different recipes can
therefore produce the same input hash. Exact binary64 bits are preserved, so
positive and negative zero intentionally have different input hashes.

Reference solutions are parent-only and excluded from the worker input hash.
The current runner saves them as JSON with the input metadata and arrays. Accuracy
reports identify their arithmetic as binary64 products, `math.fsum` accumulation,
and scaled L2 norms. Infinite diagnostics use JSON objects such as
`{"nonfinite":"+inf"}` rather than invalid JSON numeric literals.

## Output artifacts and results

The output directory contains:

- `profile.json` and `profile.canonical.json`: parsed recipe and its JCS bytes.
- `version-manifest.json` and `execution.json`: software identity and run occurrence.
- `inputs/<case>/`: four little-endian `.bin` arrays, `metadata.json`, and the
  parent-only `reference.json`, `generation.json`, and `identity.json` (input and
  reference hashes).
- `runs/<case>/<solver>/<pass>/result.json`: process resources, raw phase/teardown
  records, per-RHS validation, outcome, provenance hashes, and accepted measured
  timing samples. `stdout.log`/`stderr.log` are bounded captured worker logs.
- `phase-trace.json` for trace passes; perf passes can additionally retain
  `perf.data`, `perf-events.txt`, and collector/decoder logs.
- `results.jsonl`: a flushed result per completed execution, retaining prior
  results if the overall run is interrupted; `summary.json` counts final outcomes.

The run UUID distinguishes repeated executions of identical inputs. Timings and
machine-specific details are not folded into the profile hash. The current runner
records output, reference, and trace content digests alongside artifact paths. Hashes establish content identity, not trust in a generator's
assertions or correctness of a solver's answer.

The CLI exits zero when every execution is successful or explicitly unsupported,
one when an execution fails, and two for a configuration or orchestration error.
Inspect outcome counts: a zero exit can include unavailable requested instrumentation.

Deployment solver entries may additionally specify `runtime_dependencies`, an
object mapping descriptive labels to file paths (resolved against deployment
JSON). Their SHA-256 hashes enter the software version manifest independently
of executable hashes. This supports externally linked solver libraries; automatic
transitive shared-library discovery is not yet performed.

# Serializable benchmark profiles — draft 1.0

A benchmark profile is a portable JSON experiment recipe. It specifies what to
generate and measure without embedding generator code, backend libraries, host
paths, or software versions. The [JSON Schema](../schemas/benchmark-profile.schema.json)
and [example profile](../examples/benchmark-profile.json) make this draft concrete.
No generator registry, profile loader, canonicalizer, or execution engine is
implemented yet. The schema's `.invalid` identifier is a stable draft identifier,
not a service to contact; validators load the schema from this repository.

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
this draft does not imply factor reuse across width cases. Future schedules for
repeated solves or numeric refactorization require explicit lifecycle semantics.

`baseline` instrumentation has an empty collector list and records the standard
wall/CPU and process resource metrics. `trace` names one or more required
collectors; their definitions describe precisely which OS events and phase
markers to collect. Trace collectors may be implemented with perf, BPF, or other
platform services. The collector names in the example are proposed registry
identifiers, not installed tools. Unsupported collectors produce an explicit
`unsupported_instrumentation` result; they are never silently omitted or treated
as solver unsupported. A failure does not prevent independent passes from running.
Failures or lost data after activation are `instrumentation_failed`.
Baseline timing summaries never combine traced samples with baseline samples.

The example generator IDs are also proposed, not existing implementations:

- `laplacian_1d` produces a full tridiagonal CSR matrix of the specified `order`,
  with the supplied constant `diagonal` and `off_diagonal` entries.
- `manufactured_constant_columns` creates parent-only reference column j with
  every entry equal to `first_value + j * column_increment`, then forms RHS
  column j by multiplying by the delivered matrix, with binary64 output. A
  generator implementation must document its accumulation/rounding algorithm.
- Both examples are deterministic and ignore the seed numerically, but require
  an explicit seed. Stochastic generators must identify their random algorithm
  as part of their generator contract and version manifest.

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
4. Check generated CSR/dense sizes, overflow, address-space limits, property and
   attribute consistency, and the declared real/complex encoding. An interleaved
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

The separate version manifest identifies the actual implementations used:
generator IDs mapped to source revision/content digest and RNG algorithm version;
solver IDs mapped to executable digest, adapter/backend versions, compiler and
runtime dependencies; orchestrator/worker/protocol/schema implementations; and
trace collector implementations. Pin dependencies or capture enough identity to
detect differences. Dirty source trees need a content digest, not only a Git SHA.
Updating an implementation may change this manifest hash while preserving the
profile hash. That makes version-to-version comparisons of one recipe possible;
it does not assume the generated bytes stayed unchanged.

The resolved execution record separately captures executable paths/arguments,
effective environment, host/CPU/kernel, affinity/thread policy, collector
availability/settings, time, and run ID. Any solver or collector configuration
with behavioral effects must be explicit in the recipe's supported options or in
this recorded resolution; unrecorded ambient settings are not reproducible. A
comparison must check this execution record as well as hashes. No credential or
secret needs to be stored in a public provenance record.

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
encoding, assertions, and quantitative attributes; dense metadata describes the
packed column-major RHS shape/strides. In this normalized metadata copy, insert
the v1 property keys `symmetric`, `structurally_symmetric`, `positive_definite`,
`diagonally_dominant`, and `strictly_diagonally_dominant`, using `"unknown"` for any omitted key,
and insert `attributes.row_diagonal_dominance_margin: null` when that attribute
is absent. Preserve supplied unknown property/attribute keys and their values
for hashing even if the receiver cannot use them as mathematical assertions.
Do not insert defaults for unknown names. Changing these normalization rules
requires a new input-hash domain version, not receiver-dependent normalization.
`complex_order` is absent for real encoding and required for complex encoding,
as in the run descriptor. Omit case/pass names, transport fields,
handles, options, and provenance from input metadata. Two different recipes can
therefore produce the same input hash. Exact binary64 bits are preserved, so
positive and negative zero intentionally have different input hashes.

Reference solutions are parent-only and excluded from the worker input hash.
When supplied, preserve them with a separate reference-artifact digest and the
verification arithmetic/policy so accuracy results can be reproduced. Record
each output solution digest separately if retaining result artifacts.

The final run record joins `profile_hash`, `version_manifest_hash`, each
`input_hash`, case/solver/pass IDs, execution metadata, accuracy/timing results,
and trace artifact digests. A UUID identifies the individual run occurrence;
identical experiment hashes do not collapse repeated runs into one result.
Neither timings nor machine-specific details are folded into the profile hash.
Hashes establish content identity, not trust in a generator's assertions or
correctness of a solver's answer.

# Local worker protocol — version 1.0

This is the language-neutral protocol implemented by the Python parent in
`python/solver_benchmark/supervisor.py` and the C++ worker in `src/worker.cpp`.
Nothing on the wire depends on `solver_t`, `std::mdspan`, a C++ enum/struct, or a
particular libc. The adapter-facing entry point is declared in
`include/solver_benchmark/worker.h`; the earlier control-flow sketches have been
replaced by the runtime. Earlier PR drafts were not released
compatibility contracts.

A **data format** specifies the representation of arrays exchanged with a worker.
A [benchmark profile](benchmark-profile.md) specifies the complete experiment:
generator recipes, solver configurations, repetition schedules, and measurement
passes. The two are independently versioned concepts.

## Transport and bootstrap

The initial transport is local Linux: a bidirectional Unix stream socket for
control, an immutable shared-memory input object, and a separate writable output
object. Each `(case, solver, measurement pass)` launches a **fresh executable
image**, never backend code in a fork-only continuation. The worker can be built
and installed elsewhere. Launch arguments select inherited descriptors (the actual numbers are selected
by the parent and need not be 3, 4, and 5):

```text
/external/path/solver-worker --sb-control-fd=3 --sb-input-fd=4 --sb-output-fd=5
```

The worker also accepts `--describe`, which prints the same worker identity
object used by hello and exits without shared mappings or solver construction.
The parent uses this command when resolving the software manifest. Backend-specific
arguments must not shadow the standardized `--sb-*` descriptor flags.

The parent makes only these intended descriptors available to the worker. It
owns object lifetime and closes handles after reaping the child. Input is frozen
against writes and resizing before launch (for example a sealed Linux memfd);
the worker maps it read-only. Output size is fixed/sealed against resizing, but
the worker can map its contents writable. The parent holds an independent
original problem for verification. There are no raw pointers, mutexes, atomics,
condition variables, C++ objects, or allocator state in shared storage.

The child uses stdout/stderr only for logs; the parent drains/caps them without
blocking control handling. The control socket is exclusively for framed protocol
messages. Missing/invalid descriptors are harness errors. Environment and
threading settings are established before exec and recorded with the case.

Each frame is a **4-byte unsigned little-endian length**, followed by that many
bytes of UTF-8 JSON. Length must be between 1 and 1,048,576 bytes. Receivers handle
partial reads/writes and interrupted system calls. EOF mid-frame is a protocol
failure. Duplicate JSON keys, invalid UTF-8, nonfinite JSON numbers, invalid field
types, and out-of-range values are rejected. Sending a frame on a closed socket
must become an error, not kill the parent via SIGPIPE.

For precision across languages, **buffer sizes/offsets and other specified uint64
fields use canonical unsigned decimal JSON strings** (`"0"` or a nonzero digit followed by digits); all buffer
offsets, byte lengths, entry counts, dense strides, and iteration counts use this
rule. Signed CSR offsets are nonnegative values bounded by INT64_MAX. Other
integer fields use JSON numbers restricted to their stated 32-bit ranges.
Timing/tolerance fields are finite nonnegative JSON numbers.

## Versions and data formats

The worker sends `hello` first, before interpreting mapped data. It advertises
supported protocol majors and maximum backward-compatible minors, data formats,
and the instrumentation modes supported by its standardized worker runtime:

```json
{
  "type": "hello",
  "protocols": [{"major": 1, "max_minor": 0}],
  "data_formats": ["csr-f64-i32-i64-le"],
  "instrumentation_modes": ["baseline", "trace"],
  "worker": {
    "name": "example-solver",
    "version": "0.1",
    "build_id": "record-an-executable-or-build-digest",
    "adapter_api_version": 1
  }
}
```

The implemented parent selects protocol 1.0 when the worker advertises major 1
with a nonnegative maximum minor, then sends `run`. The C++ worker accepts 1.0. Lack of a common version/data format is a protocol
compatibility failure, before solver construction. A successful `ready` echoes
the exact selected version/data format. Major changes may break message meaning
or state transitions. Minor changes may add optional fields; unknown optional
object members are ignored. Unknown message types or missing mandatory fields
fail the session. Behavior-changing extensions require explicit future
negotiation, not guessing from an ignored field. v1.0 requires no extensions.

`adapter_api_version` is diagnostic and independent of protocol version. A worker
written without this C++ interface may report it as null. A compiled executable
can continue speaking protocol 1.0 even if its internal adapter API changes.

The only initial data format is `csr-f64-i32-i64-le`: IEEE-754 binary64 values,
signed 32-bit column IDs, signed 64-bit row offsets, little-endian arrays, full
square CSR, and packed column-major RHS/solution blocks. A worker with
incompatible native representation must reject this format; it must not
reinterpret data incorrectly. Additional scalar/index combinations need named
data formats. This is same-host IPC, not a network transport.

## Run descriptor and shared arrays

The parent sends one `run` per worker lifetime. This example describes a 3-by-3
tridiagonal matrix with seven stored entries, two RHS columns, one warm-up, and
three measured repetitions in the baseline pass. Hash values below are
illustrative placeholders of the required shape, not computed example digests:

```json
{
  "type": "run",
  "protocol": {"major": 1, "minor": 0},
  "data_format": "csr-f64-i32-i64-le",
  "case_id": "laplacian-3/example-solver/threads-1",
  "pass_id": "baseline",
  "provenance": {
    "profile_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "version_manifest_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    "input_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  },
  "matrix": {
    "order": 3,
    "nonzeros": "7",
    "encoding": "real",
    "properties": {
      "symmetric": "yes",
      "structurally_symmetric": "yes",
      "positive_definite": "yes"
    }
  },
  "dense": {"rows": 3, "columns": 2, "row_stride": "1", "column_stride": "3"},
  "buffers": {
    "input_bytes": "168",
    "output_bytes": "48",
    "row_offsets": {"byte_offset": "0", "count": "4"},
    "column_indices": {"byte_offset": "32", "count": "7"},
    "values": {"byte_offset": "64", "count": "7"},
    "rhs": {"byte_offset": "120", "count": "6"},
    "solution": {"byte_offset": "0", "count": "6"}
  },
  "instrumentation": {"kind": "baseline", "collectors": []},
  "plan": {"warmup_runs": 1, "measured_runs": 3, "case_timeout_seconds": 300},
  "options": {
    "thread_limit": 1,
    "relative_tolerance": 1e-10,
    "absolute_tolerance": 0,
    "iteration_limit": "10000"
  }
}
```

The example input contains row offsets `[0,2,5,7]`, column IDs
`[0,1,0,1,2,1,2]`, values `[2,-1,-1,2,-1,-1,2]`, and two RHS columns packed as
`[1,0,1,2,0,2]`. Four padding bytes precede values. The corresponding reference
solution block `[1,1,1,2,2,2]` is parent-only.

Required fields and constraints:

| Field | Contract |
| --- | --- |
| `case_id`, `pass_id` | Nonempty UTF-8 strings, each at most 1024 bytes; echoed by every later message. |
| `provenance` | Object containing `profile_hash`, `version_manifest_hash`, and `input_hash`; each is `sha256:` followed by 64 lowercase hexadecimal digits. Definitions and hash inputs are in the benchmark profile document. |
| `matrix.order` | JSON integer in `[1, INT32_MAX]`, always the delivered real matrix order. |
| `matrix.nonzeros` | Decimal string in `[0, INT64_MAX]`, counting delivered CSR entries. |
| `matrix.encoding` | `"real"` or `"complex_interleaved"`; semantics below. |
| `matrix.complex_order` | Required only for complex encoding, forbidden for real encoding; JSON integer satisfying `2 * complex_order == order`. |
| `matrix.properties` | Object mapping property names to `"yes"`, `"no"`, or `"unknown"`; omitted names mean unknown. Definitions are in README. Unknown names carry no assertion to this receiver. |
| `dense.rows` | Equal to `matrix.order`. |
| `dense.columns` | RHS count, a JSON integer in `[1, UINT32_MAX]`. |
| `dense.row_stride`, `dense.column_stride` | uint64 decimal strings; must equal `1` and `dense.rows` respectively in this initial packed column-major format. Strides are in elements, not bytes. |
| `input_bytes`, `output_bytes` | Positive uint64 decimal strings; must match actual handle sizes. |
| Buffer `byte_offset` | uint64 decimal byte offset from the start of its own object. |
| Buffer `count` | uint64 decimal element count, bounded by the data format and address space. RHS and solution counts both equal `rows * columns`. |
| `instrumentation.kind` | `"baseline"` or `"trace"`; applies to this entire process lifetime. |
| `instrumentation.collectors` | Array of distinct nonempty collector identifiers; empty for baseline, nonempty for trace. All must be available; no silent fallback or omission. |
| `warmup_runs` | uint32 JSON integer, at least zero. |
| `measured_runs` | uint32 JSON integer, at least one. |
| `case_timeout_seconds` | Strictly positive finite number; authoritative deadline enforced by parent from launch. |
| `thread_limit` | uint32 JSON integer, at least one. |
| Tolerances | Finite nonnegative numbers; common per-RHS L2 residual acceptance rule in README. |
| `iteration_limit` | Positive uint64 decimal string; only iterative backends use it. |

All listed descriptor, plan, and option fields are required except the conditional
`complex_order` and individual property entries. Input arrays are row
offsets, column IDs, values, and RHS. Solution resides only in the output object.
Regions within an object cannot overlap. Row offsets/values/RHS/solution are
aligned to 8 bytes; columns to 4. Padding is allowed and has no meaning. There is
no native struct header in either object; JSON supplies all metadata. Dense
blocks are reconstructed as local rank-two views; no `mdspan` object is serialized.

Both sides check multiplication/addition overflow, alignment, region bounds,
address-space fit, element counts, CSR invariants, finite values/RHS, and known
property contradictions before exposing typed views. `order + 1`,
`2 * complex_order`, and `rows * columns` are computed in a wider checked type.
Validate offset monotonicity and ranges before indexing column/value storage.
Reject malformed data as a protocol/input error, not `unsupported`.

Diagonal dominance is not part of the property vocabulary. Solvers may determine
it during `pre_analyze`, with its cost reported as preparation outside the core
analysis-through-solve total. Positive definiteness remains a three-state property.
Mathematical proofs of supplied assertions are not required by the transport;
the parent records the generating recipe and implementation identity.

### Interleaved complex encoding

`encoding: "complex_interleaved"` asserts that the real matrix of order `2m`
represents a complex matrix of order `m == complex_order`. A complex entry
`a + i*b` at `(i,j)` corresponds to the real block at rows `(2i,2i+1)` and
columns `(2j,2j+1)`:

```text
[ a  -b ]
[ b   a ]
```

Every block must satisfy this identity numerically; absent CSR entries are zero,
so zero components need not have explicit stored entries. The assertion does not
promise a four-entry stored pattern per complex nonzero. The parent validates
this encoding before publication and the worker validates it before exposing
views. Dimensions must be even; malformed blocks are input errors. RHS and
solution rows are interleaved real/imaginary components for every RHS column.
All properties describe the delivered real matrix,
not an unmarked property of the logical complex matrix.

A real backend may solve these arrays unchanged. A complex backend reconstructs
its native matrix during `pre_analyze`; RHS packing and solution unpacking occur
within solve timing. Every backend returns the same real solution representation.
The parent validates against the delivered real matrix for identical acceptance
semantics. Euclidean residual norms agree with the logical complex system;
componentwise and infinity-norm diagnostics remain explicitly real-system metrics.

## Preparation and instrumentation negotiation

The worker sends `ready` only after validation and successful instrumentation
negotiation. Required fields are `type`, `case_id`, `pass_id`, `provenance`,
`protocol`, `data_format`, and `instrumentation`, echoing the accepted request.
The parent records worker identity/build metadata from hello alongside the resolved
manifest and records requested options verbatim. An adapter must
honor those options or report unsupported during setup. Native stopping criteria
and solver-specific compile/launch configuration accompany the worker's build
metadata/documentation; arbitrary opaque backend options are deferred.

After `ready`, the worker waits for a parent `start` message containing the same
`case_id`, `pass_id`, and `provenance`. The parent sends start only after all
requested collectors are attached and armed, including any dynamically registered
worker markers. Baseline passes use the same barrier without collectors. No
trial, solution reset, or solver construction may occur before start. Collector
attachment failure before start is `unsupported_instrumentation`; the parent
terminates/reaps the waiting worker and records the reason. The deadline covers
this barrier as well as collector attachment. Collectors may miss early process
startup events; their capture start and scope must be recorded explicitly.

The parent resolves collector availability, kernel support, and permissions before
sending run. A missing collector produces parent outcome
`unsupported_instrumentation`, potentially without launching a worker. A worker
that cannot supply the requested marker mode sends `instrumentation_unavailable`
with `case_id`, `pass_id`, `provenance`, and a `message`, then exits with code 4
before ready or any trial. Missing collectors must not silently turn a trace pass
into a baseline pass. Other compatible passes/cases may still run.

Each pass has its own warm-up schedule and fresh process. Baseline and trace pass
results share input/profile/version hashes, but have different pass IDs. The
parent records requested and actually activated collectors and trace artifacts.
An advertised trace mode means the worker can emit phase markers; it does not
assert that every parent-side collector is installed on the host.

Phase timestamps originate **inside the standardized worker**, not the solver
adapter or parent's receive loop. All trace passes attach optional `marker` fields
to phase/teardown begin messages and `trace`/`observations` fields to completed
phase/teardown messages, including warm-ups. No trace fields are required for a
baseline pass. Completed intervals use the actual worker clock samples; a begin
marker alone is only a progress timestamp taken before phase-entry IPC.

The optional completed `trace` object is:

```text
{begin_monotonic_ns: uint64 decimal string,
 end_monotonic_ns: uint64 decimal string,
 pid: integer, tid: integer, clock: "CLOCK_MONOTONIC"}
```

Begin-message `marker` uses `monotonic_ns` instead of begin/end timestamps and
has the same PID/TID/clock fields. Case/pass/provenance/trial/phase identity comes
from the containing message. Teardown uses its message type rather than a phase
field. IPC and resource snapshots occur outside the reported clock interval.
The trace pass still perturbs execution and its durations never enter baseline
summaries. A crash leaves the active phase incomplete; no end timestamp is invented.

`linux.phase_resources` requires completed `observations` containing:

| Field | Meaning |
| --- | --- |
| `source`, `scope` | `getrusage+procfs`, `worker_process`. |
| `minor_page_faults`, `major_page_faults` | Differences in process `getrusage(RUSAGE_SELF)` counters at phase boundaries. |
| `voluntary_context_switches`, `involuntary_context_switches` | Process counter differences at phase boundaries. |
| `rss_before_bytes`, `rss_after_bytes` | `/proc/self/status` VmRSS snapshots; null if unavailable. |
| `threads_before`, `threads_after` | `/proc/self/status` thread-count snapshots; null if unavailable. |
| `memory_measurement` | `boundary_snapshots_not_peak`. |

These observations are currently emitted for every trace pass. Snapshot work may
itself contribute to resource counter deltas. Before/after RSS and thread counts
do not reconstruct changes within the phase or establish a phase peak. Lifetime
max RSS is still collected separately at child reap.

The optional perf collectors and exact events are listed in the
[profile specification](benchmark-profile.md). The parent launches `perf record`
attached to the worker, with inherited events, `--clockid mono`, and events disabled
initially. It enables collection through perf's control pipe and waits for its ACK
before sending the worker start message. This requires a compatible perf/kernel
and sufficient access to the requested events. It preserves `perf.data`, decoded
`perf-events.txt`, and logs; decode errors or detected lost events mark collection
incomplete. Worker intervals and perf events can be joined offline on monotonic
time and process/thread identity. No BPF backend, automatic event-to-phase reducer,
or allocation-lifetime reconstruction is implemented.

The parent writes `phase-trace.json` with the phase/teardown records and provenance.
Missing required phase observations or incomplete marker pairs mark instrumentation
incomplete. OS mapping events are not heap allocation counts, and differences in
lifetime max RSS are not per-phase peaks. Collectors remain outside the solver's
dependency graph and are stopped during the parent's cleanup path.

## Trial messages and state machine

Every post-run message contains matching `case_id`, `pass_id`, and `provenance`.
A trial is an object `{"kind":"warmup"|"measured", "index":uint32}`. Indices begin
at zero within each kind. All warm-ups precede measured trials; no reordering,
repetition, or unrequested trial is allowed.

| Direction/type | Additional required fields and effect |
| --- | --- |
| parent `start` | After ready, once only; releases the worker to begin trials after collectors are armed. |
| worker `trial_begin` | `trial`; resets all solution columns to zero outside phase timing. |
| worker `phase_begin` | `trial`, `phase`; phase is `setup`, `pre_analyze`, `analysis`, `numeric`, or `solve`. |
| worker `phase_end` | `trial`, `phase`, `status`, `message`, `iterations`, `times`; completes exactly the current phase. |
| worker `teardown_begin` | `trial`; begins solver destruction after the last reached phase. |
| worker `teardown_end` | `trial`, `times`; destruction completed. |
| worker `trial_complete` | `trial`, `outcome`, `solution_ready`; output belongs to this trial until ACK. |
| parent `trial_ack` | `trial`; preceding output has been consumed and may be overwritten. |
| worker `complete` | `outcome`; terminal pass execution status, followed by process exit. |
| worker `error` | `message`; optional harness diagnostic in place of normal completion, followed by exit 2. |

`status` is `success`, `not_applicable`, `unsupported`, or `failed`.
`not_applicable` is legal only for setup/pre_analyze/analysis/numeric. `message`
is a UTF-8 string, possibly empty. `iterations` is a uint64 decimal string or null
when not reported; for a block solve it is the backend's block iteration count,
not a fabricated sum of per-column counts. Adapters document that convention.
`times` is null for a warm-up; for a measured phase/teardown it is
`{"wall_seconds":number,"cpu_seconds":number}` with finite nonnegative numbers.
The worker samples monotonic wall time and process user+system CPU across all
threads, not the calling thread alone. IPC is outside these clock intervals.
Workers may additionally provide `system_cpu_seconds`, a finite nonnegative
process-wide kernel CPU delta from `getrusage(RUSAGE_SELF)`. This optional field
is present in the native worker's measured phase and teardown times; older or
external workers may omit it. Its microsecond-resolution samples and adjacent
clock reads introduce boundary overhead into the native worker's phase timings.
Allow for uncertainty on the order of several microseconds, especially for short
phases. This is a rule of thumb, not a measured confidence interval or a
guaranteed bound; scheduling and other effects can cause larger variations.

Setup binds one immutable matrix to the new solver instance; no later callback
accepts another matrix. `pre_analyze` prepares the backend representation, while
analysis performs symbolic/ordering work and numeric performs numerical work.
The parent reports both analysis-through-solve time and the total including setup
and pre-analysis, with teardown separately available. The matrix and dense
storage remain valid through solver destruction.

Phases occur in order. On unsupported/failed, later phases are omitted, and
teardown still runs. Omitted phases are reported by the parent as not run, never
as zero-duration successful samples. `outcome` is `success`, `unsupported`, or
`failed`. `solution_ready` is true exactly when all applicable phases completed
and solve returned success; otherwise false. Teardown must complete before
`trial_complete`. Every trial_complete, including a failed/declined trial, is
acknowledged before the worker sends terminal complete or starts the next trial.

The worker finishes all writes before announcing solution readiness, then waits
for the matching ACK. The parent copies and validates **every RHS column** before
sending ACK and records per-column results plus their worst error. This is the
ownership/synchronization boundary; there is no polling of a shared status flag.
Implementations must use their platform's shared-memory visibility and IPC
ordering guarantees. Waiting never requires a libc-specific shared lock. Backend
subprocesses/background computations that could continue writing solution data
after readiness are outside the initial execution model.

An example measured phase result is:

```json
{
  "type": "phase_end",
  "case_id": "laplacian-3/example-solver/threads-1",
  "pass_id": "baseline",
  "provenance": {
    "profile_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "version_manifest_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    "input_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  },
  "trial": {"kind": "measured", "index": 0},
  "phase": "numeric",
  "status": "success",
  "message": "",
  "iterations": null,
  "times": {"wall_seconds": 0.002, "cpu_seconds": 0.0019}
}
```

Success requires every requested trial in this pass. Early terminal
unsupported/failed requires an acknowledged trial with that outcome and no
subsequent trials. Instrumentation-unavailable is the sole normal pre-trial
decline path, separate from solver unsupported. Accuracy rejection is a
**parent** classification: the worker may complete all trials successfully while
the parent records `accuracy_failed`. Accuracy failure does not stop later trials,
including when first detected during warm-up. Instrumented and baseline passes
are classified and summarized independently.

## Exit, supervision, and resources

| Worker exit | Meaning |
| --- | --- |
| `0` | `complete` outcome success. |
| `1` | `complete` outcome failed. |
| `2` | Harness/configuration/protocol error; no successful completion. |
| `3` | `complete` outcome unsupported. |
| `4` | Valid pre-ready `instrumentation_unavailable` response. |
| signal/other code | Abnormal worker failure. |

A pass is never successful/unsupported from an exit code alone. The parent must
receive a consistent terminal record, validate the entire trial/phase sequence,
and reap a matching normal exit. A crash even **after** complete still fails the
pass. Exit zero with a missing/truncated footer is a protocol failure. Terminal
status/exit disagreement is a protocol failure. A signal is a worker crash;
an expired deadline is a timeout even if the parent then kills the child.

The parent monitors the control socket, logs, deadline, and child exit, and
checks/finalizes collector output; it must not block indefinitely on a frame or ACK. On timeout or
protocol error it terminates the worker process group and reaps the child,
including failures during handshake or teardown. Parent exceptions take the same
cleanup path, with collectors detached/stopped and artifacts finalized as complete
or incomplete under a bounded cleanup policy. Broken connections terminate the
worker rather than leaving an orphan waiting forever. The supervisor uses a
blocking selector (and pidfd where available), plus a sleeping deadline watchdog;
it does not busy-spin while the solver computes. Already completed samples
and last active state survive; unfinished phase times remain absent.

Kernel process resources are collected by the parent at reap (for example Linux
`wait4`), including after a signal. Normalize RSS units to bytes and label its
scope `worker_process_lifetime_including_warmup`, with the pass ID. Unavailable
values, including all child statistics after spawn failure or a prelaunch
instrumentation rejection, are null. Never infer a resettable per-phase memory
peak from process max RSS. Record whole-process wall/user/system CPU separately
from worker-reported phase timings. Reports retain exit code, signal,
deadline/protocol errors, execution and instrumentation completeness, per-RHS
accuracy, provenance hashes, and the classified outcome of each pass.

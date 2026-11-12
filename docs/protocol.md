# Local worker protocol — draft 1.0

This document is the proposed language-neutral schema and state machine. Nothing
here depends on the layout of `solver_t`, `std::span`, a C++ enum/struct, or a
particular libc. The C++ models in `mockup/runtime.h` are illustrative local
objects only. This draft is not implemented or frozen.

## Transport and bootstrap

The initial transport is local Linux: a bidirectional Unix stream socket for
control, an immutable shared-memory input object, and a separate writable output
object. The parent launches a **fresh executable image**, never runs backend code
in a fork-only continuation. The worker can be built and installed elsewhere.
Launch arguments select inherited descriptors, for example:

```text
/external/path/solver-worker --sb-control-fd=3 --sb-input-fd=4 --sb-output-fd=5
```

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

For precision across languages, **64-bit integer fields use canonical unsigned
decimal JSON strings** (`"0"` or a nonzero digit followed by digits); all buffer
offsets, byte lengths, entry counts, and iteration counts use this rule. Signed
CSR offsets are nonnegative values bounded by INT64_MAX. Other integer fields
use JSON numbers restricted to their stated 32-bit ranges. Timing/tolerance
fields are finite nonnegative JSON numbers in seconds or scalar units.

## Versions and profiles

The worker sends `hello` first, before interpreting mapped data. It advertises
supported protocol majors and maximum backward-compatible minors:

```json
{
  "type": "hello",
  "protocols": [{"major": 1, "max_minor": 0}],
  "profiles": ["csr-f64-i32-i64-le"],
  "worker": {
    "name": "example-solver",
    "version": "0.1",
    "build_id": "record-an-executable-or-build-digest",
    "adapter_api_version": 1
  }
}
```

The parent selects a common major and the minimum supported minor for that
major, then sends `run`. Lack of a common version/profile is a protocol
compatibility failure, before solver construction. A successful `ready` echoes
the exact selected version/profile. Major changes may break message meaning or
state transitions. Minor changes may add optional fields; unknown optional object
members are ignored. Unknown message types or missing mandatory fields fail the
session. Behavior-changing extensions require explicit future negotiation,
not guessing from an ignored field. v1.0 requires no extensions.

`adapter_api_version` is diagnostic and independent of protocol version. A worker
written without this C++ interface may report it as null. A compiled executable
can continue speaking protocol 1.0 even if its internal adapter API changes.

The only initial profile is `csr-f64-i32-i64-le`: IEEE-754 binary64 values, signed
32-bit column IDs, signed 64-bit row offsets, little-endian arrays, full square
CSR, and one RHS/solution. A worker with incompatible native representation must
reject this profile; it must not reinterpret data incorrectly. Additional type
combinations need named profiles. This is same-host IPC, not a network transport.

## Run descriptor and shared arrays

The parent sends one `run` per worker lifetime. This example describes a 3-by-3
tridiagonal matrix with seven stored entries, one warm-up, and three measured
repetitions:

```json
{
  "type": "run",
  "protocol": {"major": 1, "minor": 0},
  "profile": "csr-f64-i32-i64-le",
  "case_id": "laplacian-3/example-solver/threads-1",
  "matrix": {
    "order": 3,
    "nonzeros": "7",
    "properties": {
      "symmetric": "yes",
      "structurally_symmetric": "yes",
      "positive_definite": "yes",
      "diagonally_dominant": "yes",
      "strictly_diagonally_dominant": "no"
    }
  },
  "buffers": {
    "input_bytes": "144",
    "output_bytes": "24",
    "row_offsets": {"byte_offset": "0", "count": "4"},
    "column_indices": {"byte_offset": "32", "count": "7"},
    "values": {"byte_offset": "64", "count": "7"},
    "rhs": {"byte_offset": "120", "count": "3"},
    "solution": {"byte_offset": "0", "count": "3"}
  },
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
`[0,1,0,1,2,1,2]`, values `[2,-1,-1,2,-1,-1,2]`, and RHS `[1,0,1]`.
Four padding bytes precede values. A reference solution `[1,1,1]` is parent-only.

Required fields and constraints:

| Field | Contract |
| --- | --- |
| `case_id` | Nonempty UTF-8 string, at most 1024 bytes; echoed by every later message. |
| `matrix.order` | JSON integer in `[1, INT32_MAX]`. |
| `matrix.nonzeros` | Decimal string in `[0, INT64_MAX]`. |
| `matrix.properties` | Object mapping property names to `"yes"`, `"no"`, or `"unknown"`; omitted names mean unknown. Definitions are in README. Unknown names carry no assertion to this receiver. |
| `input_bytes`, `output_bytes` | Positive uint64 decimal strings; must match actual handle sizes. |
| Buffer `byte_offset` | uint64 decimal byte offset from the start of its own object. |
| Buffer `count` | uint64 decimal element count, bounded by the profile and address space. |
| `warmup_runs` | uint32 JSON integer, at least zero. |
| `measured_runs` | uint32 JSON integer, at least one. |
| `case_timeout_seconds` | Strictly positive finite number; authoritative deadline enforced by parent from launch. |
| `thread_limit` | uint32 JSON integer, at least one. |
| Tolerances | Finite nonnegative numbers; common L2 residual acceptance rule in README. |
| `iteration_limit` | Positive uint64 decimal string; only iterative backends use it. |

All listed descriptor, plan, and option fields are required; properties may be
an empty object. Input arrays are row offsets, column IDs, values, and RHS.
Solution resides only in the output object. Regions within an object cannot
overlap. Row offsets/values/RHS/solution are aligned to 8 bytes; columns to 4.
Padding is allowed and has no meaning. There is no native struct header in either
object; the JSON descriptor supplies all metadata.

Both sides check multiplication/addition overflow, alignment, region bounds,
address-space fit, element counts, CSR invariants, finite values/RHS, and known
property contradictions before exposing typed views. `order + 1` is computed in
a wider type, not int32. Validate offset monotonicity and ranges before indexing
column/value storage. Reject malformed data as a protocol/input error, not
`unsupported`. Mathematical property proofs are not required by the transport.

The worker sends `ready` only after validation; its required fields are `type`,
`case_id`, `protocol`, and `profile`. The worker identity/build metadata is captured
from hello. The requested options are recorded verbatim; an adapter must honor
them or report unsupported during setup. Native stopping criteria and any
solver-specific compile/launch configuration must accompany the worker's build
metadata/documentation; arbitrary opaque backend options are deferred.

## Trial messages and state machine

Every post-run message contains the matching `case_id`. A trial is an object
`{"kind":"warmup"|"measured", "index":uint32}`. Indices begin at zero within
each kind. All warm-ups precede all measured trials; no reordering, repetition,
or unrequested trial is allowed.

| Direction/type | Additional required fields and effect |
| --- | --- |
| worker `trial_begin` | `trial`; resets solution to zero outside phase timing. |
| worker `phase_begin` | `trial`, `phase`; phase is `setup`, `analysis`, `numeric`, or `solve`. |
| worker `phase_end` | `trial`, `phase`, `status`, `message`, `iterations`, `times`; completes exactly the current phase. |
| worker `teardown_begin` | `trial`; begins solver destruction after the last reached phase. |
| worker `teardown_end` | `trial`, `times`; destruction completed. |
| worker `trial_complete` | `trial`, `outcome`, `solution_ready`; output belongs to this trial until ACK. |
| parent `trial_ack` | `trial`; preceding output has been consumed and may be overwritten. |
| worker `complete` | `outcome`; terminal case execution status, followed by process exit. |
| worker `error` | `message`; optional harness diagnostic in place of a normal completion, followed by exit 2. |

`status` is `success`, `not_applicable`, `unsupported`, or `failed`.
`not_applicable` is legal only for setup/analysis/numeric. `message` is a UTF-8
string, possibly empty. `iterations` is a uint64 decimal string or null when not
reported. `times` is null for a warm-up; for a measured phase/teardown it is
`{"wall_seconds":number,"cpu_seconds":number}` with finite nonnegative numbers.
The worker samples monotonic wall time and process user+system CPU across all
threads, not the calling thread alone. IPC is outside these clock intervals.

Phases occur in order. On unsupported/failed, later phases are omitted, and
teardown still runs. Omitted phases are reported by the parent as not run, never
as zero-duration successful samples. `outcome` is `success`, `unsupported`, or
`failed`. `solution_ready` is true exactly when all applicable phases completed
and solve returned success; otherwise false. Teardown must complete before
`trial_complete`. Every trial_complete, including a failed/declined trial, is
acknowledged before the worker sends a terminal complete or starts the next trial.

The worker finishes all writes before announcing solution readiness, then waits
for the matching ACK. The parent copies and validates output before sending ACK.
This is the ownership/synchronization boundary; there is no polling of a shared
status flag. Implementations must use their platform's shared-memory visibility
and IPC ordering guarantees. Waiting never requires a libc-specific shared lock.
The initial profile has no backend subprocesses/background computations that
could continue writing the solution after readiness.

An example measured phase result is:

```json
{
  "type": "phase_end",
  "case_id": "laplacian-3/example-solver/threads-1",
  "trial": {"kind": "measured", "index": 0},
  "phase": "numeric",
  "status": "success",
  "message": "",
  "iterations": null,
  "times": {"wall_seconds": 0.002, "cpu_seconds": 0.0019}
}
```

Success requires every requested trial. Early terminal unsupported/failed
requires an acknowledged trial with that outcome and no subsequent trials.
Accuracy rejection is a **parent** classification: the worker may complete all
trials successfully while the parent records `accuracy_failed`. Accuracy failure
does not stop later trials, including when first detected during warm-up.

## Exit, supervision, and resources

| Worker exit | Meaning |
| --- | --- |
| `0` | `complete` outcome success. |
| `1` | `complete` outcome failed. |
| `2` | Harness/configuration/protocol error; no successful completion. |
| `3` | `complete` outcome unsupported. |
| signal/other code | Abnormal worker failure. |

A case is never successful/unsupported from an exit code alone. The parent must
receive a consistent terminal record, validate the entire trial/phase sequence,
and reap a matching normal exit. A crash even **after** complete still fails the
case. Exit zero with a missing/truncated footer is a protocol failure. Terminal
status/exit disagreement is a protocol failure. A signal is a worker crash;
an expired deadline is a timeout even if the parent then kills the child.

The parent monitors the control socket, logs, deadline, and child exit together;
it must not block waiting indefinitely for a frame or ACK. On timeout or protocol
error it terminates the worker process group and reaps the child, including when
failure occurs during handshake or teardown. Parent exceptions take the same
cleanup path. Broken connections terminate the worker rather than leaving an
orphan waiting forever. The parent retains already completed samples and the
last active state; unfinished phase times remain absent.

Kernel process resources are collected by the parent at reap (for example Linux
`wait4`), including after a signal. Normalize RSS units to bytes and label its
scope `worker_process_lifetime_including_warmup`. Unavailable values, including
all child statistics after spawn failure, are null. Never infer a resettable
per-phase memory peak from process max RSS. Record whole-process wall/user/system
CPU separately from worker-reported phase timings. The benchmark report also
keeps exit code, signal, deadline/protocol errors, execution completeness, every
accuracy result, and the final classified case outcome.

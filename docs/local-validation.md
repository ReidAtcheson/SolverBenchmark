# Local validation

The first functional implementation was exercised on an Intel Core i7-1360P
(x86_64 Linux), using GCC 13.3 and Clang 18.1 with libstdc++ 13. Each build passed
44 unittest cases via CTest, including native worker boundaries and actual Eigen
integration. The installed CMake SDK also built the standalone external-worker
example successfully.

The packaged CLI ran `examples/benchmark-profile.json` with Eigen SparseLU:

| Case | Shape | RHS columns | Baseline measured trials | Resource-pass measured trials | Worst relative residual |
| --- | --- | --- | --- | --- | --- |
| 1D | 1000 | 3 | 3 | 2 | 2.96e-15 |
| 2D | 20 x 16 | 3 | 3 | 2 | 1.16e-15 |
| 3D | 8 x 7 x 6 | 3 | 3 | 2 | 1.13e-15 |

Each process also completed one full warm-up. All six executions succeeded;
all returned RHS columns passed independent validation. These are correctness
smoke runs, not a claim of stable performance measurements on an idle machine.
Raw artifacts are generated under the chosen results directory and are not
committed. The recipe, software, and input hashes in those artifacts identify
the exact run rather than relying on these rounded numbers.

Tests cover all dimensions/anisotropic spacings, Dirichlet boundaries, multiple
RHS columns, complex expansion, inaccurate/nonfinite answers, malformed schemas,
canonical hashes, exceptions, unsupported input, crashes, hangs, incomplete or
invalid protocol sequences, buffer validation, and independent tracing passes.

The environment's sandbox denies inherited Unix-socket sends, so functional
process tests were run with that restriction lifted. No alternative transport or
fake solver path was substituted. Python's ensurepip was absent; the available
uv installer populated the repository-local virtual environment.

ARM64 CI is configured but was not executed locally. AMD hardware was not tested.
The perf executable was absent, so event-level collection remains unverified on
this host; its explicit unavailable path was tested. Built-in phase resource
collection was exercised. No GCP jobs were submitted, and no BPF implementation
is included. Helmholtz wavenumber shifting is reserved for a later generator.

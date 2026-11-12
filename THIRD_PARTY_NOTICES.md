# Third-party software

SolverBenchmark's original code is MIT licensed. Dependencies retain their own
licenses; this repository does not relicense them. CMake fetches pinned upstream
source without modifying it. Preserve these notices and dependency license texts
when distributing builds. Modified MPL-covered files must remain available under
MPL-2.0; our separate adapter files remain MIT.

| Dependency | Pinned version/revision | License | Use |
| --- | --- | --- | --- |
| Eigen | 3.4.0, `3147391d946bb4b6c68edd901f2add6ac1f31f8c` | MPL-2.0 for selected modules | SparseLU adapter, built with `EIGEN_MPL2_ONLY` |
| Kokkos mdspan | `b15adf7aa6626b231a1aa01a4d2f8ba558a24527` | Apache-2.0 WITH LLVM-exception | Header-only dense views; no Kokkos runtime |
| nlohmann/json | 3.11.3, `9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03` | MIT | Worker control protocol |
| jsonschema | 4.26.0 | MIT | Parent profile validation |
| rfc8785 | 0.1.4 | Apache-2.0 | Canonical profile/manifest JSON |

Python dependency distributions include their transitive dependency notices.
Eigen's upstream license information: https://libeigen.gitlab.io/pages/licensing_faq/
Eigen x86/ARM support: https://libeigen.gitlab.io/pages/versatility/
The selected SparseLU path does not use optional external SuiteSparse, MKL, or
BLAS libraries. A serial adapter is a portability baseline, not a claim of optimal
multicore performance. Intel, AMD, and ARM builds use the same source; binaries
must be built for the target architecture. Only locally tested hardware is claimed
in result artifacts. No `-march=native` is added by this project.

Eigen's built-in COLAMD implementation retains the University of Florida
1998–2003 notice and original availability information. The SDK/binary installation
includes the full upstream `Eigen_Colamd.h` under `share/SolverBenchmark/licenses/`
to preserve that notice. Pinned Eigen source (including that notice) is available
at https://github.com/eigen-mirror/eigen/tree/3147391d946bb4b6c68edd901f2add6ac1f31f8c
and https://codeload.github.com/eigen-mirror/eigen/tar.gz/3147391d946bb4b6c68edd901f2add6ac1f31f8c .

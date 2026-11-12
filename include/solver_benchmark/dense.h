#pragma once

#include <cstddef>

// Default to the C++23 standard facility. Toolchains without <mdspan> may use
// the header-only Kokkos backport, explicitly selected for the entire worker.
// This choice never affects the wire representation or the parent toolchain.
#if defined(SOLVER_BENCHMARK_USE_KOKKOS_MDSPAN)
#include <mdspan/mdspan.hpp>
#else
#include <mdspan>
#endif

namespace solver_benchmark {
#if defined(SOLVER_BENCHMARK_USE_KOKKOS_MDSPAN)
namespace md = Kokkos;
#else
namespace md = std;
#endif

template <class element_t>
using dense_view_t = md::mdspan<element_t, md::dextents<std::size_t, 2>, md::layout_stride>;

// Shape is (rows, RHS count); row/column strides count elements, not bytes.
// Runtime validates dimensions, positive strides, nonoverlap, and storage bounds
// BEFORE constructing views. v1 wire blocks are packed column-major (1, rows).
} // namespace solver_benchmark

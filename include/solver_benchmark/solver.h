#pragma once

// Draft, header-only adapter interface. No process or transport dependencies.
// C++20 is required for std::span. These types NEVER form a wire ABI.

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <span>
#include <string>

namespace solver_benchmark {

inline constexpr std::uint32_t adapter_api_version = 1;

using scalar_t = double;
using index_t = std::int32_t;   // Row/column IDs and square matrix order.
using offset_t = std::int64_t;  // CSR offsets and number of stored entries.

static_assert(sizeof(scalar_t) == 8 && std::numeric_limits<scalar_t>::is_iec559);
static_assert(sizeof(index_t) == 4 && sizeof(offset_t) == 8);

enum class property_state_t : std::uint8_t { unknown, yes, no };

enum class matrix_property_t : std::uint8_t {
    symmetric,
    structurally_symmetric,
    positive_definite,
    diagonally_dominant,
    strictly_diagonally_dominant,
    count
};

class matrix_properties_t {
public:
    constexpr property_state_t get(matrix_property_t property) const noexcept {
        return states_[static_cast<std::size_t>(property)];
    }

    constexpr void set(matrix_property_t property, property_state_t state) noexcept {
        states_[static_cast<std::size_t>(property)] = state;
    }

private:
    // Zero initialization means unknown, never false.
    std::array<property_state_t,
        static_cast<std::size_t>(matrix_property_t::count)> states_{};
};

struct matrix_info_t {
    index_t order = 0;
    offset_t nonzeros = 0;
    matrix_properties_t properties;
};

// A borrowed, immutable view of a validated square CSR matrix, with zero-based
// offsets/columns, strictly increasing columns within each row, no duplicates,
// and full storage (no implicit symmetric triangle). Explicit zeros are allowed.
// The worker validates the transport and CSR invariants BEFORE creating a view.
// Copies of this view borrow the same storage. Storage lasts until AFTER solver
// destruction; adapters may retain a copy, but may not modify its input arrays.
class csr_matrix_view_t {
public:
    constexpr csr_matrix_view_t(matrix_info_t info,
        std::span<const offset_t> row_offsets,
        std::span<const index_t> column_indices,
        std::span<const scalar_t> values) noexcept
        : info_(info), row_offsets_(row_offsets),
          column_indices_(column_indices), values_(values) {}

    constexpr const matrix_info_t& info() const noexcept { return info_; }
    constexpr property_state_t property(matrix_property_t property) const noexcept {
        return info_.properties.get(property);
    }
    constexpr std::span<const offset_t> row_offsets() const noexcept { return row_offsets_; }
    constexpr std::span<const index_t> column_indices() const noexcept { return column_indices_; }
    constexpr std::span<const scalar_t> values() const noexcept { return values_; }

private:
    matrix_info_t info_;
    std::span<const offset_t> row_offsets_;       // order + 1 entries
    std::span<const index_t> column_indices_;    // nonzeros entries
    std::span<const scalar_t> values_;           // nonzeros entries
};

struct solver_options_t {
    std::uint32_t thread_limit = 1;  // Positive maximum for solver compute threads.
    scalar_t relative_tolerance = 1e-10;
    scalar_t absolute_tolerance = 0;
    std::uint64_t iteration_limit = 10000;  // Applies to iterative solvers.
};

enum class phase_status_t {
    success,
    not_applicable,  // No distinct operation; valid for setup/analysis/numeric only.
    unsupported,    // Declines the problem/configuration, not a failed attempt.
    failed          // Attempted operation failed (singularity, no convergence, ...).
};

struct phase_result_t {
    phase_status_t status = phase_status_t::success;
    std::string message;
    std::optional<std::uint64_t> iterations = std::nullopt;
};

// One fresh instance per full warm-up or measured repetition. The same matrix
// is passed to analysis, numeric, and solve. All callbacks must finish their
// work (including asynchronous backend work) before returning. Adapters own
// their conversions, factors, handles, and workspace; allocations count toward
// whichever phase performs them. Exceptions are caught by the worker; process
// crashes/timeouts are handled by the parent. Destruction must not throw.
class solver_t {
public:
    virtual ~solver_t() = default;

    // Metadata only: configure the backend and reject known incompatibilities.
    // Factory construction is included in this phase's timing by the worker.
    virtual phase_result_t setup(const matrix_info_t& matrix,
        const solver_options_t& options) = 0;

    // Full matrix access starts here. Includes format/index conversion, pattern
    // analysis, ordering, and any property checks the adapter chooses to do.
    virtual phase_result_t analyze(const csr_matrix_view_t& matrix) = 0;

    // Numerical factorization, preconditioner construction, or equivalent work.
    virtual phase_result_t numeric(const csr_matrix_view_t& matrix) = 0;

    // One contiguous RHS and solution, both of length matrix.info().order.
    // RHS is immutable; buffers never alias. The runner initializes solution to
    // zero before each repetition, so iterative solvers get a zero initial guess.
    // Input/solution storage remains valid through destruction. Success means
    // the backend completed; the parent independently decides accuracy acceptance.
    virtual phase_result_t solve(const csr_matrix_view_t& matrix,
        std::span<const scalar_t> rhs, std::span<scalar_t> solution) = 0;
};

// Invoked locally in the worker. The factory must construct an independent
// solver; it must not return or reuse factors from another repetition.
using solver_factory_t = std::unique_ptr<solver_t> (*)();

} // namespace solver_benchmark

#pragma once
#include <solver_benchmark/solver.h>
#include <Eigen/SparseLU>
#include <limits>
#include <optional>

namespace solver_benchmark {
// Portable, sequential Eigen SparseLU. No vendor BLAS, OpenMP, or native CPU flags.
class eigen_sparse_lu_t final : public solver_t {
    using sparse_t = Eigen::SparseMatrix<double, Eigen::ColMajor, int>;
    std::optional<csr_matrix_view_t> source_;
    sparse_t matrix_;
    Eigen::SparseLU<sparse_t, Eigen::COLAMDOrdering<int>> solver_;
public:
    phase_result_t setup(const csr_matrix_view_t& matrix, const solver_options_t&) override {
        if (matrix.info().nonzeros > std::numeric_limits<int>::max())
            return {phase_status_t::unsupported, "Eigen adapter requires nnz <= INT32_MAX"};
        source_ = matrix;
        return {};
    }
    phase_result_t pre_analyze() override {
        const auto& a = *source_;
        std::vector<Eigen::Triplet<double, int>> entries;
        entries.reserve(static_cast<std::size_t>(a.info().nonzeros));
        for (index_t i = 0; i < a.info().order; ++i)
            for (offset_t p = a.row_offsets()[i]; p < a.row_offsets()[i + 1]; ++p)
                entries.emplace_back(i, a.column_indices()[p], a.values()[p]);
        matrix_.resize(a.info().order, a.info().order);
        matrix_.setFromTriplets(entries.begin(), entries.end());
        matrix_.makeCompressed();
        return {};
    }
    phase_result_t analyze() override {
        solver_.analyzePattern(matrix_);
        return {};
    }
    phase_result_t numeric() override {
        solver_.factorize(matrix_);
        if (solver_.info() != Eigen::Success)
            return {phase_status_t::failed, solver_.lastErrorMessage()};
        return {};
    }
    phase_result_t solve(dense_view_t<const scalar_t> rhs, dense_view_t<scalar_t> output) override {
        Eigen::MatrixXd b(rhs.extent(0), rhs.extent(1));
        for (std::size_t j = 0; j < rhs.extent(1); ++j)
            for (std::size_t i = 0; i < rhs.extent(0); ++i)
                b(i,j) = rhs.data_handle()[i * rhs.stride(0) + j * rhs.stride(1)];
        Eigen::MatrixXd x = solver_.solve(b);
        if (solver_.info() != Eigen::Success || !x.allFinite())
            return {phase_status_t::failed, "Eigen solve failed or produced nonfinite output"};
        for (std::size_t j = 0; j < output.extent(1); ++j)
            for (std::size_t i = 0; i < output.extent(0); ++i)
                output.data_handle()[i * output.stride(0) + j * output.stride(1)] = x(i,j);
        return {};
    }
};
}

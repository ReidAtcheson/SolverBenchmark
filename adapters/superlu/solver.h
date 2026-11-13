#pragma once
#include <solver_benchmark/solver.h>
#include <slu_ddefs.h>
#include <cmath>
#include <limits>
#include <optional>
#include <vector>

#if SUPERLU_MAJOR_VERSION < 5
#error "SuperLU 5 or newer is required"
#endif

namespace solver_benchmark {
// SuperLU's sequential sparse LU, using a caller-selected BLAS library.
class superlu_t final : public solver_t {
    std::optional<csr_matrix_view_t> source_;
    std::vector<int_t> offsets_, rows_;
    std::vector<double> values_;
    std::vector<int> perm_c_, perm_r_, etree_;
    SuperMatrix a_{}, ac_{}, l_{}, u_{};
    superlu_options_t options_{};
    SuperLUStat_t stat_{};
    bool stat_initialized_ = false;
public:
    ~superlu_t() override {
        if (l_.Store) Destroy_SuperNode_Matrix(&l_);
        if (u_.Store) Destroy_CompCol_Matrix(&u_);
        if (ac_.Store) Destroy_CompCol_Permuted(&ac_);
        if (a_.Store) Destroy_SuperMatrix_Store(&a_);
        if (stat_initialized_) StatFree(&stat_);
    }
    phase_result_t setup(const csr_matrix_view_t& matrix, const solver_options_t&) override {
        if (matrix.info().nonzeros > std::numeric_limits<int_t>::max())
            return {phase_status_t::unsupported, "SuperLU index type cannot represent nnz"};
        source_ = matrix;
        set_default_options(&options_);
        options_.PrintStat = NO;
        StatInit(&stat_);
        stat_initialized_ = true;
        return {};
    }
    phase_result_t pre_analyze() override {
        const auto& a = *source_;
        const int n = a.info().order;
        offsets_.assign(static_cast<std::size_t>(n) + 1, 0);
        rows_.resize(a.info().nonzeros);
        values_.resize(a.info().nonzeros);
        for (auto col : a.column_indices()) ++offsets_[static_cast<std::size_t>(col) + 1];
        for (int i = 0; i < n; ++i) offsets_[i + 1] += offsets_[i];
        auto next = offsets_;
        for (int i = 0; i < n; ++i) {
            for (offset_t p = a.row_offsets()[i]; p < a.row_offsets()[i + 1]; ++p) {
                auto k = next[a.column_indices()[p]]++;
                rows_[k] = i;
                values_[k] = a.values()[p];
            }
        }
        dCreate_CompCol_Matrix(&a_, n, n, static_cast<int_t>(a.info().nonzeros),
            values_.data(), rows_.data(), offsets_.data(), SLU_NC, SLU_D, SLU_GE);
        perm_c_.resize(n); perm_r_.resize(n); etree_.resize(n);
        return {};
    }
    phase_result_t analyze() override {
        get_perm_c(options_.ColPerm, &a_, perm_c_.data());
        sp_preorder(&options_, &a_, perm_c_.data(), etree_.data(), &ac_);
        return {};
    }
    phase_result_t numeric() override {
        GlobalLU_t glu{};
        int_t info = 0;
        dgstrf(&options_, &ac_, sp_ienv(2), sp_ienv(1), etree_.data(), nullptr, 0,
            perm_c_.data(), perm_r_.data(), &l_, &u_, &glu, &stat_, &info);
        if (info != 0) return {phase_status_t::failed, "SuperLU factorization failed (info=" + std::to_string(info) + ")"};
        return {};
    }
    phase_result_t solve(dense_view_t<const scalar_t> rhs, dense_view_t<scalar_t> output) override {
        if (rhs.extent(1) > static_cast<std::size_t>(std::numeric_limits<int>::max()))
            return {phase_status_t::unsupported, "SuperLU RHS count exceeds INT_MAX"};
        const int n = source_->info().order, nrhs = static_cast<int>(rhs.extent(1));
        std::vector<double> b(static_cast<std::size_t>(n) * nrhs), x(b.size());
        for (int j = 0; j < nrhs; ++j)
            for (int i = 0; i < n; ++i)
                b[static_cast<std::size_t>(j) * n + i] = rhs.data_handle()[i * rhs.stride(0) + j * rhs.stride(1)];
        x = b;
        SuperMatrix bm{}, xm{};
        dCreate_Dense_Matrix(&bm, n, nrhs, b.data(), n, SLU_DN, SLU_D, SLU_GE);
        dCreate_Dense_Matrix(&xm, n, nrhs, x.data(), n, SLU_DN, SLU_D, SLU_GE);
        int info = 0;
        dgstrs(NOTRANS, &l_, &u_, perm_c_.data(), perm_r_.data(), &xm, &stat_, &info);
        if (info == 0) {
            std::vector<double> ferr(nrhs), berr(nrhs), scale(n, 1.0);
            char equed = 'N';
            dgsrfs(NOTRANS, &a_, &l_, &u_, perm_c_.data(), perm_r_.data(), &equed,
                scale.data(), scale.data(), &bm, &xm, ferr.data(), berr.data(), &stat_, &info);
        }
        Destroy_SuperMatrix_Store(&bm);
        Destroy_SuperMatrix_Store(&xm);
        if (info != 0) return {phase_status_t::failed, "SuperLU solve/refinement failed (info=" + std::to_string(info) + ")"};
        for (int j = 0; j < nrhs; ++j)
            for (int i = 0; i < n; ++i) {
                double v = x[static_cast<std::size_t>(j) * n + i];
                if (!std::isfinite(v)) return {phase_status_t::failed, "SuperLU produced nonfinite output"};
                output.data_handle()[i * output.stride(0) + j * output.stride(1)] = v;
            }
        return {};
    }
};
}

#include <solver_benchmark/worker.h>

#include <cmath>
#include <csignal>
#include <stdexcept>
#include <string_view>
#include <vector>
#include <unistd.h>
#include <sys/prctl.h>

namespace sb = solver_benchmark;
namespace {
std::string_view behavior = "good";

class fixture_t final : public sb::solver_t {
    std::optional<sb::csr_matrix_view_t> matrix_;
public:
    sb::phase_result_t setup(const sb::csr_matrix_view_t& matrix,
        const sb::solver_options_t&) override {
        matrix_ = matrix;
        if (behavior == "unsupported") return {sb::phase_status_t::unsupported, "fixture declines"};
        if (behavior == "exception") throw std::runtime_error("fixture setup exception");
        return {};
    }
    sb::phase_result_t pre_analyze() override { return {sb::phase_status_t::not_applicable, ""}; }
    sb::phase_result_t analyze() override { return {}; }
    sb::phase_result_t numeric() override {
        if (behavior == "crash") std::raise(SIGSEGV);
        if (behavior == "hang") for (;;) ::pause();
        return {};
    }
    sb::phase_result_t solve(sb::dense_view_t<const sb::scalar_t> b,
        sb::dense_view_t<sb::scalar_t> x) override {
        if (behavior == "wrong") return {}; // Runner zero-initializes x.
        // Small dense Gaussian elimination fixture, deliberately outside the
        // benchmark adapter catalog. Independent reference for protocol tests.
        const auto n = static_cast<std::size_t>(matrix_->info().order);
        if (n > 128) return {sb::phase_status_t::unsupported, "fixture order limit"};
        std::vector<double> a(n * n, 0);
        for (std::size_t i = 0; i < n; ++i)
            for (auto k = matrix_->row_offsets()[i]; k < matrix_->row_offsets()[i + 1]; ++k)
                a[i*n + static_cast<std::size_t>(matrix_->column_indices()[k])] = matrix_->values()[k];
        for (std::size_t j = 0; j < b.extent(1); ++j) {
            auto lu = a;
            std::vector<double> rhs(n);
            for (std::size_t i = 0; i < n; ++i) rhs[i] = b.data_handle()[i*b.stride(0)+j*b.stride(1)];
            for (std::size_t k = 0; k < n; ++k) {
                std::size_t pivot = k;
                for (std::size_t i = k+1; i < n; ++i)
                    if (std::abs(lu[i*n+k]) > std::abs(lu[pivot*n+k])) pivot = i;
                if (lu[pivot*n+k] == 0) return {sb::phase_status_t::failed, "singular fixture input"};
                for (std::size_t c = 0; c < n; ++c) std::swap(lu[k*n+c],lu[pivot*n+c]);
                std::swap(rhs[k],rhs[pivot]);
                for (std::size_t i = k+1; i < n; ++i) {
                    const auto scale=lu[i*n+k]/lu[k*n+k];
                    for (std::size_t c=k+1;c<n;++c) lu[i*n+c]-=scale*lu[k*n+c];
                    rhs[i]-=scale*rhs[k];
                }
            }
            for (std::size_t i=n;i-- >0;) {
                for (std::size_t c=i+1;c<n;++c) rhs[i]-=lu[i*n+c]*rhs[c];
                rhs[i]/=lu[i*n+i];
                x.data_handle()[i*x.stride(0)+j*x.stride(1)]=rhs[i];
            }
        }
        return {};
    }
};
}

int main(int argc, char** argv) {
    // Test crashes must not wait for a host's piped core-dump service.
    if (::prctl(PR_SET_DUMPABLE, 0L) != 0 || ::prctl(PR_GET_DUMPABLE) != 0) return 2;
    for (int i = 1; i < argc; ++i) {
        const std::string_view argument(argv[i]);
        if (argument.starts_with("--fixture=")) behavior = argument.substr(10);
    }
    return sb::worker_main(argc, argv, +[]() -> std::unique_ptr<sb::solver_t> {
        return std::make_unique<fixture_t>();
    }, {"test-fixture", "1", "test-only"});
}

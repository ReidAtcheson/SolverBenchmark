#include <solver_benchmark/worker.h>
#include <optional>
#include <vector>
using namespace solver_benchmark;
class diagonal_t final : public solver_t {
    std::optional<csr_matrix_view_t> matrix;
    std::vector<double> diagonal;
public:
    phase_result_t setup(const csr_matrix_view_t& a, const solver_options_t&) override {
        matrix=a; return {};
    }
    phase_result_t pre_analyze() override { return {phase_status_t::not_applicable, ""}; }
    phase_result_t analyze() override {
        diagonal.resize(matrix->info().order);
        for (index_t row=0; row<matrix->info().order; ++row) {
            for (offset_t p=matrix->row_offsets()[row]; p<matrix->row_offsets()[row+1]; ++p) {
                if (matrix->column_indices()[p] != row && matrix->values()[p] != 0)
                    return {phase_status_t::unsupported,"example supports only diagonal matrices"};
                if (matrix->column_indices()[p] == row) diagonal[row]=matrix->values()[p];
            }
        }
        return {};
    }
    phase_result_t numeric() override {
        for (auto value:diagonal) if (value==0) return {phase_status_t::failed,"singular diagonal"};
        return {};
    }
    phase_result_t solve(dense_view_t<const double> b, dense_view_t<double> x) override {
        for(std::size_t j=0;j<b.extent(1);++j)
            for(std::size_t i=0;i<b.extent(0);++i)
                x.data_handle()[i*x.stride(0)+j*x.stride(1)]=b.data_handle()[i*b.stride(0)+j*b.stride(1)]/diagonal[i];
        return {};
    }
};
int main(int argc,char**argv) {
    return worker_main(argc,argv,+[]()->std::unique_ptr<solver_t>{return std::make_unique<diagonal_t>();},
                       {"external-diagonal","1.0","example"});
}

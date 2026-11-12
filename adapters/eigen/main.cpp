#include "solver.h"
#include <solver_benchmark/worker.h>
int main(int argc, char** argv) {
    return solver_benchmark::worker_main(argc, argv,
        +[]() -> std::unique_ptr<solver_benchmark::solver_t> {
            return std::make_unique<solver_benchmark::eigen_sparse_lu_t>();
        }, {"eigen-sparse-lu", "3.4.0", "Eigen-3147391d;sequential;MPL2-only;" __VERSION__});
}

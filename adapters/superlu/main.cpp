#include "solver.h"
#include <solver_benchmark/worker.h>
#define SB_STRINGIFY_INNER(x) #x
#define SB_STRINGIFY(x) SB_STRINGIFY_INNER(x)
#define SB_SUPERLU_VERSION SB_STRINGIFY(SUPERLU_MAJOR_VERSION) "." SB_STRINGIFY(SUPERLU_MINOR_VERSION) "." SB_STRINGIFY(SUPERLU_PATCH_VERSION)
int main(int argc, char** argv) {
    return solver_benchmark::worker_main(argc, argv,
        +[]() -> std::unique_ptr<solver_benchmark::solver_t> {
            return std::make_unique<solver_benchmark::superlu_t>();
        }, {"superlu", SB_SUPERLU_VERSION, "sequential;double-refinement;" __VERSION__});
}

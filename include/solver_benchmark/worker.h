#pragma once

#include <solver_benchmark/solver.h>

#include <string>

namespace solver_benchmark {

struct worker_identity_t {
    std::string name = "unnamed-solver";
    std::string version = "unknown";
    std::string build_id = "unknown";
};

// Linux standardized worker. Backend adapters only supply a local factory and
// identity. --describe prints the identity JSON without constructing a solver.
// Other arguments specify inherited control/input/output descriptors; see the
// wire protocol. stdout/stderr are logs and never carry framed control messages.
int worker_main(int argc, char** argv, solver_factory_t factory,
    const worker_identity_t& identity = {});

} // namespace solver_benchmark

// CONTROL-FLOW MOCKUP: no solver implementations or backend libraries here.
#include "runtime.h"

namespace solver_benchmark::mockup {

case_report_t benchmark_case(const prepared_problem_t& problem,
    const worker_spec_t& worker, const run_plan_t& plan) {
    // Canonicalize/load/generate before this point. Reject invalid CSR, nonfinite
    // inputs, inconsistent sizes, invalid options, or measured_runs == 0 here.
    validate_problem_and_plan(problem, plan);

    // Copy just A and b into sealed shared input; allocate one separate output
    // vector. The reference solution and original A/b remain parent-owned.
    shared_case_t shared(problem);
    case_report_t report;
    report.process = launch_and_supervise(worker, plan, shared,
        [&](const trial_completed_t& trial) {
            if (trial.outcome != phase_status_t::success || !trial.solution_ready) return;

            // The worker waits for ACK, so later trials cannot overwrite x.
            // Use an independent copy and original inputs for verification.
            const auto solution = shared.copy_solution();
            report.accuracy.push_back({trial.trial,
                validate_solution(problem, solution, plan.solver_options)});
            // Failed accuracy does not erase timings or stop later repetitions.
            // Warm-up validation is retained but excluded from timing summaries.
        });

    // Includes kernel exit/signal/RSS statistics even if the worker died before
    // sending a terminal record. EOF/exit zero alone cannot establish success.
    report.outcome = classify(report, plan);
    return report;
}

// Suite orchestration calls benchmark_case sequentially for each
// (problem, external worker executable, configuration), records the result,
// then continues after a failure/unsupported case. The parent may be rewritten
// in Python or another language without changing the wire protocol.

} // namespace solver_benchmark::mockup

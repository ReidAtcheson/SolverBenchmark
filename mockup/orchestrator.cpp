// CONTROL-FLOW MOCKUP: no solver implementations or backend libraries here.
#include "runtime.h"

namespace solver_benchmark::mockup {

case_report_t benchmark_case(const prepared_problem_t& problem,
    const worker_spec_t& worker, const run_plan_t& plan, const case_context_t& context) {
    // Canonicalize/load/generate before this point. Reject invalid CSR, nonfinite
    // inputs, inconsistent sizes, invalid options, or measured_runs == 0 here.
    validate_problem_and_plan(problem, plan);

    // Copy just A and b into sealed shared input; allocate one separate output
    // block. The reference solution and original A/B remain parent-owned.
    shared_case_t shared(problem);
    case_report_t report;
    report.context = context;
    report.instrumentation = plan.instrumentation;
    report.process = launch_and_supervise(worker, plan, context, shared,
        [&](const trial_completed_t& trial) {
            if (trial.outcome != phase_status_t::success || !trial.solution_ready) return;

            // The worker waits for ACK, so later trials cannot overwrite x.
            // Use an independent copy and original inputs for verification.
            const auto solution = shared.copy_solution();
            report.accuracy.push_back({trial.trial,
                validate_solution(problem, solution.view(), plan.solver_options)});
            // Failed accuracy does not erase timings or stop later repetitions.
            // Warm-up validation is retained but excluded from timing summaries.
        });

    // Includes kernel exit/signal/RSS statistics even if the worker died before
    // sending a terminal record. EOF/exit zero alone cannot establish success.
    report.outcome = classify(report, plan);
    return report;
}

std::vector<case_report_t> benchmark_profile(std::string_view profile_json,
    std::string_view deployment_json) {
    profile_session_t session(profile_json, deployment_json);
    std::vector<case_report_t> reports;
    for (const auto& execution : session.executions()) {
        // Each pass gets a fresh executable, its own warm-ups and repetitions.
        // Input bytes remain identical across compared solvers/passes. Collector
        // negotiation/attachment belongs to the supervisor, not to solver_t.
        reports.push_back(benchmark_case(execution.problem, execution.worker,
            execution.plan, execution.context));
    }
    // Reports join profile/version/input hashes and pass ID; never pool trace
    // timings into baseline summaries. Generation/hashing/IPC remain unimplemented.
    return reports;
}

} // namespace solver_benchmark::mockup

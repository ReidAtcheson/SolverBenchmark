// CONTROL-FLOW MOCKUP: compiles for syntax, but runtime.h has no implementation.
// Build this standardized entry point in each solver's own toolchain/runtime.
#include "runtime.h"

#include <algorithm>
#include <exception>

namespace solver_benchmark::mockup {
namespace {

bool continues(phase_status_t status) {
    return status == phase_status_t::success || status == phase_status_t::not_applicable;
}

int exit_code(phase_status_t status) {
    if (status == phase_status_t::unsupported) return 3;
    return status == phase_status_t::success ? 0 : 1;
}

template <class callback_t>
phase_result_t invoke_phase(worker_session_t& session, trial_id_t trial,
    phase_t phase, callback_t&& callback) {
    // Send progress BEFORE starting the clock. A crash preserves the last phase.
    session.phase_begin(trial, phase);
    const bool measured = trial.kind == run_kind_t::measured;
    const auto start = measured ? sample_clocks() : clock_sample_t{};
    phase_result_t result;
    try {
        result = callback();
        if (phase == phase_t::solve && result.status == phase_status_t::not_applicable) {
            result = {phase_status_t::failed, "solve cannot be not_applicable"};
        }
    } catch (const std::exception& error) {
        result = {phase_status_t::failed, error.what()};
    } catch (...) {
        result = {phase_status_t::failed, "unknown solver exception"};
    }
    const auto times = measured
        ? std::optional<phase_times_t>{elapsed(start, sample_clocks())}
        : std::nullopt;
    // Serialization and IPC never enter the phase clock interval.
    session.phase_end(trial, phase, result, times);
    return result;
}

phase_status_t run_trial(worker_session_t& session, trial_id_t trial,
    solver_factory_t factory, mapped_problem_t problem) {
    session.trial_begin(trial);
    std::fill(problem.solution.begin(), problem.solution.end(), scalar_t{0});
    std::unique_ptr<solver_t> solver;

    auto result = invoke_phase(session, trial, phase_t::setup, [&] {
        solver = factory();  // Allocation/constructor charged to setup.
        if (!solver) return phase_result_t{phase_status_t::failed, "null solver factory result"};
        return solver->setup(problem.matrix.info(), session.plan().solver_options);
    });
    if (continues(result.status)) {
        result = invoke_phase(session, trial, phase_t::analysis,
            [&] { return solver->analyze(problem.matrix); });
    }
    if (continues(result.status)) {
        result = invoke_phase(session, trial, phase_t::numeric,
            [&] { return solver->numeric(problem.matrix); });
    }
    if (continues(result.status)) {
        result = invoke_phase(session, trial, phase_t::solve,
            [&] { return solver->solve(problem.matrix, problem.rhs, problem.solution); });
    }

    // Cleanup is observable and measured separately, without another solver API
    // phase. A crash/hang here must prevent a successful case classification.
    session.teardown_begin(trial);
    const bool measured = trial.kind == run_kind_t::measured;
    const auto start = measured ? sample_clocks() : clock_sample_t{};
    solver.reset();
    session.teardown_end(trial, measured
        ? std::optional<phase_times_t>{elapsed(start, sample_clocks())}
        : std::nullopt);

    const bool solved = result.status == phase_status_t::success;
    session.trial_complete(trial, result.status, solved);
    session.wait_for_trial_ack(trial);  // Parent consumes this exact solution.
    return result.status;
}

} // namespace

int worker_main(int argc, char** argv, solver_factory_t factory) {
    try {
        if (!factory) {
            log_worker_error("null solver factory");
            return 2;  // Harness error before handshake, not a solver trial failure.
        }
        worker_session_t session(argc, argv);  // Handshake + map/validate, untimed.
        const auto& plan = session.plan();
        const auto problem = session.problem();

        for (std::uint32_t i = 0; i < plan.warmup_runs; ++i) {
            const auto outcome = run_trial(session, {run_kind_t::warmup, i}, factory, problem);
            if (outcome != phase_status_t::success) {
                session.complete(outcome);
                return exit_code(outcome);
            }
        }
        for (std::uint32_t i = 0; i < plan.measured_runs; ++i) {
            const auto outcome = run_trial(session, {run_kind_t::measured, i}, factory, problem);
            if (outcome != phase_status_t::success) {
                session.complete(outcome);
                return exit_code(outcome);
            }
        }
        session.complete(phase_status_t::success);
        return 0;
    } catch (const std::exception& error) {
        log_worker_error(error.what());
        return 2;  // Harness error, never a successful/unsupported completion.
    } catch (...) {
        log_worker_error("unknown worker error");
        return 2;
    }
}

} // namespace solver_benchmark::mockup

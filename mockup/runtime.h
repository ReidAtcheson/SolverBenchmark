#pragma once

// DECLARATIONS ONLY: proposed services for the standardized worker and parent.
// These are process-local C++ models, not shared-memory or serialized structs.
// See docs/protocol.md for the independently versioned wire contract.

#include <solver_benchmark/solver.h>

#include <functional>
#include <string>
#include <vector>

namespace solver_benchmark::mockup {

enum class phase_t { setup, analysis, numeric, solve };
enum class run_kind_t { warmup, measured };

struct trial_id_t {
    run_kind_t kind;
    std::uint32_t index;  // Zero-based within this kind.
};

struct run_plan_t {
    std::uint32_t warmup_runs = 1;
    std::uint32_t measured_runs = 1;
    solver_options_t solver_options;
    double case_timeout_seconds = 300;  // Launch through exit, including warm-up.
};

struct phase_times_t {
    double wall_seconds;
    double cpu_seconds;  // User + system across ALL worker threads.
};

struct clock_sample_t {
    std::uint64_t monotonic_nanoseconds;
    std::uint64_t process_cpu_nanoseconds;
};

clock_sample_t sample_clocks();
phase_times_t elapsed(clock_sample_t start, clock_sample_t stop);

struct mapped_problem_t {
    csr_matrix_view_t matrix;
    std::span<const scalar_t> rhs;
    std::span<scalar_t> solution;
};

// Owns the worker's control connection and mappings. Its implementation sends
// hello, negotiates the protocol/profile, validates the run request and mappings,
// and only then exposes views. Close/unmap happens in its destructor.
class worker_session_t {
public:
    worker_session_t(int argc, char** argv);
    ~worker_session_t();
    worker_session_t(const worker_session_t&) = delete;
    worker_session_t& operator=(const worker_session_t&) = delete;

    const run_plan_t& plan() const;
    mapped_problem_t problem();
    void trial_begin(trial_id_t trial);
    void phase_begin(trial_id_t trial, phase_t phase);
    void phase_end(trial_id_t trial, phase_t phase, const phase_result_t& result,
        std::optional<phase_times_t> times);
    void teardown_begin(trial_id_t trial);
    void teardown_end(trial_id_t trial, std::optional<phase_times_t> times);
    void trial_complete(trial_id_t trial, phase_status_t outcome, bool solution_ready);

    // Parent copies/validates this trial's solution before acknowledging. The
    // worker may not overwrite the output mapping or begin another trial first.
    void wait_for_trial_ack(trial_id_t trial);
    void complete(phase_status_t outcome);
};

void log_worker_error(const char* message) noexcept;  // stderr, never protocol stdout.
int worker_main(int argc, char** argv, solver_factory_t factory);

// Parent owns an independent reference problem. expected_solution is optional
// and is NEVER sent to the worker. No backend code is linked into the parent.
struct prepared_problem_t {
    csr_matrix_view_t matrix;
    std::span<const scalar_t> rhs;
    std::optional<std::span<const scalar_t>> expected_solution;
};

struct worker_spec_t {
    std::string executable;  // Arbitrary externally built worker, absolute path.
    std::vector<std::string> arguments;
    std::vector<std::string> environment;  // Explicit KEY=VALUE entries; recorded.
};

// Checked input descriptor + immutable input and writable output handles.
// This owner keeps handles alive until the child is reaped, even on failures.
class shared_case_t {
public:
    explicit shared_case_t(const prepared_problem_t& problem);
    ~shared_case_t();
    shared_case_t(const shared_case_t&) = delete;
    shared_case_t& operator=(const shared_case_t&) = delete;
    std::vector<scalar_t> copy_solution() const;
};

struct trial_completed_t {
    trial_id_t trial;
    phase_status_t outcome;
    bool solution_ready;
};

struct phase_record_t {
    trial_id_t trial;
    phase_t phase;
    bool completed;  // A started-but-unfinished phase has no result/timing.
    std::optional<phase_result_t> result;
    std::optional<phase_times_t> times;  // Also absent for warm-ups.
};

struct teardown_record_t {
    trial_id_t trial;
    bool completed;
    std::optional<phase_times_t> times;
};

struct process_report_t {
    bool launched = false;
    bool timed_out = false;
    std::optional<int> exit_code;
    std::optional<int> terminating_signal;
    std::optional<std::uint64_t> max_rss_bytes;  // Lifetime high-water, includes warm-up.
    std::optional<double> wall_seconds;        // Whole child lifetime.
    std::optional<double> user_cpu_seconds;
    std::optional<double> system_cpu_seconds;
    bool complete_message_received = false;
    std::optional<phase_status_t> reported_outcome;
    std::string harness_error;  // Spawn, negotiation, malformed protocol, etc.
    std::vector<phase_record_t> phases;  // Preserve partial progress after a crash.
    std::vector<teardown_record_t> teardowns;
};

// Launches a fresh executable image, sets up inherited handles, negotiates,
// sends the descriptor and run plan, and monitors control/logs + exit together.
// On trial_complete it invokes the callback, then ACKs; the callback runs outside
// worker timings. The watchdog stays active while the callback runs. A case
// deadline covers EVERY state, including handshake/ACK/teardown. On timeout/
// protocol error/callback exception it terminates the worker
// process group, drains bounded logs, and reaps the child before returning.
// Spawn failures, signals, missing completion, and timeouts are report values.
process_report_t launch_and_supervise(const worker_spec_t& worker,
    const run_plan_t& plan, shared_case_t& shared,
    const std::function<void(const trial_completed_t&)>& on_trial_complete);

struct accuracy_t {
    bool finite = false;
    bool accepted = false;
    double residual_l2 = 0;
    double relative_residual_l2 = 0;
    double normwise_backward_error_inf = 0;
    double componentwise_backward_error = 0;
    std::optional<double> relative_reference_error_l2;
};

accuracy_t validate_solution(const prepared_problem_t& original,
    std::span<const scalar_t> solution, const solver_options_t& policy);
void validate_problem_and_plan(const prepared_problem_t& problem, const run_plan_t& plan);

enum class case_outcome_t {
    success, unsupported, solver_failed, accuracy_failed, worker_crashed,
    timed_out, launch_failed, protocol_error
};

struct accuracy_record_t {
    trial_id_t trial;
    accuracy_t accuracy;
};

struct case_report_t {
    case_outcome_t outcome = case_outcome_t::protocol_error;
    process_report_t process;
    std::vector<accuracy_record_t> accuracy;
};

// Requires matching terminal message + exit code and complete expected trials
// (or a well-formed early failure/decline), not just exit code zero. Process/
// protocol failures take precedence; all accuracy records are retained.
case_outcome_t classify(const case_report_t& report, const run_plan_t& plan);
case_report_t benchmark_case(const prepared_problem_t& problem,
    const worker_spec_t& worker, const run_plan_t& plan);

} // namespace solver_benchmark::mockup

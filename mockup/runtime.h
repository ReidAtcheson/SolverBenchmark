#pragma once

// DECLARATIONS ONLY: proposed services for the standardized worker and parent.
// These are process-local C++ models, not shared-memory or serialized structs.
// See docs/protocol.md for the independently versioned wire contract.

#include <solver_benchmark/solver.h>

#include <functional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace solver_benchmark::mockup {

enum class phase_t { setup, pre_analyze, analysis, numeric, solve };
enum class run_kind_t { warmup, measured };
enum class instrumentation_kind_t { baseline, trace };

struct instrumentation_t {
    instrumentation_kind_t kind = instrumentation_kind_t::baseline;
    std::vector<std::string> collectors; // Empty for baseline; explicit for trace.
};

struct case_context_t {
    std::string case_id;
    std::string solver_id;
    std::string pass_id;
    std::string profile_hash;
    std::string version_manifest_hash;
    std::string input_hash;
};

class instrumentation_unavailable_t : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct trial_id_t {
    run_kind_t kind;
    std::uint32_t index;  // Zero-based within this kind.
};

struct run_plan_t {
    std::uint32_t warmup_runs = 1;
    std::uint32_t measured_runs = 1;
    solver_options_t solver_options;
    instrumentation_t instrumentation;
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
    dense_view_t<const scalar_t> rhs;
    dense_view_t<scalar_t> solution;
};

// Owns the worker's control connection and mappings. Its implementation sends
// hello, negotiates the protocol/data format, validates the run request and mappings,
// then sends ready and waits for parent start (collectors armed) before exposing
// views or executing trials. The barrier applies to baseline passes too.
// Close/unmap happens in its destructor.
// If worker markers are unavailable for a requested trace pass, sends
// instrumentation_unavailable before ready and throws instrumentation_unavailable_t.
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
    // No-op in baseline passes. Emitted in the worker, on the trace clock, with
    // case/pass/trial/phase + PID/TID identity. Never infer trace boundaries from
    // parent receipt times. No backend-specific instrumentation in solver_t.
    void trace_phase_begin(trial_id_t trial, phase_t phase);
    void trace_phase_end(trial_id_t trial, phase_t phase);
    void trace_teardown_begin(trial_id_t trial);
    void trace_teardown_end(trial_id_t trial);
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
    dense_view_t<const scalar_t> rhs;
    std::optional<dense_view_t<const scalar_t>> expected_solution;
};

struct worker_spec_t {
    std::string executable;  // Arbitrary externally built worker, absolute path.
    std::vector<std::string> arguments;
    std::vector<std::string> environment;  // Explicit KEY=VALUE entries; recorded.
};

// Parent-owned snapshot of one complete packed column-major output block.
struct dense_snapshot_t {
    std::vector<scalar_t> values;
    std::size_t rows;
    std::size_t columns;
    dense_view_t<const scalar_t> view() const;
};

// Checked input descriptor + immutable input and writable output handles.
// This owner keeps handles alive until the child is reaped, even on failures.
class shared_case_t {
public:
    explicit shared_case_t(const prepared_problem_t& problem);
    ~shared_case_t();
    shared_case_t(const shared_case_t&) = delete;
    shared_case_t& operator=(const shared_case_t&) = delete;
    dense_snapshot_t copy_solution() const;
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
    std::optional<std::string> instrumentation_error; // No silent fallback.
    std::vector<std::string> trace_artifacts; // Missing/dropped data recorded below.
    bool trace_complete = false; // Meaningful only for requested trace passes.
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
    const run_plan_t& plan, const case_context_t& context, shared_case_t& shared,
    const std::function<void(const trial_completed_t&)>& on_trial_complete);

struct rhs_accuracy_t {
    bool finite = false;
    bool accepted = false;
    double residual_l2 = 0;
    double relative_residual_l2 = 0;
    double normwise_backward_error_inf = 0;
    double componentwise_backward_error = 0;
    std::optional<double> relative_reference_error_l2;
};

struct accuracy_t {
    bool finite = false;
    bool accepted = false; // Every RHS must pass independently.
    double worst_relative_residual_l2 = 0;
    std::vector<rhs_accuracy_t> columns;
};

accuracy_t validate_solution(const prepared_problem_t& original,
    dense_view_t<const scalar_t> solution, const solver_options_t& policy);
void validate_problem_and_plan(const prepared_problem_t& problem, const run_plan_t& plan);

enum class case_outcome_t {
    success, unsupported, solver_failed, accuracy_failed, worker_crashed,
    timed_out, launch_failed, protocol_error, unsupported_instrumentation,
    instrumentation_failed
};

struct accuracy_record_t {
    trial_id_t trial;
    accuracy_t accuracy;
};

struct case_report_t {
    case_context_t context;
    instrumentation_t instrumentation;
    case_outcome_t outcome = case_outcome_t::protocol_error;
    process_report_t process;
    std::vector<accuracy_record_t> accuracy;
};

// Requires matching terminal message + exit code and complete expected trials
// (or a well-formed early failure/decline), not just exit code zero. Process/
// protocol failures take precedence; all accuracy records are retained.
case_outcome_t classify(const case_report_t& report, const run_plan_t& plan);
case_report_t benchmark_case(const prepared_problem_t& problem,
    const worker_spec_t& worker, const run_plan_t& plan, const case_context_t& context);

struct execution_plan_t {
    prepared_problem_t problem;
    worker_spec_t worker;
    run_plan_t plan;
    case_context_t context;
};

// Unimplemented parent service. Validates profile JSON/schema AND semantic rules,
// canonicalizes/hashes it, resolves generator/solver IDs using deployment JSON,
// hashes a separate software manifest, and materializes immutable cases once.
// Owns backing arrays for every borrowed execution plan until destruction.
// Expands cases x solvers x passes in the order specified by the profile.
class profile_session_t {
public:
    profile_session_t(std::string_view profile_json, std::string_view deployment_json);
    ~profile_session_t();
    profile_session_t(const profile_session_t&) = delete;
    profile_session_t& operator=(const profile_session_t&) = delete;
    const std::vector<execution_plan_t>& executions() const;
};

std::vector<case_report_t> benchmark_profile(std::string_view profile_json,
    std::string_view deployment_json);

} // namespace solver_benchmark::mockup

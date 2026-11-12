#include <solver_benchmark/worker.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <bit>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <vector>

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

namespace solver_benchmark {
namespace {
using json = nlohmann::json;
constexpr std::uint32_t max_frame = 1024 * 1024;

[[noreturn]] void invalid(const std::string& message) {
    throw std::invalid_argument(message);
}

void require(bool condition, const std::string& message) {
    if (!condition) invalid(message);
}

[[noreturn]] void os_error(const char* operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

std::uint64_t decimal(const json& value, std::uint64_t maximum = UINT64_MAX) {
    require(value.is_string(), "expected decimal-string integer");
    const auto& s = value.get_ref<const std::string&>();
    require(!s.empty() && (s.size() == 1 || s[0] != '0'), "noncanonical decimal integer");
    std::uint64_t result = 0;
    for (const unsigned char c : s) {
        require(c >= '0' && c <= '9', "invalid decimal integer");
        const auto digit = static_cast<std::uint64_t>(c - '0');
        require(result <= (UINT64_MAX - digit) / 10, "decimal integer overflow");
        result = result * 10 + digit;
    }
    require(result <= maximum, "decimal integer out of range");
    return result;
}

std::uint32_t integer(const json& value, std::uint32_t minimum = 0,
    std::uint32_t maximum = UINT32_MAX) {
    require(value.is_number_integer(), "expected JSON integer");
    if (value.is_number_unsigned()) {
        const auto v = value.get<std::uint64_t>();
        require(v >= minimum && v <= maximum, "JSON integer out of range");
        return static_cast<std::uint32_t>(v);
    }
    const auto v = value.get<std::int64_t>();
    require(v >= minimum && v <= maximum, "JSON integer out of range");
    return static_cast<std::uint32_t>(v);
}

double real(const json& value, bool positive = false) {
    require(value.is_number(), "expected JSON number");
    const double result = value.get<double>();
    require(std::isfinite(result) && (positive ? result > 0 : result >= 0),
        "invalid finite nonnegative number");
    return result;
}

std::string identifier(const json& value) {
    require(value.is_string(), "expected identifier string");
    auto result = value.get<std::string>();
    require(!result.empty() && result.size() <= 1024, "invalid identifier length");
    return result;
}

json read_message(int fd) {
    auto read_all = [&](void* dst, std::size_t size) {
        auto* bytes = static_cast<char*>(dst);
        while (size) {
            const auto n = ::recv(fd, bytes, size, 0);
            if (n < 0) { if (errno == EINTR) continue; os_error("recv"); }
            if (n == 0) throw std::runtime_error("control channel closed mid-message");
            bytes += n;
            size -= static_cast<std::size_t>(n);
        }
    };
    std::array<unsigned char, 4> header{};
    read_all(header.data(), header.size());
    const std::uint32_t length = static_cast<std::uint32_t>(header[0])
        | (static_cast<std::uint32_t>(header[1]) << 8)
        | (static_cast<std::uint32_t>(header[2]) << 16)
        | (static_cast<std::uint32_t>(header[3]) << 24);
    require(length && length <= max_frame, "invalid control frame length");
    std::string bytes(length, '\0');
    read_all(bytes.data(), bytes.size());
    std::vector<std::set<std::string>> keys;
    auto check_keys = [&](int, json::parse_event_t event, json& parsed) {
        if (event == json::parse_event_t::object_start) keys.emplace_back();
        if (event == json::parse_event_t::key) {
            require(keys.back().insert(parsed.get<std::string>()).second, "duplicate JSON key");
        }
        if (event == json::parse_event_t::object_end) keys.pop_back();
        return true;
    };
    auto message = json::parse(bytes, check_keys);
    require(message.is_object(), "control message must be an object");
    return message;
}

void send_message(int fd, const json& message) {
    // Backends may return diagnostics in a locale encoding or truncated UTF-8.
    // Keep the control channel valid even when those diagnostic bytes are not.
    const auto bytes = message.dump(-1, ' ', false, json::error_handler_t::replace);
    require(!bytes.empty() && bytes.size() <= max_frame, "outgoing frame too large");
    const auto length = static_cast<std::uint32_t>(bytes.size());
    const std::array<unsigned char, 4> header{static_cast<unsigned char>(length),
        static_cast<unsigned char>(length >> 8), static_cast<unsigned char>(length >> 16),
        static_cast<unsigned char>(length >> 24)};
    auto write_all = [&](const void* src, std::size_t size) {
        auto* p = static_cast<const char*>(src);
        while (size) {
            const auto n = ::send(fd, p, size, MSG_NOSIGNAL);
            if (n < 0) { if (errno == EINTR) continue; os_error("send"); }
            if (n == 0) throw std::runtime_error("control send made no progress");
            p += n;
            size -= static_cast<std::size_t>(n);
        }
    };
    write_all(header.data(), header.size());
    write_all(bytes.data(), bytes.size());
}

struct mapping_t {
    void* data = MAP_FAILED;
    std::size_t bytes = 0;
    mapping_t() = default;
    mapping_t(const mapping_t&) = delete;
    mapping_t& operator=(const mapping_t&) = delete;
    ~mapping_t() { if (data != MAP_FAILED) ::munmap(data, bytes); }

    void map(int fd, std::uint64_t requested, bool writable) {
        require(requested > 0 && requested <= SIZE_MAX, "mapping size out of range");
        struct stat stat{};
        if (::fstat(fd, &stat)) os_error("fstat shared memory");
        require(stat.st_size >= 0 && static_cast<std::uint64_t>(stat.st_size) == requested,
            "shared memory size mismatch");
        const int seals = ::fcntl(fd, F_GET_SEALS);
        if (seals < 0) os_error("get shared memory seals");
        const int required = F_SEAL_GROW | F_SEAL_SHRINK | (writable ? 0 : F_SEAL_WRITE);
        require((seals & required) == required, "shared memory lacks required seals");
        bytes = static_cast<std::size_t>(requested);
        data = ::mmap(nullptr, bytes, PROT_READ | (writable ? PROT_WRITE : 0), MAP_SHARED, fd, 0);
        if (data == MAP_FAILED) os_error("mmap shared memory");
    }
};

struct region_t { std::size_t offset; std::size_t bytes; };

region_t region(const json& descriptor, std::uint64_t count, std::size_t width,
    std::size_t mapping_bytes) {
    const auto offset = decimal(descriptor.at("byte_offset"), SIZE_MAX);
    require(decimal(descriptor.at("count")) == count, "array count mismatch");
    require(count <= SIZE_MAX / width, "array size overflow");
    const auto bytes = static_cast<std::size_t>(count) * width;
    require(offset % width == 0, "array alignment mismatch");
    require(offset <= mapping_bytes && bytes <= mapping_bytes - offset, "array outside mapping");
    return {static_cast<std::size_t>(offset), bytes};
}

template <class T>
std::span<T> view(mapping_t& mapping, region_t r) {
    auto* base = static_cast<unsigned char*>(mapping.data);
    return {reinterpret_cast<T*>(base + r.offset), r.bytes / sizeof(T)};
}

struct clocks_t { std::uint64_t wall; std::uint64_t cpu; };

std::uint64_t clock_ns(clockid_t id) {
    timespec t{};
    if (::clock_gettime(id, &t)) os_error("clock_gettime");
    return static_cast<std::uint64_t>(t.tv_sec) * 1000000000ULL
        + static_cast<std::uint64_t>(t.tv_nsec);
}

clocks_t clocks() { return {clock_ns(CLOCK_MONOTONIC), clock_ns(CLOCK_PROCESS_CPUTIME_ID)}; }

json duration(clocks_t start, clocks_t stop, bool measured) {
    if (!measured) return nullptr;
    return {{"wall_seconds", static_cast<double>(stop.wall - start.wall) / 1e9},
        {"cpu_seconds", static_cast<double>(stop.cpu - start.cpu) / 1e9}};
}

struct resources_t {
    rusage usage{};
    json rss = nullptr;
    json threads = nullptr;
};

resources_t resources() {
    resources_t r;
    if (::getrusage(RUSAGE_SELF, &r.usage)) os_error("getrusage");
    std::ifstream status("/proc/self/status");
    std::string line;
    while (std::getline(status, line)) {
        std::string_view value;
        if (line.starts_with("VmRSS:")) value = std::string_view(line).substr(6);
        else if (line.starts_with("Threads:")) value = std::string_view(line).substr(8);
        else continue;
        const auto first = value.find_first_not_of(" \t");
        if (first == std::string_view::npos) continue;
        value.remove_prefix(first);
        std::uint64_t number = 0;
        const auto parsed = std::from_chars(value.data(), value.data() + value.size(), number);
        if (parsed.ec != std::errc{}) continue;
        if (line.starts_with("VmRSS:")) r.rss = number * 1024;
        else r.threads = number;
    }
    return r;
}

json observations(const resources_t& a, const resources_t& b) {
    return {{"source", "getrusage+procfs"}, {"scope", "worker_process"},
        {"minor_page_faults", b.usage.ru_minflt - a.usage.ru_minflt},
        {"major_page_faults", b.usage.ru_majflt - a.usage.ru_majflt},
        {"voluntary_context_switches", b.usage.ru_nvcsw - a.usage.ru_nvcsw},
        {"involuntary_context_switches", b.usage.ru_nivcsw - a.usage.ru_nivcsw},
        {"rss_before_bytes", a.rss}, {"rss_after_bytes", b.rss},
        {"threads_before", a.threads}, {"threads_after", b.threads},
        {"boundary_observer_overhead_included", true},
        {"memory_measurement", "boundary_snapshots_not_peak"}};
}

std::string status_name(phase_status_t status) {
    switch (status) {
        case phase_status_t::success: return "success";
        case phase_status_t::not_applicable: return "not_applicable";
        case phase_status_t::unsupported: return "unsupported";
        case phase_status_t::failed: return "failed";
    }
    throw std::runtime_error("invalid adapter phase status");
}

bool continues(phase_status_t status) {
    return status == phase_status_t::success || status == phase_status_t::not_applicable;
}

json identity_json(const worker_identity_t& identity) {
    return {{"name", identity.name}, {"version", identity.version},
        {"build_id", identity.build_id}, {"adapter_api_version", adapter_api_version}};
}

int parse_fd(std::string_view text) {
    int fd = -1;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), fd);
    require(parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size() && fd >= 0,
        "invalid inherited descriptor argument");
    return fd;
}

class session_t {
public:
    int control = -1;
    json request;
    json context;
    mapping_t input, output;
    std::optional<csr_matrix_view_t> matrix;
    std::span<const scalar_t> rhs;
    std::span<scalar_t> solution;
    std::size_t columns = 0;
    solver_options_t options;
    std::uint32_t warmups = 0, repetitions = 0;
    bool trace = false;

    ~session_t() { if (control >= 0) ::close(control); }

    void emit(std::string_view type, json payload = json::object()) {
        for (const auto& [key, value] : context.items()) payload[key] = value;
        payload["type"] = type;
        send_message(control, payload);
    }

    void receive_expected(std::string_view type, const json* trial = nullptr) {
        const auto message = read_message(control);
        require(message.at("type") == type, "unexpected parent message");
        for (const auto& [key, value] : context.items()) {
            require(message.at(key) == value, "parent correlation mismatch");
        }
        if (trial) require(message.at("trial") == *trial, "trial acknowledgment mismatch");
    }

    bool prepare(int argc, char** argv, const worker_identity_t& identity) {
        int input_fd = -1, output_fd = -1;
        for (int i = 1; i < argc; ++i) {
            const std::string_view arg(argv[i]);
            if (arg.starts_with("--sb-control-fd=")) control = parse_fd(arg.substr(16));
            else if (arg.starts_with("--sb-input-fd=")) input_fd = parse_fd(arg.substr(14));
            else if (arg.starts_with("--sb-output-fd=")) output_fd = parse_fd(arg.substr(15));
        }
        require(control >= 0 && input_fd >= 0 && output_fd >= 0, "missing inherited descriptors");
        require(control != input_fd && control != output_fd && input_fd != output_fd,
            "inherited descriptors must differ");
        require(std::endian::native == std::endian::little, "worker requires little-endian host");
        emit("hello", {{"protocols", json::array({{{"major", 1}, {"max_minor", 0}}})},
            {"data_formats", {"csr-f64-i32-i64-le"}},
            {"instrumentation_modes", {"baseline", "trace"}}, {"worker", identity_json(identity)}});
        request = read_message(control);
        require(request.at("type") == "run", "expected run message");
        require(integer(request.at("protocol").at("major")) == 1
            && integer(request.at("protocol").at("minor")) == 0, "unsupported protocol version");
        require(request.at("data_format") == "csr-f64-i32-i64-le", "unsupported data format");
        context = {{"case_id", identifier(request.at("case_id"))},
            {"pass_id", identifier(request.at("pass_id"))}, {"provenance", request.at("provenance")}};
        for (const auto* key : {"profile_hash", "version_manifest_hash", "input_hash"}) {
            const auto hash = identifier(context.at("provenance").at(key));
            require(hash.size() == 71 && hash.starts_with("sha256:")
                && hash.find_first_not_of("0123456789abcdef", 7) == std::string::npos,
                "invalid provenance hash");
        }
        const auto& plan = request.at("plan");
        warmups = integer(plan.at("warmup_runs"));
        repetitions = integer(plan.at("measured_runs"), 1);
        (void)real(plan.at("case_timeout_seconds"), true);
        const auto& opt = request.at("options");
        options.thread_limit = integer(opt.at("thread_limit"), 1);
        options.relative_tolerance = real(opt.at("relative_tolerance"));
        options.absolute_tolerance = real(opt.at("absolute_tolerance"));
        options.iteration_limit = decimal(opt.at("iteration_limit"));
        require(options.iteration_limit > 0, "iteration limit must be positive");

        const auto& instrumentation = request.at("instrumentation");
        require(instrumentation.at("kind") == "baseline" || instrumentation.at("kind") == "trace",
            "invalid instrumentation kind");
        trace = instrumentation.at("kind") == "trace";
        const auto& collectors = instrumentation.at("collectors");
        require(collectors.is_array() && (trace ? !collectors.empty() : collectors.empty()),
            "invalid collector list");
        std::set<std::string> unique;
        for (const auto& item : collectors) {
            const auto collector = identifier(item);
            require(unique.insert(collector).second, "duplicate collector");
            if (collector != "linux.phase_resources" && collector != "linux.vm_events"
                && collector != "linux.thread_events" && collector != "linux.page_faults") {
                emit("instrumentation_unavailable", {{"message", "unsupported collector: " + collector}});
                return false;
            }
        }

        const auto& buffers = request.at("buffers");
        input.map(input_fd, decimal(buffers.at("input_bytes")), false);
        output.map(output_fd, decimal(buffers.at("output_bytes")), true);
        ::close(input_fd);
        ::close(output_fd);
        matrix_info_t info;
        const auto& m = request.at("matrix");
        info.order = static_cast<index_t>(integer(m.at("order"), 1, INT32_MAX));
        info.nonzeros = static_cast<offset_t>(decimal(m.at("nonzeros"), INT64_MAX));
        const auto n = static_cast<std::size_t>(info.order);
        const auto nnz = static_cast<std::uint64_t>(info.nonzeros);
        const auto& dense = request.at("dense");
        require(integer(dense.at("rows"), 1) == n, "dense row mismatch");
        columns = integer(dense.at("columns"), 1);
        require(decimal(dense.at("row_stride")) == 1
            && decimal(dense.at("column_stride")) == n, "dense layout must be packed column-major");
        require(columns <= SIZE_MAX / n, "dense element count overflow");
        const auto elements = n * columns;
        const auto offsets_r = region(buffers.at("row_offsets"), n + 1, 8, input.bytes);
        const auto columns_r = region(buffers.at("column_indices"), nnz, 4, input.bytes);
        const auto values_r = region(buffers.at("values"), nnz, 8, input.bytes);
        const auto rhs_r = region(buffers.at("rhs"), elements, 8, input.bytes);
        const auto solution_r = region(buffers.at("solution"), elements, 8, output.bytes);
        std::array regions{offsets_r, columns_r, values_r, rhs_r};
        std::sort(regions.begin(), regions.end(), [](auto a, auto b) { return a.offset < b.offset; });
        std::size_t end = 0;
        for (auto r : regions) {
            if (r.bytes == 0) continue;
            require(r.offset >= end, "overlapping input arrays");
            end = r.offset + r.bytes;
        }
        const auto offsets = view<const offset_t>(input, offsets_r);
        const auto column_ids = view<const index_t>(input, columns_r);
        const auto values = view<const scalar_t>(input, values_r);
        rhs = view<const scalar_t>(input, rhs_r);
        solution = view<scalar_t>(output, solution_r);
        require(offsets.front() == 0 && offsets.back() == info.nonzeros, "invalid CSR endpoints");
        for (std::size_t row = 0; row < n; ++row) {
            require(offsets[row] >= 0 && offsets[row] <= offsets[row + 1]
                && offsets[row + 1] <= info.nonzeros, "invalid CSR row offsets");
            index_t previous = -1;
            for (offset_t k = offsets[row]; k < offsets[row + 1]; ++k) {
                require(column_ids[k] > previous && column_ids[k] < info.order,
                    "CSR columns must be sorted, unique, and in range");
                require(std::isfinite(values[k]), "nonfinite matrix value");
                previous = column_ids[k];
            }
        }
        require(std::all_of(rhs.begin(), rhs.end(), [](auto x) { return std::isfinite(x); }),
            "nonfinite RHS value");
        const auto& properties = m.at("properties");
        require(properties.is_object(), "properties must be an object");
        for (const auto& [key, value] : properties.items()) {
            require(value == "yes" || value == "no" || value == "unknown", "invalid property state");
            std::optional<matrix_property_t> property;
            if (key == "symmetric") property = matrix_property_t::symmetric;
            else if (key == "structurally_symmetric") property = matrix_property_t::structurally_symmetric;
            else if (key == "positive_definite") property = matrix_property_t::positive_definite;
            if (property) info.properties.set(*property, value == "yes" ? property_state_t::yes
                : value == "no" ? property_state_t::no : property_state_t::unknown);
        }
        require(m.at("encoding") == "real" || m.at("encoding") == "complex_interleaved", "invalid encoding");
        if (m.at("encoding") == "real") require(!m.contains("complex_order"), "unexpected complex order");
        else {
            info.encoding = matrix_encoding_t::complex_interleaved;
            info.complex_order = static_cast<index_t>(integer(m.at("complex_order"), 1, INT32_MAX / 2));
            require(2 * *info.complex_order == info.order, "complex order mismatch");
            for (index_t row = 0; row < info.order; ++row) {
                const auto other = row ^ 1;
                for (offset_t k = offsets[row]; k < offsets[row + 1]; ++k) {
                    const auto column = column_ids[k], paired_column = column ^ 1;
                    const auto first = column_ids.begin() + offsets[other];
                    const auto last = column_ids.begin() + offsets[other + 1];
                    const auto found = std::lower_bound(first, last, paired_column);
                    const scalar_t paired_value = found != last && *found == paired_column
                        ? values[static_cast<std::size_t>(found - column_ids.begin())] : 0;
                    const auto expected = ((row ^ column) & 1) ? -values[k] : values[k];
                    require(paired_value == expected, "invalid interleaved complex block");
                }
            }
        }
        matrix.emplace(info, offsets, column_ids, values);
        emit("ready", {{"protocol", request.at("protocol")}, {"data_format", request.at("data_format")},
            {"instrumentation", instrumentation}});
        receive_expected("start");
        return true;
    }

    json marker() const {
        return {{"monotonic_ns", std::to_string(clock_ns(CLOCK_MONOTONIC))},
            {"pid", ::getpid()}, {"tid", ::syscall(SYS_gettid)}, {"clock", "CLOCK_MONOTONIC"}};
    }

    void add_trace(json& record, clocks_t start, clocks_t stop,
        const resources_t& before, const resources_t& after) const {
        if (!trace) return;
        record["trace"] = {{"begin_monotonic_ns", std::to_string(start.wall)},
            {"end_monotonic_ns", std::to_string(stop.wall)}, {"pid", ::getpid()},
            {"tid", ::syscall(SYS_gettid)}, {"clock", "CLOCK_MONOTONIC"}};
        record["observations"] = observations(before, after);
    }

    template <class Callback>
    phase_result_t phase(const json& trial, const char* name, bool measured, Callback&& callback) {
        json begin = {{"trial", trial}, {"phase", name}};
        if (trace) begin["marker"] = marker();
        emit("phase_begin", begin);
        const auto before = trace ? resources() : resources_t{};
        const auto start = clocks();
        phase_result_t result;
        try {
            result = callback();
            (void)status_name(result.status);
            if (std::string_view(name) == "solve" && result.status == phase_status_t::not_applicable)
                result = {phase_status_t::failed, "solve cannot be not_applicable"};
        } catch (const std::exception& error) {
            result = {phase_status_t::failed, error.what()};
        } catch (...) {
            result = {phase_status_t::failed, "unknown solver exception"};
        }
        const auto stop = clocks();
        const auto after = trace ? resources() : resources_t{};
        json record = {{"trial", trial}, {"phase", name}, {"status", status_name(result.status)},
            {"message", result.message.substr(0, 16384)}, {"iterations", nullptr},
            {"times", duration(start, stop, measured)}};
        if (result.iterations) record["iterations"] = std::to_string(*result.iterations);
        add_trace(record, start, stop, before, after);
        emit("phase_end", record);
        return result;
    }

    phase_status_t trial(const char* kind, std::uint32_t index, solver_factory_t factory) {
        const json id = {{"kind", kind}, {"index", index}};
        const bool measured = std::string_view(kind) == "measured";
        emit("trial_begin", {{"trial", id}});
        std::fill(solution.begin(), solution.end(), 0);
        const auto rows = static_cast<std::size_t>(matrix->info().order);
        const md::dextents<std::size_t, 2> extents(rows, columns);
        const md::layout_stride::mapping<md::dextents<std::size_t, 2>> mapping(extents,
            std::array<std::size_t, 2>{1, rows});
        dense_view_t<const scalar_t> b(rhs.data(), mapping);
        dense_view_t<scalar_t> x(solution.data(), mapping);
        std::unique_ptr<solver_t> solver;
        auto result = phase(id, "setup", measured, [&] {
            solver = factory();
            if (!solver) return phase_result_t{phase_status_t::failed, "null solver factory result"};
            return solver->setup(*matrix, options);
        });
        if (continues(result.status)) result = phase(id, "pre_analyze", measured, [&] { return solver->pre_analyze(); });
        if (continues(result.status)) result = phase(id, "analysis", measured, [&] { return solver->analyze(); });
        if (continues(result.status)) result = phase(id, "numeric", measured, [&] { return solver->numeric(); });
        if (continues(result.status)) result = phase(id, "solve", measured, [&] { return solver->solve(b, x); });
        json begin = {{"trial", id}};
        if (trace) begin["marker"] = marker();
        emit("teardown_begin", begin);
        const auto before = trace ? resources() : resources_t{};
        const auto start = clocks();
        solver.reset();
        const auto stop = clocks();
        const auto after = trace ? resources() : resources_t{};
        json end = {{"trial", id}, {"times", duration(start, stop, measured)}};
        add_trace(end, start, stop, before, after);
        emit("teardown_end", end);
        emit("trial_complete", {{"trial", id}, {"outcome", status_name(result.status)},
            {"solution_ready", result.status == phase_status_t::success}});
        receive_expected("trial_ack", &id);
        return result.status;
    }
};
} // namespace

int worker_main(int argc, char** argv, solver_factory_t factory, const worker_identity_t& identity) {
    session_t session;
    try {
        for (int i = 1; i < argc; ++i) {
            if (std::string_view(argv[i]) == "--describe") {
                std::cout << identity_json(identity).dump() << '\n';
                return 0;
            }
        }
        require(factory != nullptr, "null solver factory");
        const auto parent = ::getppid();
        if (::prctl(PR_SET_PDEATHSIG, SIGKILL)) os_error("prctl parent death signal");
        require(::getppid() == parent, "parent exited during worker startup");
        if (!session.prepare(argc, argv, identity)) return 4;
        phase_status_t result = phase_status_t::success;
        for (std::uint32_t i = 0; i < session.warmups && result == phase_status_t::success; ++i)
            result = session.trial("warmup", i, factory);
        for (std::uint32_t i = 0; i < session.repetitions && result == phase_status_t::success; ++i)
            result = session.trial("measured", i, factory);
        session.emit("complete", {{"outcome", status_name(result)}});
        return result == phase_status_t::success ? 0 : result == phase_status_t::unsupported ? 3 : 1;
    } catch (const std::exception& error) {
        std::cerr << "solver worker: " << error.what() << '\n';
        try {
            if (session.control >= 0 && !session.context.empty())
                session.emit("error", {{"message", std::string(error.what()).substr(0, 16384)}});
        } catch (...) {}
        return 2;
    } catch (...) {
        std::cerr << "solver worker: unknown runtime error\n";
        return 2;
    }
}
} // namespace solver_benchmark

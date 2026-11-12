"""Linux process boundary: immutable shared inputs, framed control, and bounded supervision.

The module deliberately knows nothing about a solver implementation or generator.
"""
from __future__ import annotations

import array
import copy
import fcntl
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from typing import Callable

MAX_FRAME = 1_048_576
MAX_LOG = 1_048_576
PHASES = ("setup", "pre_analyze", "analysis", "numeric", "solve")
PERF_EVENTS = {
    "linux.vm_events": ("syscalls:sys_enter_mmap", "syscalls:sys_enter_munmap", "syscalls:sys_enter_brk"),
    "linux.thread_events": ("sched:sched_process_fork", "sched:sched_process_exit", "sched:sched_switch"),
    "linux.page_faults": ("page-faults",),
}
COLLECTORS = {"linux.phase_resources", *PERF_EVENTS}


def _artifact_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class ProtocolError(RuntimeError):
    pass


class InstrumentationUnavailable(RuntimeError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _decode(data: bytes) -> dict:
    def invalid_number(value):
        raise ProtocolError(f"nonfinite JSON number: {value}")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=invalid_number)
    except (ValueError, UnicodeError) as error:
        raise ProtocolError(f"invalid JSON frame: {error}") from error
    if not isinstance(value, dict):
        raise ProtocolError("protocol message must be an object")
    return value


def _encode(value: dict) -> bytes:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not 0 < len(data) <= MAX_FRAME:
        raise ProtocolError("outgoing frame size out of range")
    return struct.pack("<I", len(data)) + data


def _kill_group(pid: int):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _seconds(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class _Sequence:
    """Checks the complete event sequence; a successful exit alone is insufficient."""
    def __init__(self, request, report, expected_identity=None):
        self.request, self.report = request, report
        self.expected_identity = expected_identity
        self.identity = {key: request[key] for key in ("case_id", "pass_id", "provenance")}
        self.state = "hello"
        self.trial = None
        self.next_trial = 0
        self.phase_index = 0
        self.phase_result = None
        self.phase_record = None
        self.teardown_record = None
        self.last_outcome = None
        self.warmups = request["plan"]["warmup_runs"]
        self.measured = request["plan"]["measured_runs"]

    def expected_trial(self):
        if self.next_trial < self.warmups:
            return {"kind": "warmup", "index": self.next_trial}
        return {"kind": "measured", "index": self.next_trial - self.warmups}

    def check_times(self, message):
        times = message.get("times", "missing")
        if self.trial["kind"] == "warmup":
            if times is not None:
                raise ProtocolError("warm-up must not report timing samples")
        elif not isinstance(times, dict) or not all(_seconds(times.get(k)) for k in ("wall_seconds", "cpu_seconds")):
            raise ProtocolError("invalid measured timing")

    def accept(self, message):
        kind = message.get("type")
        if self.state == "hello":
            if kind != "hello":
                raise ProtocolError("expected hello")
            if not any(type(p.get("major")) is int and p["major"] == 1 and type(p.get("max_minor")) is int and p["max_minor"] >= 0
                       for p in message.get("protocols", []) if isinstance(p, dict)):
                raise ProtocolError("no common protocol version")
            if self.request["data_format"] not in message.get("data_formats", []):
                raise ProtocolError("worker does not support requested data format")
            identity = message.get("worker")
            if not isinstance(identity, dict) or not all(isinstance(identity.get(k), str) for k in ("name", "version", "build_id")):
                raise ProtocolError("invalid worker identity")
            if self.expected_identity is not None and identity != self.expected_identity:
                raise ProtocolError("worker identity differs from resolved software manifest")
            if self.request["instrumentation"]["kind"] not in message.get("instrumentation_modes", []):
                raise InstrumentationUnavailable("worker does not support requested instrumentation mode")
            self.report["worker"] = identity
            self.state = "ready"
            return "run"
        if any(message.get(k) != v for k, v in self.identity.items()):
            raise ProtocolError("case/pass/provenance mismatch")
        if kind == "error":
            raise ProtocolError(str(message.get("message", "worker harness error")))
        if kind == "instrumentation_unavailable" and self.state == "ready":
            if self.request["instrumentation"]["kind"] != "trace" or not isinstance(message.get("message"), str):
                raise ProtocolError("invalid instrumentation-unavailable response")
            self.report["diagnostics"].append(message["message"])
            self.state = "unavailable"
            return None
        if kind == "ready" and self.state == "ready":
            for key in ("protocol", "data_format", "instrumentation"):
                if message.get(key) != self.request[key]:
                    raise ProtocolError(f"ready mismatch: {key}")
            self.state = "trial_or_complete"
            return "start"
        if kind == "complete" and self.state == "trial_or_complete":
            outcome = message.get("outcome")
            if outcome == "success":
                if self.next_trial != self.warmups + self.measured or self.last_outcome != "success":
                    raise ProtocolError("premature success completion")
            elif outcome not in ("unsupported", "failed") or outcome != self.last_outcome:
                raise ProtocolError("invalid early terminal outcome")
            self.report["process"]["complete_message_received"] = True
            self.report["process"]["reported_outcome"] = outcome
            self.state = "complete"
            return None
        if kind == "trial_begin" and self.state == "trial_or_complete":
            if self.last_outcome not in (None, "success") or self.next_trial >= self.warmups + self.measured:
                raise ProtocolError("unexpected additional trial")
            if message.get("trial") != self.expected_trial():
                raise ProtocolError("trial order mismatch")
            self.trial = message["trial"]
            self.phase_index = 0
            self.phase_result = None
            self.state = "phase_begin"
            return None
        if message.get("trial") != self.trial:
            raise ProtocolError("unexpected message or trial mismatch")
        if kind == "phase_begin" and self.state == "phase_begin":
            if self.phase_index >= len(PHASES) or message.get("phase") != PHASES[self.phase_index]:
                raise ProtocolError("phase order mismatch")
            self.phase_record = {"trial": self.trial, "phase": message["phase"], "completed": False,
                                 "result": None, "times": None}
            self.report["phases"].append(self.phase_record)
            self.state = "phase_end"
            return None
        if kind == "phase_end" and self.state == "phase_end":
            if message.get("phase") != PHASES[self.phase_index]:
                raise ProtocolError("phase end mismatch")
            status = message.get("status")
            if status not in ("success", "not_applicable", "unsupported", "failed"):
                raise ProtocolError("invalid phase status")
            if self.phase_index == 4 and status == "not_applicable":
                raise ProtocolError("solve cannot be not applicable")
            if not isinstance(message.get("message"), str):
                raise ProtocolError("missing diagnostic message")
            iterations = message.get("iterations", "missing")
            if iterations is not None and (not isinstance(iterations, str) or not re.fullmatch(r"0|[1-9][0-9]*", iterations)
                                           or int(iterations) > 2**64 - 1):
                raise ProtocolError("invalid iteration count")
            self.check_times(message)
            self.phase_result = status
            self.phase_record.update(completed=True, result={"status": status, "message": message["message"],
                                                             "iterations": iterations}, times=message["times"])
            for key in ("trace", "observations"):
                if key in message:
                    self.phase_record[key] = message[key]
            self.phase_index += 1
            self.state = "teardown_begin" if status in ("failed", "unsupported") or self.phase_index == 5 else "phase_begin"
            return None
        if kind == "teardown_begin" and self.state == "teardown_begin":
            self.teardown_record = {"trial": self.trial, "completed": False, "times": None}
            self.report["teardowns"].append(self.teardown_record)
            self.state = "teardown_end"
            return None
        if kind == "teardown_end" and self.state == "teardown_end":
            self.check_times(message)
            self.teardown_record.update(completed=True, times=message["times"])
            for key in ("trace", "observations"):
                if key in message:
                    self.teardown_record[key] = message[key]
            self.state = "trial_complete"
            return None
        if kind == "trial_complete" and self.state == "trial_complete":
            expected = "success" if self.phase_result == "success" else self.phase_result
            if message.get("outcome") != expected or type(message.get("solution_ready")) is not bool or message["solution_ready"] != (expected == "success"):
                raise ProtocolError("trial outcome disagrees with phases")
            self.last_outcome = expected
            self.next_trial += 1
            self.state = "trial_or_complete"
            return "ack"
        raise ProtocolError(f"unexpected {kind!r} in state {self.state}")


class _Perf:
    """Optional perf record collection, armed through perf's acknowledged control pipe."""
    def __init__(self, pid, collectors, output_dir, deadline):
        self.process = None
        self.control = None
        self.log = None
        self.path = output_dir / "perf.data"
        self.executable = None
        events = list(dict.fromkeys(event for name in collectors for event in PERF_EVENTS.get(name, ())))
        if not events:
            return
        executable = shutil.which("perf")
        self.executable = executable
        if not executable:
            raise InstrumentationUnavailable("perf is not installed")
        control_read, control_write = os.pipe()
        ack_read, ack_write = os.pipe()
        self.control = control_write
        self.log = (output_dir / "perf.log").open("wb")
        try:
            command = [executable, "record", "--clockid", "mono", "--delay=-1", f"--control=fd:{control_read},{ack_write}",
                       "--pid", str(pid), "--count", "1", "--output", str(self.path)]
            for event in events:
                command += ["--event", event]
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                            pass_fds=(control_read, ack_write), start_new_session=True)
            os.close(control_read); control_read = -1
            os.close(ack_write); ack_write = -1
            os.write(control_write, b"enable\n")
            with selectors.DefaultSelector() as selector:
                selector.register(ack_read, selectors.EVENT_READ)
                if not selector.select(max(0, min(10, deadline - time.monotonic()))):
                    raise InstrumentationUnavailable("perf did not acknowledge collector activation")
                if not os.read(ack_read, 64).startswith(b"ack"):
                    raise InstrumentationUnavailable("perf could not activate collectors; see perf.log")
            if self.process.poll() is not None:
                raise InstrumentationUnavailable("perf exited while arming collectors; see perf.log")
        except (OSError, ValueError) as error:
            self.close()
            raise InstrumentationUnavailable(f"cannot arm perf collectors: {error}") from error
        except BaseException:
            self.close()
            raise
        finally:
            for fd in (control_read, ack_read, ack_write):
                if fd >= 0:
                    os.close(fd)

    def close(self):
        if self.control is not None:
            os.close(self.control); self.control = None
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_group(self.process.pid)
                self.process.wait()
        if self.log is not None:
            self.log.close(); self.log = None

    def finish(self):
        if self.process is None:
            return True, []
        self.close()
        if self.process.returncode not in (0, -signal.SIGINT) or not self.path.exists() or self.path.stat().st_size == 0:
            return False, [str(self.path)] if self.path.exists() else []
        # Decode the recorded stream and preserve loss records rather than assuming
        # that a nonempty perf.data means complete collection.
        events_path = self.path.with_name("perf-events.txt")
        with events_path.open("wb") as events, self.path.with_name("perf-decode.log").open("wb") as errors:
            try:
                decoded = subprocess.run([self.executable, "script", "--ns", "--show-lost-events", "-i", str(self.path)],
                                         stdout=events, stderr=errors, timeout=30)
            except subprocess.TimeoutExpired:
                return False, [str(self.path), str(events_path)]
        lost = False
        with events_path.open(errors="replace") as events:
            for line in events:
                if re.search(r"\bLOST\b", line):
                    lost = True
        return decoded.returncode == 0 and not lost, [str(self.path), str(events_path)]


def _new_report():
    return {"outcome": "protocol_error", "process": {
        "launched": False, "exit_code": None, "terminating_signal": None, "wall_seconds": None,
        "user_cpu_seconds": None, "system_cpu_seconds": None, "max_rss_bytes": None,
        "max_rss_scope": "worker_process_lifetime_including_warmup", "complete_message_received": False,
    }, "phases": [], "teardowns": [], "accuracy": [], "trace_artifacts": [], "trace_complete": None,
        "diagnostics": [], "logs_truncated": False, "trace_artifact_hashes": {}, "solution_artifacts": []}


def run_worker(worker: dict, request: dict, arrays: dict[str, bytes],
               on_solution: Callable[[list[float]], dict], output_dir: Path) -> dict:
    """Run one case/solver/pass; return a JSON-ready report even on worker failure.

    Input buffers must already be validated canonical little-endian arrays. The
    worker independently checks their descriptor/data invariants. Parent validation
    callbacks run outside phase timing, while a watchdog enforces child lifetime.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    request = copy.deepcopy(request)
    report = _new_report()
    instrumentation = request["instrumentation"]
    collectors = instrumentation["collectors"]
    if any(name not in COLLECTORS for name in collectors):
        report["outcome"] = "unsupported_instrumentation"
        report["diagnostics"].append("unknown collector requested")
        return report
    if any(name in PERF_EVENTS for name in collectors) and not shutil.which("perf"):
        report["outcome"] = "unsupported_instrumentation"
        report["diagnostics"].append("perf is not installed; requested collectors were not omitted")
        return report
    if not hasattr(os, "memfd_create"):
        report["outcome"] = "launch_failed"
        report["diagnostics"].append("Linux memfd support is required")
        return report
    input_fd = output_fd = -1
    parent = child = None
    output_mapping = None
    process = None
    perf = None
    pidfd = None
    done = threading.Event()
    timed_out = threading.Event()
    watchdog = None
    started = time.monotonic()
    deadline = started + request["plan"]["case_timeout_seconds"]
    raw_status = usage = None
    try:
        chunks = bytearray()
        buffers = {}
        for name, width in (("row_offsets", 8), ("column_indices", 4), ("values", 8), ("rhs", 8)):
            data = arrays[name]
            if len(data) % width:
                raise ProtocolError(f"invalid byte size for {name}")
            chunks.extend(b"\0" * ((-len(chunks)) % width))
            buffers[name] = {"byte_offset": str(len(chunks)), "count": str(len(data) // width)}
            chunks.extend(data)
        output_size = request["dense"]["rows"] * request["dense"]["columns"] * 8
        if output_size < 8:
            raise ProtocolError("empty output")
        input_fd = os.memfd_create("solver-benchmark-input", os.MFD_ALLOW_SEALING)
        output_fd = os.memfd_create("solver-benchmark-output", os.MFD_ALLOW_SEALING)
        os.ftruncate(input_fd, len(chunks))
        offset = 0
        while offset < len(chunks):
            offset += os.pwrite(input_fd, memoryview(chunks)[offset:], offset)
        fcntl.fcntl(input_fd, fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        os.ftruncate(output_fd, output_size)
        fcntl.fcntl(output_fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        output_mapping = mmap.mmap(output_fd, output_size, access=mmap.ACCESS_READ)
        buffers.update(input_bytes=str(len(chunks)), output_bytes=str(output_size),
                       solution={"byte_offset": "0", "count": str(output_size // 8)})
        request["buffers"] = buffers
        request["type"] = "run"
        request["protocol"] = {"major": 1, "minor": 0}
        parent, child = socket.socketpair()
        environment = os.environ.copy()
        extra = worker.get("environment", {})
        if isinstance(extra, list):
            extra = dict(item.split("=", 1) for item in extra)
        environment.update(extra)
        arguments = [worker["executable"], *worker.get("arguments", []),
                     f"--sb-control-fd={child.fileno()}", f"--sb-input-fd={input_fd}", f"--sb-output-fd={output_fd}"]
        started = time.monotonic()
        deadline = started + request["plan"]["case_timeout_seconds"]
        report["process"]["started_at_unix_ns"] = str(time.time_ns())
        report["process"]["environment"] = {
            key: environment[key] for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                                             "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "OMP_PROC_BIND",
                                             "OMP_PLACES") if key in environment}
        report["process"]["affinity"] = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        report["process"]["host"] = os.uname().nodename
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, pass_fds=(child.fileno(), input_fd, output_fd), start_new_session=True)
        except OSError as error:
            report["outcome"] = "launch_failed"
            report["diagnostics"].append(str(error))
            return report
        report["process"].update(launched=True, pid=process.pid)
        child.close(); child = None
        parent.setblocking(False)

        def expire():
            if not done.wait(max(0, deadline - time.monotonic())):
                timed_out.set()
                _kill_group(process.pid)

        watchdog = threading.Thread(target=expire, name="benchmark-deadline", daemon=True)
        watchdog.start()
        sequence = _Sequence(request, report, worker.get("identity"))
        incoming = bytearray()
        outgoing = bytearray()
        frame_length = None
        control_eof = False
        log_counts = {"stdout": 0, "stderr": 0}
        with selectors.DefaultSelector() as selector, (output_dir / "stdout.log").open("wb") as stdout_log, (output_dir / "stderr.log").open("wb") as stderr_log:
            selector.register(parent, selectors.EVENT_READ, "control")
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if hasattr(os, "pidfd_open"):
                try:
                    pidfd = os.pidfd_open(process.pid)
                    selector.register(pidfd, selectors.EVENT_READ, "exit")
                except OSError:
                    pidfd = None

            def enqueue(message):
                outgoing.extend(_encode(message))
                selector.modify(parent, selectors.EVENT_READ | selectors.EVENT_WRITE, "control")

            def handle(message):
                nonlocal perf
                action = sequence.accept(message)
                if action == "run":
                    enqueue(request)
                elif action == "start":
                    if instrumentation["kind"] == "trace":
                        perf = _Perf(process.pid, collectors, output_dir, deadline)
                    enqueue({"type": "start", **sequence.identity})
                elif action == "ack":
                    if message["solution_ready"]:
                        raw_solution = output_mapping[:]
                        solution_path = output_dir / f"solution-{message['trial']['kind']}-{message['trial']['index']}.bin"
                        solution_path.write_bytes(raw_solution)
                        artifact = {"trial": message["trial"], "path": str(solution_path),
                                    "sha256": "sha256:" + hashlib.sha256(raw_solution).hexdigest(),
                                    "format": "binary64-le-column-major",
                                    "rows": request["dense"]["rows"], "columns": request["dense"]["columns"]}
                        report["solution_artifacts"].append(artifact)
                        data = array.array("d")
                        data.frombytes(raw_solution)
                        if __import__("sys").byteorder != "little":
                            data.byteswap()
                        accuracy = on_solution(data.tolist())
                        report["accuracy"].append({"trial": message["trial"], "accuracy": accuracy,
                                                     "solution_hash": artifact["sha256"]})
                    if not timed_out.is_set():
                        enqueue({"type": "trial_ack", **sequence.identity, "trial": message["trial"]})

            while raw_status is None or not control_eof:
                if timed_out.is_set() or time.monotonic() >= deadline:
                    timed_out.set()
                    _kill_group(process.pid)
                    break
                timeout = max(0, deadline - time.monotonic())
                if pidfd is None:
                    timeout = min(timeout, 0.25)  # Only fallback on older kernels.
                events = selector.select(timeout)
                for key, mask in events:
                    if key.data == "control":
                        if mask & selectors.EVENT_WRITE and outgoing:
                            try:
                                sent = parent.send(outgoing)
                                del outgoing[:sent]
                            except BlockingIOError:
                                pass
                            if not outgoing:
                                selector.modify(parent, selectors.EVENT_READ, "control")
                        if mask & selectors.EVENT_READ:
                            try:
                                data = parent.recv(65536)
                            except BlockingIOError:
                                continue
                            if not data:
                                control_eof = True
                                selector.unregister(parent)
                                if incoming or frame_length is not None:
                                    raise ProtocolError("truncated control frame")
                            else:
                                incoming.extend(data)
                                while True:
                                    if frame_length is None:
                                        if len(incoming) < 4:
                                            break
                                        frame_length = struct.unpack("<I", incoming[:4])[0]
                                        del incoming[:4]
                                        if not 0 < frame_length <= MAX_FRAME:
                                            raise ProtocolError("invalid frame length")
                                    if len(incoming) < frame_length:
                                        break
                                    frame = bytes(incoming[:frame_length]); del incoming[:frame_length]
                                    frame_length = None
                                    handle(_decode(frame))
                    elif key.data in ("stdout", "stderr"):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                        else:
                            remaining = max(0, MAX_LOG - log_counts[key.data])
                            (stdout_log if key.data == "stdout" else stderr_log).write(data[:remaining])
                            log_counts[key.data] += len(data)
                            if len(data) > remaining:
                                report["logs_truncated"] = True
                    elif key.data == "exit":
                        selector.unregister(pidfd)
                if raw_status is None:
                    waited, status, child_usage = os.wait4(process.pid, os.WNOHANG)
                    if waited:
                        raw_status, usage = status, child_usage
                        process.returncode = os.waitstatus_to_exitcode(status)
                        done.set()
                # EOF without exit is monitored until deadline; complete without
                # normal exit cannot pass, including a destructor/atexit crash.
            if timed_out.is_set():
                report["outcome"] = "timed_out"
            elif raw_status is not None:
                if os.WIFSIGNALED(raw_status):
                    report["outcome"] = "worker_crashed"
                elif sequence.state == "unavailable" and os.WEXITSTATUS(raw_status) == 4:
                    report["outcome"] = "unsupported_instrumentation"
                elif sequence.state != "complete":
                    raise ProtocolError("worker exited without complete protocol")
                else:
                    outcome = report["process"]["reported_outcome"]
                    if os.WEXITSTATUS(raw_status) != {"success": 0, "failed": 1, "unsupported": 3}[outcome]:
                        raise ProtocolError("terminal message/exit status disagree")
                    report["outcome"] = {"success": "success", "failed": "solver_failed", "unsupported": "unsupported"}[outcome]
                    if outcome == "success" and any(not item["accuracy"].get("accepted", False) for item in report["accuracy"]):
                        report["outcome"] = "accuracy_failed"
    except InstrumentationUnavailable as error:
        report["outcome"] = "unsupported_instrumentation"
        report["diagnostics"].append(str(error))
    except (ProtocolError, OSError, ValueError, TypeError, KeyError) as error:
        report["outcome"] = "timed_out" if timed_out.is_set() else "protocol_error"
        report["diagnostics"].append(str(error))
    except Exception as error:
        report["outcome"] = "protocol_error"
        report["diagnostics"].append(f"parent validation/supervision error: {error}")
    finally:
        if process is not None:
            if raw_status is None:
                _kill_group(process.pid)
                _, raw_status, usage = os.wait4(process.pid, 0)
                process.returncode = os.waitstatus_to_exitcode(raw_status)
            done.set()
            if watchdog is not None:
                watchdog.join(timeout=1)
            report["process"].update(wall_seconds=time.monotonic() - started,
                                     exit_code=os.WEXITSTATUS(raw_status) if os.WIFEXITED(raw_status) else None,
                                     terminating_signal=os.WTERMSIG(raw_status) if os.WIFSIGNALED(raw_status) else None,
                                     user_cpu_seconds=usage.ru_utime, system_cpu_seconds=usage.ru_stime,
                                     max_rss_bytes=usage.ru_maxrss * 1024)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        if perf is not None:
            try:
                complete, artifacts = perf.finish()
            except Exception as error:
                perf.close()
                complete, artifacts = False, []
                report["diagnostics"].append(f"perf finalization failed: {error}")
            report["trace_artifacts"].extend(artifacts)
            report["trace_complete"] = complete
            if not complete and report["outcome"] in ("success", "accuracy_failed", "solver_failed", "unsupported"):
                report["outcome"] = "instrumentation_failed"
                report["diagnostics"].append("perf data is incomplete, lost, or could not be decoded")
        if instrumentation["kind"] == "trace" and report["outcome"] not in ("unsupported_instrumentation", "launch_failed"):
            marker_path = output_dir / "phase-trace.json"
            records = [{"trial": r["trial"], "phase": r.get("phase", "teardown"),
                        "trace": r.get("trace"), "observations": r.get("observations"), "completed": r["completed"]}
                       for r in report["phases"] + report["teardowns"]]
            marker_path.write_text(json.dumps({"case_id": request["case_id"], "pass_id": request["pass_id"],
                                               "provenance": request["provenance"], "records": records}, indent=2))
            report["trace_artifacts"].append(str(marker_path))
            complete = bool(records) and all(r["completed"] and isinstance(r["trace"], dict) and
                                             ("linux.phase_resources" not in collectors or isinstance(r["observations"], dict))
                                             for r in records)
            report["trace_complete"] = complete and report["trace_complete"] is not False
            if not report["trace_complete"] and report["outcome"] in ("success", "accuracy_failed", "solver_failed", "unsupported"):
                report["outcome"] = "instrumentation_failed"
                report["diagnostics"].append("missing phase markers or required resource observations")
        for artifact in report["trace_artifacts"]:
            try:
                report["trace_artifact_hashes"][artifact] = _artifact_hash(artifact)
            except OSError as error:
                report["trace_complete"] = False
                report["diagnostics"].append(f"cannot hash trace artifact: {error}")
                if report["outcome"] in ("success", "accuracy_failed"):
                    report["outcome"] = "instrumentation_failed"
        if output_mapping is not None:
            output_mapping.close()
        for connection in (parent, child):
            if connection is not None:
                connection.close()
        for fd in (input_fd, output_fd, pidfd):
            if fd is not None and fd >= 0:
                os.close(fd)
    return report

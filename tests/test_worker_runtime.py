"""Exercise the native worker's trust boundary without the parent supervisor."""

import copy
import fcntl
import json
import mmap
import os
import socket
import struct
import subprocess
import unittest


WORKER = os.environ.get("SB_WORKER_FIXTURE")


class WorkerSession:
    def __init__(self, values=(2.0, -1.0, -1.0, 2.0), columns=(0, 1, 0, 1)):
        self.parent, child = socket.socketpair()
        self.parent.settimeout(5)
        self.input_fd = os.memfd_create("worker-test-input", os.MFD_ALLOW_SEALING)
        self.output_fd = os.memfd_create("worker-test-output", os.MFD_ALLOW_SEALING)
        # Canonical 2x2 CSR plus two RHS columns, laid out independently from
        # the production parent serializer so descriptor mistakes are detectable.
        payload = struct.pack("<3q4i4d4d", 0, 2, 4, *columns, *values, 1, 1, 2, 2)
        os.write(self.input_fd, payload)
        os.ftruncate(self.output_fd, 32)
        size_seals = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
        fcntl.fcntl(self.input_fd, fcntl.F_ADD_SEALS, size_seals | fcntl.F_SEAL_WRITE)
        fcntl.fcntl(self.output_fd, fcntl.F_ADD_SEALS, size_seals)
        self.output = mmap.mmap(self.output_fd, 32)
        try:
            self.process = subprocess.Popen(
                [WORKER, f"--sb-control-fd={child.fileno()}",
                 f"--sb-input-fd={self.input_fd}", f"--sb-output-fd={self.output_fd}"],
                pass_fds=(child.fileno(), self.input_fd, self.output_fd),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        finally:
            child.close()
        self.context = {
            "case_id": "native-runtime-test", "pass_id": "baseline",
            "provenance": {key: "sha256:" + "a" * 64 for key in
                           ("profile_hash", "version_manifest_hash", "input_hash")},
        }
        self.request = {
            "type": "run", **self.context,
            "protocol": {"major": 1, "minor": 0}, "data_format": "csr-f64-i32-i64-le",
            "matrix": {"order": 2, "nonzeros": "4", "encoding": "real",
                       "properties": {"symmetric": "yes", "positive_definite": "yes"}},
            "dense": {"rows": 2, "columns": 2, "row_stride": "1", "column_stride": "2"},
            "buffers": {
                "input_bytes": str(len(payload)), "output_bytes": "32",
                "row_offsets": {"byte_offset": "0", "count": "3"},
                "column_indices": {"byte_offset": "24", "count": "4"},
                "values": {"byte_offset": "40", "count": "4"},
                "rhs": {"byte_offset": "72", "count": "4"},
                "solution": {"byte_offset": "0", "count": "4"},
            },
            "instrumentation": {"kind": "baseline", "collectors": []},
            "plan": {"warmup_runs": 0, "measured_runs": 1, "case_timeout_seconds": 5},
            "options": {"thread_limit": 1, "relative_tolerance": 1e-10,
                        "absolute_tolerance": 0, "iteration_limit": "100"},
        }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.parent.close()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.process.stderr.close()
        self.output.close()
        os.close(self.input_fd)
        os.close(self.output_fd)

    def send(self, value):
        self.send_raw(json.dumps(value).encode())

    def send_raw(self, value):
        self.parent.sendall(struct.pack("<I", len(value)) + value)

    def receive(self):
        def exact(count):
            result = b""
            while len(result) < count:
                chunk = self.parent.recv(count - len(result))
                if not chunk:
                    raise EOFError("worker control closed")
                result += chunk
            return result
        length, = struct.unpack("<I", exact(4))
        if not 0 < length <= 1024 * 1024:
            raise ValueError("invalid worker frame size")
        return json.loads(exact(length))


@unittest.skipUnless(WORKER and os.path.isfile(WORKER), "native worker fixture not built")
class WorkerBoundaryTests(unittest.TestCase):
    def reject(self, mutate=lambda _: None, *, values=(2.0, -1.0, -1.0, 2.0),
               columns=(0, 1, 0, 1)):
        with WorkerSession(values, columns) as session:
            self.assertEqual(session.receive()["type"], "hello")
            mutate(session.request)
            session.send(session.request)
            message = session.receive()
            self.assertEqual(message["type"], "error", message)
            self.assertEqual(session.process.wait(timeout=5), 2)

    def test_misaligned_buffer_rejected(self):
        self.reject(lambda r: r["buffers"]["values"].update(byte_offset="41"))

    def test_overlapping_regions_rejected(self):
        self.reject(lambda r: r["buffers"]["values"].update(byte_offset="24"))

    def test_out_of_bounds_region_rejected(self):
        self.reject(lambda r: r["buffers"]["rhs"].update(byte_offset="104"))

    def test_integer_wraparound_rejected(self):
        self.reject(lambda r: r["matrix"].update(nonzeros="18446744073709551616"))

    def test_noncanonical_integer_rejected(self):
        self.reject(lambda r: r["options"].update(iteration_limit="0100"))

    def test_invalid_dense_stride_rejected(self):
        self.reject(lambda r: r["dense"].update(column_stride="3"))

    def test_nonfinite_matrix_rejected(self):
        self.reject(values=(float("nan"), -1.0, -1.0, 2.0))

    def test_unsorted_or_duplicate_csr_rejected(self):
        self.reject(columns=(0, 0, 0, 1))

    def test_invalid_complex_blocks_rejected(self):
        self.reject(lambda r: r["matrix"].update(encoding="complex_interleaved", complex_order=1))

    def test_duplicate_json_key_rejected(self):
        with WorkerSession() as session:
            session.receive()
            message = json.dumps(session.request).replace('"order": 2', '"order": 2, "order": 2')
            session.send_raw(message.encode())
            # Parsing fails before a validated correlation context exists.
            with self.assertRaises(EOFError):
                session.receive()
            self.assertEqual(session.process.wait(timeout=5), 2)

    def test_start_requires_matching_context(self):
        with WorkerSession() as session:
            session.receive()
            session.send(session.request)
            self.assertEqual(session.receive()["type"], "ready")
            context = copy.deepcopy(session.context)
            context["case_id"] = "wrong-case"
            session.send({"type": "start", **context})
            self.assertEqual(session.receive()["type"], "error")
            self.assertEqual(session.process.wait(timeout=5), 2)

    def test_valid_complex_multirhs_and_trace(self):
        with WorkerSession(values=(2.0, 0.0, 0.0, 2.0)) as session:
            session.receive()
            session.request["matrix"].update(encoding="complex_interleaved", complex_order=1)
            session.request["instrumentation"] = {"kind": "trace", "collectors": ["linux.phase_resources"]}
            session.send(session.request)
            self.assertEqual(session.receive()["type"], "ready")
            session.send({"type": "start", **session.context})
            phases = []
            while True:
                event = session.receive()
                if event["type"] == "phase_end":
                    phases.append(event)
                    self.assertIn("observations", event)
                    self.assertLessEqual(int(event["trace"]["begin_monotonic_ns"]),
                                         int(event["trace"]["end_monotonic_ns"]))
                if event["type"] == "trial_complete":
                    self.assertEqual(struct.unpack("<4d", session.output[:]), (0.5, 0.5, 1.0, 1.0))
                    session.send({"type": "trial_ack", **session.context, "trial": event["trial"]})
                if event["type"] == "complete":
                    self.assertEqual(event["outcome"], "success")
                    break
            self.assertEqual([p["phase"] for p in phases],
                             ["setup", "pre_analyze", "analysis", "numeric", "solve"])
            self.assertEqual(session.process.wait(timeout=5), 0)

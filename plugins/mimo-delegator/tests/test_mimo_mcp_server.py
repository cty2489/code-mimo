"""Unit and process-level tests for the MiMo Delegator MCP server."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mimo_mcp_server as server


class TestValidation(unittest.TestCase):
    def test_workdir_rejects_root_home_and_missing(self):
        for path in ["/", str(Path.home()), "/nonexistent/path/xyz"]:
            with self.assertRaises(ValueError):
                server._validate_workdir(path)

    def test_workdir_accepts_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                server._validate_workdir(directory), Path(directory).resolve()
            )

    def test_paths_require_scope_and_reject_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            with self.assertRaises(ValueError):
                server._validate_paths(workdir, [])
            with self.assertRaises(ValueError):
                server._validate_paths(workdir, ["../../etc/passwd"])
            resolved = server._validate_paths(workdir, ["src"])
            self.assertEqual(resolved, [(workdir / "src").resolve()])

    def test_paths_reject_too_many(self):
        with self.assertRaises(ValueError):
            server._validate_paths(Path("/tmp"), [f"path-{i}" for i in range(60)])

    def test_verification_validation(self):
        for commands in [[], [""], ["   "], ["\t\n"]]:
            with self.assertRaises(ValueError):
                server._validate_verification(commands)
        with self.assertRaises(ValueError):
            server._validate_verification([f"command-{i}" for i in range(25)])
        with self.assertRaises(ValueError):
            server._validate_verification(["x" * 3000])
        self.assertEqual(server._validate_verification(["pytest"]), ["pytest"])

    def test_task_id_validation(self):
        server._validate_task_id("mimo-0123456789ab")
        for value in ["", "mimo-123", "mimo-GGGGGGGGGGGG", "../../../etc"]:
            with self.assertRaises(ValueError):
                server._validate_task_id(value)

    def test_integer_and_bool_validation(self):
        self.assertEqual(server._validate_int(3, "value", 0, 5), 3)
        with self.assertRaises(TypeError):
            server._validate_int(True, "value", 0)
        with self.assertRaises(ValueError):
            server._validate_int(6, "value", 0, 5)
        with self.assertRaises(ValueError):
            server._validate_nonblank("   ", "task")

    def test_non_executable_mimo_path_has_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mimo"
            path.write_text("not executable")
            with patch.dict(os.environ, {"MIMO_EXECUTABLE": str(path)}):
                with self.assertRaisesRegex(FileNotFoundError, "not executable"):
                    server._locate_mimo()


class TestSnapshotsAndScope(unittest.TestCase):
    def test_diff_files(self):
        self.assertEqual(server._diff_files({}, {"a.py": "x"}), ["a.py"])
        self.assertEqual(
            server._diff_files({"a.py": "1"}, {"a.py": "2"}), ["a.py"]
        )
        self.assertEqual(server._diff_files({"a.py": "1"}, {}), ["a.py"])
        self.assertEqual(server._diff_files({"a.py": "1"}, {"a.py": "1"}), [])

    def test_snapshot_skips_large_and_generated_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in server.SKIP_DIRS:
                (root / name).mkdir()
                (root / name / "ignored.txt").write_text("x")
            (root / "data" / "qdrant").mkdir(parents=True)
            (root / "data" / "qdrant" / "ignored.txt").write_text("x")
            (root / "data" / "keep.txt").write_text("keep")
            (root / "model.GGUF").write_text("model")
            (root / "code.py").write_text("print('ok')")
            snapshot = server._snapshot_files(root)
            self.assertIn("code.py", snapshot)
            self.assertIn("data/keep.txt", snapshot)
            self.assertNotIn("data/qdrant/ignored.txt", snapshot)
            self.assertNotIn("model.GGUF", snapshot)
            self.assertNotIn("qdrant", server.SKIP_DIRS)

    def test_snapshot_metadata_contains_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            path.write_text("hello")
            _, size = server._snapshot_files(Path(directory))["file.txt"].split(":")
            self.assertEqual(int(size, 16), 5)

    def test_scope_accepts_directory_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            allowed_dir = workdir / "src"
            ok, violations = server._check_scope(
                ["src/main.py"], [allowed_dir], workdir
            )
            self.assertTrue(ok)
            self.assertEqual(violations, [])
            allowed_file = workdir / "README.md"
            ok, _ = server._check_scope(["README.md"], [allowed_file], workdir)
            self.assertTrue(ok)
            ok, violations = server._check_scope(
                ["outside.txt"], [allowed_file], workdir
            )
            self.assertFalse(ok)
            self.assertEqual(violations, ["outside.txt"])


class TestParsingAndCompaction(unittest.TestCase):
    def test_parse_real_jsonl_shapes(self):
        lines = [
            json.dumps({"sessionID": "session-1"}),
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "from part"},
                    "parts": [{"type": "text", "text": "from parts"}],
                    "text": "top level",
                }
            ),
            json.dumps({"type": "text", "text": "last text"}),
        ]
        parsed = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(parsed["session_id"], "session-1")
        self.assertEqual(parsed["summary"], "last text")

    def test_parse_fallbacks_and_malformed_lines(self):
        lines = [
            "not json",
            json.dumps({"session_id": "session-2"}),
            json.dumps(
                {
                    "type": "text",
                    "parts": [{"type": "text", "text": "array text"}],
                }
            ),
        ]
        parsed = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(parsed["session_id"], "session-2")
        self.assertEqual(parsed["summary"], "array text")

    def test_changed_files_are_compact(self):
        result = server._compress_changed_files([f"file-{i}" for i in range(1000)])
        self.assertEqual(len(result), 51)
        self.assertEqual(result[-1], "...and 950 more files")

    def test_verification_is_compact(self):
        result = server._compress_verification_result(
            [
                {
                    "command": "x" * 500,
                    "passed": False,
                    "exit_code": 1,
                    "stdout": "o" * 1000,
                    "stderr": "e" * 1000,
                }
            ]
        )
        self.assertEqual(len(result[0]["command"]), server.MAX_VERIFICATION_CMD_CHARS)
        self.assertEqual(len(result[0]["stdout"]), server.MAX_VERIFICATION_OUTPUT_CHARS)
        self.assertEqual(len(result[0]["stderr"]), server.MAX_VERIFICATION_OUTPUT_CHARS)

    def test_prompt_contains_scope_task_and_verification(self):
        prompt = server._build_mimo_prompt(
            "fix bug", ["src", "tests"], ["pytest", "make lint"]
        )
        for value in ["fix bug", "src", "tests", "pytest", "make lint"]:
            self.assertIn(value, prompt)

    def test_command_defaults_to_pure(self):
        command = server._build_mimo_command(
            "/bin/mimo", Path("/tmp/project"), "task", "session-1", True
        )
        self.assertIn("--pure", command)
        self.assertIn("--session", command)
        self.assertNotIn("--pure", server._build_mimo_command(
            "/bin/mimo", Path("/tmp/project"), "task", "", False
        ))


class IsolatedStateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(server, "BASE_DIR", root / "state"))
        self.stack.enter_context(patch.object(server, "TASKS_DIR", root / "state" / "tasks"))
        self.stack.enter_context(patch.object(server, "LOGS_DIR", root / "state" / "logs"))
        self.stack.enter_context(patch.object(server, "LOCKS_DIR", root / "state" / "locks"))
        server._ensure_dirs()

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def make_state(self, **overrides):
        task_id = overrides.pop("task_id", server._gen_task_id())
        state = {
            "task_id": task_id,
            "status": "success",
            "workdir": "/tmp",
            "session_id": "session-1",
            "iterations": 2,
            "changed_files": ["a.py"],
            "verification": [{"command": "echo ok", "passed": True, "exit_code": 0}],
            "summary": "all good",
            "log_path": "/tmp/task.log",
            "worker_pid": 0,
            "child_pid": 0,
            "created_ts": time.time() - 3,
            "started_ts": time.time() - 2,
            "finished_ts": time.time(),
        }
        state.update(overrides)
        server._save_task_state(task_id, state)
        return task_id, state


class TestPersistedState(IsolatedStateTest):
    def test_atomic_save_and_compact_result(self):
        task_id, _ = self.make_state()
        result = server.task_result(task_id)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["changed_files"], ["a.py"])
        self.assertGreaterEqual(result["elapsed_seconds"], 0)

    def test_missing_and_invalid_task_ids_fail(self):
        with self.assertRaises(FileNotFoundError):
            server.task_result("mimo-000000000000")
        with self.assertRaises(ValueError):
            server.task_result("bad-id")

    def test_dead_worker_is_recovered_as_failed(self):
        task_id, _ = self.make_state(
            status="running", worker_pid=999_999_999, finished_ts=None
        )
        result = server.task_result(task_id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("unexpectedly", result["summary"])

    def test_stale_refresh_does_not_overwrite_newer_terminal_state(self):
        task_id, _ = self.make_state(
            status="running", worker_pid=999_999_999, finished_ts=None
        )
        stale = server._load_task_state(task_id)
        latest = dict(stale)
        latest.update(
            status="success",
            worker_pid=0,
            child_pid=0,
            summary="worker finished successfully",
            finished_ts=time.time(),
        )
        server._save_task_state(task_id, latest)

        with patch.object(server, "_process_alive", return_value=False):
            refreshed = server._refresh_stale_task(stale)

        persisted = server._load_task_state(task_id)
        self.assertEqual(refreshed["status"], "success")
        self.assertEqual(persisted["status"], "success")
        self.assertEqual(persisted["summary"], "worker finished successfully")

    def test_cancel_does_not_overwrite_success_persisted_after_refresh(self):
        task_id, _ = self.make_state(
            status="running",
            worker_pid=999_999_999,
            child_pid=0,
            finished_ts=None,
            lock_token="test-lock-token",
        )
        real_cancel_path = server._cancel_path(task_id)

        class SuccessBeforeCancelWrite:
            def write_text(inner_self, *args, **kwargs):
                latest = server._load_task_state(task_id)
                latest.update(
                    status="success",
                    worker_pid=0,
                    child_pid=0,
                    summary="worker won the completion race",
                    finished_ts=time.time(),
                )
                server._save_task_state(task_id, latest)
                return real_cancel_path.write_text(*args, **kwargs)

            def unlink(inner_self, *args, **kwargs):
                return real_cancel_path.unlink(*args, **kwargs)

        def immediate_result(*_args, **_kwargs):
            return server._task_result_from_state(server._load_task_state(task_id))

        with patch.object(server, "_process_alive", return_value=True):
            with patch.object(
                server, "_cancel_path", return_value=SuccessBeforeCancelWrite()
            ):
                with patch.object(
                    server, "_wait_for_task", side_effect=immediate_result
                ):
                    result = server.cancel_task(task_id)

        persisted = server._load_task_state(task_id)
        self.assertEqual(result["status"], "success")
        self.assertEqual(persisted["status"], "success")
        self.assertEqual(persisted["summary"], "worker won the completion race")

    def test_dead_worker_stops_live_child_before_releasing_workdir(self):
        workdir = Path(self.temporary.name) / "project"
        workdir.mkdir()
        ready_path = Path(self.temporary.name) / "descendant-ready"
        descendant_code = (
            "import signal,sys,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
        )
        leader_code = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])"
        )
        leader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                leader_code,
                descendant_code,
                str(ready_path),
            ],
            start_new_session=True,
        )
        leader.wait(timeout=2)
        deadline = time.monotonic() + 2
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready_path.exists(), "descendant process did not start")
        process_group_id = leader.pid

        def process_group_alive():
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return False
            return True

        self.assertTrue(process_group_alive())
        worker_pid = 999_999_999
        task_id, _ = self.make_state(
            status="running",
            workdir=str(workdir),
            worker_pid=worker_pid,
            child_pid=process_group_id,
            finished_ts=None,
            lock_token="test-lock-token",
        )
        original_process_alive = server._process_alive

        def process_alive(pid):
            if pid == worker_pid:
                return False
            return original_process_alive(pid)

        def release_after_child_stopped(*_args, **_kwargs):
            self.assertFalse(
                process_group_alive(),
                "workdir lock was released while a delegated descendant was still alive",
            )

        try:
            with patch.object(server, "_process_alive", side_effect=process_alive):
                with patch.object(
                    server, "_release_workdir", side_effect=release_after_child_stopped
                ) as release:
                    refreshed = server._refresh_stale_task(
                        server._load_task_state(task_id)
                    )
            self.assertEqual(refreshed["status"], "failed")
            release.assert_called_once()
        finally:
            if process_group_alive():
                os.killpg(process_group_id, signal.SIGKILL)
                deadline = time.monotonic() + 2
                while process_group_alive() and time.monotonic() < deadline:
                    time.sleep(0.02)

    def test_wait_validation(self):
        task_id, _ = self.make_state()
        self.assertEqual(server.wait_task(task_id, 0)["status"], "success")
        with self.assertRaises(ValueError):
            server.wait_task(task_id, server.MAX_WAIT_SECONDS + 1)

    def test_version_one_state_gets_runtime_defaults(self):
        task_id = server._gen_task_id()
        server._task_path(task_id).write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": "failed",
                    "workdir": "/tmp",
                    "timeout_seconds": 123,
                }
            )
        )
        state = server._load_task_state(task_id)
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["max_runtime_seconds"], 123)
        self.assertEqual(state["verification_timeout_seconds"], 123)
        self.assertTrue(state["pure"])


FAKE_MIMO = r'''#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from pathlib import Path

args = sys.argv[1:]
args_log = os.environ.get("FAKE_MIMO_ARGS_LOG", "")
if args_log:
    with open(args_log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args, ensure_ascii=False) + "\n")

counter = 1
counter_path = os.environ.get("FAKE_MIMO_COUNTER", "")
if counter_path:
    path = Path(counter_path)
    if path.exists():
        counter = int(path.read_text()) + 1
    path.write_text(str(counter))

session_id = os.environ.get("FAKE_MIMO_SESSION", "fake-session")
if os.environ.get("FAKE_MIMO_IGNORE_SIGTERM", "0") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ready_path = os.environ.get("FAKE_MIMO_READY", "")
    if ready_path:
        Path(ready_path).write_text("ready")
partial_sleep = float(os.environ.get("FAKE_MIMO_PARTIAL_SLEEP", "0"))
if partial_sleep:
    sys.stdout.write('{"type":"text"')
    sys.stdout.flush()
    time.sleep(partial_sleep)
    raise SystemExit(int(os.environ.get("FAKE_MIMO_EXIT_CODE", "0")))

print(json.dumps({"type": "step_start", "sessionID": session_id}), flush=True)

sleep_before = float(os.environ.get("FAKE_MIMO_SLEEP_BEFORE", "0"))
if sleep_before:
    time.sleep(sleep_before)

edit_path = os.environ.get("FAKE_MIMO_EDIT", "")
edit_on_call = int(os.environ.get("FAKE_MIMO_EDIT_ON_CALL", "1"))
if edit_path and counter >= edit_on_call:
    target = Path.cwd() / edit_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(os.environ.get("FAKE_MIMO_CONTENT", "done"))

summary = os.environ.get("FAKE_MIMO_SUMMARY", "fake task complete")
print(json.dumps({
    "type": "text",
    "sessionID": session_id,
    "part": {"type": "text", "text": summary},
}), flush=True)

sleep_after = float(os.environ.get("FAKE_MIMO_SLEEP_AFTER", "0"))
if sleep_after:
    time.sleep(sleep_after)

raise SystemExit(int(os.environ.get("FAKE_MIMO_EXIT_CODE", "0")))
'''


class RuntimeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workdir = self.root / "project"
        self.workdir.mkdir()
        self.state_home = self.root / "state"
        self.fake_mimo = self.root / "fake_mimo.py"
        self.fake_mimo.write_text(FAKE_MIMO)
        self.fake_mimo.chmod(0o755)
        self.args_log = self.root / "args.jsonl"
        self.counter = self.root / "counter.txt"
        self.task_ids = []

        environment = {
            "MIMO_DELEGATOR_HOME": str(self.state_home),
            "MIMO_EXECUTABLE": str(self.fake_mimo),
            "FAKE_MIMO_ARGS_LOG": str(self.args_log),
            "FAKE_MIMO_COUNTER": str(self.counter),
            "FAKE_MIMO_SLEEP_BEFORE": "0",
            "FAKE_MIMO_SLEEP_AFTER": "0",
            "FAKE_MIMO_EDIT": "result.txt",
            "FAKE_MIMO_EDIT_ON_CALL": "1",
            "FAKE_MIMO_CONTENT": "done",
            "FAKE_MIMO_SUMMARY": "fake task complete",
            "FAKE_MIMO_EXIT_CODE": "0",
            "FAKE_MIMO_SESSION": "fake-session",
            "FAKE_MIMO_PARTIAL_SLEEP": "0",
            "FAKE_MIMO_IGNORE_SIGTERM": "0",
            "FAKE_MIMO_READY": "",
        }
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, environment, clear=False))
        self.stack.enter_context(patch.object(server, "BASE_DIR", self.state_home))
        self.stack.enter_context(patch.object(server, "TASKS_DIR", self.state_home / "tasks"))
        self.stack.enter_context(patch.object(server, "LOGS_DIR", self.state_home / "logs"))
        self.stack.enter_context(patch.object(server, "LOCKS_DIR", self.state_home / "locks"))
        server._ensure_dirs()

    def tearDown(self):
        for task_id in self.task_ids:
            try:
                if server.task_result(task_id)["status"] in server.ACTIVE_STATUSES:
                    server.cancel_task(task_id)
            except (FileNotFoundError, ValueError):
                pass
        self.stack.close()
        self.temporary.cleanup()

    def delegate(self, **overrides):
        arguments = {
            "workdir": str(self.workdir),
            "task": "create result",
            "allowed_paths": ["result.txt"],
            "verification_commands": ["test -f result.txt"],
            "max_iterations": 2,
            "wait_seconds": 5,
            "max_runtime_seconds": 0,
            "verification_timeout_seconds": 5,
            "pure": True,
        }
        arguments.update(overrides)
        result = server.delegate_task(**arguments)
        self.task_ids.append(result["task_id"])
        return result

    def read_args(self):
        return [json.loads(line) for line in self.args_log.read_text().splitlines()]

    def wait_for_child(self, task_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = server._load_task_state(task_id)
            if state.get("child_pid"):
                return int(state["child_pid"])
            time.sleep(0.05)
        self.fail("worker did not start a child process")

    def test_quick_task_succeeds_and_uses_pure(self):
        result = self.delegate()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["session_id"], "fake-session")
        self.assertEqual(result["changed_files"], ["result.txt"])
        self.assertIn("--pure", self.read_args()[0])

    def test_pure_can_be_disabled_explicitly(self):
        result = self.delegate(pure=False)
        self.assertEqual(result["status"], "success")
        self.assertNotIn("--pure", self.read_args()[0])

    def test_long_task_returns_running_and_finishes_in_background(self):
        os.environ["FAKE_MIMO_SLEEP_BEFORE"] = "1.2"
        started = time.monotonic()
        result = self.delegate(wait_seconds=0)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn(result["status"], server.ACTIVE_STATUSES)
        worker = server._LOCAL_WORKERS.pop(result["task_id"])
        final = server.wait_task(result["task_id"], 5)
        worker.wait(timeout=2)
        self.assertEqual(final["status"], "success")

    def test_verification_failure_retries_same_session(self):
        os.environ["FAKE_MIMO_EDIT_ON_CALL"] = "2"
        result = self.delegate(max_iterations=2)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["iterations"], 2)
        calls = self.read_args()
        self.assertEqual(len(calls), 2)
        self.assertIn("--session", calls[1])
        self.assertIn("fake-session", calls[1])

    def test_retry_without_session_repeats_full_task_context(self):
        os.environ["FAKE_MIMO_SESSION"] = ""
        os.environ["FAKE_MIMO_EDIT_ON_CALL"] = "2"
        result = self.delegate(max_iterations=2)
        self.assertEqual(result["status"], "success")
        second_call = self.read_args()[1]
        self.assertNotIn("--session", second_call)
        self.assertIn("Task: create result", second_call[-1])
        self.assertIn("Additional feedback", second_call[-1])

    def test_nonzero_mimo_exit_does_not_run_verification_retry(self):
        os.environ["FAKE_MIMO_EDIT"] = ""
        os.environ["FAKE_MIMO_EXIT_CODE"] = "7"
        result = self.delegate(verification_commands=["touch should-not-exist"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.workdir / "should-not-exist").exists())
        self.assertEqual(len(self.read_args()), 1)

    def test_verification_timeout_does_not_retry_mimo(self):
        result = self.delegate(
            verification_commands=["sleep 5"],
            verification_timeout_seconds=1,
            max_iterations=3,
            wait_seconds=3,
        )
        self.assertEqual(result["status"], "stalled")
        self.assertIn("Verification timed out", result["summary"])
        self.assertEqual(len(self.read_args()), 1)

    def test_runtime_limit_preserves_partial_jsonl(self):
        os.environ["FAKE_MIMO_EDIT"] = ""
        os.environ["FAKE_MIMO_SLEEP_BEFORE"] = "10"
        result = self.delegate(
            max_runtime_seconds=1,
            wait_seconds=3,
            verification_commands=["true"],
        )
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(result["session_id"], "fake-session")
        log = Path(result["log_path"]).read_text()
        self.assertIn("step_start", log)
        self.assertIn("TIMEOUT", log)

    def test_partial_line_output_still_respects_runtime_limit(self):
        os.environ["FAKE_MIMO_EDIT"] = ""
        os.environ["FAKE_MIMO_PARTIAL_SLEEP"] = "10"
        started = time.monotonic()
        result = self.delegate(
            max_runtime_seconds=1,
            wait_seconds=3,
            verification_commands=["true"],
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "stalled")
        self.assertLess(elapsed, 3)
        self.assertIn("max_runtime_seconds=1", result["summary"])

    def test_partial_line_output_can_be_cancelled(self):
        os.environ["FAKE_MIMO_EDIT"] = ""
        os.environ["FAKE_MIMO_PARTIAL_SLEEP"] = "20"
        result = self.delegate(wait_seconds=0)
        child_pid = self.wait_for_child(result["task_id"])
        cancelled = server.cancel_task(result["task_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        time.sleep(0.2)
        self.assertFalse(server._process_alive(child_pid))

    def test_cancel_stops_child_process(self):
        os.environ["FAKE_MIMO_SLEEP_BEFORE"] = "20"
        result = self.delegate(wait_seconds=0)
        child_pid = self.wait_for_child(result["task_id"])
        cancelled = server.cancel_task(result["task_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        time.sleep(0.2)
        self.assertFalse(server._process_alive(child_pid))

    def test_cancel_kills_term_ignoring_child_before_releasing_lock(self):
        ready_path = self.root / "term-ignore-ready"
        os.environ["FAKE_MIMO_EDIT"] = ""
        os.environ["FAKE_MIMO_IGNORE_SIGTERM"] = "1"
        os.environ["FAKE_MIMO_READY"] = str(ready_path)
        os.environ["FAKE_MIMO_SLEEP_BEFORE"] = "20"
        result = self.delegate(wait_seconds=0)
        child_pid = self.wait_for_child(result["task_id"])
        deadline = time.monotonic() + 2
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready_path.exists(), "fake MiMo did not install SIGTERM handler")

        active_result = dict(result)
        active_result["status"] = "running"
        try:
            with patch.object(
                server, "_wait_for_task", return_value=active_result
            ):
                cancelled = server.cancel_task(result["task_id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertFalse(server._process_group_alive(child_pid))
            self.assertFalse(server._workdir_lock_path(self.workdir).exists())
        finally:
            if server._process_group_alive(child_pid):
                os.killpg(child_pid, signal.SIGKILL)

    def test_scope_violation_is_terminal(self):
        os.environ["FAKE_MIMO_EDIT"] = "outside.txt"
        result = self.delegate(
            allowed_paths=["allowed"], verification_commands=["true"]
        )
        self.assertEqual(result["status"], "scope_violation")
        with self.assertRaises(ValueError):
            server.continue_task(result["task_id"], "try again", wait_seconds=0)

    def test_workdir_lock_rejects_concurrent_task(self):
        os.environ["FAKE_MIMO_SLEEP_BEFORE"] = "20"
        first = self.delegate(wait_seconds=0)
        self.wait_for_child(first["task_id"])
        with self.assertRaises(RuntimeError):
            server.delegate_task(
                workdir=str(self.workdir),
                task="second",
                allowed_paths=["second.txt"],
                verification_commands=["true"],
                wait_seconds=0,
            )

    def test_initial_lock_cannot_be_stolen_before_state_save(self):
        original_claim = server._claim_workdir
        probing = False
        probed = False

        def claim_then_probe(*args, **kwargs):
            nonlocal probing, probed
            original_claim(*args, **kwargs)
            if probing or probed:
                return
            probing = True
            probed = True
            try:
                with self.assertRaises(RuntimeError):
                    server.delegate_task(
                        workdir=str(self.workdir),
                        task="competing task",
                        allowed_paths=["result.txt"],
                        verification_commands=["true"],
                        wait_seconds=5,
                    )
            finally:
                probing = False

        with patch.object(server, "_claim_workdir", side_effect=claim_then_probe):
            result = self.delegate()

        self.assertTrue(probed)
        self.assertEqual(result["status"], "success")

    def test_continue_lock_cannot_be_stolen_before_state_becomes_queued(self):
        initial = self.delegate()
        self.assertEqual(initial["status"], "success")
        original_claim = server._claim_workdir
        probing = False
        probed = False

        def claim_then_probe(*args, **kwargs):
            nonlocal probing, probed
            original_claim(*args, **kwargs)
            if probing or probed:
                return
            probing = True
            probed = True
            try:
                with self.assertRaises(RuntimeError):
                    server.delegate_task(
                        workdir=str(self.workdir),
                        task="competing task",
                        allowed_paths=["result.txt"],
                        verification_commands=["true"],
                        wait_seconds=5,
                    )
            finally:
                probing = False

        with patch.object(server, "_claim_workdir", side_effect=claim_then_probe):
            continued = server.continue_task(
                initial["task_id"], "run one more verified pass", wait_seconds=5
            )

        self.assertTrue(probed)
        self.assertEqual(continued["status"], "success")

    def test_continue_task_uses_existing_session(self):
        os.environ["FAKE_MIMO_EDIT"] = ""
        os.environ["FAKE_MIMO_EXIT_CODE"] = "1"
        failed = self.delegate(verification_commands=["true"])
        self.assertEqual(failed["status"], "failed")
        os.environ["FAKE_MIMO_EXIT_CODE"] = "0"
        os.environ["FAKE_MIMO_EDIT"] = "result.txt"
        continued = server.continue_task(
            failed["task_id"], "finish the task", wait_seconds=5
        )
        self.assertEqual(continued["status"], "success")
        self.assertIn("--session", self.read_args()[-1])

    def test_continue_without_session_repeats_full_task_context(self):
        os.environ["FAKE_MIMO_SESSION"] = ""
        os.environ["FAKE_MIMO_EDIT"] = ""
        os.environ["FAKE_MIMO_EXIT_CODE"] = "1"
        failed = self.delegate(verification_commands=["true"])
        self.assertEqual(failed["status"], "failed")

        os.environ["FAKE_MIMO_EXIT_CODE"] = "0"
        os.environ["FAKE_MIMO_EDIT"] = "result.txt"
        continued = server.continue_task(
            failed["task_id"], "finish the task", wait_seconds=5
        )
        self.assertEqual(continued["status"], "success")
        last_call = self.read_args()[-1]
        self.assertNotIn("--session", last_call)
        self.assertIn("Task: create result", last_call[-1])
        self.assertIn("finish the task", last_call[-1])


class TestVerificationRunner(unittest.TestCase):
    def test_timeout_is_separate_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            result = server._run_verification(
                Path(directory), ["sleep 2"], timeout_seconds=1
            )
        self.assertFalse(result[0]["passed"])
        self.assertTrue(result[0]["timed_out"])
        self.assertIn("Timeout", result[0]["stderr"])


class TestPluginSkillMetadata(unittest.TestCase):
    def test_default_prompt_names_skill(self):
        yaml_path = (
            Path(__file__).resolve().parent.parent
            / "skills"
            / "delegate-to-mimo"
            / "agents"
            / "openai.yaml"
        )
        self.assertIn("$delegate-to-mimo", yaml_path.read_text())


if __name__ == "__main__":
    unittest.main()

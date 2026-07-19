"""Unit and process-level tests for the MiMo Delegator MCP server."""

import datetime
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
        for task_id in list(server._LOCAL_WORKERS):
            proc = server._LOCAL_WORKERS.pop(task_id, None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

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
        self.assertIn("changed_files_count", result)
        self.assertGreaterEqual(result["elapsed_seconds"], 0)

    def test_brief_omits_full_file_list(self):
        task_id, _ = self.make_state(changed_files=["a.py", "b.py", "c.py"])
        brief = server.task_result(task_id, detail="brief")
        self.assertIn("changed_files_count", brief)
        self.assertEqual(brief["changed_files_count"], 3)
        self.assertNotIn("changed_files", brief)
        self.assertNotIn("key_decisions", brief)

    def test_summary_includes_full_details(self):
        task_id, _ = self.make_state(
            changed_files=["a.py"],
            key_decisions=["used approach X"],
            known_issues=["flaky test"],
        )
        summary = server.task_result(task_id, detail="summary")
        self.assertIn("changed_files", summary)
        self.assertEqual(summary["changed_files"], ["a.py"])
        self.assertIn("key_decisions", summary)
        self.assertIn("known_issues", summary)

    def test_brief_has_recommended_next_action(self):
        task_id, _ = self.make_state(status="success")
        brief = server.task_result(task_id, detail="brief")
        self.assertIn("recommended_next_action", brief)
        self.assertIn("inspect", brief["recommended_next_action"])

    def test_brief_has_phase(self):
        task_id, _ = self.make_state(status="running")
        brief = server.task_result(task_id, detail="brief")
        self.assertIn("phase", brief)

    def test_task_result_rejects_invalid_detail(self):
        task_id, _ = self.make_state()
        with self.assertRaises(ValueError):
            server.task_result(task_id, detail="evidence")

    def test_task_result_schema_has_detail_enum(self):
        import asyncio

        async def _check():
            tools = await server.mcp.list_tools()
            for tool in tools:
                if tool.name == "task_result":
                    schema = tool.inputSchema
                    detail_prop = schema["properties"]["detail"]
                    self.assertEqual(detail_prop["type"], "string")
                    self.assertEqual(detail_prop["default"], "brief")
                    self.assertEqual(sorted(detail_prop["enum"]), ["brief", "summary"])
                    return
            self.fail("task_result tool not found in MCP tool list")

        asyncio.run(_check())

    def test_missing_and_invalid_task_ids_fail(self):
        with self.assertRaises(FileNotFoundError):
            server.task_result("mimo-000000000000")
        with self.assertRaises(ValueError):
            server.task_result("bad-id")

    def test_dead_worker_is_recovered_as_worker_crashed(self):
        task_id, _ = self.make_state(
            status="running", worker_pid=999_999_999, finished_ts=None
        )
        with patch.object(server, "_worker_process_matches", return_value=False):
            result = server.task_result(task_id)
        self.assertEqual(result["status"], "worker_crashed")
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

        with patch.object(server, "_worker_process_matches", return_value=False):
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

        with patch.object(server, "_worker_process_matches", return_value=True):
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
        descendant = subprocess.Popen(
            [sys.executable, "-c", descendant_code, str(ready_path)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 2
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready_path.exists(), "descendant process did not start")
        process_group_id = descendant.pid

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
                with patch.object(server, "_worker_process_matches", side_effect=lambda pid, tid="": process_alive(pid)):
                    with patch.object(
                        server, "_release_workdir", side_effect=release_after_child_stopped
                    ) as release:
                        refreshed = server._refresh_stale_task(
                            server._load_task_state(task_id)
                        )
            self.assertEqual(refreshed["status"], "worker_crashed")
            release.assert_called_once()
        finally:
            if process_group_alive():
                os.killpg(process_group_id, signal.SIGKILL)
                deadline = time.monotonic() + 2
                while process_group_alive() and time.monotonic() < deadline:
                    time.sleep(0.02)
            descendant.wait(timeout=2)

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
        self.assertEqual(state["phase"], "finished")
        self.assertIsNone(state["last_heartbeat_at"])
        self.assertIsNone(state["last_progress_at"])
        self.assertEqual(state["progress_note"], "")

    def test_old_state_without_new_fields_gets_defaults(self):
        task_id = server._gen_task_id()
        server._task_path(task_id).write_text(
            json.dumps({"task_id": task_id, "status": "running", "workdir": "/tmp"})
        )
        state = server._load_task_state(task_id)
        self.assertEqual(state["phase"], "running")
        self.assertIsNone(state["last_heartbeat_at"])
        self.assertEqual(state["progress_note"], "")
        self.assertEqual(state["key_decisions"], [])
        self.assertEqual(state["known_issues"], [])

    def test_stalled_from_heartbeat_expiry(self):
        old_hb = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=server.STALLED_HEARTBEAT_SECONDS + 10)
        ).isoformat()
        task_id, _ = self.make_state(
            status="running",
            worker_pid=999_999_999,
            finished_ts=None,
            last_heartbeat_at=old_hb,
        )
        with patch.object(server, "_worker_process_matches", return_value=True):
            result = server.task_result(task_id)
        self.assertEqual(result["status"], "stalled")
        self.assertIn("heartbeat expired", result["summary"])
        self.assertTrue(result.get("worker_active"))
        self.assertEqual(result.get("stall_reason"), "heartbeat_expired")


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
            server._reap_local_worker(task_id, 1)
        for task_id in list(server._LOCAL_WORKERS):
            proc = server._LOCAL_WORKERS.pop(task_id, None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
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
        self.assertEqual(result["changed_files_count"], 1)
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


class TestTaskEvidence(IsolatedStateTest):
    def test_evidence_returns_log_tail(self):
        task_id, state = self.make_state()
        log_path = Path(state["log_path"])
        lines = [f"line {i}\n" for i in range(100)]
        log_path.write_text("".join(lines), encoding="utf-8")
        result = server.task_evidence(task_id, log_tail_lines=10)
        self.assertIn("line 99", result["log_tail"])
        self.assertNotIn("line 0", result["log_tail"])

    def test_evidence_log_bounded_by_chars(self):
        task_id, state = self.make_state()
        log_path = Path(state["log_path"])
        log_path.write_text("x" * 100_000, encoding="utf-8")
        result = server.task_evidence(task_id, log_tail_lines=200)
        self.assertLessEqual(len(result["log_tail"]), server.MAX_EVIDENCE_CHARS)

    def test_evidence_verification_output(self):
        task_id, state = self.make_state(
            verification=[
                {
                    "command": "pytest",
                    "passed": False,
                    "exit_code": 1,
                    "stdout": "out" * 100,
                    "stderr": "err" * 100,
                }
            ]
        )
        result = server.task_evidence(task_id, verification_output=True)
        self.assertEqual(len(result["verification"]), 1)
        self.assertFalse(result["verification"][0]["passed"])

    def test_evidence_verification_disabled_by_default(self):
        task_id, state = self.make_state(
            verification=[{"command": "echo ok", "passed": True, "exit_code": 0}]
        )
        result = server.task_evidence(task_id)
        self.assertEqual(result["verification"], [])

    def test_evidence_rejects_invalid_task_id(self):
        with self.assertRaises(ValueError):
            server.task_evidence("bad-id")


class TestContinueResumeIntent(IsolatedStateTest):
    def test_resume_intent_stored_in_state(self):
        task_id, _ = self.make_state(status="failed")
        with patch.object(server, "_locate_mimo", return_value="/bin/true"):
            with patch.object(server, "_claim_workdir"):
                with patch.object(server, "_launch_worker"):
                    server.continue_task(
                        task_id, "fix it", resume_intent="fix_verification"
                    )
        state = server._load_task_state(task_id)
        self.assertEqual(state.get("resume_intent"), "fix_verification")

    def test_scope_violation_still_cannot_continue(self):
        task_id, _ = self.make_state(status="scope_violation")
        with self.assertRaises(ValueError):
            server.continue_task(task_id, "try again", resume_intent="retry_transient")


class TestPhaseInState(IsolatedStateTest):
    def test_new_task_starts_with_queued_phase(self):
        task_id, _ = self.make_state(status="queued")
        state = server._load_task_state(task_id)
        self.assertEqual(state["phase"], "queued")

    def test_phase_matches_terminal_status(self):
        for status in ["success", "failed", "stalled", "cancelled", "worker_crashed"]:
            task_id, _ = self.make_state(status=status)
            state = server._load_task_state(task_id)
            self.assertEqual(state["phase"], "finished", f"phase for {status}")

    def test_heartbeat_fields_present(self):
        task_id, _ = self.make_state()
        state = server._load_task_state(task_id)
        self.assertIn("last_heartbeat_at", state)
        self.assertIn("last_progress_at", state)
        self.assertIn("progress_note", state)


class TestHeartbeatNotInterruptedByWait(IsolatedStateTest):
    def test_wait_returns_without_killing_worker(self):
        task_id, _ = self.make_state(status="running", worker_pid=999_999_999)
        with patch.object(server, "_process_alive", return_value=True):
            with patch.object(server, "_refresh_stale_task", side_effect=lambda s: s):
                result = server.wait_task(task_id, 0)
        self.assertEqual(result["status"], "running")


class RuntimeIntegrationV03Test(unittest.TestCase):
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
            server._reap_local_worker(task_id, 1)
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

    def test_brief_result_for_completed_task(self):
        task_id = self.delegate()["task_id"]
        result = server.task_result(task_id, detail="brief")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["phase"], "finished")
        self.assertIn("changed_files_count", result)
        self.assertIn("verification_conclusion", result)
        self.assertIn("recommended_next_action", result)
        self.assertEqual(result["changed_files_count"], 1)

    def test_summary_result_for_completed_task(self):
        result = server.task_result(self.delegate()["task_id"], detail="summary")
        self.assertIn("changed_files", result)
        self.assertIn("key_decisions", result)

    def test_evidence_tool_for_completed_task(self):
        task_id = self.delegate()["task_id"]
        evidence = server.task_evidence(task_id, log_tail_lines=10)
        self.assertIn("log_tail", evidence)
        self.assertEqual(evidence["status"], "success")

    def test_phase_transitions_during_execution(self):
        os.environ["FAKE_MIMO_SLEEP_BEFORE"] = "1.2"
        result = self.delegate(wait_seconds=0)
        self.task_ids.append(result["task_id"])
        state = server._load_task_state(result["task_id"])
        self.assertIn(state["phase"], ("queued", "starting", "running"))
        final = server.wait_task(result["task_id"], 5)
        self.assertEqual(final["phase"], "finished")

    def test_heartbeat_updated_during_execution(self):
        os.environ["FAKE_MIMO_SLEEP_BEFORE"] = "1.5"
        result = self.delegate(wait_seconds=0)
        self.task_ids.append(result["task_id"])
        time.sleep(0.3)
        state = server._load_task_state(result["task_id"])
        self.assertIsNotNone(state.get("last_heartbeat_at"))
        server.cancel_task(result["task_id"])

    def test_worker_crashed_on_dead_worker(self):
        result = self.delegate(wait_seconds=0)
        self.task_ids.append(result["task_id"])
        state = server._load_task_state(result["task_id"])
        worker_pid = state["worker_pid"]
        server.cancel_task(result["task_id"])
        time.sleep(0.3)
        state = server._load_task_state(result["task_id"])
        if state["status"] in server.ACTIVE_STATUSES:
            fake_pid = 999_999_998
            state["worker_pid"] = fake_pid
            state["status"] = "running"
            server._save_task_state(result["task_id"], state)
            with patch.object(server, "_process_alive", return_value=False):
                refreshed = server._refresh_stale_task(
                    server._load_task_state(result["task_id"])
                )
            self.assertEqual(refreshed["status"], "worker_crashed")


class TestVerificationHeartbeat(IsolatedStateTest):
    def test_verification_updates_heartbeat_periodically(self):
        task_id, state = self.make_state(
            status="running",
            child_pid=0,
            verification=[],
        )
        state["last_heartbeat_at"] = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=600)
        ).isoformat()
        server._save_task_state(task_id, state)
        with tempfile.TemporaryDirectory() as wd:
            with patch.object(server, "HEARTBEAT_INTERVAL_SECONDS", 0.1):
                result = server._run_verification(
                    Path(wd), ["sleep 0.6"], timeout_seconds=5,
                    task_id=task_id, state=state,
                )
        self.assertTrue(result[0]["passed"])
        reloaded = server._load_task_state(task_id)
        hb = reloaded.get("last_heartbeat_at", "")
        self.assertTrue(hb)
        hb_time = datetime.datetime.fromisoformat(hb).timestamp()
        self.assertGreater(hb_time, time.time() - 2)


class TestBriefDefaults(IsolatedStateTest):
    def test_wait_task_returns_brief(self):
        task_id, _ = self.make_state(
            status="success",
            changed_files=["a.py", "b.py"],
            key_decisions=["approach X"],
        )
        result = server.wait_task(task_id, 0)
        self.assertIn("changed_files_count", result)
        self.assertNotIn("changed_files", result)
        self.assertNotIn("key_decisions", result)

    def test_delegate_returns_brief(self):
        task_id, _ = self.make_state(
            status="success",
            changed_files=["a.py"],
        )
        result = server.task_result(task_id, detail="brief")
        self.assertIn("changed_files_count", result)
        self.assertNotIn("changed_files", result)


class TestStalledContinueGuard(IsolatedStateTest):
    def test_continue_rejects_stalled_with_active_worker(self):
        task_id, _ = self.make_state(
            status="stalled",
            stall_reason="heartbeat_expired",
            worker_active=True,
            worker_pid=12345,
        )
        with patch.object(server, "_worker_process_matches", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                server.continue_task(task_id, "fix it", wait_seconds=0)
            self.assertIn("stalled", str(ctx.exception))
            self.assertIn("still running", str(ctx.exception))

    def test_continue_accepts_stalled_without_active_worker(self):
        task_id, _ = self.make_state(
            status="stalled",
            stall_reason="runtime_or_verification_timeout",
            worker_active=False,
            worker_pid=0,
        )
        with patch.object(server, "_locate_mimo", return_value="/bin/true"):
            with patch.object(server, "_claim_workdir"):
                with patch.object(server, "_launch_worker"):
                    with patch.object(server, "_wait_for_task", return_value={"status": "queued"}):
                        result = server.continue_task(task_id, "fix it", wait_seconds=0)
        self.assertEqual(result["status"], "queued")


class TestWorkerProcessMatches(IsolatedStateTest):
    def test_returns_false_for_dead_pid(self):
        self.assertFalse(server._worker_process_matches(999_999_999))

    def test_returns_false_when_cmdline_unavailable(self):
        with patch.object(server, "_read_process_cmdline", return_value=None):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._worker_process_matches(12345))

    def test_returns_false_for_non_python_process(self):
        with patch.object(server, "_read_process_cmdline", return_value="/bin/ls -la"):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._worker_process_matches(12345))

    def test_returns_true_for_matching_worker_cmdline(self):
        script = str(Path(server.__file__).resolve())
        cmdline = f"{sys.executable} {script} --worker mimo-abc123def456"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertTrue(server._worker_process_matches(12345))

    def test_returns_false_for_wrong_script_path(self):
        cmdline = f"{sys.executable} /wrong/path.py --worker mimo-abc123def456"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._worker_process_matches(12345))

    def test_returns_false_for_python_without_worker_flag(self):
        script = str(Path(server.__file__).resolve())
        cmdline = f"{sys.executable} {script} mimo-abc123def456"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._worker_process_matches(12345))


class TestIsOurWorkerPid(IsolatedStateTest):
    def test_returns_false_when_cmdline_unavailable(self):
        with patch.object(server, "_read_process_cmdline", return_value=None):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._is_our_worker_pid(12345, "mimo-abc123def456"))

    def test_returns_false_for_foreign_process(self):
        cmdline = "/usr/bin/some_other_script.py --worker mimo-abc123def456"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._is_our_worker_pid(12345, "mimo-abc123def456"))

    def test_returns_true_for_exact_match(self):
        script = str(Path(server.__file__).resolve())
        cmdline = f"{sys.executable} {script} --worker mimo-abc123def456"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertTrue(server._is_our_worker_pid(12345, "mimo-abc123def456"))

    def test_returns_false_for_dead_pid(self):
        self.assertFalse(server._is_our_worker_pid(999_999_999, "mimo-abc123def456"))


class TestDoctor(IsolatedStateTest):
    def test_doctor_all_ok(self):
        with patch.object(server, "_locate_mimo", return_value="/usr/bin/true"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                args=["--version"], returncode=0, stdout="mimo 1.0.0\n"
            )):
                result = server.doctor()
        self.assertEqual(result["mimo"]["status"], "ok")
        self.assertIn("1.0.0", result["mimo"]["version"])
        self.assertIn("All checks passed", result["findings_en"])
        self.assertIn("一切正常", result["findings_zh"])
        self.assertIn("version", result)

    def test_doctor_mimo_missing(self):
        with patch.object(server, "_locate_mimo", side_effect=FileNotFoundError("not found")):
            result = server.doctor()
        self.assertEqual(result["mimo"]["status"], "error")
        self.assertTrue(any("executable" in f.lower() or "可执行" in f for f in result["findings_en"] + result["findings_zh"]))

    def test_doctor_mimo_version_timeout(self):
        with patch.object(server, "_locate_mimo", return_value="/usr/bin/true"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="--version", timeout=5)):
                result = server.doctor()
        self.assertEqual(result["mimo"]["status"], "ok")
        self.assertIn("timeout", result["mimo"]["version"].lower())
        self.assertTrue(any("version" in f.lower() for f in result["findings_en"] + result["findings_zh"]))

    def test_doctor_corrupt_state(self):
        corrupt_path = server.TASKS_DIR / "mimo-corrupt.json"
        corrupt_path.write_text("NOT JSON", encoding="utf-8")
        with patch.object(server, "_locate_mimo", return_value="/bin/true"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="mimo 1.0\n"
            )):
                result = server.doctor()
        self.assertTrue(len(result["corrupt_states"]) >= 1)
        self.assertTrue(any("corrupt" in f.lower() or "损坏" in f for f in result["findings_en"] + result["findings_zh"]))

    def test_doctor_stale_lock(self):
        task_id, _ = self.make_state(status="success")
        lock_path = server._workdir_lock_path(Path("/tmp"))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"task_id": task_id, "lock_token": "tok"}), encoding="utf-8")
        with patch.object(server, "_locate_mimo", return_value="/bin/true"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="mimo 1.0\n"
            )):
                result = server.doctor()
        stale = [l for l in result["stale_locks"] if l["owner_task"] == task_id]
        self.assertTrue(len(stale) >= 1)

    def test_doctor_dirs_writable(self):
        with patch.object(server, "_locate_mimo", return_value="/bin/true"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="mimo 1.0\n"
            )):
                result = server.doctor()
        for d in result["directories"]:
            self.assertEqual(d["status"], "ok")

    def test_doctor_version_mismatch(self):
        src = server.TASKS_DIR / "_src_manifest.json"
        src.write_text(json.dumps({"version": "0.2.0"}), encoding="utf-8")
        with patch.dict(os.environ, {"MIMO_DELEGATOR_SOURCE_PATH": str(src)}):
            with patch.object(server, "_locate_mimo", return_value="/bin/true"):
                with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="mimo 1.0\n"
                )):
                    result = server.doctor()
        self.assertEqual(result["version"].get("source_version"), "0.2.0")
        self.assertFalse(result["version"].get("version_match", True))

    def test_doctor_cli(self):
        import subprocess as _sp
        result = _sp.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "mimo_mcp_server.py"), "--doctor"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "MIMO_EXECUTABLE": "/usr/bin/true", "MIMO_DELEGATOR_HOME": str(server.BASE_DIR)},
        )
        data = json.loads(result.stdout)
        self.assertIn("findings_en", data)
        self.assertIn("mimo", data)
        self.assertIn("version", data)


class TestHeartbeatRecovery(IsolatedStateTest):
    def test_heartbeat_recovery_from_stalled(self):
        old_hb = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=server.STALLED_HEARTBEAT_SECONDS + 10)
        ).isoformat()
        task_id, _ = self.make_state(
            status="running",
            worker_pid=999_999_999,
            finished_ts=None,
            last_heartbeat_at=old_hb,
        )
        with patch.object(server, "_worker_process_matches", return_value=True):
            stalled = server.task_result(task_id)
        self.assertEqual(stalled["status"], "stalled")
        self.assertEqual(stalled["stall_reason"], "heartbeat_expired")
        self.assertTrue(stalled["worker_active"])

        state = server._load_task_state(task_id)
        state["last_heartbeat_at"] = _now_iso()
        server._save_task_state(task_id, state)

        with patch.object(server, "_worker_process_matches", return_value=True):
            recovered = server.task_result(task_id)
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered["phase"], "running")
        self.assertEqual(recovered["stall_reason"], "")

    def test_stalled_worker_dies_transitions_to_crashed(self):
        old_hb = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=server.STALLED_HEARTBEAT_SECONDS + 10)
        ).isoformat()
        task_id, _ = self.make_state(
            status="stalled",
            stall_reason="heartbeat_expired",
            worker_active=True,
            worker_pid=999_999_999,
            finished_ts=None,
        )
        with patch.object(server, "_worker_process_matches", return_value=False):
            with patch.object(server, "_release_workdir"):
                result = server.task_result(task_id)
        self.assertEqual(result["status"], "worker_crashed")
        self.assertFalse(result.get("worker_active"))
        self.assertEqual(result.get("stall_reason"), "")
        reloaded = server._load_task_state(task_id)
        self.assertEqual(reloaded["worker_pid"], 0)
        self.assertEqual(reloaded["child_pid"], 0)

    def test_stalled_worker_alive_keeps_stalled(self):
        old_hb = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=server.STALLED_HEARTBEAT_SECONDS + 10)
        ).isoformat()
        task_id, _ = self.make_state(
            status="stalled",
            stall_reason="heartbeat_expired",
            worker_active=True,
            worker_pid=999_999_999,
            finished_ts=None,
        )
        with patch.object(server, "_worker_process_matches", return_value=True):
            result = server.task_result(task_id)
        self.assertEqual(result["status"], "stalled")
        self.assertTrue(result.get("worker_active"))


class TestHeartbeatTOCTOU(IsolatedStateTest):
    def test_heartbeat_refresh_between_reads_prevents_false_stall(self):
        old_hb = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=server.STALLED_HEARTBEAT_SECONDS + 10)
        ).isoformat()
        task_id, _ = self.make_state(
            status="running",
            worker_pid=999_999_999,
            finished_ts=None,
            last_heartbeat_at=old_hb,
        )
        call_count = [0]
        original_load = server._load_task_state

        def load_with_refresh(*args, **kwargs):
            state = original_load(*args, **kwargs)
            call_count[0] += 1
            if call_count[0] == 2:
                state["last_heartbeat_at"] = _now_iso()
            return state

        with patch.object(server, "_worker_process_matches", return_value=True):
            with patch.object(server, "_load_task_state", side_effect=load_with_refresh):
                result = server.task_result(task_id)
        self.assertEqual(result["status"], "running")
        self.assertNotEqual(result["status"], "stalled")


class TestPIDOwnership(IsolatedStateTest):
    def test_worker_matches_requires_exact_script_path(self):
        cmdline = "/usr/bin/python3 /wrong/path/mimo_mcp_server.py --worker mimo-abc"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._worker_process_matches(12345))

    def test_worker_matches_requires_worker_flag(self):
        script = str(Path(server.__file__).resolve())
        cmdline = f"{sys.executable} {script} mimo-abc123def456"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._worker_process_matches(12345))

    def test_worker_matches_requires_task_id_in_cmdline(self):
        script = str(Path(server.__file__).resolve())
        cmdline = f"{sys.executable} {script} --worker other-task-id"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertFalse(server._worker_process_matches(12345, "mimo-abc123def456"))

    def test_worker_matches_without_task_id_skips_taskid_check(self):
        script = str(Path(server.__file__).resolve())
        cmdline = f"{sys.executable} {script} --worker other-task-id"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertTrue(server._worker_process_matches(12345))

    def test_may_be_active_conservative_on_unknown(self):
        with patch.object(server, "_read_process_cmdline", return_value=None):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertTrue(server._process_may_be_active(12345))

    def test_may_be_active_rejects_foreign_python(self):
        cmdline = "/usr/bin/python3 /other/script.py"
        with patch.object(server, "_read_process_cmdline", return_value=cmdline):
            with patch.object(server, "_process_alive", return_value=True):
                self.assertTrue(server._process_may_be_active(12345))

    def test_may_be_active_rejects_dead(self):
        self.assertFalse(server._process_may_be_active(999_999_999))


class TestContinueClearsMetadata(IsolatedStateTest):
    def test_continue_clears_old_stall_fields(self):
        task_id, _ = self.make_state(
            status="failed",
            stall_reason="heartbeat_expired",
            worker_active=True,
            last_heartbeat_at="2024-01-01T00:00:00+00:00",
            last_progress_at="2024-01-01T00:00:00+00:00",
            progress_note="old note",
        )
        with patch.object(server, "_locate_mimo", return_value="/bin/true"):
            with patch.object(server, "_claim_workdir"):
                with patch.object(server, "_launch_worker"):
                    server.continue_task(task_id, "fix it", wait_seconds=0)
        state = server._load_task_state(task_id)
        self.assertEqual(state["stall_reason"], "")
        self.assertFalse(state["worker_active"])
        self.assertIsNone(state["last_heartbeat_at"])
        self.assertIsNone(state["last_progress_at"])
        self.assertEqual(state["progress_note"], "")


class TestDoctorVersionMismatch(IsolatedStateTest):
    def test_doctor_reports_version_mismatch(self):
        src = server.TASKS_DIR / "_src_manifest.json"
        src.write_text(json.dumps({"version": "0.2.0"}), encoding="utf-8")
        with patch.dict(os.environ, {"MIMO_DELEGATOR_SOURCE_PATH": str(src)}):
            with patch.object(server, "_locate_mimo", return_value="/bin/true"):
                with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="mimo 1.0\n"
                )):
                    result = server.doctor()
        self.assertFalse(result["version"].get("version_match", True))
        self.assertTrue(any("version mismatch" in f.lower() or "版本不一致" in f for f in result["findings_en"] + result["findings_zh"]))

    def test_doctor_reports_unreadable_source_path(self):
        with patch.dict(os.environ, {"MIMO_DELEGATOR_SOURCE_PATH": "/nonexistent/path.json"}):
            with patch.object(server, "_locate_mimo", return_value="/bin/true"):
                with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="mimo 1.0\n"
                )):
                    result = server.doctor()
        self.assertTrue(any("unreadable" in f.lower() or "不可读" in f for f in result["findings_en"] + result["findings_zh"]))

    def test_doctor_reports_directory_source_path(self):
        with patch.dict(os.environ, {"MIMO_DELEGATOR_SOURCE_PATH": str(server.TASKS_DIR)}):
            with patch.object(server, "_locate_mimo", return_value="/bin/true"):
                with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="mimo 1.0\n"
                )):
                    result = server.doctor()
        self.assertEqual(result["version"].get("source_version"), "path-is-directory")
        self.assertTrue(any("unreadable" in f.lower() or "不可读" in f for f in result["findings_en"] + result["findings_zh"]))


def _now_iso():
    return server._now_iso()


if __name__ == "__main__":
    unittest.main()

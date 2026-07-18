"""Tests for mimo_mcp_server — all MiMo CLI calls are mocked."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mimo_mcp_server as server


class TestValidateWorkdir(unittest.TestCase):
    def test_root_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_workdir("/")

    def test_home_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_workdir(str(Path.home()))

    def test_nonexistent_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_workdir("/nonexistent/path/xyz")

    def test_valid_dir_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            result = server._validate_workdir(td)
            self.assertEqual(result, Path(td).resolve())


class TestValidatePaths(unittest.TestCase):
    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_paths(Path("/tmp"), [])

    def test_escape_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                server._validate_paths(Path(td), ["../../etc/passwd"])

    def test_valid_relative_path(self):
        with tempfile.TemporaryDirectory() as td:
            result = server._validate_paths(Path(td), ["src"])
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].is_relative_to(Path(td).resolve()))

    def test_too_many_paths_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError, msg="exceeds max"):
                server._validate_paths(Path(td), [f"d{i}" for i in range(60)])


class TestValidateVerification(unittest.TestCase):
    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_verification([])

    def test_blank_command_rejected(self):
        """#3: blank/whitespace-only commands must be rejected."""
        with self.assertRaises(ValueError, msg="must not be blank"):
            server._validate_verification([""])
        with self.assertRaises(ValueError, msg="must not be blank"):
            server._validate_verification(["   "])
        with self.assertRaises(ValueError, msg="must not be blank"):
            server._validate_verification(["\t\n"])

    def test_too_many_rejected(self):
        with self.assertRaises(ValueError, msg="exceeds max"):
            server._validate_verification([f"cmd{i}" for i in range(25)])

    def test_long_command_rejected(self):
        with self.assertRaises(ValueError, msg="exceeds"):
            server._validate_verification(["x" * 3000])

    def test_nonempty_accepted(self):
        result = server._validate_verification(["pytest", "make lint"])
        self.assertEqual(result, ["pytest", "make lint"])


class TestValidateTaskId(unittest.TestCase):
    def test_valid_uuid_format(self):
        tid = f"mimo-{os.urandom(6).hex()}"
        server._validate_task_id(tid)

    def test_invalid_rejected(self):
        for bad in ["../../../etc/passwd", "mimo-123", "", "mimo-GGGGGGGGGGGG"]:
            with self.assertRaises(ValueError, msg=f"should reject: {bad!r}"):
                server._validate_task_id(bad)


class TestDiffFiles(unittest.TestCase):
    def test_added_file(self):
        self.assertEqual(server._diff_files({}, {"a.py": "x"}), ["a.py"])

    def test_modified_file(self):
        self.assertEqual(server._diff_files({"a.py": "1"}, {"a.py": "2"}), ["a.py"])

    def test_deleted_file(self):
        self.assertEqual(server._diff_files({"a.py": "1"}, {}), ["a.py"])

    def test_no_changes(self):
        snap = {"a.py": "x", "b.py": "y"}
        self.assertEqual(server._diff_files(snap, snap), [])


class TestCheckScope(unittest.TestCase):
    def test_all_in_scope(self):
        with tempfile.TemporaryDirectory() as td:
            allowed = [Path(td) / "src"]
            ok, v = server._check_scope(["src/main.py"], allowed, Path(td))
            self.assertTrue(ok)
            self.assertEqual(v, [])

    def test_violation_detected(self):
        with tempfile.TemporaryDirectory() as td:
            allowed = [Path(td) / "src"]
            ok, v = server._check_scope(["../etc/passwd"], allowed, Path(td))
            self.assertFalse(ok)
            self.assertIn("../etc/passwd", v)

    def test_workdir_as_explicit_base(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            allowed = [wd / "a" / "deep"]
            ok, _ = server._check_scope(["a/deep/file.py"], allowed, wd)
            self.assertTrue(ok)

    def test_single_file_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            f = wd / "single.txt"
            f.write_text("x")
            ok, _ = server._check_scope(["single.txt"], [f], wd)
            self.assertTrue(ok)
            ok2, _ = server._check_scope(["other.txt"], [f], wd)
            self.assertFalse(ok2)


class TestParseMimoOutput(unittest.TestCase):
    def test_real_part_object_format(self):
        """#1: Real MiMo JSONL: {type:'text', sessionID:'...', part:{type:'text', text:'OK'}}."""
        lines = [
            json.dumps({"sessionID": "sess-real"}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "All tests pass"}}),
        ]
        result = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(result["session_id"], "sess-real")
        self.assertEqual(result["summary"], "All tests pass")

    def test_parts_array_fallback(self):
        lines = [
            json.dumps({"sessionID": "s1"}),
            json.dumps({"type": "text", "parts": [{"type": "text", "text": "chunk"}]}),
        ]
        result = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["summary"], "chunk")

    def test_top_level_text_fallback(self):
        lines = [
            json.dumps({"session_id": "s2"}),
            json.dumps({"type": "text", "text": "direct text"}),
        ]
        result = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(result["session_id"], "s2")
        self.assertEqual(result["summary"], "direct text")

    def test_part_object_takes_precedence(self):
        """#1: singular part object should win over parts array."""
        lines = [
            json.dumps({"sessionID": "s3"}),
            json.dumps({
                "type": "text",
                "part": {"type": "text", "text": "from_part"},
                "parts": [{"type": "text", "text": "from_parts"}],
            }),
        ]
        result = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(result["summary"], "from_part")

    def test_last_valid_text_wins(self):
        lines = [
            json.dumps({"type": "text", "part": {"type": "text", "text": "first"}}),
            json.dumps({"type": "text", "text": "second"}),
        ]
        result = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(result["summary"], "second")

    def test_session_id_preserved_from_separate_line(self):
        lines = [
            json.dumps({"sessionID": "sid-abc"}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "ok"}}),
        ]
        result = server._parse_mimo_output("\n".join(lines))
        self.assertEqual(result["session_id"], "sid-abc")

    def test_malformed_input(self):
        result = server._parse_mimo_output("not json")
        self.assertEqual(result["session_id"], "")
        self.assertEqual(result["summary"], "")


class TestSnapshotFiles(unittest.TestCase):
    def test_metadata_includes_size(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "test.py").write_text("hello")
            snap = server._snapshot_files(root)
            mtime_hex, size_hex = snap["test.py"].split(":")
            self.assertTrue(len(mtime_hex) > 0)
            self.assertEqual(int(size_hex, 16), 5)

    def test_skips_git_venv_node_modules_cache_gguf(self):
        """#2: skips .git/.venv/node_modules/cache dirs + *.gguf (case-insensitive)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for d in [".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"]:
                (root / d).mkdir()
                (root / d / "f.txt").write_text("x")
            (root / "data").mkdir()
            (root / "data" / "keep.txt").write_text("k")
            (root / "model.gguf").write_text("g")
            (root / "model.GGUF").write_text("G")
            (root / "code.py").write_text("c")
            snap = server._snapshot_files(root)
            self.assertIn("code.py", snap)
            self.assertIn("data/keep.txt", snap)
            self.assertNotIn("model.gguf", snap)
            self.assertNotIn("model.GGUF", snap)
            for d in [".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"]:
                self.assertFalse(any(d in k for k in snap))

    def test_skips_data_qdrant_not_entire_data(self):
        """#2: only data/qdrant is skipped, not entire data dir; qdrant itself is NOT in SKIP_DIRS."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            (root / "data" / "qdrant").mkdir()
            (root / "data" / "qdrant" / "chunk.txt").write_text("x")
            (root / "data" / "other.txt").write_text("y")
            (root / "qdrant").mkdir()
            (root / "qdrant" / "standalone.txt").write_text("z")
            snap = server._snapshot_files(root)
            self.assertIn("data/other.txt", snap)
            self.assertNotIn("data/qdrant/chunk.txt", snap)
            self.assertIn("qdrant/standalone.txt", snap)

    def test_no_generic_qdrant_in_skip_dirs(self):
        """#2: qdrant must NOT be in SKIP_DIRS — only data/qdrant is excluded."""
        self.assertNotIn("qdrant", server.SKIP_DIRS)


class TestGenTaskId(unittest.TestCase):
    def test_uuid_format(self):
        tid = server._gen_task_id()
        self.assertTrue(tid.startswith("mimo-"))
        self.assertEqual(len(tid), 17)

    def test_no_collision(self):
        ids = {server._gen_task_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)


class TestBuildMimoPrompt(unittest.TestCase):
    def test_includes_allowed_and_verification(self):
        prompt = server._build_mimo_prompt("fix bug", ["src", "tests"], ["pytest", "make lint"])
        self.assertIn("src", prompt)
        self.assertIn("pytest", prompt)
        self.assertIn("fix bug", prompt)
        self.assertIn("Scope:", prompt)


class TestCompressChangedFiles(unittest.TestCase):
    def test_under_limit(self):
        files = [f"f{i}.py" for i in range(40)]
        result = server._compress_changed_files(files)
        self.assertEqual(len(result), 40)
        self.assertNotIn("more files", result[-1] if result else "")

    def test_over_limit_truncated(self):
        """#4: >50 files should show first 50 + overflow indicator."""
        files = [f"f{i}.py" for i in range(1000)]
        result = server._compress_changed_files(files)
        self.assertEqual(len(result), 51)
        self.assertIn("950 more files", result[-1])

    def test_exactly_50(self):
        files = [f"f{i}.py" for i in range(50)]
        result = server._compress_changed_files(files)
        self.assertEqual(len(result), 50)


class TestCompressVerificationResult(unittest.TestCase):
    def test_passed_commands_compressed(self):
        v = [{"command": "pytest", "exit_code": 0, "passed": True, "stdout": "ok", "stderr": ""}]
        result = server._compress_verification_result(v)
        self.assertEqual(result[0]["command"], "pytest")
        self.assertTrue(result[0]["passed"])
        self.assertNotIn("stderr", result[0])
        self.assertNotIn("stdout", result[0])

    def test_failed_commands_show_tail(self):
        v = [{"command": "pytest -x", "exit_code": 1, "passed": False, "stdout": "out" * 200, "stderr": "err" * 200}]
        result = server._compress_verification_result(v)
        self.assertFalse(result[0]["passed"])
        self.assertLessEqual(len(result[0]["stderr"]), server.MAX_VERIFICATION_OUTPUT_CHARS)
        self.assertLessEqual(len(result[0]["stdout"]), server.MAX_VERIFICATION_OUTPUT_CHARS)

    def test_long_command_truncated(self):
        """#4: command truncated to 200 chars."""
        v = [{"command": "x" * 500, "exit_code": 0, "passed": True, "stdout": "", "stderr": ""}]
        result = server._compress_verification_result(v)
        self.assertLessEqual(len(result[0]["command"]), server.MAX_VERIFICATION_CMD_CHARS)


class TestValidateNonblank(unittest.TestCase):
    def test_blank_rejected(self):
        with self.assertRaises(ValueError):
            server._validate_nonblank("", "task")
        with self.assertRaises(ValueError):
            server._validate_nonblank("   ", "task")
        with self.assertRaises(ValueError):
            server._validate_nonblank("\n\t", "feedback")


class TestDelegateTask(unittest.TestCase):
    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_first_pass_success(self, mock_run, mock_locate):
        with tempfile.TemporaryDirectory() as td:
            mock_run.return_value = MagicMock(
                stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": "ok"}}),
                stderr="", returncode=0,
            )
            result = server.delegate_task(
                workdir=td, task="impl", allowed_paths=["."],
                verification_commands=["echo ok"],
            )
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["iterations"], 1)
            self.assertEqual(result["session_id"], "s1")
            self.assertTrue(result["task_id"].startswith("mimo-"))

    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_retry_includes_session_flag(self, mock_run, mock_locate):
        """#1: iteration > 1 must include --session."""
        with tempfile.TemporaryDirectory() as td:
            call_n = [0]

            def side_effect(*args, **kwargs):
                call_n[0] += 1
                if call_n[0] == 1:
                    return MagicMock(
                        stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": ""}}),
                        stderr="", returncode=0,
                    )
                return MagicMock(
                    stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": "fixed"}}),
                    stderr="", returncode=0,
                )

            mock_run.side_effect = side_effect
            verify_n = [0]

            def mock_verify(wd, cmds, timeout):
                verify_n[0] += 1
                if verify_n[0] == 1:
                    return [{"command": "t", "exit_code": 1, "passed": False, "stdout": "", "stderr": "err", "elapsed_s": 0}]
                return [{"command": "t", "exit_code": 0, "passed": True, "stdout": "", "stderr": "", "elapsed_s": 0}]

            with patch("mimo_mcp_server._run_verification", side_effect=mock_verify):
                result = server.delegate_task(
                    workdir=td, task="fix", allowed_paths=["."],
                    verification_commands=["t"], max_iterations=3,
                )

            calls = mock_run.call_args_list
            self.assertEqual(len(calls), 2)
            second_args = calls[1][0][0]
            self.assertIn("--session", second_args)
            self.assertIn("s1", second_args)
            self.assertEqual(result["status"], "success")

    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_changed_files_accumulated(self, mock_run, mock_locate):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            call_n = [0]

            def side_effect(*args, **kwargs):
                call_n[0] += 1
                (root / f"iter{call_n[0]}.py").write_text(str(call_n[0]))
                return MagicMock(
                    stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": ""}}),
                    stderr="", returncode=0,
                )

            mock_run.side_effect = side_effect
            verify_n = [0]

            def mock_verify(wd, cmds, timeout):
                verify_n[0] += 1
                if verify_n[0] <= 2:
                    return [{"command": "t", "exit_code": 1, "passed": False, "stdout": "", "stderr": "", "elapsed_s": 0}]
                return [{"command": "t", "exit_code": 0, "passed": True, "stdout": "", "stderr": "", "elapsed_s": 0}]

            with patch("mimo_mcp_server._run_verification", side_effect=mock_verify):
                result = server.delegate_task(
                    workdir=td, task="accum", allowed_paths=["."],
                    verification_commands=["t"], max_iterations=3,
                )

            self.assertEqual(result["iterations"], 3)
            self.assertIn("iter1.py", result["changed_files"])
            self.assertIn("iter2.py", result["changed_files"])
            self.assertIn("iter3.py", result["changed_files"])

    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_scope_violation(self, mock_run, mock_locate):
        with tempfile.TemporaryDirectory() as td:
            mock_run.return_value = MagicMock(
                stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": ""}}),
                stderr="", returncode=0,
            )
            snap_n = [0]

            def mock_snap(root):
                snap_n[0] += 1
                if snap_n[0] == 1:
                    return {}
                return {"../etc/passwd": "abc"}

            with patch("mimo_mcp_server._snapshot_files", side_effect=mock_snap):
                result = server.delegate_task(
                    workdir=td, task="task", allowed_paths=["src"],
                    verification_commands=["echo ok"],
                )
            self.assertEqual(result["status"], "scope_violation")
            self.assertIn("outside allowed_paths", result["summary"])

    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_returncode_nonzero_fails(self, mock_run, mock_locate):
        with tempfile.TemporaryDirectory() as td:
            mock_run.return_value = MagicMock(
                stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": "done"}}),
                stderr="", returncode=1,
            )
            with patch(
                "mimo_mcp_server._run_verification",
                return_value=[{"command": "t", "exit_code": 0, "passed": True, "stdout": "", "stderr": "", "elapsed_s": 0}],
            ):
                result = server.delegate_task(
                    workdir=td, task="task", allowed_paths=["."],
                    verification_commands=["t"], max_iterations=1,
                )
            self.assertEqual(result["status"], "failed")

    def test_blank_task_rejected(self):
        """#3: blank task must be rejected."""
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError, msg="must not be blank"):
                server.delegate_task(workdir=td, task="", allowed_paths=["."], verification_commands=["t"])
            with self.assertRaises(ValueError, msg="must not be blank"):
                server.delegate_task(workdir=td, task="   ", allowed_paths=["."], verification_commands=["t"])

    def test_bool_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(TypeError):
                server.delegate_task(
                    workdir=td, task="t", allowed_paths=["."],
                    verification_commands=["t"], max_iterations=True,
                )

    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_1000_changed_files_compressed(self, mock_run, mock_locate):
        """#4: 1000 changed_files => return max 50 + overflow indicator."""
        with tempfile.TemporaryDirectory() as td:
            mock_run.return_value = MagicMock(
                stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": ""}}),
                stderr="", returncode=0,
            )
            # Mock snapshot to return 1000 files
            big_snap = {f"dir/f{i}.py": f"{i:x}:10" for i in range(1000)}
            snap_n = [0]

            def mock_snap(root):
                snap_n[0] += 1
                if snap_n[0] == 1:
                    return {}
                return big_snap

            with patch("mimo_mcp_server._snapshot_files", side_effect=mock_snap):
                with patch(
                    "mimo_mcp_server._run_verification",
                    return_value=[{"command": "t", "exit_code": 0, "passed": True, "stdout": "", "stderr": "", "elapsed_s": 0}],
                ):
                    result = server.delegate_task(
                        workdir=td, task="big", allowed_paths=["."],
                        verification_commands=["t"], max_iterations=1,
                    )

            self.assertEqual(len(result["changed_files"]), 51)
            self.assertIn("950 more files", result["changed_files"][-1])
            # Serialized result must be under 20KB
            serialized = json.dumps(result)
            self.assertLess(len(serialized), 20_000)

    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_20_long_commands_compressed(self, mock_run, mock_locate):
        """#4: 20 long verification commands => compressed in return."""
        with tempfile.TemporaryDirectory() as td:
            mock_run.return_value = MagicMock(
                stdout=json.dumps({"sessionID": "s1", "type": "text", "part": {"type": "text", "text": ""}}),
                stderr="", returncode=0,
            )
            cmds = [f"echo {'x' * 500} && test_{i}" for i in range(20)]
            with patch(
                "mimo_mcp_server._run_verification",
                return_value=[{"command": c, "exit_code": 0, "passed": True, "stdout": "y" * 1000, "stderr": "z" * 1000} for c in cmds],
            ):
                result = server.delegate_task(
                    workdir=td, task="cmds", allowed_paths=["."],
                    verification_commands=["placeholder"], max_iterations=1,
                )

            # verification comes from _run_verification mock, compress applied to return
            serialized = json.dumps(result)
            self.assertLess(len(serialized), 20_000)
            # Each command in return should be truncated to 200 chars
            for v in result["verification"]:
                self.assertLessEqual(len(v.get("command", "")), server.MAX_VERIFICATION_CMD_CHARS)


class TestContinueTask(unittest.TestCase):
    @patch("mimo_mcp_server._locate_mimo", return_value="/usr/bin/echo")
    @patch("mimo_mcp_server.subprocess.run")
    def test_continue_resumes_with_session(self, mock_run, mock_locate):
        with tempfile.TemporaryDirectory() as td:
            task_id = f"mimo-{os.urandom(6).hex()}"
            state = {
                "task_id": task_id, "workdir": td, "task": "orig",
                "allowed_paths": ["."], "verification_commands": ["echo ok"],
                "max_iterations": 3, "timeout_seconds": 60,
                "session_id": "old-session", "iterations": 1,
                "changed_files": [], "verification": [], "summary": "",
                "status": "failed",
                "log_path": str(server.LOGS_DIR / f"{task_id}.log"),
                "created_at": "2026-01-01T00:00:00Z",
            }
            server._save_task_state(task_id, state)

            mock_run.return_value = MagicMock(
                stdout=json.dumps({"sessionID": "new-sess", "type": "text", "part": {"type": "text", "text": "fixed"}}),
                stderr="", returncode=0,
            )
            with patch(
                "mimo_mcp_server._run_verification",
                return_value=[{"command": "echo ok", "exit_code": 0, "passed": True, "stdout": "", "stderr": "", "elapsed_s": 0}],
            ):
                result = server.continue_task(task_id, feedback="try again")

            cargs = mock_run.call_args[0][0]
            self.assertIn("--session", cargs)
            self.assertIn("old-session", cargs)
            self.assertEqual(result["status"], "success")

    def test_blank_feedback_rejected(self):
        """#3: blank feedback must be rejected."""
        with self.assertRaises(ValueError, msg="must not be blank"):
            server.continue_task("mimo-000000000000", "")
        with self.assertRaises(ValueError, msg="must not be blank"):
            server.continue_task("mimo-000000000000", "   ")

    def test_scope_violation_blocks_continue(self):
        """#3: scope_violation status must block continue_task."""
        with tempfile.TemporaryDirectory() as td:
            task_id = f"mimo-{os.urandom(6).hex()}"
            state = {
                "task_id": task_id, "workdir": td, "task": "t",
                "allowed_paths": ["."], "verification_commands": ["echo"],
                "max_iterations": 3, "timeout_seconds": 60,
                "session_id": "s1", "iterations": 1,
                "changed_files": [], "verification": [], "summary": "",
                "status": "scope_violation",
                "log_path": str(server.LOGS_DIR / f"{task_id}.log"),
                "created_at": "2026-01-01T00:00:00Z",
            }
            server._save_task_state(task_id, state)
            with self.assertRaises(ValueError, msg="scope_violation"):
                server.continue_task(task_id, feedback="fix")

    def test_invalid_task_id_rejected(self):
        with self.assertRaises(ValueError):
            server.continue_task("../../../etc/passwd", "feedback")


class TestTaskResult(unittest.TestCase):
    def test_returns_compact_state(self):
        task_id = f"mimo-{os.urandom(6).hex()}"
        state = {
            "task_id": task_id, "workdir": "/tmp", "task": "t",
            "allowed_paths": ["."], "verification_commands": ["echo"],
            "max_iterations": 3, "timeout_seconds": 60,
            "session_id": "s1", "iterations": 2,
            "changed_files": ["a.py"],
            "verification": [{"command": "echo", "passed": True}],
            "summary": "all good", "status": "success",
            "log_path": "/tmp/log",
            "created_at": "2026-01-01T00:00:00Z",
        }
        server._save_task_state(task_id, state)
        result = server.task_result(task_id)
        self.assertEqual(result["task_id"], task_id)
        self.assertEqual(result["status"], "success")
        self.assertLessEqual(len(result["summary"]), server.MAX_SUMMARY_CHARS)

    def test_missing_task_raises(self):
        with self.assertRaises(FileNotFoundError):
            server.task_result("mimo-000000000000")

    def test_invalid_task_id_rejected(self):
        with self.assertRaises(ValueError):
            server.task_result("bad-id")


class TestUnusedImports(unittest.TestCase):
    def test_no_hashlib_import(self):
        """#6: hashlib should not be imported (removed in cleanup)."""
        import mimo_mcp_server
        self.assertFalse(hasattr(mimo_mcp_server, 'hashlib'))


class TestOpenaiYaml(unittest.TestCase):
    def test_default_prompt_contains_dollar_delegate_to_mimo(self):
        import yaml
        yaml_path = Path(__file__).resolve().parent.parent / "skills" / "delegate-to-mimo" / "agents" / "openai.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        default_prompt = data["interface"]["default_prompt"]
        self.assertIn("$delegate-to-mimo", default_prompt)


if __name__ == "__main__":
    unittest.main()

"""MiMo Delegator MCP server.

Runs MiMo coding tasks in independent worker processes. MCP calls stay short,
while task state and JSONL output are persisted for later status checks.
"""

import codecs
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Literal

from mcp.server.fastmcp import FastMCP


BASE_DIR = Path(
    os.environ.get(
        "MIMO_DELEGATOR_HOME",
        str(Path.home() / ".codex" / "mimo-delegator"),
    )
).expanduser()
TASKS_DIR = BASE_DIR / "tasks"
LOGS_DIR = BASE_DIR / "logs"
LOCKS_DIR = BASE_DIR / "locks"

MAX_SUMMARY_CHARS = 1200
MAX_VERIFICATION_OUTPUT_CHARS = 500
MAX_VERIFICATION_CMD_CHARS = 200
DEFAULT_WAIT_SECONDS = 45
MAX_WAIT_SECONDS = 55
DEFAULT_MAX_RUNTIME_SECONDS = 0
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_ITERATIONS = 3
LOCK_RESERVATION_GRACE_SECONDS = 30
HOME = Path.home()

HEARTBEAT_INTERVAL_SECONDS = 30
STALLED_HEARTBEAT_SECONDS = int(
    os.environ.get("MIMO_STALLED_HEARTBEAT_SECONDS", "300")
)
MAX_EVIDENCE_LOG_LINES = 200
MAX_EVIDENCE_CHARS = 8000

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

ACTIVE_STATUSES = {"queued", "running", "verifying", "cancelling"}
TERMINAL_STATUSES = {
    "success",
    "failed",
    "stalled",
    "scope_violation",
    "cancelled",
    "worker_crashed",
}

TASK_ID_RE = re.compile(r"^mimo-[0-9a-f]{12}$")

MAX_TASK_LEN = 100_000
MAX_FEEDBACK_LEN = 50_000
MAX_ALLOWED_PATHS = 50
MAX_VERIFICATION_CMDS = 20
MAX_CMD_LEN = 2000
MAX_CHANGED_FILES_RETURN = 50

mcp = FastMCP("mimoDelegator")
_LOCAL_WORKERS: dict[str, subprocess.Popen[Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def _cancel_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.cancel"


def _locate_mimo() -> str:
    env = os.environ.get("MIMO_EXECUTABLE")
    if env:
        if Path(env).is_file() and os.access(env, os.X_OK):
            return env
        raise FileNotFoundError(f"MIMO_EXECUTABLE is not executable: {env}")
    found = shutil.which("mimo")
    if found:
        return found
    home_bin = HOME / ".mimocode" / "bin" / "mimo"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    raise FileNotFoundError(
        "Cannot locate mimo binary. Set MIMO_EXECUTABLE or install to ~/.mimocode/bin/mimo"
    )


def _snapshot_files(root: Path) -> dict[str, str]:
    """Return recursive file metadata used to detect task changes."""
    snap: dict[str, str] = {}
    if not root.is_dir():
        return snap
    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        relative_dir = current.relative_to(root)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS
            and not (relative_dir == Path("data") and name == "qdrant")
        ]
        for filename in filenames:
            path = current / filename
            if path.suffix.lower() == ".gguf":
                continue
            relative = path.relative_to(root)
            try:
                stat = path.stat()
            except OSError:
                continue
            snap[str(relative)] = f"{stat.st_mtime_ns:x}:{stat.st_size:x}"
    return snap


def _diff_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _validate_workdir(workdir: str) -> Path:
    wd = Path(workdir).resolve()
    if not wd.is_dir():
        raise ValueError(f"workdir does not exist: {wd}")
    if wd == Path("/"):
        raise ValueError("workdir must not be /")
    if wd == HOME:
        raise ValueError("workdir must not be the user home directory")
    return wd


def _validate_paths(workdir: Path, allowed_paths: List[str]) -> list[Path]:
    if not allowed_paths:
        raise ValueError("allowed_paths must be non-empty")
    if len(allowed_paths) > MAX_ALLOWED_PATHS:
        raise ValueError(f"allowed_paths exceeds max of {MAX_ALLOWED_PATHS}")
    resolved_wd = workdir.resolve()
    resolved: list[Path] = []
    for path in allowed_paths:
        resolved_path = (workdir / path).resolve()
        if not resolved_path.is_relative_to(resolved_wd):
            raise ValueError(
                f"allowed_path '{path}' escapes workdir (resolved: {resolved_path})"
            )
        resolved.append(resolved_path)
    return resolved


def _validate_verification(verification_commands: List[str]) -> List[str]:
    if not verification_commands:
        raise ValueError("verification_commands must be non-empty")
    if len(verification_commands) > MAX_VERIFICATION_CMDS:
        raise ValueError(
            f"verification_commands exceeds max of {MAX_VERIFICATION_CMDS}"
        )
    for index, command in enumerate(verification_commands):
        if not command or not command.strip():
            raise ValueError(f"verification_command[{index}] must not be blank")
        if len(command) > MAX_CMD_LEN:
            raise ValueError(
                f"verification_command[{index}] exceeds {MAX_CMD_LEN} chars"
            )
    return verification_commands


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise ValueError(f"Invalid task_id format: {task_id}")


def _validate_int(value: int, name: str, minimum: int, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{name} must be >= {minimum}")
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank or whitespace-only")


def _save_task_state(task_id: str, state: dict[str, Any]) -> None:
    """Atomically persist state so readers never observe partial JSON."""
    _ensure_dirs()
    state["updated_at"] = _now_iso()
    state["updated_ts"] = time.time()
    state["revision"] = int(state.get("revision", 0)) + 1
    path = _task_path(task_id)
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        temp_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _load_task_state(task_id: str) -> dict[str, Any]:
    _validate_task_id(task_id)
    path = _task_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"Task {task_id} not found")
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("schema_version", 1)
    state.setdefault(
        "max_runtime_seconds", int(state.get("timeout_seconds", 0))
    )
    state.setdefault(
        "verification_timeout_seconds",
        int(state.get("timeout_seconds", DEFAULT_VERIFICATION_TIMEOUT_SECONDS)),
    )
    state.setdefault("pure", True)
    state.setdefault("lock_token", "")
    state.setdefault("worker_pid", 0)
    state.setdefault("child_pid", 0)
    state.setdefault(
        "worker_error_path", str(LOGS_DIR / f"{task_id}.worker.log")
    )
    state.setdefault("phase", _phase_for_status(state.get("status", "queued")))
    state.setdefault("last_heartbeat_at", None)
    state.setdefault("last_progress_at", None)
    state.setdefault("progress_note", "")
    state.setdefault("key_decisions", [])
    state.setdefault("known_issues", [])
    state.setdefault("stall_reason", "")
    state.setdefault("worker_active", False)
    return state


def _process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _worker_process_matches(pid: int, task_id: str = "") -> bool:
    """Strict check: returns True only if pid is alive and cmdline matches our worker.

    Returns False for dead, foreign, or unknown (cmdline unreadable) processes.
    If task_id is provided, also verifies it appears in the command line.
    Used for ownership decisions where false positives are dangerous.
    """
    if not _process_alive(pid):
        return False
    cmdline = _read_process_cmdline(pid)
    if cmdline is None:
        return False
    if "python" not in cmdline.lower():
        return False
    script = str(Path(__file__).resolve())
    if script in cmdline and "--worker" in cmdline:
        if task_id:
            return task_id in cmdline
        return True
    return False


def _process_may_be_active(pid: int) -> bool:
    """Conservative check: returns True if process is alive and might be ours.

    Used for occupancy/lock checks where blocking is safer than releasing.
    Returns True for alive processes with unreadable cmdline (unknown).
    """
    if not _process_alive(pid):
        return False
    cmdline = _read_process_cmdline(pid)
    if cmdline is None:
        return True
    if "python" not in cmdline.lower():
        return False
    return True


def _is_our_worker_pid(pid: int, task_id: str) -> bool:
    """Strict ownership check for kill decisions. Fail closed: returns False if unsure."""
    if not _process_alive(pid):
        return False
    cmdline = _read_process_cmdline(pid)
    if cmdline is None:
        return False
    script = str(Path(__file__).resolve())
    if script in cmdline and "--worker" in cmdline and task_id in cmdline:
        return True
    return False


def _read_process_cmdline(pid: int) -> str | None:
    """Read process command line. Returns None if unavailable (ps failed, permission, etc)."""
    try:
        procfs = Path(f"/proc/{pid}/cmdline")
        if procfs.exists():
            raw = procfs.read_bytes()
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except (OSError, PermissionError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _process_group_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_if_child(pid: int) -> None:
    """Reap a process only when this server owns it; orphan workers are not children."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, ProcessLookupError):
        pass


def _workdir_lock_path(workdir: Path) -> Path:
    digest = hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()[:20]
    return LOCKS_DIR / f"{digest}.lock"


def _new_lock_token() -> str:
    return uuid.uuid4().hex


def _lock_payload(task_id: str, lock_token: str) -> str:
    return json.dumps({"task_id": task_id, "lock_token": lock_token})


def _read_lock(lock_path: Path) -> tuple[str, str]:
    raw = lock_path.read_text(encoding="utf-8").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw, ""
    if not isinstance(payload, dict):
        return "", ""
    return str(payload.get("task_id", "")), str(payload.get("lock_token", ""))


def _lock_is_recent(lock_path: Path) -> bool:
    try:
        return time.time() - lock_path.stat().st_mtime < LOCK_RESERVATION_GRACE_SECONDS
    except FileNotFoundError:
        return False


def _claim_workdir(workdir: Path, task_id: str, lock_token: str) -> None:
    """Atomically allow only one delegated task per workdir."""
    _ensure_dirs()
    lock_path = _workdir_lock_path(workdir)
    for _ in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                existing_id, existing_token = _read_lock(lock_path)
            except FileNotFoundError:
                continue
            try:
                existing = _load_task_state(existing_id)
            except (ValueError, FileNotFoundError, json.JSONDecodeError):
                if _lock_is_recent(lock_path):
                    raise RuntimeError(
                        f"Workdir has a recent task reservation {existing_id or 'unknown'}"
                    )
                lock_path.unlink(missing_ok=True)
                continue

            state_token = str(existing.get("lock_token", ""))
            if existing_token != state_token and _lock_is_recent(lock_path):
                raise RuntimeError(
                    f"Workdir has a task reservation pending for {existing_id}"
                )
            worker_alive = _process_may_be_active(int(existing.get("worker_pid", 0)))
            child_alive = _process_group_alive(int(existing.get("child_pid", 0)))
            recently_queued = (
                existing.get("status") == "queued"
                and time.time()
                - float(existing.get("updated_ts", existing.get("created_ts", 0)))
                < LOCK_RESERVATION_GRACE_SECONDS
            )
            if worker_alive or child_alive or (
                existing.get("status") in ACTIVE_STATUSES and recently_queued
            ):
                raise RuntimeError(
                    f"Workdir already has active task {existing_id} "
                    f"(status={existing.get('status')})"
                )
            lock_path.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            lock_file.write(_lock_payload(task_id, lock_token))
        return
    raise RuntimeError(f"Could not acquire workdir lock for {workdir}")


def _release_workdir(workdir: Path, task_id: str, lock_token: str) -> None:
    lock_path = _workdir_lock_path(workdir)
    try:
        existing_id, existing_token = _read_lock(lock_path)
        if existing_id == task_id and existing_token == lock_token:
            lock_path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def _parse_mimo_output(raw: str) -> dict[str, Any]:
    """Parse MiMo JSONL for the session ID and latest text summary."""
    session_id = ""
    summary_text = ""
    for line in raw.strip().splitlines():
        try:
            obj = json.loads(line.strip())
        except (json.JSONDecodeError, AttributeError):
            continue
        if not isinstance(obj, dict):
            continue
        sid = obj.get("sessionID") or obj.get("session_id", "")
        if sid:
            session_id = sid
        if obj.get("type") != "text":
            continue
        text_value = ""
        part = obj.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            text_value = part.get("text", "")
        if not text_value:
            parts = obj.get("parts", [])
            if isinstance(parts, list):
                for candidate in reversed(parts):
                    if isinstance(candidate, dict) and candidate.get("type") == "text":
                        text_value = candidate.get("text", "")
                        if text_value:
                            break
        if not text_value:
            text_value = obj.get("text", "")
        if text_value:
            summary_text = text_value
    return {
        "session_id": session_id,
        "summary": summary_text[:MAX_SUMMARY_CHARS],
    }


def _compact_failure_info(
    changed_files: list[str], verification: list[dict[str, Any]], summary: str
) -> str:
    failed = [item for item in verification if not item.get("passed")]
    lines = [f"Previous attempt: {summary[:300]}"]
    lines.append(f"Changed: {changed_files[:20]}")
    for item in failed[:5]:
        error_tail = (item.get("stderr", "") or "")[-200:]
        lines.append(
            f"FAIL [{item['command'][:100]}] rc={item['exit_code']}: {error_tail}"
        )
    return "\n".join(lines)[:MAX_SUMMARY_CHARS]


def _check_scope(
    changed_files: list[str], allowed_paths: list[Path], workdir: Path
) -> tuple[bool, list[str]]:
    violations: list[str] = []
    resolved_wd = workdir.resolve()
    resolved_allowed = [path.resolve() for path in allowed_paths]
    for changed_file in changed_files:
        file_path = (resolved_wd / changed_file).resolve()
        if not any(file_path.is_relative_to(path) for path in resolved_allowed):
            violations.append(changed_file)
    return not violations, violations


def _gen_task_id() -> str:
    return f"mimo-{uuid.uuid4().hex[:12]}"


def _phase_for_status(status: str) -> str:
    if status in TERMINAL_STATUSES:
        return "finished"
    return {"queued": "queued", "running": "running", "verifying": "verifying"}.get(
        status, "queued"
    )


def _recommend_next_action(status: str) -> str:
    return {
        "queued": "wait_task(task_id)",
        "running": "wait_task(task_id) or check later",
        "verifying": "wait_task(task_id)",
        "success": "inspect changes and verify independently",
        "failed": "continue_task(task_id, feedback) or inspect logs",
        "stalled": "check worker_active: if true, check again later; if false, continue or cancel",
        "worker_crashed": "inspect logs; task may need to be restarted",
        "scope_violation": "inspect worktree; cannot auto-continue",
        "cancelled": "none",
    }.get(status, "check task_result(task_id)")


def _build_mimo_prompt(
    task: str, allowed_paths: List[str], verification_commands: List[str]
) -> str:
    scope = "\n".join(f"  - {path}" for path in allowed_paths)
    commands = "\n".join(f"  - {command}" for command in verification_commands)
    return (
        f"Scope: modify ONLY files under these paths within the workdir:\n{scope}\n"
        f"After changes, these verification commands must pass:\n{commands}\n"
        "Do NOT return long logs; just do the work.\n\n"
        f"Task: {task}"
    )


def _build_attempt_message(
    state: dict[str, Any], verification_commands: List[str], feedback: str = ""
) -> str:
    """Use compact feedback in-session, but retain full context without a session ID."""
    if feedback and state.get("session_id"):
        return feedback
    task = str(state["task"])
    if feedback:
        task = f"{task}\n\nAdditional feedback from the previous attempt:\n{feedback}"
    return _build_mimo_prompt(task, state["allowed_paths"], verification_commands)


def _compress_verification_result(
    verification: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for item in verification:
        entry: dict[str, Any] = {
            "command": item.get("command", "")[:MAX_VERIFICATION_CMD_CHARS],
            "passed": item.get("passed", False),
            "exit_code": item.get("exit_code", -1),
        }
        if not item.get("passed"):
            entry["stderr"] = item.get("stderr", "")[:MAX_VERIFICATION_OUTPUT_CHARS]
            entry["stdout"] = item.get("stdout", "")[:MAX_VERIFICATION_OUTPUT_CHARS]
        result.append(entry)
    return result


def _compress_changed_files(files: list[str]) -> list[str]:
    if len(files) <= MAX_CHANGED_FILES_RETURN:
        return files
    shown = files[:MAX_CHANGED_FILES_RETURN]
    shown.append(f"...and {len(files) - MAX_CHANGED_FILES_RETURN} more files")
    return shown


def _signal_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _terminate_process(proc: subprocess.Popen[Any], grace_seconds: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    _signal_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc.pid, signal.SIGKILL)
        proc.wait(timeout=grace_seconds)


def _terminate_orphan_group(pid: int, grace_seconds: float = 1.0) -> bool:
    """Terminate a detached child after its worker has unexpectedly exited."""
    if not _process_group_alive(pid):
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _reap_if_child(pid)
        if not _process_group_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        _reap_if_child(pid)
        return not _process_group_alive(pid)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _reap_if_child(pid)
        if not _process_group_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _cancel_requested(task_id: str) -> bool:
    return _cancel_path(task_id).exists()


def _run_verification(
    workdir: Path,
    commands: list[str],
    timeout_seconds: int,
    task_id: str = "",
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run verification commands with independent timeout and cancellation."""
    results: list[dict[str, Any]] = []
    for command in commands:
        started = time.monotonic()
        timed_out = False
        cancelled = False
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
                try:
                    proc = subprocess.Popen(
                        ["/bin/zsh", "-lc", command],
                        cwd=str(workdir),
                        stdout=stdout_file,
                        stderr=stderr_file,
                        text=True,
                        start_new_session=True,
                    )
                except Exception as error:
                    results.append(
                        {
                            "command": command,
                            "exit_code": -2,
                            "passed": False,
                            "stdout": "",
                            "stderr": str(error)[-MAX_VERIFICATION_OUTPUT_CHARS:],
                            "elapsed_s": 0,
                        }
                    )
                    continue

                if state is not None:
                    state["child_pid"] = proc.pid
                    _save_task_state(state["task_id"], state)

                last_vhb = 0.0
                while proc.poll() is None:
                    if task_id and _cancel_requested(task_id):
                        cancelled = True
                        _terminate_process(proc)
                        break
                    if timeout_seconds and time.monotonic() - started >= timeout_seconds:
                        timed_out = True
                        _terminate_process(proc)
                        break
                    if state is not None:
                        now_mono = time.monotonic()
                        if now_mono - last_vhb >= HEARTBEAT_INTERVAL_SECONDS:
                            state["last_heartbeat_at"] = _now_iso()
                            _save_task_state(state["task_id"], state)
                            last_vhb = now_mono
                    time.sleep(0.1)

                return_code = proc.wait()
                if state is not None:
                    state["child_pid"] = 0
                    _save_task_state(state["task_id"], state)

                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read()[-MAX_VERIFICATION_OUTPUT_CHARS:]
                stderr = stderr_file.read()[-MAX_VERIFICATION_OUTPUT_CHARS:]
                if timed_out:
                    stderr = f"Timeout after {timeout_seconds}s\n{stderr}".strip()
                if cancelled:
                    stderr = f"Cancelled\n{stderr}".strip()
                results.append(
                    {
                        "command": command,
                        "exit_code": return_code,
                        "passed": return_code == 0 and not timed_out and not cancelled,
                        "stdout": stdout,
                        "stderr": stderr,
                        "elapsed_s": round(time.monotonic() - started, 3),
                        "timed_out": timed_out,
                        "cancelled": cancelled,
                    }
                )
        if cancelled:
            break
    return results


def _build_mimo_command(
    mimo_bin: str,
    workdir: Path,
    message: str,
    session_id: str,
    pure: bool,
) -> list[str]:
    command = [mimo_bin, "run"]
    if pure:
        command.append("--pure")
    command.extend(["--format", "json", "--dir", str(workdir)])
    if session_id:
        command.extend(["--session", session_id])
    command.append(message)
    return command


def _run_mimo_streaming(
    state: dict[str, Any], message: str
) -> dict[str, Any]:
    """Stream MiMo JSONL to disk and keep compact progress in task state."""
    task_id = state["task_id"]
    workdir = Path(state["workdir"])
    command = _build_mimo_command(
        _locate_mimo(),
        workdir,
        message,
        state.get("session_id", ""),
        bool(state.get("pure", True)),
    )
    log_path = Path(state["log_path"])
    started = time.monotonic()
    max_runtime = int(state.get("max_runtime_seconds", 0))
    timed_out = False
    cancelled = False
    summary = ""
    session_id = state.get("session_id", "")

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(
            f"--- iteration {state.get('iterations', 0)} "
            f"pure={bool(state.get('pure', True))} ---\n"
        )
        log_file.flush()
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                start_new_session=True,
            )
        except Exception as error:
            log_file.write(f"START_ERROR: {error}\n")
            log_file.flush()
            return {
                "returncode": -2,
                "timed_out": False,
                "cancelled": False,
                "session_id": session_id,
                "summary": f"Failed to start MiMo: {error}"[:MAX_SUMMARY_CHARS],
            }

        state["child_pid"] = proc.pid
        state["last_event_at"] = _now_iso()
        _save_task_state(task_id, state)

        assert proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        pipe_fd = proc.stdout.fileno()
        os.set_blocking(pipe_fd, False)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending_text = ""
        pipe_open = True
        last_state_save = 0.0
        last_heartbeat = time.monotonic()

        def record_text(text: str, flush_partial: bool = False) -> None:
            nonlocal pending_text, session_id, summary, last_state_save
            if text:
                log_file.write(text)
                log_file.flush()
                pending_text += text
                state["last_event_at"] = _now_iso()

            lines: list[str] = []
            while "\n" in pending_text:
                line, pending_text = pending_text.split("\n", 1)
                lines.append(line)
            if flush_partial and pending_text:
                lines.append(pending_text)
                pending_text = ""

            saw_compact_event = False
            for line in lines:
                parsed = _parse_mimo_output(line)
                if parsed["session_id"]:
                    session_id = parsed["session_id"]
                    state["session_id"] = session_id
                    saw_compact_event = True
                if parsed["summary"]:
                    summary = parsed["summary"]
                    state["summary"] = summary
                    state["last_progress_at"] = _now_iso()
                    state["progress_note"] = summary[:200]
                    saw_compact_event = True

            now = time.monotonic()
            if text and (saw_compact_event or now - last_state_save >= 1):
                state["last_heartbeat_at"] = _now_iso()
                _save_task_state(task_id, state)
                last_state_save = now

        try:
            while True:
                if _cancel_requested(task_id):
                    cancelled = True
                    _terminate_process(proc)
                    break
                if max_runtime and time.monotonic() - started >= max_runtime:
                    timed_out = True
                    _terminate_process(proc)
                    break

                now_mono = time.monotonic()
                if now_mono - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    state["last_heartbeat_at"] = _now_iso()
                    _save_task_state(task_id, state)
                    last_heartbeat = now_mono

                events = selector.select(timeout=0.25)
                for _, _ in events:
                    try:
                        chunk = os.read(pipe_fd, 64 * 1024)
                    except BlockingIOError:
                        continue
                    if chunk:
                        record_text(decoder.decode(chunk))
                    elif pipe_open:
                        selector.unregister(proc.stdout)
                        pipe_open = False

                if proc.poll() is not None:
                    break

            while pipe_open:
                try:
                    chunk = os.read(pipe_fd, 64 * 1024)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                record_text(decoder.decode(chunk))
            record_text(decoder.decode(b"", final=True), flush_partial=True)
        except BaseException:
            _terminate_process(proc)
            state["child_pid"] = 0
            _save_task_state(task_id, state)
            raise
        finally:
            selector.close()
            proc.stdout.close()

        return_code = proc.wait()
        if timed_out:
            log_file.write(f"TIMEOUT: MiMo exceeded {max_runtime}s\n")
        if cancelled:
            log_file.write("CANCELLED\n")
        log_file.flush()

    state["child_pid"] = 0
    state["session_id"] = session_id
    state["summary"] = summary[:MAX_SUMMARY_CHARS]
    _save_task_state(task_id, state)
    return {
        "returncode": return_code,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "session_id": session_id,
        "summary": summary[:MAX_SUMMARY_CHARS],
    }


def _finalize_state(
    state: dict[str, Any], status: str, summary: str | None = None
) -> None:
    state["status"] = status
    state["phase"] = "finished"
    state["worker_active"] = False
    if status == "stalled" and not state.get("stall_reason"):
        state["stall_reason"] = "runtime_or_verification_timeout"
    if summary is not None:
        state["summary"] = summary[:MAX_SUMMARY_CHARS]
    state["child_pid"] = 0
    state["worker_pid"] = 0
    state["finished_at"] = _now_iso()
    state["finished_ts"] = time.time()
    _save_task_state(state["task_id"], state)


def _worker_main(task_id: str) -> int:
    """Execute one queued task. Invoked in a detached Python process."""
    _validate_task_id(task_id)
    state = _load_task_state(task_id)
    workdir = Path(state["workdir"]).resolve()
    allowed = _validate_paths(workdir, state["allowed_paths"])
    verification_commands = _validate_verification(state["verification_commands"])
    accumulated = set(state.get("changed_files", []))
    final_status = "failed"

    state["worker_pid"] = os.getpid()
    state["status"] = "running"
    state["phase"] = "starting"
    state["started_at"] = _now_iso()
    state["started_ts"] = time.time()
    state["finished_at"] = None
    state["finished_ts"] = None
    state["last_heartbeat_at"] = _now_iso()
    state["stall_reason"] = ""
    state["worker_active"] = True
    resume_feedback = state.pop("resume_feedback", "")
    resume_intent = state.pop("resume_intent", "")
    if resume_intent:
        state.setdefault("key_decisions", []).append(
            f"resume_intent={resume_intent}"
        )
    _save_task_state(task_id, state)

    message = _build_attempt_message(state, verification_commands, resume_feedback)

    try:
        for _ in range(int(state["max_iterations"])):
            if _cancel_requested(task_id):
                final_status = "cancelled"
                _finalize_state(state, final_status, "Task cancelled before MiMo started")
                return 0

            state["iterations"] = int(state.get("iterations", 0)) + 1
            state["status"] = "running"
            state["phase"] = "running"
            state["verification"] = []
            _save_task_state(task_id, state)
            before = _snapshot_files(workdir)

            run_result = _run_mimo_streaming(state, message)
            after = _snapshot_files(workdir)
            accumulated.update(_diff_files(before, after))
            changed_files = sorted(accumulated)
            state["changed_files"] = changed_files

            in_scope, violations = _check_scope(changed_files, allowed, workdir)
            if not in_scope:
                final_status = "scope_violation"
                _finalize_state(
                    state,
                    final_status,
                    f"Files outside allowed_paths: {violations[:10]}",
                )
                return 1

            if run_result["cancelled"] or _cancel_requested(task_id):
                final_status = "cancelled"
                _finalize_state(state, final_status, "Task cancelled")
                return 0
            if run_result["timed_out"]:
                final_status = "stalled"
                last_summary = run_result.get("summary", "")
                _finalize_state(
                    state,
                    final_status,
                    f"MiMo exceeded max_runtime_seconds={state['max_runtime_seconds']}. "
                    f"Last output: {last_summary}",
                )
                return 1
            if run_result["returncode"] != 0:
                final_status = "failed"
                _finalize_state(
                    state,
                    final_status,
                    run_result.get("summary")
                    or f"MiMo exited with code {run_result['returncode']}",
                )
                return 1

            state["status"] = "verifying"
            state["phase"] = "verifying"
            _save_task_state(task_id, state)
            verification = _run_verification(
                workdir,
                verification_commands,
                int(state["verification_timeout_seconds"]),
                task_id,
                state,
            )
            state["verification"] = verification

            if _cancel_requested(task_id) or any(
                item.get("cancelled") for item in verification
            ):
                final_status = "cancelled"
                _finalize_state(state, final_status, "Task cancelled during verification")
                return 0

            timed_out_commands = [
                item["command"] for item in verification if item.get("timed_out")
            ]
            if timed_out_commands:
                final_status = "stalled"
                _finalize_state(
                    state,
                    final_status,
                    f"Verification timed out: {timed_out_commands[:3]}",
                )
                return 1

            if all(item.get("passed", False) for item in verification):
                final_status = "success"
                _finalize_state(
                    state,
                    final_status,
                    run_result.get("summary") or "MiMo task and verification succeeded",
                )
                return 0

            failure_feedback = _compact_failure_info(
                changed_files,
                verification,
                run_result.get("summary", ""),
            )
            message = _build_attempt_message(
                state, verification_commands, failure_feedback
            )
            state["summary"] = failure_feedback
            _save_task_state(task_id, state)

        final_status = "failed"
        _finalize_state(state, final_status, state.get("summary") or "Verification failed")
        return 1
    except Exception as error:
        final_status = "failed"
        _finalize_state(state, final_status, f"Worker error: {error}")
        worker_error_path = Path(state["worker_error_path"])
        with worker_error_path.open("a", encoding="utf-8") as error_file:
            error_file.write(traceback.format_exc())
        return 1
    finally:
        _release_workdir(workdir, task_id, str(state.get("lock_token", "")))
        _cancel_path(task_id).unlink(missing_ok=True)


def _launch_worker(state: dict[str, Any]) -> None:
    task_id = state["task_id"]
    worker_error_path = Path(state["worker_error_path"])
    with worker_error_path.open("a", encoding="utf-8") as error_file:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", task_id],
            cwd=state["workdir"],
            stdin=subprocess.DEVNULL,
            stdout=error_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    _LOCAL_WORKERS[task_id] = proc
    current = _load_task_state(task_id)
    if current.get("status") == "queued" and not current.get("worker_pid"):
        current["worker_pid"] = proc.pid
        _save_task_state(task_id, current)


def _reap_local_worker(task_id: str, wait_seconds: float = 0) -> None:
    proc = _LOCAL_WORKERS.get(task_id)
    if proc is None:
        return
    if proc.poll() is None and wait_seconds:
        try:
            proc.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            return
    if proc.poll() is not None:
        proc.wait()
        _LOCAL_WORKERS.pop(task_id, None)


def _terminate_worker(task_id: str, worker_pid: int) -> bool:
    proc = _LOCAL_WORKERS.get(task_id)
    if proc is not None and proc.pid == worker_pid:
        try:
            _terminate_process(proc, grace_seconds=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        _reap_local_worker(task_id, 1)
        return proc.poll() is not None
    if not _is_our_worker_pid(worker_pid, task_id):
        return False
    return _terminate_orphan_group(worker_pid)


def _refresh_stale_task(state: dict[str, Any]) -> dict[str, Any]:
    task_id = state["task_id"]
    _reap_local_worker(
        task_id, 1 if state.get("status") in TERMINAL_STATUSES else 0
    )
    latest = _load_task_state(task_id)

    worker_pid = int(latest.get("worker_pid", 0))
    if worker_pid and _worker_process_matches(worker_pid, task_id):
        if latest.get("status") == "stalled" and latest.get("stall_reason") == "heartbeat_expired":
            last_hb = latest.get("last_heartbeat_at")
            if last_hb:
                try:
                    hb_time = datetime.fromisoformat(last_hb).timestamp()
                    if time.time() - hb_time <= STALLED_HEARTBEAT_SECONDS:
                        prev_phase = latest.get("phase", "running")
                        restore_status = "verifying" if prev_phase == "verifying" else "running"
                        latest["status"] = restore_status
                        latest["phase"] = restore_status
                        latest["stall_reason"] = ""
                        latest["worker_active"] = True
                        latest["summary"] = ""
                        _save_task_state(task_id, latest)
                        return latest
                except (ValueError, TypeError):
                    pass

    # For stalled+heartbeat_expired, re-verify worker alive on every check
    if latest.get("status") == "stalled" and latest.get("stall_reason") == "heartbeat_expired":
        if worker_pid and _worker_process_matches(worker_pid, task_id):
            latest["worker_active"] = True
            _save_task_state(task_id, latest)
            return latest
        else:
            latest["status"] = "worker_crashed"
            latest["stall_reason"] = ""
            latest["worker_active"] = False
            latest["worker_pid"] = 0
            latest["child_pid"] = 0
            latest["phase"] = "finished"
            latest["summary"] = "Worker died while heartbeat-expired; task crashed"
            latest["finished_at"] = _now_iso()
            latest["finished_ts"] = time.time()
            _save_task_state(task_id, latest)
            _release_workdir(Path(latest["workdir"]), task_id, str(latest.get("lock_token", "")))
            return latest

    if latest.get("status") not in ACTIVE_STATUSES:
        return latest

    if worker_pid and _worker_process_matches(worker_pid, task_id):
        last_hb = latest.get("last_heartbeat_at")
        if last_hb:
            try:
                hb_time = datetime.fromisoformat(last_hb).timestamp()
                if time.time() - hb_time > STALLED_HEARTBEAT_SECONDS:
                    latest = _load_task_state(task_id)
                    if latest.get("status") not in ACTIVE_STATUSES:
                        return latest
                    recheck_hb = latest.get("last_heartbeat_at")
                    if recheck_hb:
                        try:
                            recheck_time = datetime.fromisoformat(recheck_hb).timestamp()
                            if time.time() - recheck_time <= STALLED_HEARTBEAT_SECONDS:
                                return latest
                        except (ValueError, TypeError):
                            pass
                    latest["status"] = "stalled"
                    latest["stall_reason"] = "heartbeat_expired"
                    latest["worker_active"] = True
                    latest["summary"] = (
                        f"Worker heartbeat expired "
                        f"({STALLED_HEARTBEAT_SECONDS}s), process may still be running"
                    )
                    _save_task_state(task_id, latest)
                    return latest
            except (ValueError, TypeError):
                pass
        return latest
    if not worker_pid:
        updated_ts = float(latest.get("updated_ts", latest.get("created_ts", 0)))
        if time.time() - updated_ts < LOCK_RESERVATION_GRACE_SECONDS:
            return latest

    # The worker cannot write again after it is dead. Reload once more so an
    # already-persisted success is never overwritten from a stale caller snapshot.
    latest = _load_task_state(task_id)
    if latest.get("status") not in ACTIVE_STATUSES:
        return latest
    latest_worker_pid = int(latest.get("worker_pid", 0))
    if latest_worker_pid and _worker_process_matches(latest_worker_pid, task_id):
        return latest

    child_pid = int(latest.get("child_pid", 0))
    if child_pid and not _terminate_orphan_group(child_pid):
        latest["status"] = "stalled"
        latest["phase"] = "finished"
        latest["stall_reason"] = "child_process_unstoppable"
        latest["worker_pid"] = 0
        latest["worker_active"] = False
        latest["summary"] = (
            f"Worker exited unexpectedly; child process group {child_pid} "
            "could not be stopped"
        )
        latest["finished_at"] = _now_iso()
        latest["finished_ts"] = time.time()
        _save_task_state(task_id, latest)
        return latest

    workdir = Path(latest["workdir"])
    latest["child_pid"] = 0
    _finalize_state(latest, "worker_crashed", "Worker exited unexpectedly")
    _release_workdir(workdir, task_id, str(latest.get("lock_token", "")))
    return latest


def _task_result_from_state(
    state: dict[str, Any], detail: str = "brief"
) -> dict[str, Any]:
    started_ts = float(state.get("started_ts") or state.get("created_ts") or time.time())
    ended_ts = float(state.get("finished_ts") or time.time())
    status = state["status"]
    changed = state.get("changed_files", [])
    verification = state.get("verification", [])
    all_passed = all(item.get("passed", False) for item in verification) if verification else False
    any_failed = any(not item.get("passed", False) for item in verification) if verification else False

    if detail == "brief":
        verify_conclusion = "pending"
        if verification:
            passed = sum(1 for v in verification if v.get("passed"))
            verify_conclusion = f"{passed}/{len(verification)} passed"
            if any_failed:
                failed_cmds = [v.get("command", "")[:80] for v in verification if not v.get("passed")]
                verify_conclusion += f"; failed: {'; '.join(failed_cmds[:3])}"

        return {
            "task_id": state["task_id"],
            "status": status,
            "phase": state.get("phase", _phase_for_status(status)),
            "session_id": state.get("session_id", ""),
            "iterations": state.get("iterations", 0),
            "elapsed_seconds": round(max(0.0, ended_ts - started_ts), 1),
            "last_event_at": state.get("last_event_at"),
            "last_progress_at": state.get("last_progress_at"),
            "progress_note": state.get("progress_note", ""),
            "changed_files_count": len(changed),
            "verification_conclusion": verify_conclusion,
            "summary": state.get("summary", "")[:MAX_SUMMARY_CHARS],
            "log_path": state.get("log_path", ""),
            "recommended_next_action": _recommend_next_action(status),
            "worker_active": state.get("worker_active", False),
            "stall_reason": state.get("stall_reason", ""),
        }

    result: dict[str, Any] = {
        "task_id": state["task_id"],
        "status": status,
        "phase": state.get("phase", _phase_for_status(status)),
        "session_id": state.get("session_id", ""),
        "iterations": state.get("iterations", 0),
        "elapsed_seconds": round(max(0.0, ended_ts - started_ts), 1),
        "last_event_at": state.get("last_event_at"),
        "last_progress_at": state.get("last_progress_at"),
        "progress_note": state.get("progress_note", ""),
        "changed_files": _compress_changed_files(changed),
        "verification": _compress_verification_result(verification),
        "summary": state.get("summary", "")[:MAX_SUMMARY_CHARS],
        "log_path": state.get("log_path", ""),
        "recommended_next_action": _recommend_next_action(status),
        "key_decisions": state.get("key_decisions", []),
        "known_issues": state.get("known_issues", []),
    }
    return result


def _get_task_result(task_id: str, detail: str = "brief") -> dict[str, Any]:
    state = _refresh_stale_task(_load_task_state(task_id))
    return _task_result_from_state(state, detail=detail)


def _wait_for_task(task_id: str, wait_seconds: int, detail: str = "brief") -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    while True:
        result = _get_task_result(task_id, detail=detail)
        if result["status"] in TERMINAL_STATUSES or time.monotonic() >= deadline:
            return result
        time.sleep(0.2)


def _new_task_state(
    task_id: str,
    lock_token: str,
    workdir: Path,
    task: str,
    allowed_paths: List[str],
    verification_commands: List[str],
    max_iterations: int,
    max_runtime_seconds: int,
    verification_timeout_seconds: int,
    pure: bool,
) -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema_version": 2,
        "task_id": task_id,
        "lock_token": lock_token,
        "workdir": str(workdir),
        "task": task,
        "allowed_paths": allowed_paths,
        "verification_commands": verification_commands,
        "max_iterations": max_iterations,
        "max_runtime_seconds": max_runtime_seconds,
        "verification_timeout_seconds": verification_timeout_seconds,
        "pure": pure,
        "session_id": "",
        "iterations": 0,
        "changed_files": [],
        "verification": [],
        "summary": "",
        "status": "queued",
        "phase": "queued",
        "worker_pid": 0,
        "child_pid": 0,
        "log_path": str(LOGS_DIR / f"{task_id}.log"),
        "worker_error_path": str(LOGS_DIR / f"{task_id}.worker.log"),
        "created_at": now,
        "created_ts": time.time(),
        "started_at": None,
        "started_ts": None,
        "finished_at": None,
        "finished_ts": None,
        "last_event_at": None,
        "last_heartbeat_at": None,
        "last_progress_at": None,
        "progress_note": "",
        "key_decisions": [],
        "known_issues": [],
        "stall_reason": "",
        "worker_active": False,
        "revision": 0,
    }


@mcp.tool()
def delegate_task(
    workdir: str,
    task: str,
    allowed_paths: List[str],
    verification_commands: List[str],
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    verification_timeout_seconds: int = DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
    pure: bool = True,
) -> dict[str, Any]:
    """Start a MiMo task and return success or a running task ID within wait_seconds."""
    if not isinstance(workdir, str):
        raise TypeError("workdir must be a string")
    if not isinstance(task, str) or len(task) > MAX_TASK_LEN:
        raise ValueError(f"task must be a string of at most {MAX_TASK_LEN} chars")
    if not isinstance(pure, bool):
        raise TypeError("pure must be a bool")
    _validate_nonblank(task, "task")
    max_iterations = _validate_int(max_iterations, "max_iterations", 1, 10)
    wait_seconds = _validate_int(wait_seconds, "wait_seconds", 0, MAX_WAIT_SECONDS)
    max_runtime_seconds = _validate_int(
        max_runtime_seconds, "max_runtime_seconds", 0
    )
    verification_timeout_seconds = _validate_int(
        verification_timeout_seconds, "verification_timeout_seconds", 0
    )
    workdir_path = _validate_workdir(workdir)
    _validate_paths(workdir_path, allowed_paths)
    _validate_verification(verification_commands)
    _locate_mimo()

    _ensure_dirs()
    task_id = _gen_task_id()
    lock_token = _new_lock_token()
    _claim_workdir(workdir_path, task_id, lock_token)
    state = _new_task_state(
        task_id,
        lock_token,
        workdir_path,
        task,
        allowed_paths,
        verification_commands,
        max_iterations,
        max_runtime_seconds,
        verification_timeout_seconds,
        pure,
    )
    Path(state["log_path"]).write_text("", encoding="utf-8")
    Path(state["worker_error_path"]).write_text("", encoding="utf-8")
    _cancel_path(task_id).unlink(missing_ok=True)
    _save_task_state(task_id, state)
    try:
        _launch_worker(state)
    except Exception as error:
        _finalize_state(state, "failed", f"Failed to launch worker: {error}")
        _release_workdir(workdir_path, task_id, lock_token)
        raise
    return _wait_for_task(task_id, wait_seconds, detail="brief")


@mcp.tool()
def continue_task(
    task_id: str,
    feedback: str,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    resume_intent: str = "",
) -> dict[str, Any]:
    """Resume a completed task in its existing MiMo session.

    resume_intent is optional metadata describing why you are continuing:
    e.g. "resume_work", "fix_verification", "address_review", "retry_transient".
    It does not change behavior but is recorded for observability.
    scope_violation tasks cannot be continued regardless of intent.
    """
    if not isinstance(feedback, str) or len(feedback) > MAX_FEEDBACK_LEN:
        raise ValueError(
            f"feedback must be a string of at most {MAX_FEEDBACK_LEN} chars"
        )
    _validate_nonblank(feedback, "feedback")
    if resume_intent and not isinstance(resume_intent, str):
        raise TypeError("resume_intent must be a string")
    if resume_intent and len(resume_intent) > 500:
        raise ValueError("resume_intent must be at most 500 chars")
    wait_seconds = _validate_int(wait_seconds, "wait_seconds", 0, MAX_WAIT_SECONDS)
    state = _refresh_stale_task(_load_task_state(task_id))
    if state["status"] in ACTIVE_STATUSES:
        raise ValueError(f"Task {task_id} is still {state['status']}")
    if state["status"] == "scope_violation":
        raise ValueError(
            f"Task {task_id} is in scope_violation status and cannot be continued"
        )
    if state["status"] == "stalled":
        if state.get("stall_reason") == "heartbeat_expired" and state.get("worker_active"):
            raise ValueError(
                f"Task {task_id} is stalled (heartbeat expired) but Worker "
                "is still running. Check again later or cancel_task first."
            )

    workdir = Path(state["workdir"])
    lock_token = _new_lock_token()
    _claim_workdir(workdir, task_id, lock_token)
    _cancel_path(task_id).unlink(missing_ok=True)
    state["lock_token"] = lock_token
    state["status"] = "queued"
    state["phase"] = "queued"
    state["worker_pid"] = 0
    state["child_pid"] = 0
    state["verification"] = []
    state["summary"] = ""
    state["stall_reason"] = ""
    state["worker_active"] = False
    state["last_heartbeat_at"] = None
    state["last_progress_at"] = None
    state["progress_note"] = ""
    state["resume_feedback"] = feedback
    if resume_intent:
        state["resume_intent"] = resume_intent
    state["finished_at"] = None
    state["finished_ts"] = None
    _save_task_state(task_id, state)
    try:
        _launch_worker(state)
    except Exception as error:
        _finalize_state(state, "failed", f"Failed to launch worker: {error}")
        _release_workdir(workdir, task_id, lock_token)
        raise
    return _wait_for_task(task_id, wait_seconds, detail="brief")


@mcp.tool()
def task_result(task_id: str, detail: Literal["brief", "summary"] = "brief") -> dict[str, Any]:
    """Return compact, non-blocking status for a delegated task.

    detail="brief" (default): task_id, status, phase, counts, summary, next action.
    detail="summary": adds changed_files list, verification details, key_decisions, known_issues.
    For raw log snippets or verification output, use the task_evidence tool.
    """
    if detail not in ("brief", "summary"):
        raise ValueError(f"detail must be 'brief' or 'summary', got '{detail}'")
    state = _refresh_stale_task(_load_task_state(task_id))
    return _task_result_from_state(state, detail=detail)


@mcp.tool()
def wait_task(
    task_id: str, wait_seconds: int = DEFAULT_WAIT_SECONDS
) -> dict[str, Any]:
    """Wait briefly for a task to finish without stopping its background worker."""
    wait_seconds = _validate_int(wait_seconds, "wait_seconds", 0, MAX_WAIT_SECONDS)
    return _wait_for_task(task_id, wait_seconds, detail="brief")


@mcp.tool()
def cancel_task(task_id: str) -> dict[str, Any]:
    """Request cancellation and terminate the active MiMo or verification process."""
    state = _refresh_stale_task(_load_task_state(task_id))
    child_pid = int(state.get("child_pid", 0))
    if state["status"] in TERMINAL_STATUSES and not _process_group_alive(child_pid):
        return _task_result_from_state(state)

    _cancel_path(task_id).write_text("cancel\n", encoding="utf-8")
    state = _load_task_state(task_id)
    child_pid = int(state.get("child_pid", 0))
    if state["status"] in TERMINAL_STATUSES:
        if not child_pid:
            _cancel_path(task_id).unlink(missing_ok=True)
            return _task_result_from_state(state)
        if not _terminate_orphan_group(child_pid):
            result = _task_result_from_state(state)
            result["summary"] = "Cancellation requested; child process is still stopping"
            return result
        state["child_pid"] = 0
        _finalize_state(state, "cancelled", "Task cancelled")
        _release_workdir(
            Path(state["workdir"]), task_id, str(state.get("lock_token", ""))
        )
        _cancel_path(task_id).unlink(missing_ok=True)
        return _task_result_from_state(state)

    if child_pid and not _terminate_orphan_group(child_pid):
        result = _task_result_from_state(state)
        result["summary"] = "Cancellation requested; child process is still stopping"
        return result

    result = _wait_for_task(task_id, 5)
    if result["status"] in TERMINAL_STATUSES:
        return result

    state = _load_task_state(task_id)
    if state["status"] in TERMINAL_STATUSES:
        return _task_result_from_state(state)

    child_pid = int(state.get("child_pid", 0))
    if child_pid and not _terminate_orphan_group(child_pid):
        result = _task_result_from_state(state)
        result["summary"] = "Cancellation requested; child process is still stopping"
        return result
    worker_pid = int(state.get("worker_pid", 0))
    if worker_pid and not _terminate_worker(task_id, worker_pid):
        result = _task_result_from_state(state)
        result["summary"] = "Cancellation requested; worker process is still stopping"
        return result

    # No worker remains, so this final reload is stable and cannot clobber a
    # completion that raced with the cancellation request.
    state = _load_task_state(task_id)
    if state["status"] in TERMINAL_STATUSES:
        return _task_result_from_state(state)
    state["child_pid"] = 0
    state["worker_pid"] = 0
    _finalize_state(state, "cancelled", "Task cancelled")
    _release_workdir(
        Path(state["workdir"]), task_id, str(state.get("lock_token", ""))
    )
    _cancel_path(task_id).unlink(missing_ok=True)
    _reap_local_worker(task_id, 1)
    return _task_result_from_state(state)


@mcp.tool()
def task_evidence(
    task_id: str,
    log_tail_lines: int = 50,
    verification_output: bool = False,
) -> dict[str, Any]:
    """Return bounded log and verification evidence for debugging a delegated task.

    log_tail_lines: number of tail lines from the MiMo log (0-200, default 50).
    verification_output: if True, include compressed verification stdout/stderr.
    All output is bounded to avoid flooding the caller context.
    """
    _validate_task_id(task_id)
    log_tail_lines = _validate_int(
        log_tail_lines, "log_tail_lines", 0, MAX_EVIDENCE_LOG_LINES
    )
    state = _load_task_state(task_id)
    result: dict[str, Any] = {
        "task_id": task_id,
        "status": state.get("status", ""),
        "phase": state.get("phase", ""),
    }

    log_path = Path(state.get("log_path", ""))
    if log_path.exists() and log_tail_lines > 0:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        tail = lines[-log_tail_lines:]
        result["log_tail"] = "\n".join(tail)[:MAX_EVIDENCE_CHARS]
    else:
        result["log_tail"] = ""

    if verification_output:
        result["verification"] = _compress_verification_result(
            state.get("verification", [])
        )
    else:
        result["verification"] = []

    return result


def _doctor_check_mimo() -> tuple[str, str, str]:
    """Check MiMo executable availability and --version. Returns (status, path, version_info)."""
    try:
        mimo = _locate_mimo()
    except FileNotFoundError as exc:
        return "error", str(exc), ""
    try:
        result = subprocess.run(
            [mimo, "--version"], capture_output=True, text=True, timeout=5,
        )
        ver = result.stdout.strip()[:120] if result.returncode == 0 else ""
        if not ver and result.returncode != 0:
            ver = f"(exit {result.returncode})"
    except subprocess.TimeoutExpired:
        ver = "(timeout)"
    except OSError as exc:
        ver = f"(error: {str(exc)[:60]})"
    return "ok", mimo, ver


def _doctor_check_dirs() -> list[dict[str, Any]]:
    """Check state directories writability."""
    results = []
    for label, path in [("tasks", TASKS_DIR), ("logs", LOGS_DIR), ("locks", LOCKS_DIR)]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".doctor_probe"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            results.append({"path": str(path), "status": "ok"})
        except Exception as exc:
            results.append({"path": str(path), "status": "error", "error": str(exc)[:120]})
    return results


def _doctor_check_corrupt_states() -> list[dict[str, Any]]:
    """Find unreadable or corrupt task state files."""
    corrupt = []
    if not TASKS_DIR.exists():
        return corrupt
    for path in TASKS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "task_id" not in data:
                corrupt.append({"file": str(path), "status": "missing_task_id"})
        except (json.JSONDecodeError, OSError) as exc:
            corrupt.append({"file": str(path), "status": "corrupt", "error": str(exc)[:80]})
    return corrupt


def _doctor_check_stale_locks() -> list[dict[str, Any]]:
    """Find lock files whose owning task is no longer active."""
    stale = []
    if not LOCKS_DIR.exists():
        return stale
    for path in LOCKS_DIR.glob("*.lock"):
        owner_id = ""
        try:
            owner_id, _ = _read_lock(path)
            if not owner_id:
                continue
            owner_state = _load_task_state(owner_id)
            if owner_state.get("status") in TERMINAL_STATUSES:
                stale.append({"lock_file": str(path), "owner_task": owner_id, "owner_status": owner_state["status"]})
        except (FileNotFoundError, ValueError, OSError):
            stale.append({"lock_file": str(path), "owner_task": owner_id or "?", "owner_status": "unknown"})
    return stale


def _doctor_check_orphan_processes() -> list[dict[str, Any]]:
    """Find child processes whose worker is dead."""
    orphans = []
    if not TASKS_DIR.exists():
        return orphans
    for path in TASKS_DIR.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if state.get("status") not in ACTIVE_STATUSES:
            continue
        worker_pid = int(state.get("worker_pid", 0))
        child_pid = int(state.get("child_pid", 0))
        if worker_pid and not _process_alive(worker_pid) and child_pid and _process_group_alive(child_pid):
            orphans.append({"task_id": state.get("task_id", ""), "child_pid": child_pid})
    return orphans


def _doctor_check_version() -> dict[str, Any]:
    """Read installed plugin version and optionally compare with source manifest."""
    plugin_json = Path(__file__).resolve().parent.parent / ".codex-plugin" / "plugin.json"
    result: dict[str, Any] = {"version": "unknown", "source": "unreadable"}
    try:
        data = json.loads(plugin_json.read_text(encoding="utf-8"))
        result = {"version": data.get("version", "unknown"), "source": str(plugin_json)}
    except Exception:
        pass
    source_path = os.environ.get("MIMO_DELEGATOR_SOURCE_PATH", "")
    if source_path:
        try:
            src_data = json.loads(Path(source_path).read_text(encoding="utf-8"))
            src_ver = src_data.get("version", "unknown")
            result["source_version"] = src_ver
            result["version_match"] = result["version"] == src_ver
        except Exception:
            sp = Path(source_path)
            if sp.is_dir():
                result["source_version"] = "path-is-directory"
            else:
                result["source_version"] = "unreadable"
            result["version_match"] = False
    return result


@mcp.tool()
def doctor() -> dict[str, Any]:
    """Diagnose delegator health: executable, dirs, state, locks, processes, version.

    Diagnostic only — does not delete files, kill processes, or modify state.
    Returns short actionable findings in Chinese and English.
    """
    findings: list[str] = []
    findings_en: list[str] = []

    mimo_status, mimo_detail, mimo_version = _doctor_check_mimo()
    if mimo_status != "ok":
        findings.append(f"MiMo 可执行文件异常: {mimo_detail}")
        findings_en.append(f"MiMo executable issue: {mimo_detail}")
    elif mimo_version and mimo_version.startswith("("):
        findings.append(f"MiMo --version 异常: {mimo_version}")
        findings_en.append(f"MiMo --version issue: {mimo_version}")

    dirs = _doctor_check_dirs()
    for d in dirs:
        if d["status"] != "ok":
            findings.append(f"目录不可写: {d['path']} ({d.get('error', '')})")
            findings_en.append(f"Directory not writable: {d['path']} ({d.get('error', '')})")

    corrupt = _doctor_check_corrupt_states()
    for c in corrupt:
        findings.append(f"损坏状态文件: {c['file']} ({c['status']})")
        findings_en.append(f"Corrupt state file: {c['file']} ({c['status']})")

    stale_locks = _doctor_check_stale_locks()
    for lk in stale_locks:
        findings.append(f"残留锁: {lk['lock_file']} (owner={lk['owner_task']}, status={lk['owner_status']})")
        findings_en.append(f"Stale lock: {lk['lock_file']} (owner={lk['owner_task']}, status={lk['owner_status']})")

    orphans = _doctor_check_orphan_processes()
    for o in orphans:
        findings.append(f"孤儿子进程: task={o['task_id']} child_pid={o['child_pid']}")
        findings_en.append(f"Orphan child process: task={o['task_id']} child_pid={o['child_pid']}")

    version_info = _doctor_check_version()

    if version_info.get("source_version") and not version_info.get("version_match", True):
        findings.append(
            f"插件版本不一致: 已安装={version_info.get('version')}, 源码={version_info.get('source_version')}"
        )
        findings_en.append(
            f"Plugin version mismatch: installed={version_info.get('version')}, source={version_info.get('source_version')}"
        )
    if version_info.get("source_version") in ("unreadable", "path-is-directory"):
        findings.append("源码版本文件不可读或不是文件 (MIMO_DELEGATOR_SOURCE_PATH)")
        findings_en.append("Source version file unreadable or not a file (MIMO_DELEGATOR_SOURCE_PATH)")

    return {
        "mimo": {"status": mimo_status, "detail": mimo_detail, "version": mimo_version},
        "directories": dirs,
        "corrupt_states": corrupt,
        "stale_locks": stale_locks,
        "orphan_processes": orphans,
        "version": version_info,
        "findings_zh": findings or ["一切正常"],
        "findings_en": findings_en or ["All checks passed"],
    }


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker_main(sys.argv[2]))
    if len(sys.argv) == 2 and sys.argv[1] == "--doctor":
        import json as _json

        result = doctor()
        print(_json.dumps(result, indent=2, ensure_ascii=False))
        raise SystemExit(0 if not result["findings_en"] or result["findings_en"] == ["All checks passed"] else 1)
    mcp.run(transport="stdio")

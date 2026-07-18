"""MiMo Delegator MCP Server.

Delegates coding tasks to the MiMo CLI as a subprocess, with file-scope
guards, automatic retry on verification failure, and compact results.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TASKS_DIR = Path.home() / ".codex" / "mimo-delegator" / "tasks"
LOGS_DIR = Path.home() / ".codex" / "mimo-delegator" / "logs"
MAX_SUMMARY_CHARS = 1200
MAX_VERIFICATION_OUTPUT_CHARS = 500
MAX_VERIFICATION_CMD_CHARS = 200
MAX_TIMEOUT_SECONDS = 3600
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_ITERATIONS = 3
HOME = Path.home()

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

TASK_ID_RE = re.compile(r"^mimo-[0-9a-f]{12}$")

MAX_TASK_LEN = 100_000
MAX_FEEDBACK_LEN = 50_000
MAX_ALLOWED_PATHS = 50
MAX_VERIFICATION_CMDS = 20
MAX_CMD_LEN = 2000
MAX_CHANGED_FILES_RETURN = 50

mcp = FastMCP("mimoDelegator")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_dirs() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _locate_mimo() -> str:
    env = os.environ.get("MIMO_EXECUTABLE")
    if env and Path(env).is_file():
        return env
    found = shutil.which("mimo")
    if found:
        return found
    home_bin = HOME / ".mimocode" / "bin" / "mimo"
    if home_bin.is_file():
        return str(home_bin)
    raise FileNotFoundError(
        "Cannot locate mimo binary. Set MIMO_EXECUTABLE or install to ~/.mimocode/bin/mimo"
    )


def _snapshot_files(root: Path) -> dict[str, str]:
    """Recursive file metadata snapshot: {relative_path: mtime_ns_hex:size_hex}."""
    snap: dict[str, str] = {}
    if not root.is_dir():
        return snap
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        parts = p.relative_to(root).parts
        if any(d in SKIP_DIRS for d in parts):
            continue
        if len(parts) >= 2 and parts[0] == "data" and parts[1] == "qdrant":
            continue
        if p.suffix.lower() == ".gguf":
            continue
        try:
            st = p.stat()
            mtime = st.st_mtime_ns
            size = st.st_size
        except OSError:
            continue
        snap[rel] = f"{mtime:x}:{size:x}"
    return snap


def _diff_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed: list[str] = []
    all_keys = set(before) | set(after)
    for k in sorted(all_keys):
        if before.get(k) != after.get(k):
            changed.append(k)
    return changed


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
    for p in allowed_paths:
        rp = (workdir / p).resolve()
        if not rp.is_relative_to(resolved_wd):
            raise ValueError(
                f"allowed_path '{p}' escapes workdir (resolved: {rp})"
            )
        resolved.append(rp)
    return resolved


def _validate_verification(verification_commands: List[str]) -> List[str]:
    if not verification_commands:
        raise ValueError("verification_commands must be non-empty")
    if len(verification_commands) > MAX_VERIFICATION_CMDS:
        raise ValueError(
            f"verification_commands exceeds max of {MAX_VERIFICATION_CMDS}"
        )
    for i, cmd in enumerate(verification_commands):
        if not cmd or not cmd.strip():
            raise ValueError(f"verification_command[{i}] must not be blank")
        if len(cmd) > MAX_CMD_LEN:
            raise ValueError(
                f"verification_command[{i}] exceeds {MAX_CMD_LEN} chars"
            )
    return verification_commands


def _validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.match(task_id):
        raise ValueError(f"Invalid task_id format: {task_id}")


def _run_verification(
    workdir: Path, commands: list[str], timeout: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for cmd in commands:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                ["/bin/zsh", "-lc", cmd],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=min(timeout, MAX_TIMEOUT_SECONDS),
            )
            elapsed = round(time.monotonic() - t0, 3)
            out = (proc.stdout or "")[-MAX_VERIFICATION_OUTPUT_CHARS:]
            err = (proc.stderr or "")[-MAX_VERIFICATION_OUTPUT_CHARS:]
            results.append(
                {
                    "command": cmd,
                    "exit_code": proc.returncode,
                    "passed": proc.returncode == 0,
                    "stdout": out,
                    "stderr": err,
                    "elapsed_s": elapsed,
                }
            )
        except subprocess.TimeoutExpired:
            elapsed = round(time.monotonic() - t0, 3)
            results.append(
                {
                    "command": cmd,
                    "exit_code": -1,
                    "passed": False,
                    "stdout": "",
                    "stderr": f"Timeout after {timeout}s",
                    "elapsed_s": elapsed,
                }
            )
        except Exception as e:
            results.append(
                {
                    "command": cmd,
                    "exit_code": -2,
                    "passed": False,
                    "stdout": "",
                    "stderr": str(e)[-MAX_VERIFICATION_OUTPUT_CHARS:],
                    "elapsed_s": 0,
                }
            )
    return results


def _parse_mimo_output(raw: str) -> dict[str, Any]:
    """Parse mimo --format json output for sessionID and summary text.

    Real MiMo JSONL: lines are JSON objects. sessionID is top-level.
    Text events: type="text" with content in part.text (singular object),
    parts[].text (array), or top-level text. Last valid text wins.
    """
    session_id = ""
    summary_text = ""
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # Preserve top-level sessionID
        if "sessionID" in obj or "session_id" in obj:
            sid = obj.get("sessionID") or obj.get("session_id", "")
            if sid:
                session_id = sid
        # Text event: type=text — last valid text wins
        if obj.get("type") == "text":
            text_val = ""
            # 1) singular part object
            part = obj.get("part")
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text", "")
                if t:
                    text_val = t
            # 2) parts array (fallback within same event)
            if not text_val:
                parts = obj.get("parts", [])
                if isinstance(parts, list):
                    for p in reversed(parts):
                        if isinstance(p, dict) and p.get("type") == "text":
                            t = p.get("text", "")
                            if t:
                                text_val = t
                                break
            # 3) top-level text (fallback within same event)
            if not text_val:
                t = obj.get("text", "")
                if t:
                    text_val = t
            if text_val:
                summary_text = text_val
    return {"session_id": session_id, "summary": summary_text[:MAX_SUMMARY_CHARS]}


def _save_task_state(task_id: str, state: dict[str, Any]) -> None:
    _ensure_dirs()
    path = TASKS_DIR / f"{task_id}.json"
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _load_task_state(task_id: str) -> dict[str, Any]:
    _validate_task_id(task_id)
    path = TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Task {task_id} not found")
    return json.loads(path.read_text())


def _compact_failure_info(
    changed_files: list[str], verification: list[dict[str, Any]], summary: str
) -> str:
    """Build a compact failure message for MiMo retry (compressed tail errors)."""
    failed = [v for v in verification if not v.get("passed")]
    lines = [f"Previous attempt: {summary[:300]}"]
    lines.append(f"Changed: {changed_files[:20]}")
    for v in failed[:5]:
        tail_err = (v.get("stderr", "") or "")[-200:]
        lines.append(f"FAIL [{v['command'][:100]}] rc={v['exit_code']}: {tail_err}")
    return "\n".join(lines)[:MAX_SUMMARY_CHARS]


def _check_scope(
    changed_files: list[str], allowed_paths: list[Path], workdir: Path
) -> tuple[bool, list[str]]:
    """Check that all changed files fall within allowed_paths relative to workdir."""
    violations: list[str] = []
    if not allowed_paths:
        return True, []
    resolved_wd = workdir.resolve()
    resolved_allowed = [a.resolve() for a in allowed_paths]
    for f in changed_files:
        fp = (resolved_wd / f).resolve()
        in_scope = any(fp.is_relative_to(a) for a in resolved_allowed)
        if not in_scope:
            violations.append(f)
    return len(violations) == 0, violations


def _gen_task_id() -> str:
    return f"mimo-{uuid.uuid4().hex[:12]}"


def _build_mimo_prompt(
    task: str, allowed_paths: List[str], verification_commands: List[str]
) -> str:
    """Prepend scope/boundary info to the MiMo prompt."""
    scope = "\n".join(f"  - {p}" for p in allowed_paths)
    cmds = "\n".join(f"  - {c}" for c in verification_commands)
    return (
        f"Scope: modify ONLY files under these paths within the workdir:\n{scope}\n"
        f"After changes, these verification commands must pass:\n{cmds}\n"
        f"Do NOT return long logs; just do the work.\n\nTask: {task}"
    )


def _compress_verification_result(
    verification: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compress verification for MCP return: cmd max 200 chars, stderr/stdout max 500."""
    result = []
    for v in verification:
        entry: dict[str, Any] = {
            "command": (v.get("command", ""))[:MAX_VERIFICATION_CMD_CHARS],
            "passed": v.get("passed", False),
            "exit_code": v.get("exit_code", -1),
        }
        if not v.get("passed"):
            entry["stderr"] = (v.get("stderr", ""))[:MAX_VERIFICATION_OUTPUT_CHARS]
            entry["stdout"] = (v.get("stdout", ""))[:MAX_VERIFICATION_OUTPUT_CHARS]
        result.append(entry)
    return result


def _compress_changed_files(files: list[str]) -> list[str]:
    """Compress changed_files for MCP return: max 50 items with overflow indicator."""
    if len(files) <= MAX_CHANGED_FILES_RETURN:
        return files
    shown = files[:MAX_CHANGED_FILES_RETURN]
    remaining = len(files) - MAX_CHANGED_FILES_RETURN
    shown.append(f"...and {remaining} more files")
    return shown


def _validate_nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank or whitespace-only")


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def delegate_task(
    workdir: str,
    task: str,
    allowed_paths: List[str],
    verification_commands: List[str],
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Delegate a coding task to MiMo CLI."""
    # Validate types (reject bool, etc.)
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        raise TypeError("max_iterations must be an int")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise TypeError("timeout_seconds must be an int")
    if not isinstance(task, str) or len(task) > MAX_TASK_LEN:
        raise ValueError(f"task must be a string of at most {MAX_TASK_LEN} chars")
    if not isinstance(workdir, str):
        raise TypeError("workdir must be a string")

    _validate_nonblank(task, "task")

    _ensure_dirs()
    task_id = _gen_task_id()
    mimo_bin = _locate_mimo()

    wd = _validate_workdir(workdir)
    allowed = _validate_paths(wd, allowed_paths)
    _validate_verification(verification_commands)

    max_iterations = max(1, min(max_iterations, 10))
    timeout_seconds = max(30, min(timeout_seconds, MAX_TIMEOUT_SECONDS))

    log_path = LOGS_DIR / f"{task_id}.log"
    log_path.write_text("")

    state: dict[str, Any] = {
        "task_id": task_id,
        "workdir": str(wd),
        "task": task,
        "allowed_paths": allowed_paths,
        "verification_commands": verification_commands,
        "max_iterations": max_iterations,
        "timeout_seconds": timeout_seconds,
        "session_id": "",
        "iterations": 0,
        "changed_files": [],
        "verification": [],
        "summary": "",
        "status": "running",
        "log_path": str(log_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_task_state(task_id, state)

    session_id = ""
    final_summary = ""
    all_verification: list[dict[str, Any]] = []
    accumulated_changed: set[str] = set()
    accumulated_changed_list: list[str] = []

    prompt = _build_mimo_prompt(task, allowed_paths, verification_commands)

    for iteration in range(1, max_iterations + 1):
        state["iterations"] = iteration
        snap_before = _snapshot_files(wd)

        cmd = [
            mimo_bin,
            "run",
            "--format",
            "json",
            "--dir",
            str(wd),
        ]
        if iteration > 1 and session_id:
            cmd.extend(["--session", session_id])

        if iteration == 1:
            cmd.append(prompt)
        else:
            cmd.append(_compact_failure_info(
                accumulated_changed_list, all_verification, final_summary
            ))

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=min(timeout_seconds, MAX_TIMEOUT_SECONDS),
            )
            raw_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            raw_output = f"TIMEOUT: MiMo exceeded {timeout_seconds}s\n"
            proc = type("P", (), {"stdout": "", "stderr": raw_output, "returncode": -1})()

        with open(log_path, "a") as f:
            f.write(f"--- iteration {iteration} ---\n")
            f.write(raw_output)
            f.write("\n")

        parsed = _parse_mimo_output(proc.stdout or "")
        if parsed["session_id"]:
            session_id = parsed["session_id"]
        state["session_id"] = session_id
        final_summary = parsed["summary"] or f"exit_code={proc.returncode}"

        snap_after = _snapshot_files(wd)
        iter_changed = _diff_files(snap_before, snap_after)
        accumulated_changed.update(iter_changed)
        accumulated_changed_list = sorted(accumulated_changed)
        state["changed_files"] = accumulated_changed_list

        scope_ok, violations = _check_scope(accumulated_changed_list, allowed, wd)
        if not scope_ok:
            state["status"] = "scope_violation"
            state["summary"] = f"Files outside allowed_paths: {violations[:10]}"
            state["verification"] = []
            _save_task_state(task_id, state)
            return {
                "task_id": task_id,
                "status": "scope_violation",
                "session_id": session_id,
                "iterations": iteration,
                "changed_files": _compress_changed_files(accumulated_changed_list),
                "verification": [],
                "summary": state["summary"][:MAX_SUMMARY_CHARS],
                "log_path": str(log_path),
            }

        all_verification = _run_verification(wd, verification_commands, timeout_seconds)
        state["verification"] = all_verification

        all_passed = all(v.get("passed", False) for v in all_verification)
        mimo_ok = proc.returncode == 0

        if all_passed and mimo_ok:
            state["status"] = "success"
            state["summary"] = final_summary[:MAX_SUMMARY_CHARS]
            _save_task_state(task_id, state)
            return {
                "task_id": task_id,
                "status": "success",
                "session_id": session_id,
                "iterations": iteration,
                "changed_files": _compress_changed_files(accumulated_changed_list),
                "verification": _compress_verification_result(all_verification),
                "summary": state["summary"],
                "log_path": str(log_path),
            }

    state["status"] = "failed"
    state["summary"] = final_summary[:MAX_SUMMARY_CHARS]
    _save_task_state(task_id, state)
    return {
        "task_id": task_id,
        "status": "failed",
        "session_id": session_id,
        "iterations": max_iterations,
        "changed_files": _compress_changed_files(accumulated_changed_list),
        "verification": _compress_verification_result(all_verification),
        "summary": state["summary"],
        "log_path": str(log_path),
    }


@mcp.tool()
def continue_task(task_id: str, feedback: str) -> dict[str, Any]:
    """Continue a previously delegated task with additional feedback."""
    if not isinstance(feedback, str) or len(feedback) > MAX_FEEDBACK_LEN:
        raise ValueError(
            f"feedback must be a string of at most {MAX_FEEDBACK_LEN} chars"
        )
    _validate_nonblank(feedback, "feedback")
    _validate_task_id(task_id)
    state = _load_task_state(task_id)

    if state.get("status") == "scope_violation":
        raise ValueError(
            f"Task {task_id} is in scope_violation status and cannot be continued"
        )

    wd = Path(state["workdir"]).resolve()
    allowed = _validate_paths(wd, state["allowed_paths"])
    verification_commands = state["verification_commands"]
    timeout_seconds = state.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    mimo_bin = _locate_mimo()

    snap_before = _snapshot_files(wd)

    cmd = [
        mimo_bin,
        "run",
        "--format",
        "json",
        "--dir",
        str(wd),
    ]
    if state.get("session_id"):
        cmd.extend(["--session", state["session_id"]])
    cmd.append(feedback)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=min(timeout_seconds, MAX_TIMEOUT_SECONDS),
        )
        raw_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        raw_output = f"TIMEOUT: MiMo exceeded {timeout_seconds}s\n"
        proc = type("P", (), {"stdout": "", "stderr": raw_output, "returncode": -1})()

    log_path = Path(state.get("log_path", str(LOGS_DIR / f"{task_id}.log")))
    with open(log_path, "a") as f:
        f.write(f"--- continue_task feedback ---\n")
        f.write(raw_output)
        f.write("\n")

    parsed = _parse_mimo_output(proc.stdout or "")
    if parsed["session_id"]:
        state["session_id"] = parsed["session_id"]

    snap_after = _snapshot_files(wd)
    iter_changed = _diff_files(snap_before, snap_after)

    prior = set(state.get("changed_files", []))
    prior.update(iter_changed)
    accumulated = sorted(prior)

    scope_ok, violations = _check_scope(accumulated, allowed, wd)
    if not scope_ok:
        state["status"] = "scope_violation"
        state["summary"] = f"Files outside allowed_paths: {violations[:10]}"
        state["changed_files"] = accumulated
        state["verification"] = []
        state["iterations"] = state.get("iterations", 0) + 1
        _save_task_state(task_id, state)
        return {
            "task_id": task_id,
            "status": "scope_violation",
            "session_id": state["session_id"],
            "iterations": state["iterations"],
            "changed_files": _compress_changed_files(accumulated),
            "verification": [],
            "summary": state["summary"][:MAX_SUMMARY_CHARS],
            "log_path": str(log_path),
        }

    verification = _run_verification(wd, verification_commands, timeout_seconds)
    all_passed = all(v.get("passed", False) for v in verification)
    mimo_ok = proc.returncode == 0

    state["iterations"] = state.get("iterations", 0) + 1
    state["changed_files"] = accumulated
    state["verification"] = verification
    state["summary"] = (parsed["summary"] or f"exit_code={proc.returncode}")[
        :MAX_SUMMARY_CHARS
    ]
    state["status"] = "success" if (all_passed and mimo_ok) else "failed"
    _save_task_state(task_id, state)

    return {
        "task_id": task_id,
        "status": state["status"],
        "session_id": state["session_id"],
        "iterations": state["iterations"],
        "changed_files": _compress_changed_files(accumulated),
        "verification": _compress_verification_result(verification),
        "summary": state["summary"],
        "log_path": str(log_path),
    }


@mcp.tool()
def task_result(task_id: str) -> dict[str, Any]:
    """Return compact status for a previously delegated task."""
    _validate_task_id(task_id)
    state = _load_task_state(task_id)
    return {
        "task_id": state["task_id"],
        "status": state["status"],
        "session_id": state.get("session_id", ""),
        "iterations": state.get("iterations", 0),
        "changed_files": _compress_changed_files(state.get("changed_files", [])),
        "verification": _compress_verification_result(state.get("verification", [])),
        "summary": state.get("summary", "")[:MAX_SUMMARY_CHARS],
        "log_path": state.get("log_path", ""),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")

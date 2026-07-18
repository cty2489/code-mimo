---
name: delegate-to-mimo
description: "Delegate large coding tasks to MiMo CLI via MCP. Use delegate_task for multi-file implementation, broad refactoring, or tasks with clear test/lint/build acceptance criteria. Saves Codex context tokens by offloading generation to MiMo subprocess."
---

# Delegate to MiMo

Codex delegates to MiMo CLI, retains full acceptance control.

## Flow

1. `delegate_task(workdir, task, allowed_paths, verification_commands)`
2. Review result; **independently verify** — never trust MiMo self-test
3. On failure: `continue_task(task_id, feedback)` or handle directly
4. `task_result(task_id)` to check status

## Hard Rules

- **Codex must independently verify.** MiMo verification is informational only.
- **scope_violation is terminal.** No auto-bypass. `continue_task` rejects it.
- **Quality over token savings.** Correctness first.
- Read logs only on failure; read only relevant sections from `log_path`.
- MiMo auto-retries up to `max_iterations` on verification failure.

---
name: delegate-to-mimo
description: "将大型编码任务委派给 MiMo CLI 后台 Worker / Delegate substantial coding tasks to persistent MiMo workers. Use for multi-file implementation, broad refactoring, or work with clear test/lint/build criteria. Saves Codex context while Codex retains acceptance control."
---

# Delegate to MiMo / 委派给 MiMo

Codex 将实现工作委派给 MiMo CLI，同时保留最终验收权 / Codex delegates implementation to MiMo CLI while retaining acceptance control.

## Flow

1. Delegate only substantial work: `delegate_task(workdir, task, allowed_paths, verification_commands)`.
2. Keep `pure=true` unless the user explicitly needs MiMo external plugins.
3. If status is `running`, use `wait_task(task_id)` sparingly or return the task ID for later checking. Never poll rapidly.
4. On completion, inspect the diff and **independently verify**; never trust MiMo self-test alone.
5. On verification failure, use `continue_task(task_id, feedback)` or handle directly.
6. Use `cancel_task(task_id)` when the user asks to stop a task.

## Hard Rules

- **Codex must independently verify.** MiMo verification is informational only.
- **scope_violation is terminal.** No auto-bypass. `continue_task` rejects it.
- **Quality over token savings.** Correctness first.
- Handle trivial one-file edits directly unless the user explicitly requests MiMo.
- Only one delegated task may run in the same workdir at a time.
- A `running` result means the independent worker continues after the MCP call returns.
- Read logs only on failure; read only relevant sections from `log_path`.
- Verification failure may reuse the same MiMo session until `max_iterations` total attempts are reached.

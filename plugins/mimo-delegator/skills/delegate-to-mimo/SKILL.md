---
name: delegate-to-mimo
description: "将大型编码任务委派给 MiMo CLI 后台 Worker / Delegate substantial coding tasks to persistent MiMo workers. Use for multi-file implementation, broad refactoring, or work with clear test/lint/build criteria. Saves Codex context while Codex retains acceptance control."
---

# Delegate to MiMo / 委派给 MiMo

Codex 将实现工作委派给 MiMo CLI，同时保留最终验收权 / Codex delegates implementation to MiMo CLI while retaining acceptance control.

## Flow / 工作流

1. 仅委派有实质工作量的任务 / Delegate only substantial work: `delegate_task(workdir, task, allowed_paths, verification_commands)`.
2. 保持 `pure=true`，除非用户明确需要 MiMo 外部插件 / Keep `pure=true` unless the user explicitly needs MiMo external plugins.
3. 状态为 `running` 时，少用 `wait_task`，或直接返回 task_id 稍后查询，不要频繁轮询 / If `running`, use `wait_task` sparingly or return the task ID for later checking. Never poll rapidly.
4. `task_result(task_id, detail="brief")` 返回最小上下文；`detail="summary"` 返回完整详情 / Use `detail="brief"` for minimal context; `detail="summary"` for full details.
5. `task_evidence(task_id)` 仅在调试失败时使用，返回有界日志片段 / Use `task_evidence` only when debugging failures — returns bounded log tails and verification output.
6. 完成后，独立检查 diff 并验证；不要仅信任 MiMo 自测 / On completion, inspect the diff and **independently verify**; never trust MiMo self-test alone.
7. 验证失败时，用 `continue_task(task_id, feedback)` 续接或直接处理 / On verification failure, use `continue_task(task_id, feedback)` or handle directly.
8. 用户要求停止时，用 `cancel_task(task_id)` 取消 / Use `cancel_task(task_id)` when the user asks to stop a task.

## Result Levels / 结果层级

- **brief**（`task_result` 默认）: task_id、status、phase、elapsed、changed_files_count、verification_conclusion、recommended_next_action。最小上下文消耗 / Minimal context consumption.
- **summary**: 增加 changed_files 列表、验证详情、key_decisions、known_issues。通过 `task_result(task_id, detail="summary")` 获取 / Access via `task_result(task_id, detail="summary")`.
- **evidence**: `task_evidence(task_id, log_tail_lines, verification_output)` 返回有界日志片段 / Bounded log snippets. Never read raw large logs into context.

## Phase and Heartbeat / 阶段与心跳

- 任务跟踪 `phase` 字段：`queued → starting → running → verifying → finished` / Tasks track `phase`: `queued → starting → running → verifying → finished`.
- Worker 每 30 秒写入心跳 `last_heartbeat_at` / Worker writes `last_heartbeat_at` every 30 seconds.
- 超过 `MIMO_STALLED_HEARTBEAT_SECONDS`（默认 300s）无心跳，标记 `stalled`（heartbeat_expired）——这是观测异常，不是总时限 / If no heartbeat for 300s, marked `stalled` (heartbeat_expired) — observational anomaly, NOT a total time limit.
- 若 Worker 在 stalled 期间死亡，转为 `worker_crashed`；若心跳恢复，回到 `running`/`verifying` / If worker dies while stalled → `worker_crashed`; if heartbeat resumes → back to `running`/`verifying`.
- 长时间任务正常运行期间心跳持续更新，300s 仅为心跳间隔阈值 / Long tasks are normal; 300s is only the heartbeat gap threshold.

## Resume Intent / 恢复意图

- `continue_task(task_id, feedback, resume_intent="fix_verification")` 记录续接原因 / Records why you are continuing.
- 有效意图：`resume_work`、`fix_verification`、`address_review`、`retry_transient` / Valid intents: `resume_work`, `fix_verification`, `address_review`, `retry_transient`.
- 仅元数据，不影响行为 / Metadata only — does not change behavior.

## Hard Rules / 硬性规则

- **Codex 必须独立验证。** MiMo 验证仅供参考 / **Codex must independently verify.** MiMo verification is informational only.
- **scope_violation 是终止状态。** 不可自动绕过，`continue_task` 拒绝 / **scope_violation is terminal.** No auto-bypass. `continue_task` rejects it.
- **质量优先于节省 Token。** 正确性第一 / **Quality over token savings.** Correctness first.
- 单文件简单修改直接处理，除非用户明确要求 MiMo / Handle trivial one-file edits directly unless the user explicitly requests MiMo.
- 同一 workdir 同时只允许一个委派任务 / Only one delegated task may run in the same workdir at a time.
- `running` 表示独立 Worker 在 MCP 调用返回后继续运行 / A `running` result means the independent worker continues after the MCP call returns.
- 仅在失败时读取日志；用 `task_evidence` 获取有界证据，不要读原始日志 / Read logs only on failure; use `task_evidence` for bounded evidence, never raw log files.
- 验证失败可复用同一 MiMo 会话，直到达到 `max_iterations` / Verification failure may reuse the same MiMo session until `max_iterations` total attempts are reached.
- 300s 心跳阈值是异常检测器，不是任务总时限，长时间任务正常 / 300s heartbeat threshold is an anomaly detector, not a total task time limit. Long tasks are expected.
- Worker 仍活跃但心跳过期时 `continue_task` 拒绝；稍后复查或先 `cancel_task` / `continue_task` rejects when Worker is still alive (heartbeat expired but process running). Check again later or `cancel_task` first.
- 用 `doctor()` 诊断 MiMo 可执行文件、状态目录、损坏文件、残留锁、孤儿进程 / Use `doctor()` to diagnose MiMo executable, state directories, corrupt files, stale locks, and orphan processes.

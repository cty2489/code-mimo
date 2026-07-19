# Code MiMo

[中文说明](#中文说明) | [English](#english)

## 中文说明

`mimo-delegator` 是一个 Codex 插件，通过 MCP 将大型编码任务委派给 MiMo CLI。MiMo 负责执行实现工作；Codex 继续控制修改范围、审查结果，并独立运行验证命令。

这个插件的目标是在不把低成本模型输出视为“天然正确”的前提下，减少 Codex 的上下文和 Token 消耗。质量优先于节省 Token。

### 功能

- 将多文件编码任务委派给本机安装的 `mimo` 命令。
- 默认使用 `--pure` 隔离 MiMo 外部插件，避免本机 Memory 或 Skill 污染任务上下文。
- 使用独立后台 Worker；短任务可在一次调用内返回，长任务可持续运行数小时。
- 强制提供明确的文件白名单，并报告检测到的白名单外修改。
- 每次 MiMo 正常完成且通过修改范围检查后，运行可信的测试、Lint 或构建命令。
- 验证失败时，自动要求同一个 MiMo 会话重试，直至达到最大尝试次数。
- 分层结果返回：`task_result` 默认返回 brief 摘要（计数和结论），可选 summary 级别返回完整文件列表和验证详情。
- 按需取证：`task_evidence` 返回有界日志片段和验证输出，避免原始大日志回灌上下文。
- 心跳监测：Worker 每 30 秒更新心跳；心跳过期自动标记 stalled，进程消失标记 worker_crashed，不引入总运行上限。
- 支持状态查询、短时等待和进程组取消。
- 提供 `delegate_task`、`continue_task`、`task_result`、`wait_task`、`cancel_task`、`task_evidence` 和 `doctor` 七个 MCP 工具。

### 环境要求

- Python 3.10 或更高版本。
- 可正常工作的 MiMo CLI。可执行文件需要位于 `PATH` 中、安装在 `~/.mimocode/bin/mimo`，或通过 `MIMO_EXECUTABLE` 指定。
- 支持插件和 Git marketplace 的 Codex 版本。

安装 Python MCP 依赖：

```bash
python3 -m pip install "mcp>=1.8,<2"
```

### 在 Codex 中安装

先将本仓库注册为 marketplace，再安装插件：

```bash
codex plugin marketplace add cty2489/code-mimo
codex plugin add mimo-delegator@code-mimo
```

安装完成后，请新建一个 Codex 任务，使 MCP 工具和 Skill 被正确加载。

如果 MiMo 不在 `PATH` 中，请在启动 Codex 前设置它的位置：

```bash
export MIMO_EXECUTABLE="/absolute/path/to/mimo"
```

### 使用方法

在新的 Codex 任务中明确要求使用该 Skill，例如：

```text
使用 $delegate-to-mimo 完成这个多文件修改。只允许修改 src/ 和 tests/，并运行完整测试。
```

主要工具 `delegate_task` 接收以下参数：

- `workdir`：项目目录。
- `task`：完整的实现要求和验收标准。
- `allowed_paths`：允许 MiMo 修改的非空路径列表。
- `verification_commands`：需要执行的非空可信本地命令列表。
- `max_iterations`：最大尝试次数，默认值为 `3`。
- `wait_seconds`：首次调用最多等待多久，默认 `45` 秒，最大 `55` 秒；到时只返回 `running`，不会停止 Worker。
- `max_runtime_seconds`：每次 MiMo 尝试的运行上限，默认 `0`，表示不限总时长。
- `verification_timeout_seconds`：每条验证命令的超时，默认 `3600` 秒；设为 `0` 表示不限时。
- `pure`：是否禁用 MiMo 外部插件，默认 `true`。

如果返回 `running`，任务仍在后台执行：

```text
task_result(task_id)              # 紧凑 brief 查询
task_result(task_id, "summary")   # 完整摘要
task_evidence(task_id)            # 按需取证（日志片段）
wait_task(task_id, 45)            # 最多等待 45 秒
cancel_task(task_id)              # 请求取消并终止活动进程组
continue_task(task_id, feedback)  # 完成或失败后续接原会话
doctor()                          # 诊断插件健康状态
```

状态含义：`queued` 表示等待 Worker 启动，`running` 表示 MiMo 正在执行，`verifying` 表示正在运行验收命令；`success`、`failed`、`worker_crashed`、`scope_violation` 和 `cancelled` 是终止状态。`stalled` 会结束当前 `wait_task` 等待，但不一定是最终状态：心跳过期（heartbeat_expired）是可恢复的观测异常，Worker 仍活跃时心跳恢复后任务回到 `running`/`verifying`，Worker 死亡则转为 `worker_crashed`；运行超时（runtime timeout）或验证超时（verification timeout）表示本轮已停止，可通过 `continue_task` 续接。

心跳阈值（默认 300 秒）仅是异常检测指标，不是任务总运行时限。长时间任务正常运行期间心跳会持续更新。`continue_task` 在 Worker 仍活跃但心跳过期时会拒绝，请稍后复查或先 `cancel_task`。

阶段（phase）：`queued → starting → running → verifying → finished`，提供比状态更细粒度的任务进程信息。

恢复意图（resume_intent）：`continue_task` 支持可选的 `resume_intent` 参数（如 `fix_verification`、`resume_work`），记录恢复原因，不影响行为。

不要高频查询长任务。对于数小时任务，保存 `task_id`，稍后再查询最节省 Codex Token。同一个项目目录同时只允许一个委派任务。

Codex 在接受结果前，仍应独立检查代码差异并重新运行相关验证。

### 本地状态

任务状态和完整日志保存在项目目录之外：

```text
~/.codex/mimo-delegator/tasks/
~/.codex/mimo-delegator/logs/
```

MCP 只向 Codex 返回紧凑结果，只有排查问题时才需要读取完整日志，从而避免不必要地占用 Codex 上下文。

### 安全说明

- `allowed_paths` 是修改检测和验收保护机制，不是操作系统沙箱。MiMo 返回后，插件会报告越界修改，但不会自动撤销这些修改。
- 为避免扫描运行时缓存和大型二进制，修改检测会跳过 `.git`、`.venv`、`node_modules`、常见 Python 缓存、`data/qdrant` 和 `.gguf` 文件；不要把这些路径交给 MiMo 修改。需要强隔离时，应在独立 Git worktree 或容器中运行。
- 验证命令通过 `/bin/zsh -lc` 在本机执行，因此只能提供你信任的命令。
- 插件本身不会调用在线大模型 API。MiMo 是否联网以及如何处理数据，取决于你的 MiMo 安装和账号配置。
- 完整日志可能包含任务提示和模型输出，应按照可能含有敏感项目内容的文件进行管理。
- 建议始终在有版本控制的项目中运行插件，以便审查和恢复修改。

### 健康检查

MCP 工具 `doctor()` 诊断插件健康状态：检查 MiMo 可执行文件及版本、状态目录可写性、损坏状态文件、残留锁、孤儿进程、已安装版本。仅读取不修改，返回中英双语诊断结论。

命令行等价用法：

```bash
python3 plugins/mimo-delegator/scripts/mimo_mcp_server.py --doctor
```

CLI 有异常时返回非零退出码。可设置 `MIMO_DELEGATOR_SOURCE_PATH` 指向源码仓库中 `.codex-plugin/plugin.json` 的绝对路径，doctor 会比对已安装版本与源码版本是否一致。

### 开发与测试

在仓库根目录执行：

```bash
python3 -m unittest discover -s plugins/mimo-delegator/tests -v
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/mimo-delegator
```

测试套件使用本地假 MiMo 可执行文件测试真实进程生命周期，不会消耗 MiMo 使用额度。

## English

`mimo-delegator` is a Codex plugin that delegates large coding tasks to the MiMo CLI through MCP. MiMo performs the implementation work; Codex keeps control of scope, reviews the result, and independently runs verification commands.

The goal is to reduce Codex context and token use without treating a lower-cost model's output as automatically correct. Quality takes priority over token savings.

### Features

- Delegates multi-file coding tasks to the locally installed `mimo` command.
- Uses `--pure` by default to isolate MiMo external plugins from local Memory or Skill contamination.
- Runs independent background workers: quick tasks may finish in one call, while long tasks may continue for hours.
- Requires an explicit file allowlist and reports detected changes outside that scope.
- Runs trusted test, lint, or build commands after each MiMo attempt that exits normally and passes the scope check.
- Automatically asks the same MiMo session to retry failed verification, up to the configured maximum number of attempts.
- Layered results: `task_result` defaults to brief summary (counts and conclusions), with optional summary level for full file lists and verification details.
- On-demand evidence: `task_evidence` returns bounded log snippets and verification output, preventing raw log flooding into context.
- Heartbeat monitoring: workers update heartbeat every 30s; stale heartbeat marks `stalled`, missing process marks `worker_crashed`, no total runtime limit imposed.
- Supports compact status checks, bounded waiting, and process-group cancellation.
- Provides `delegate_task`, `continue_task`, `task_result`, `wait_task`, `cancel_task`, `task_evidence`, and `doctor` MCP tools.

### Requirements

- Python 3.10 or newer.
- A working MiMo CLI installation. The executable must be available on `PATH`, at `~/.mimocode/bin/mimo`, or through `MIMO_EXECUTABLE`.
- A Codex version that supports plugins and Git marketplaces.

Install the Python MCP dependency:

```bash
python3 -m pip install "mcp>=1.8,<2"
```

### Install in Codex

Register this repository as a marketplace, then install the plugin:

```bash
codex plugin marketplace add cty2489/code-mimo
codex plugin add mimo-delegator@code-mimo
```

Start a new Codex task after installation so the MCP tools and Skill are loaded.

If MiMo is not on `PATH`, set its location before starting Codex:

```bash
export MIMO_EXECUTABLE="/absolute/path/to/mimo"
```

### Usage

In a new Codex task, ask Codex to use the Skill explicitly, for example:

```text
Use $delegate-to-mimo for this multi-file change. Only modify src/ and tests/, and run the full test suite.
```

The main `delegate_task` tool accepts:

- `workdir`: project directory.
- `task`: complete implementation request and acceptance criteria.
- `allowed_paths`: non-empty list of paths MiMo is expected to change.
- `verification_commands`: non-empty list of trusted local commands.
- `max_iterations`: maximum total attempts, default `3` (one initial attempt plus at most two retries).
- `wait_seconds`: initial call wait, default `45` seconds and maximum `55`; expiration returns `running` without stopping the worker.
- `max_runtime_seconds`: runtime limit for each MiMo attempt, default `0` for no total limit.
- `verification_timeout_seconds`: timeout for each verification command, default `3600` seconds; use `0` for no limit.
- `pure`: disables MiMo external plugins, default `true`.

When the result is `running`, the task continues in the background:

```text
task_result(task_id)              # brief status query
task_result(task_id, "summary")   # full summary with file lists
task_evidence(task_id)            # on-demand evidence (log snippets)
wait_task(task_id, 45)            # wait up to 45 seconds
cancel_task(task_id)              # cancel the active process group
continue_task(task_id, feedback)  # resume the existing session after completion or failure
doctor()                          # diagnose plugin health
```

Status meanings: `queued` is waiting for the worker to start, `running` is active MiMo work, and `verifying` is running acceptance commands. `success`, `failed`, `worker_crashed`, `scope_violation`, and `cancelled` are terminal. `stalled` ends the current `wait_task` but is not necessarily final: heartbeat expired (heartbeat_expired) is a recoverable observational anomaly — if the worker is still alive and sends a fresh heartbeat, the task returns to `running`/`verifying`; if the worker dies, it transitions to `worker_crashed`. Runtime timeout or verification timeout means this attempt has stopped and can be resumed via `continue_task`.

The heartbeat threshold (default 300s) is an anomaly detector, not a total task time limit. Long-running tasks update heartbeat continuously during normal operation. `continue_task` rejects when the Worker is still alive but heartbeat expired — check again later or `cancel_task` first.

Phase: `queued → starting → running → verifying → finished` provides finer-grained progress information than status alone.

Resume intent: `continue_task` accepts an optional `resume_intent` parameter (e.g. `fix_verification`, `resume_work`) to record why you are continuing. This is metadata only and does not change behavior.

Do not poll long tasks rapidly. For multi-hour work, retain the `task_id` and check it later to minimize Codex token use. Only one delegated task may run in a workdir at a time.

Codex should independently inspect the diff and rerun relevant checks before accepting a result.

### Local state

Task state and full logs are stored outside the project:

```text
~/.codex/mimo-delegator/tasks/
~/.codex/mimo-delegator/logs/
```

Compact MCP responses avoid loading those full logs into Codex context. Use `task_evidence` for bounded log snippets when troubleshooting is necessary.

### Safety notes

- `allowed_paths` is a change-detection and acceptance guard, not an OS sandbox. Out-of-scope changes are reported after MiMo returns and are not automatically reverted.
- To avoid scanning runtime caches and large binaries, change detection skips `.git`, `.venv`, `node_modules`, common Python caches, `data/qdrant`, and `.gguf` files. Do not ask MiMo to modify those paths; use an isolated Git worktree or container when stronger isolation is required.
- Verification commands run locally through `/bin/zsh -lc`; provide only commands you trust.
- The plugin itself does not call an online model API. MiMo's own network and data behavior depends on your MiMo installation and account configuration.
- Full logs may contain task prompts and model output, so manage them as files that may include sensitive project content.
- Run the plugin in a version-controlled project so changes can be reviewed and recovered.

### Health check

The `doctor()` MCP tool diagnoses plugin health: checks the MiMo executable and its `--version`, state directory writability, corrupt state files, stale locks, orphan processes, and installed version. Read-only — returns bilingual (Chinese/English) diagnostic findings.

Equivalent CLI usage:

```bash
python3 plugins/mimo-delegator/scripts/mimo_mcp_server.py --doctor
```

The CLI exits with a non-zero code when issues are found. Set `MIMO_DELEGATOR_SOURCE_PATH` to the absolute path of the source repository's `.codex-plugin/plugin.json` file to compare the installed version against the source version.

### Development and tests

From the repository root:

```bash
python3 -m unittest discover -s plugins/mimo-delegator/tests -v
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/mimo-delegator
```

The test suite uses a local fake MiMo executable to exercise real process lifecycles; it does not consume MiMo usage.

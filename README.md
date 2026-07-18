# Code MiMo

[中文说明](#中文说明) | [English](#english)

## 中文说明

`mimo-delegator` 是一个 Codex 插件，通过 MCP 将大型编码任务委派给 MiMo CLI。MiMo 负责执行实现工作；Codex 继续控制修改范围、审查结果，并独立运行验证命令。

这个插件的目标是在不把低成本模型输出视为“天然正确”的前提下，减少 Codex 的上下文和 Token 消耗。质量优先于节省 Token。

### 功能

- 将多文件编码任务委派给本机安装的 `mimo` 命令。
- 强制提供明确的文件白名单，并报告白名单以外的修改。
- 每次尝试后运行可信的测试、Lint 或构建命令。
- 验证失败时，自动要求同一个 MiMo 会话重试，直至达到设定次数。
- 向 Codex 返回紧凑摘要，同时将完整 MiMo 输出保存在本地日志中。
- 提供 `delegate_task`、`continue_task` 和 `task_result` 三个 MCP 工具。

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
- `timeout_seconds`：每次尝试的超时时间，默认值为 `1800` 秒。

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
- 验证命令通过 `/bin/zsh -lc` 在本机执行，因此只能提供你信任的命令。
- 插件本身不会调用在线大模型 API。MiMo 是否联网以及如何处理数据，取决于你的 MiMo 安装和账号配置。
- 完整日志可能包含任务提示和模型输出，应按照可能含有敏感项目内容的文件进行管理。
- 建议始终在有版本控制的项目中运行插件，以便审查和恢复修改。

### 开发与测试

在仓库根目录执行：

```bash
python3 -m unittest discover -s plugins/mimo-delegator/tests -v
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/mimo-delegator
```

测试套件会模拟 MiMo 子进程调用，不会消耗 MiMo 使用额度。

## English

`mimo-delegator` is a Codex plugin that delegates large coding tasks to the MiMo CLI through MCP. MiMo performs the implementation work; Codex keeps control of scope, reviews the result, and independently runs verification commands.

The goal is to reduce Codex context and token use without treating a lower-cost model's output as automatically correct. Quality takes priority over token savings.

### Features

- Delegates multi-file coding tasks to the locally installed `mimo` command.
- Requires an explicit file allowlist and reports changes outside that scope.
- Runs trusted test, lint, or build commands after every attempt.
- Automatically asks the same MiMo session to retry failed verification, up to a configured limit.
- Returns compact summaries to Codex while keeping full MiMo output in local log files.
- Provides `delegate_task`, `continue_task`, and `task_result` MCP tools.

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
- `max_iterations`: retry limit, default `3`.
- `timeout_seconds`: per-attempt timeout, default `1800` seconds.

Codex should independently inspect the diff and rerun relevant checks before accepting a result.

### Local state

Task state and full logs are stored outside the project:

```text
~/.codex/mimo-delegator/tasks/
~/.codex/mimo-delegator/logs/
```

Compact MCP responses avoid loading those full logs into Codex context unless troubleshooting is necessary.

### Safety notes

- `allowed_paths` is a change-detection and acceptance guard, not an OS sandbox. Out-of-scope changes are reported after MiMo returns and are not automatically reverted.
- Verification commands run locally through `/bin/zsh -lc`; provide only commands you trust.
- The plugin itself does not call an online model API. MiMo's own network and data behavior depends on your MiMo installation and account configuration.
- Full logs may contain task prompts and model output, so manage them as files that may include sensitive project content.
- Run the plugin in a version-controlled project so changes can be reviewed and recovered.

### Development and tests

From the repository root:

```bash
python3 -m unittest discover -s plugins/mimo-delegator/tests -v
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/mimo-delegator
```

The test suite mocks MiMo subprocess calls; it does not consume MiMo usage.

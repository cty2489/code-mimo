# Code MiMo

`mimo-delegator` is a Codex plugin that delegates large coding tasks to the MiMo CLI through MCP. MiMo performs the implementation work; Codex keeps control of scope, reviews the result, and independently runs verification commands.

The goal is to reduce Codex context and token use without treating a cheaper model's output as automatically correct.

## Features

- Delegates multi-file coding tasks to the locally installed `mimo` command.
- Requires an explicit file allowlist and reports changes outside that scope.
- Runs trusted test, lint, or build commands after every attempt.
- Automatically asks the same MiMo session to retry failed verification, up to a configured limit.
- Returns compact summaries to Codex while keeping full MiMo output in local log files.
- Provides `delegate_task`, `continue_task`, and `task_result` MCP tools.

## Requirements

- Python 3.10 or newer.
- A working MiMo CLI installation. The executable must be available as `mimo`, at `~/.mimocode/bin/mimo`, or through `MIMO_EXECUTABLE`.
- A Codex version that supports plugins and Git marketplaces.

Install the Python MCP dependency:

```bash
python3 -m pip install "mcp>=1.8,<2"
```

## Install in Codex

Register this repository as a marketplace, then install the plugin:

```bash
codex plugin marketplace add cty2489/code-mimo
codex plugin add mimo-delegator@code-mimo
```

Start a new Codex task after installation so the MCP tools and skill are loaded.

If MiMo is not on `PATH`, set its location before starting Codex:

```bash
export MIMO_EXECUTABLE="/absolute/path/to/mimo"
```

## Usage

In a new Codex task, ask Codex to use the skill explicitly, for example:

```text
使用 $delegate-to-mimo 完成这个多文件修改。只允许修改 src/ 和 tests/，并运行完整测试。
```

The main tool accepts:

- `workdir`: project directory.
- `task`: complete implementation request and acceptance criteria.
- `allowed_paths`: non-empty list of paths MiMo is expected to change.
- `verification_commands`: non-empty list of trusted local commands.
- `max_iterations`: retry limit, default `3`.
- `timeout_seconds`: per-attempt timeout, default `1800`.

Codex should independently inspect the diff and rerun relevant checks before accepting a result.

## Local state

Task state and full logs are stored outside the project:

```text
~/.codex/mimo-delegator/tasks/
~/.codex/mimo-delegator/logs/
```

Compact MCP responses avoid loading those full logs into Codex context unless troubleshooting is necessary.

## Safety notes

- The allowlist is a detection and acceptance guard, not an OS sandbox. Out-of-scope changes are reported after MiMo returns and are not automatically reverted.
- Verification commands run locally through `/bin/zsh -lc`; provide only commands you trust.
- The plugin itself does not call an online model API. MiMo's own network and data behavior depends on your MiMo installation and account configuration.
- Run the plugin in a version-controlled project so changes can be reviewed and recovered.

## Development and tests

From the repository root:

```bash
python3 -m unittest discover -s plugins/mimo-delegator/tests -v
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/mimo-delegator
```

The test suite mocks MiMo subprocess calls; it does not consume MiMo usage.

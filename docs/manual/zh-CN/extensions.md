# 编写扩展

扩展是 snowpea 守护进程加载到自身中的 Python 代码。它可以为模型提供新工具、向系统提示词添加内容、持续告诉模型会变化的事实、查看每个工具结果、防止回合过早结束、添加斜杠命令，以及运行 MCP 服务器。读完本页，无需阅读 snowpea 的源码就能写出扩展。

其他语言：[English](../en/extensions.md) · [한국어](../ko/extensions.md) · [日本語](../ja/extensions.md) · [Español](../es/extensions.md)

本页出现的每个文件都是仓库 [`examples/extensions/`](../../../examples/extensions/README.md) 中一个完整、可运行的扩展，测试套件会逐一加载它们。

## 什么是扩展，何时编写

snowpea 有三种附加组件（见[插件](plugins.md#what-is-what)）：

| 你想要 | 编写 |
|---|---|
| 教模型一个流程、一份检查清单、一种在需要时阅读的团队风格 | **技能**（`SKILL.md`）——无需代码 |
| 以 Claude Code 或 Codex 格式打包技能、代理、命令、钩子和 MCP 服务器分享给别人 | **插件** |
| 改变代理本身的工作方式：用 Python 写的工具、提示词段落、变化时重新发送的事实、对每个工具结果的检查、让回合继续的理由 | **扩展** |

选择能完成任务的最轻量的那种。技能无需信任；扩展会在守护进程里运行代码。

## 信任模型

- 扩展**在守护进程的进程内、以守护进程的权限**运行：它能读写你能读写的所有文件，能建立网络连接，能看到所有会话。像对待 shell 脚本一样，只安装你信任的代码。
- 只扫描 `$SNOWPEA_HOME/extensions/`（默认 `~/.snowpea/extensions/`）。项目文件夹**从不**被扫描，所以打开克隆下来的仓库不会运行其中的代码。
- 模型用扩展工具做的事仍然要经过模式和审批规则：工具声明自己属于哪类操作（`read`、`write`、`exec`、`network`、`send`），由模式决定是否询问人。审批提示会显示该工具来自哪个扩展。
- 在守护进程的环境中设置 `SNOWPEA_EXTENSIONS=0`，它就不加载任何扩展启动。

## 快速上手

用起始模板创建一个扩展：

```bash
snowpea extension new hello
```

它会在 `~/.snowpea/extensions/hello/` 写入两个文件。`plugin.py`：

<!-- example: examples/extensions/hello/plugin.py -->
```python
"""hello: a snowpea extension.

Created by `snowpea extension new hello`. Edit this file, then run
`snowpea extension reload hello` and look for the tool in
`snowpea tools list`. The developer guide is docs/manual/en/extensions.md.
"""

from __future__ import annotations

from typing import Any

# Import only from snowpea_core.extensions: it is the stable API.
from snowpea_core.extensions import PluginContext, Tool, ToolContext, ToolResult


async def greet(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Runs when the model calls the tool; ``args`` follows ``input_schema``."""
    who = str(args.get("name") or "world").strip()
    if len(who) > 100:
        # ok=False: the model reads ``error`` and can call again with better args.
        return ToolResult(ok=False, error="name is longer than 100 characters")
    return ToolResult(ok=True, output=f"Hello, {who}!")


def apply(ctx: PluginContext) -> None:
    """Called once when the extension loads.

    Everything registered through ``ctx`` is undone when the extension is
    disabled, reloaded or removed. If this function raises, nothing stays
    registered and `snowpea extension list` shows the error.
    """
    ctx.tools.register(
        Tool(
            name="hello_greet",
            category="hello",
            description="Greet someone by name. Use it when the user asks to be greeted.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
            },
            # read: runs without asking in every mode. Use write, exec, network
            # or send for a tool that changes files, runs programs, goes online
            # or sends messages; the mode then decides whether to ask.
            permission="read",
            run=greet,
        )
    )
    ctx.log.info("ready")  # goes to $SNOWPEA_HOME/logs/daemon.log
```

以及 `plugin.json`：

<!-- example: examples/extensions/hello/plugin.json -->
```json
{
  "name": "hello",
  "description": "Greets people: the starting point made by snowpea extension new.",
  "version": "0.1.0",
  "engines": {"snowpea": "^1"}
}
```

把它加载到正在运行的守护进程（未运行则会启动），然后查看工具：

```bash
snowpea extension reload hello
snowpea tools list
```

`hello_greet` 会带着 `[extension:hello]` 列出。让代理向某人问好，它就会调用这个工具。现在修改 `plugin.py` 中的问候语，再运行一次 `snowpea extension reload hello`，无需重启守护进程，下一次模型调用就会使用新代码。如果写错了，`reload` 会以状态码 1 退出，并打印带文件和行号的错误：

```bash
snowpea extension list
snowpea extension info hello
```

## 文件夹布局与 `plugin.json`

一个扩展就是 `$SNOWPEA_HOME/extensions/` 中的一个文件夹。文件夹名就是扩展名：字母、数字、`-` 和 `_`，最多 64 个字符。

| 路径 | 必需 | 内容 |
|---|---|---|
| `plugin.py` | 是 | 定义 `apply(ctx)`；没有它的文件夹会被忽略 |
| `plugin.json` | 否 | 下面的清单文件 |
| `*.py`、子包 | 否 | 你自己的模块，用相对导入：`from . import helpers`、`from .lib import client`（子文件夹需要 `__init__.py`） |
| `tests/` | 否 | 你的测试；守护进程会忽略 |
| 其他 | 否 | 数据文件；用 `Path(__file__).parent` 找到它们 |

有意不支持对自己模块的绝对导入（`import helpers`）：每个扩展都作为独立的包导入，所以两个扩展即使都有 `helpers.py` 也互不可见。第三方包必须安装在守护进程的 Python（运行 `snowpea` 的那个）里；对于小的纯 Python 依赖，可以把它复制为子包并用相对导入。

`plugin.json` 字段（全部可选；未知字段会记录为警告）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | 字符串 | 如果给出，必须与文件夹名相同 |
| `title` | 字符串 | 市场索引中的显示名 |
| `description` | 字符串 | 一行说明，显示在 `snowpea extension list` 中 |
| `version` | 字符串 | 你的版本，显示在 `list` 和 `info` 中（建议 semver） |
| `engines.snowpea` | 字符串 | 编写时针对的扩展 API 主版本：`"^1"`、`"1"`、`"1.x"`、`"~1.0"` 或 `">=1"`（见[版本管理](#版本管理)） |
| `configSchema` | 对象 | 配置的 JSON Schema（见[配置与 configSchema](#配置与-configschema)） |

如果 `plugin.json` 不是合法 JSON、字段类型不对、`name` 与文件夹名不同，或 `configSchema` 不是合法的 JSON Schema，扩展就不会加载，原因会显示在 `snowpea extension list` 中。

## 生命周期

加载扩展的过程是：**发现**（含 `plugin.py` 的文件夹）→ **校验**（`plugin.json`、`engines.snowpea`、按 `configSchema` 校验配置）→ **导入** `plugin.py` → **应用**（`apply(ctx)` 注册一切）→ **运行**（守护进程调用已注册的内容）→ **释放**（按从新到旧的顺序撤销所有注册）。

- **守护进程启动时**加载所有已启用的扩展，在内置功能和技能之后，因此请求一个内置功能或技能已占用的名字会得到明确的错误，而不是被悄悄替换。
- `apply(ctx)` 可以是普通函数，也可以是 `async def`；异步的最多等待 30 秒。
- 任何一步失败，扩展都会处于 `error`（或 `incompatible`）状态，原因显示在 `snowpea extension list`，回溯写入守护进程日志，**它注册的任何东西都不会保留**：`apply` 抛出异常前注册的内容也会被撤销。
- **重新加载**（`snowpea extension reload <名称>`）会释放一切，重新读取文件夹、`plugin.json` 和设置，从磁盘上的源码重新导入扩展的每个模块（从不使用缓存的 `.pyc`），然后再次应用。已禁用的扩展保持禁用。
- **禁用**和**启用**会释放和应用；选择保存在 `settings.extensions.<名称>.enabled`。
- **删除**文件夹：下一次 `snowpea extension list`（或任何 `extension.*` 调用）会卸载它。
- 对 `settings.extensions` 的**编辑**——手动、通过 `settings_set`、通过其他客户端——在守护进程读取到变化的文件时就会生效（下一个回合之前，或任何 `extension.*` 调用时）：`enabled` 变化则加载或卸载，`config` 变化则重新加载。
- **守护进程停止时**会释放所有扩展，因此 `on_dispose` 函数有机会关闭它们打开的东西。

正在运行的会话看到的情况：工具、提示词段落、运行时上下文提供者、钩子和命令都是实时查找的。变化在每个会话的**下一次模型调用**时生效，即使在回合中间也一样；已经在运行的工具调用会用它开始时的代码完成。模型调用刚被移除的工具会得到 “unknown tool” 错误并自行调整。

## API

一切都从 `snowpea_core.extensions` 导入。这些名字是稳定的 API；它们实际所在的模块可能在不同版本间移动。

| 名称 | 是什么 |
|---|---|
| `PluginContext` | `apply(ctx)` 收到的对象 |
| `Tool`、`ToolResult`、`ToolContext`、`PermissionTag` | 工具 |
| `PromptSection`、`PromptInputs`、`SECTION_ORDERS` | 系统提示词段落 |
| `ContextProvider`、`CONTEXT_ORDERS` | 运行时上下文提供者 |
| `PostDecision` | `post_execute` 钩子的返回值 |
| `StopDecision`、`StopContext`、`STOP_ORDERS` | `turn_stopping` 监听器 |
| `Command`、`CommandContext` | 斜杠命令 |
| `ExtensionError` | 无法完成的注册所抛出的异常 |
| `API_VERSION`、`TOOL_PERMISSIONS`、`DISPOSE_TIMEOUT` | 常量 |

每个注册方法都返回一个 **disposer**：提前移除这一项注册的函数。很少需要它，扩展卸载时一切都会被移除。注册无法完成时——对象格式错误，或名字已被内置功能、技能或其他扩展占用——每个注册都会抛出 `ExtensionError`，这会让 `apply` 失败，整个扩展保持未加载。

### `ctx.tools.register(tool, *, timeout=None) -> Disposer`

向模型提供一个工具。注册到守护进程的工具注册表，因此会以 `source` = `extension:<名称>` 出现在 `snowpea tools list` 和 `tool.list` 中。

- `tool`：`Tool(name, category, description, input_schema, permission, run, parallel_safe=False)`。`name` 匹配 `[A-Za-z][A-Za-z0-9_-]{0,63}` 且未被占用；`input_schema` 是 `"type": "object"` 的 JSON Schema；`permission` 是 `read`、`write`、`exec`、`network`、`send` 之一；`run` 是 `async def run(tool_ctx, args) -> ToolResult`。
- `timeout`：秒；运行更久的调用会以超时错误失败。

<!-- example: examples/extensions/hello/plugin.py -->
```python
    ctx.tools.register(
        Tool(
            name="hello_greet",
            category="hello",
            description="Greet someone by name. Use it when the user asks to be greeted.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
            },
            # read: runs without asking in every mode. Use write, exec, network
            # or send for a tool that changes files, runs programs, goes online
            # or sends messages; the mode then decides whether to ask.
            permission="read",
            run=greet,
        )
    )
```

见[编写工具](#编写工具)。

### `ctx.prompt.section(section) -> Disposer`

向系统提示词添加一段。注册到提示词段落注册表。

- `section`：`PromptSection(name, order, render, requires_tool=None)`。`render(inputs: PromptInputs) -> str` 在每次模型调用时运行，返回文本，或返回 `""` 以省略该段。`order` 决定位置（见[提示词段落](#提示词段落)）。指定 `requires_tool` 时，只在该工具被提供时渲染。

<!-- example: examples/extensions/house-style/plugin.py -->
```python
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

### `ctx.prompt.context(provider) -> Disposer`

添加一个运行时事实。注册到运行时上下文注册表。

- `provider`：`ContextProvider(name, order, render, required=False)`。`render(core, session) -> str` 在每次模型调用前运行；只有文本变化时才发送给模型。`required=True` 时也会发给精简的委派代理。

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

### `ctx.hooks.post_execute(name, listener, *, order=0) -> Disposer`

在模型读取结果之前查看每个完成的工具调用。以 `<扩展>:<name>` 注册到 post-execute 链。

- `listener(tool_ctx, tool_name, args, result)` 返回 `None`、`PostDecision.accept(output=None, *notes)` 或 `PostDecision.block(feedback, *notes)`；可以是 `async`。
- `order`：监听器按 `order`，再按注册顺序运行。

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    ctx.hooks.post_execute("redact", redact, order=-100)
```

### `ctx.hooks.turn_stopping(name, listener, *, order=150) -> Disposer`

在回合即将结束时运行。注册到 turn-stopping 链。

- `listener(core, session, stop: StopContext)` 返回 `None` 或 `StopDecision(text, producer, ...)`；可以是 `async`。按 `order` 第一个决定胜出，它的 `text` 成为下一条指令。

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

### `ctx.commands.register(command) -> Disposer`

添加一个斜杠命令。注册到命令表（`snowpea commands list`、`command.list`、TUI 命令面板——客户端会收到 `commands.changed`）。

- `command`：`Command(name, summary, run, args_schema={})`。`name` 不带开头的 `/`；`run` 是 `async def run(cmd_ctx: CommandContext, args: str)`。

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    ctx.commands.register(
        Command(
            name="scratch",
            summary="keep, list or clear notes that last across sessions",
            run=scratch,
            args_schema={
                "type": "object",
                "properties": {
                    "arguments": {"type": "string", "description": "add <text> | clear"}
                },
            },
        )
    )
```

### `ctx.mcp.register(name, entry) -> Disposer`

在扩展加载期间运行一个 MCP 服务器。注册到 MCP 服务器列表（`snowpea mcp list` 会把该扩展显示为它的插件），服务器启动后，它的工具注册到工具注册表。

- `name`：服务器名；其工具名为 `mcp__<name>__<tool>`。
- `entry`：一个 `.mcp.json` 条目：stdio 用 `command`、`args`、`env`、`cwd`，http/sse 用 `url`、`headers`，另有 `timeoutSec`、`toolTimeoutSec`、`tools.include` / `tools.exclude`。

<!-- example: examples/extensions/glossary-mcp/plugin.py -->
```python
    ctx.mcp.register(
        "glossary",
        {
            "command": sys.executable,  # the daemon's Python
            "args": [str(server)],
            "timeoutSec": 20,
        },
    )
```

### `ctx.on_dispose(fn) -> fn`

扩展卸载时，与其他一切一起，按注册的逆序运行 `fn()`。`fn` 可以是 `async`；最多等待 `DISPOSE_TIMEOUT`（10）秒，失败或卡住的会被记录，其余照常运行。

<!-- example: examples/extensions/ticket-workflow/plugin.py -->
```python
    client = tracker.Tracker(ctx.config["baseUrl"], ctx.config.get("token", ""))
    # Registered first, so it runs last: after everything that uses the client is gone.
    ctx.on_dispose(client.close)
```

### `ctx.config`、`ctx.data_dir`、`ctx.log`、`ctx.name`、`ctx.path`

- `ctx.config`（dict）：`settings.extensions.<名称>.config`，已按 `configSchema` 校验，并用其顶层的 `default` 值补全。这是副本；新配置通过重新加载传入。

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]
```

- `ctx.data_dir`（Path）：`$SNOWPEA_HOME/extension-data/<名称>/`，首次使用时创建。把文件放在这里而不是扩展文件夹中，更新扩展时数据就会保留。

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    store: Path = ctx.data_dir / "notes.json"
```

- `ctx.log`（logging.Logger）：名为 `snowpea.extensions.<名称>`，写入守护进程日志。
- `ctx.name`（str）和 `ctx.path`（Path）：扩展的名称和文件夹。
- `ctx.core`：守护进程核心。它**不属于**稳定 API；只有在上面的内容都不够用时才读取，并且要预期它会变化。

## 编写工具

<!-- example: examples/extensions/file-stats/plugin.py -->
```python
"""file-stats: a tool in depth.

Shows an argument schema with required fields, a permission tag, errors the
model can act on, the session's backend for file access (so the tool works in
SSH and Docker sessions too), progress for a long call, work moved off the
event loop, a per-call timeout, and facts for surfaces in ``meta``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import posixpath
from typing import Any

from snowpea_core.extensions import PluginContext, Tool, ToolContext, ToolResult

MAX_FILES = 500

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "A file or folder, relative to the project root.",
        },
        "pattern": {
            "type": "string",
            "description": "For a folder: a glob such as '*.py'. Default: every file.",
            "default": "*",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _count(blobs: list[bytes]) -> tuple[int, int]:
    """Lines and bytes; runs in a worker thread."""
    return sum(blob.count(b"\n") for blob in blobs), sum(len(blob) for blob in blobs)


async def file_stats(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    # The session's files: on this machine, in a Docker container or over SSH.
    backend = tool_ctx.backend
    raw = str(args.get("path") or "").strip()
    if not raw:
        return ToolResult(ok=False, error="path is required")
    root = posixpath.normpath(str(backend.cwd).replace("\\", "/"))
    target = posixpath.normpath(str(backend.resolve(raw)).replace("\\", "/"))
    # A read tool still stays inside the project: say so instead of reading.
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        return ToolResult(ok=False, error=f"{raw} is outside the project; give a path inside it")
    if not await backend.exists(target):
        return ToolResult(ok=False, error=f"{raw} does not exist")
    parent, name = posixpath.split(target)
    if f"{name}/" in await backend.list_dir(parent):  # list_dir marks folders with "/"
        pattern = str(args.get("pattern") or "*")
        names = await backend.list_dir(target)
        files = [
            posixpath.join(target, entry)
            for entry in names
            if not entry.endswith("/") and fnmatch.fnmatch(entry, pattern)
        ]
    else:
        files = [target]
    if len(files) > MAX_FILES:
        return ToolResult(ok=False, error=f"{len(files)} files match; narrow the pattern")
    if tool_ctx.progress is not None:  # None when nobody is watching (tests, headless)
        await tool_ctx.progress.emit("stdout", f"reading {len(files)} files\n")
    blobs = [await backend.read_bytes(file) for file in files]
    # Counting is CPU work: keep it off the daemon's event loop.
    lines, size = await asyncio.to_thread(_count, blobs)
    return ToolResult(
        ok=True,
        output=f"{len(files)} files, {lines} lines, {size} bytes",
        meta={"files": len(files), "lines": lines, "bytes": size},  # for surfaces, not the model
    )


def apply(ctx: PluginContext) -> None:
    ctx.tools.register(
        Tool(
            name="file_stats",
            category="files",
            description=(
                "Count lines and bytes of a file, or of the files directly in a folder. "
                "Use it to size up files before reading them."
            ),
            input_schema=SCHEMA,
            permission="read",  # reads only: no approval needed in any mode
            run=file_stats,
            parallel_safe=True,  # two calls in one response may run at once
        ),
        timeout=30,  # a call that takes longer fails with a timeout error
    )
```

**模式（schema）和描述**是模型对工具的全部了解。在描述中写清它做什么、何时使用；为每个参数写说明；标出必需项。模型通常但并不总是遵守模式：你依赖的值要检查。

**`ToolContext`** 提供 `tool_ctx.session`（其 `workdir`、`id`、`mode`）、`tool_ctx.backend`、`tool_ctx.core`、`tool_ctx.call_id` 和 `tool_ctx.progress`。`tool_ctx.backend` 是会话的文件和 shell 所在之处——本机、Docker 容器或 SSH 主机。请通过它访问项目——`cwd`、`resolve(path)`、`exists`、`list_dir`（文件夹以 `/` 结尾）、`read_file`、`read_bytes`、`write_file`、`write_bytes`、`remove_file`、`run(command, cwd=, timeout=)`——而不是 `open()` 或 `subprocess`，否则会话在别处运行时，工具会作用于守护进程所在的机器。

**`ToolResult`** 字段：

| 字段 | 谁读取 | 用途 |
|---|---|---|
| `ok` | 所有人 | `False` 表示调用失败 |
| `output` | 模型 | `ok` 时的结果文本 |
| `error` | 模型 | 出了什么问题，写成模型能据此修正调用的样子 |
| `meta` | 客户端（`tool.result`），不给模型 | 供 UI 使用的结构化事实 |
| `contexts` | 模型，在其下一次调用前 | 附在结果旁的额外说明 |
| `path`、`diff` | 客户端 | 修改的文件和要显示的 unified diff |

返回普通 `str` 等同于 `ToolResult(ok=True, output=...)`。超过 `tools.maxResultChars`（默认 100 000）的输出会被截成开头和结尾，全文保存在模型可以读取的位置。

**权限标签**按模式决定是否询问人：

| 标签 | 含义 | plan | accept | auto |
|---|---|---|---|---|
| `read` | 只读取，不改变任何东西 | 允许 | 允许 | 允许 |
| `write` | 修改项目中的文件 | 拒绝 | 允许 | 允许 |
| `exec` | 运行程序 | 询问 | 询问 | 允许 |
| `network` | 访问网络 | 允许 | 询问 | 允许 |
| `send` | 代表用户发送消息 | 拒绝 | 询问 | 允许 |

选择工具能做的最危险的那件事的标签。对于 `write` 工具，参数 `path`、`paths` 或 `edits[].path` 中的文件会在调用前建立检查点，人可以撤销更改。审批提示会写明 `from extension <名称>`。

**错误。** 对模型能处理的情况返回 `ToolResult(ok=False, error=...)`。异常会被替你捕获：调用以 `<工具> (extension <名称>) failed: <类型>: <消息> (at plugin.py:<行>)` 失败，回溯写入守护进程日志，回合和守护进程继续运行——但请自己写错误信息，回溯对模型毫无意义。

**进度。** `await tool_ctx.progress.emit("stdout", text)` 在调用运行期间向客户端流式发送一行；没有人观看时 `progress` 为 `None`。

**超时与中断。** 除非给 `register` 传 `timeout=` 或自己限制工作（`asyncio.wait_for`），否则没有超时。当人中断回合时，运行你的工具的任务会被取消：在它正在等待的 `await` 处抛出 `asyncio.CancelledError`；在 `finally` 中清理，并让它继续传播。

**并发。** `run` 在所有会话共享的守护进程事件循环上执行。绝不要阻塞它：把文件或 CPU 工作移到 `asyncio.to_thread`，网络使用异步客户端。多个会话可能同时调用你的工具，所以要保护共享状态。同一个模型响应中的调用依次运行，除非工具是 `parallel_safe=True`（只允许 `read` 和 `network` 工具）。

## 提示词段落

<!-- example: examples/extensions/house-style/plugin.py -->
```python
"""house-style: a system-prompt section.

The section sits at order 650, in the stable band (below 1000): between the
persona (600) and the reply-language rule (700). Its text depends only on the
mode, so it does not change from one model call to the next and the provider's
prompt cache stays warm.
"""

from __future__ import annotations

from snowpea_core.extensions import PluginContext, PromptInputs, PromptSection

RULES = """House style for this team:
- Commit messages: imperative mood, at most 72 characters in the subject.
- Never add a dependency without saying why in the same answer."""

PLAN_NOTE = "In plan mode, list the commits you would make, one line each."


def render(inputs: PromptInputs) -> str:
    """Runs for every model call; return "" to leave the section out."""
    if inputs.subagent:
        return ""  # delegated agents get a brief, not the house rules
    if inputs.mode == "plan":
        return f"{RULES}\n{PLAN_NOTE}"
    return RULES


def apply(ctx: PluginContext) -> None:
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

段落按 `order`、再按名称排序。order 还决定**区段**，这关系到成本：提供商会缓存提示词的开头部分，内容变化的段落会使从它的位置往后的缓存失效。

| 区段 | order | 何时变化 | 内置段落（order） |
|---|---|---|---|
| stable | 小于 1000 | 只在模式、模型、角色或回复语言变化时 | identity (−1000), execution (−900), plan-continuity (−800), vendor (0), config-rule (100), search-honesty (200), memory-guidance (300), mode (400), role-preamble (500), role (510), persona (600), reply-language (700) |
| context | 1000–1999 | 每个会话一次，以及压缩之后 | environment (1000), context-files (1100), skills (1200), tools (1300) |
| volatile | 2000 及以上 | 每个回合 | memory (2000), plan-line (2100), context-pressure (2200) |

稳定的文本放在 stable 区段，并让 `render` 每次返回相同文本。会话中会变化的事实请改用运行时上下文提供者。`PromptInputs` 包含 `mode`、`role`、`subagent`、`tools`（及 `tool_names`）、`reply_language`、`root`（项目文件夹）等。抛出异常的段落在该次调用中被跳过，并记录一次。段落名请以扩展名作为前缀。

## 运行时上下文提供者

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
"""git-branch: a runtime-context provider.

Tells the model which git branch the session's project is on. The provider
runs before every model call, so it reads .git/HEAD directly instead of
starting a git process. The model is sent the text again only when it changes:
switch branches mid-session and the next model call says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snowpea_core.extensions import ContextProvider, PluginContext


def current_branch(workdir: Path) -> str:
    """The checked-out branch of the repository holding ``workdir``, or ""."""
    for folder in (workdir, *workdir.parents):
        head = folder / ".git" / "HEAD"
        if head.is_file():
            text = head.read_text(encoding="utf-8").strip()
            prefix = "ref: refs/heads/"
            return text[len(prefix) :] if text.startswith(prefix) else f"(detached at {text[:12]})"
    return ""


def render(core: Any, session: Any) -> str:
    branch = current_branch(Path(session.workdir))
    return f"Current git branch: {branch}" if branch else ""


def apply(ctx: PluginContext) -> None:
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

所有提供者在每次模型调用前渲染成一个有序列表。这个列表作为单个 `<system-reminder>` 发送给模型并替换上一个——在回合开始时，或在回合中间的下一次模型调用前——**只有当列表与上次发送的不同时**才发送，压缩把旧的总结掉之后会再发送一次。所以文本不变时，提供者没有任何开销。让 `render` 保持快速（不要启动进程、不要访问网络：它在每次模型调用时运行）且确定。内置 order：goal (50)、plan (100)、plan-auto (110)、context-pressure (900)。

## 钩子

### `post_execute`

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
"""redact-secrets: a post_execute hook with a validated config.

Every tool result passes through the hook before the model reads it. Text that
looks like a credential is replaced with [redacted]. Extra patterns come from
settings.extensions.redact-secrets.config.patterns, which plugin.json's
configSchema checks before apply() runs.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, PostDecision, ToolContext, ToolResult

BUILT_IN = [
    r"sk-[A-Za-z0-9_-]{20,}",  # OpenAI-style API keys
    r"ghp_[A-Za-z0-9]{36}",  # GitHub personal access tokens
    r"AKIA[0-9A-Z]{16}",  # AWS access key ids
]


def apply(ctx: PluginContext) -> None:
    # The schema guarantees a list of strings; compiling can still fail.
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]

    def redact(
        tool_ctx: ToolContext, name: str, args: dict[str, Any], result: ToolResult
    ) -> PostDecision | None:
        text = result.output if result.ok else (result.error or "")
        cleaned = text
        for pattern in patterns:
            cleaned = pattern.sub(marker, cleaned)
        if cleaned == text:
            return None  # no change: the next listener sees the result as it was
        ctx.log.info("redacted a secret from %s", name)
        if not result.ok:
            # accept() replaces the output only; for a failed call, block with the clean text.
            return PostDecision.block(cleaned)
        return PostDecision.accept(cleaned, "A credential in this result was redacted.")

    # Order -100: before listeners at the default 0, so they never see the secret.
    ctx.hooks.post_execute("redact", redact, order=-100)
```

每个工具调用——内置、MCP、宿主或扩展——在运行之后、输出被截断并展示给模型之前，都会经过这条链。每个监听器返回以下之一：

| 返回值 | 效果 |
|---|---|
| `None` | 不变 |
| `PostDecision.accept(output=None, *notes)` | 替换模型读取的文本（给出 `output` 时），并添加在模型下一次调用前发送的说明 |
| `PostDecision.block(feedback, *notes)` | 调用变为失败，错误为 `feedback`；后面的监听器不再运行 |

监听器按 `order`、再按注册顺序运行；没有内置监听器。抛出异常的监听器会被记录并跳过。

### `turn_stopping`

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
"""finish-the-job: a turn_stopping listener.

When the model is about to end its turn with an answer that still holds a
placeholder ("TODO", "FIXME", "..."), the turn goes on with one more
instruction. At most twice per turn, so a model that cannot finish is never
kept in a loop.

Order 120: after the built-in verify gate (100), which asks for a passing
check first, and before unfinished-work (150) and the Stop hooks (200). The
first listener that returns a decision wins.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, StopContext, StopDecision

PLACEHOLDER = re.compile(r"\b(TODO|FIXME)\b|<placeholder>")
MAX_PER_TURN = 2


def apply(ctx: PluginContext) -> None:
    sent: dict[tuple[str, Any], int] = {}  # (session id, turn id) -> nudges so far

    def check(core: Any, session: Any, stop: StopContext) -> StopDecision | None:
        if stop.at_budget or not PLACEHOLDER.search(stop.last_message):
            return None
        key = (session.id, session.current_turn)
        count = sent.get(key, 0)
        if count >= MAX_PER_TURN:
            return None
        sent[key] = count + 1
        if len(sent) > 1000:  # forget old turns
            sent.pop(next(iter(sent)))
        return StopDecision(
            text=(
                "Your answer still has a TODO, FIXME or placeholder. Finish that part "
                "now, or say plainly why it cannot be done."
            ),
            producer="finish-the-job",
            shown="finish-the-job: the answer still had a placeholder",
            count=count + 1,
            limit=MAX_PER_TURN,
        )

    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

当模型不调用工具直接回答时，回合就要结束。在此之前，监听器按 `order` 运行；第一个返回 `StopDecision` 的胜出，其 `text` 作为下一条指令发送。内置：

| order | 监听器 | 在何时让回合继续 |
|---|---|---|
| 100 | `verify-gate` | 修改代码后没有通过的检查、从未查看过 UI，或还有待办 |
| 150 | `unfinished-work` | 回答列出了现在就能做的工作 |
| 200 | `stop-hooks` | Claude Code 的 `Stop` 钩子要求时 |

`StopContext` 提供 `last_message`、`stop_continuations` 和 `at_budget`（这个回答是对回合预算探询的回应：让它结束）。`StopDecision(text, producer, via="message", shown="", count=None, limit=None)`：`producer` 在客户端显示的事件中写上你的名字；`count`/`limit` 让它们能显示“第 2 次，共 3 次”。**务必限制**让同一个回合继续的次数——模型可能无法满足你，而没有别的东西会停止这个循环。

## 命令

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
"""scratchpad: a slash command that keeps notes across sessions.

    /scratch add <text>   keep a note
    /scratch              list the notes
    /scratch clear        forget them

A command runs without the model: it answers with cmd_ctx.say(). The notes
live in ctx.data_dir, outside the extension's folder, so updating the
extension keeps them.
"""

from __future__ import annotations

import json
from pathlib import Path

from snowpea_core.extensions import Command, CommandContext, PluginContext


def apply(ctx: PluginContext) -> None:
    store: Path = ctx.data_dir / "notes.json"

    def read() -> list[str]:
        try:
            return list(json.loads(store.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    def write(notes: list[str]) -> None:
        store.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")

    async def scratch(cmd_ctx: CommandContext, args: str) -> None:
        verb, _, rest = args.strip().partition(" ")
        notes = read()
        if verb == "add" and rest.strip():
            write([*notes, rest.strip()])
            await cmd_ctx.say(f"Noted ({len(notes) + 1} in the scratchpad).")
        elif verb == "clear":
            write([])
            await cmd_ctx.say("Scratchpad cleared.")
        elif verb:
            await cmd_ctx.say("Usage: /scratch [add <text> | clear]")
        elif notes:
            await cmd_ctx.say("\n".join(f"{n}. {note}" for n, note in enumerate(notes, 1)))
        else:
            await cmd_ctx.say("The scratchpad is empty. Add a note with /scratch add <text>.")

    ctx.commands.register(
        Command(
            name="scratch",
            summary="keep, list or clear notes that last across sessions",
            run=scratch,
            args_schema={
                "type": "object",
                "properties": {
                    "arguments": {"type": "string", "description": "add <text> | clear"}
                },
            },
        )
    )
```

命令是一个不调用模型的独立回合。`run(cmd_ctx, args)` 以一个字符串接收名称之后的全部内容。`cmd_ctx.say(text)` 在对话中回答（文本会保留在对话里，下一个回合模型也会知道）；`cmd_ctx.session` 和 `cmd_ctx.core` 也可用。异常会让命令的回合以向用户显示的错误结束。内置命令的名字不能占用，技能或其他扩展已占用的名字也不行。之后安装的同名技能会接管这个名字，扩展的下一次重新加载会因名字冲突而失败。

## MCP 服务器

<!-- example: examples/extensions/glossary-mcp/plugin.py -->
```python
"""glossary-mcp: an MCP server that lives as long as the extension.

The entry has the shape of one .mcp.json entry. The server starts in the
background when the extension loads; its tools appear as
mcp__glossary__define once it is up, carry the MCP permission (network unless
settings.mcp says otherwise), and go away when the extension is disabled,
reloaded or removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from snowpea_core.extensions import PluginContext


def apply(ctx: PluginContext) -> None:
    server = Path(__file__).with_name("server.py")
    ctx.mcp.register(
        "glossary",
        {
            "command": sys.executable,  # the daemon's Python
            "args": [str(server)],
            "timeoutSec": 20,
        },
    )
```

服务器在后台启动，所以 `apply` 不会等待它；启动失败的服务器会被记录，扩展保持加载。名字不能在其他地方（`.mcp.json`、设置、插件）声明过。`snowpea mcp list` 显示该服务器归扩展所有；卸载时会移除其工具并停止进程。能力已经以 MCP 服务器形式存在时用它；几行 Python 就能做到时写一个工具。

## 配置与 `configSchema`

<!-- example: examples/extensions/redact-secrets/plugin.json -->
```json
{
  "name": "redact-secrets",
  "description": "Replaces credentials in tool output before the model reads it.",
  "version": "1.0.0",
  "engines": {"snowpea": "^1"},
  "configSchema": {
    "type": "object",
    "properties": {
      "patterns": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "default": [],
        "description": "More regular expressions to redact."
      },
      "replacement": {
        "type": "string",
        "default": "[redacted]",
        "description": "What a match is replaced with."
      }
    },
    "additionalProperties": false
  }
}
```

配置位于 `settings.json` 的 `extensions.<名称>.config`，与 `enabled` 并列。在 `apply` 运行之前，配置会按 `configSchema` 校验（`jsonschema` 包支持的任何 JSON Schema 草案；用 `$schema` 选择）。无效的配置会阻止扩展加载，错误会指出失败的路径，例如 `settings.extensions.redact-secrets.config.patterns[0]: '' should be non-empty`。缺失的顶层属性会使用其 `default`。客户端用 `extension.configure` 设置配置（校验、保存，然后重新加载扩展）；`extension.list` 会返回模式，客户端可以据此生成表单。只有在那台机器上的 `settings.json` 足够私密时，才把令牌之类的秘密放进配置。

## 日志与调试

- `ctx.log` 以及你在 `snowpea.extensions` 下记录的一切都写入 `$SNOWPEA_HOME/logs/daemon.log`，守护进程自己关于你的扩展的记录也在那里：加载时注册了什么、为什么失败（含完整回溯）、哪个工具、段落、提供者或钩子抛出了异常。
- `snowpea extension list` 显示每个扩展的状态——`loaded`、`unloaded`、`disabled`、`error`、`incompatible`——失败时还会显示指向你自己代码的 `Type: message (at file:line)`。
- `snowpea extension info <名称>` 显示文件夹、错误、配置、模式，以及它注册的工具、命令和 MCP 服务器。
- `snowpea extension reload <名称>` 在扩展未能加载时以状态码 1 退出，因此脚本中的“修改—重新加载”循环会在出错时停下。

常见错误：

| 症状 | 原因 |
|---|---|
| 工具从未被调用 | 描述没说何时使用，或当前模式拒绝该权限 |
| `run must be an async function` | 写成了 `def run` 而不是 `async def run` |
| 工具运行时守护进程卡住 | 在事件循环上做了阻塞工作；使用 `asyncio.to_thread` |
| 自己的模块报 `ModuleNotFoundError` | 写成了 `import helpers` 而不是 `from . import helpers` |
| 某个包报 `ModuleNotFoundError` | 未安装在守护进程的 Python 中 |
| 重新加载后行为没变 | 代码把状态放在扩展之外（核心模块、全局注册表）；把状态放在闭包或自己的模块里 |
| 提示词缓存不再起作用 | stable 区段中的段落文本在变化；把该事实移到运行时上下文提供者 |

## 测试扩展

`snowpea_core.extensions.testing.load_extension` 在临时文件夹中启动一个私有守护进程，把你的扩展复制进去，用与真实守护进程相同的管理器加载它，并打开一个会话。离开代码块时它会卸载扩展，如果还有东西保持注册，就让测试失败。不会调用模型，也不会访问网络。`hello` 示例自带的测试：

<!-- example: examples/extensions/hello/tests/test_hello.py -->
```python
"""Tests for the hello extension; run with `python -m pytest tests` in its folder.

Needs pytest and pytest-asyncio next to snowpea in the same Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.extensions.testing import load_extension

EXTENSION = Path(__file__).resolve().parents[1]  # the folder holding plugin.py


@pytest.mark.asyncio
async def test_greets_by_name(tmp_path: Path) -> None:
    # A private daemon in tmp_path; the extension is loaded as the real one would be.
    async with load_extension(EXTENSION, home=tmp_path) as ext:
        assert ext.registrations == ["tool:hello_greet"]
        result = await ext.call_tool("hello_greet", {"name": "Ada"})
        assert result.ok and result.output == "Hello, Ada!"
        too_long = await ext.call_tool("hello_greet", {"name": "x" * 101})
        assert not too_long.ok and "longer than 100" in too_long.error
    # Leaving the block unloads it and fails the test if anything stayed registered.
```

`load_extension(folder, *, config=None, home=None, check=True)` 产出的对象包含：

| 成员 | 作用 |
|---|---|
| `call_tool(name, args)` | 像代理循环那样调用工具，再经过 `post_execute` 钩子；返回 `ToolResult` |
| `system_prompt()` | 会话下一次模型调用将得到的系统提示词 |
| `runtime_context()` | 当前的 `{提供者名: 文本}` |
| `stop_turn(last_message)` | `turn_stopping` 链的决定 |
| `run_command(name, args)` | 运行斜杠命令；返回它说的话 |
| `reload(config=None)` | 重新加载，可附带新配置 |
| `registrations`、`extension`、`session`、`core` | 它注册的内容、状态与错误、会话、核心 |

`check=True` 时，加载失败会抛出带有扩展错误的 `ExtensionLoadError`，因此测试错误配置可以写成 `pytest.raises(ExtensionLoadError, match=...)`。

## 版本管理

`snowpea_core.extensions.API_VERSION` 是此 API 的主版本（目前为 `1`）。只有当扩展依赖的东西被移除或含义改变时才会提升：`ctx` 的成员或参数、`snowpea_core.extensions` 导出名称的字段或方法、`run`、`render` 或监听器的调用签名，或本页承诺的顺序。添加成员、可选参数、字段和新的内置段落不会改变它。`ctx.core` 以及 `snowpea_core.extensions` 以外的所有模块不在此承诺之内。

在 `plugin.json` 中写上你针对的主版本：`"engines": {"snowpea": "^1"}`。API 主版本不同的守护进程会把扩展列为 `incompatible`（原因中给出两个版本）并且不加载。扩展自身的版本写在 `version`。

## 分发

目前还没有扩展市场。要分享扩展，发布它的文件夹——根目录含 `plugin.py` 的 git 仓库，或文件夹的 zip——让用户把它放到 `$SNOWPEA_HOME/extensions/<名称>/`（例如 `git clone <仓库> ~/.snowpea/extensions/<名称>`）并运行 `snowpea extension reload <名称>`。更新就是 `git pull` 加重新加载；`ctx.data_dir` 中的数据会保留。由于扩展以守护进程的权限运行，请告诉用户它做什么、把什么发送到哪里，以及每个工具声明了什么权限。仓库中的 `scripts/gen_extension_index.py` 会写出列出扩展文件夹的静态 `index.json`，供想托管列表的人使用。

## CLI 与 RPC 参考

```bash
snowpea extension list
snowpea extension info hello --json
snowpea extension enable hello
snowpea extension disable hello
snowpea extension reload hello
snowpea extension reload
snowpea extension new hello
```

| 命令 | 作用 | 退出状态 |
|---|---|---|
| `snowpea extension list [--json]` | 所有扩展及其状态和错误 | 0 |
| `snowpea extension info <名称> [--json]` | 单个扩展，及其工具、命令、MCP 服务器、配置和模式 | 处于 `error` 或 `incompatible` 时为 1 |
| `snowpea extension enable <名称>` | 启用并加载 | 未能加载时为 1 |
| `snowpea extension disable <名称>` | 卸载并保持关闭 | 0 |
| `snowpea extension reload [<名称>]` | 重新加载一个，或所有已启用的 | 有任何一个未能加载时为 1 |
| `snowpea extension new <名称>` | 写出起始模板；不需要守护进程 | 文件夹已存在时为 2 |

除 `new` 外，其余都通过以下方法与守护进程通信（capability `extensions`）：

| 方法 | 参数 | 结果 |
|---|---|---|
| `extension.list` | `{}` | `{extensions: ExtensionInfo[]}`；会重新扫描文件夹 |
| `extension.enable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.disable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.reload` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.configure` | `{name, config}` | `{extension: ExtensionInfo}`；配置不符合 `configSchema` 时返回指出失败路径的 `invalid_params` 错误 |

`ExtensionInfo`：`name`、`path`、`description`、`version`、`enabled`、`state`（`loaded`、`unloaded`、`disabled`、`error`、`incompatible`）、`error`（或 null）、`effects`（持有的注册数）、`configSchema`（或 null）、`config`。完整模式见[协议参考](../../protocol.md)。

## 故障排查

| 你看到的 | 该做的 |
|---|---|
| 扩展不在 `snowpea extension list` 中 | 文件夹必须直接位于 `$SNOWPEA_HOME/extensions/` 下并包含 `plugin.py`；检查 `SNOWPEA_HOME` |
| 状态为 `unloaded` | 它是在守护进程启动后添加的：`snowpea extension reload <名称>` |
| 状态为 `error`，`plugin.json:…` | 按消息修正清单文件 |
| 状态为 `error`，`settings.extensions.<名称>.config…` | 修正该路径上的配置，或用 `extension.configure` 设置 |
| 状态为 `incompatible` | 扩展是为另一个 API 主版本编写的；获取匹配的版本 |
| `… is already registered by builtin` | 换一个名字；内置工具和命令不能被替换 |
| `… is already registered by extension:<另一个>` | 两个扩展想要同一个名字；重命名其中一个或禁用另一个 |
| 工具以 `(extension <名称>) failed:` 失败 | 工具中的异常；位置和守护进程日志中的回溯说明了在哪里 |
| 工具以 `timed out after` 失败 | 达到了传给 `register` 的 `timeout` |
| MCP 服务器的工具一直不出现 | 守护进程日志说明了服务器为何没有启动；用 `snowpea mcp test` 试试该条目 |
| 什么都没发生，也没有错误 | 守护进程以 `SNOWPEA_EXTENSIONS=0` 启动，或扩展在 `settings.json` 中被禁用 |

## 下一步

[插件](plugins.md)——技能、Claude Code 与 Codex 插件、钩子和 MCP 服务器。

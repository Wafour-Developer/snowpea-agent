# Writing extensions

An extension is Python code that the snowpea daemon loads into itself. It can give the model new tools, add to the system prompt, keep the model told about facts that change, look at every tool result, keep a turn from ending too early, add slash commands and run MCP servers. This page is everything you need to write one, without reading snowpea's source.

Other languages: [한국어](../ko/extensions.md) · [日本語](../ja/extensions.md) · [简体中文](../zh-CN/extensions.md) · [Español](../es/extensions.md)

Every file shown here is a complete, working extension under [`examples/extensions/`](../../../examples/extensions/README.md) in the repository, and the test suite loads each of them.

## What an extension is, and when to write one

snowpea has three kinds of add-on (see [Plugins](plugins.md#what-is-what)):

| You want | Write |
|---|---|
| to teach the model a procedure, a checklist, a house style it reads when relevant | a **skill** (`SKILL.md`) — no code |
| to bundle skills, agents, commands, hooks and MCP servers for others, in the Claude Code or Codex format | a **plugin** |
| to change how the agent itself works: a tool written in Python, a prompt section, a fact re-sent when it changes, a check on every tool result, a reason to keep a turn going | an **extension** |

Choose the lightest one that does the job. A skill costs nothing to trust; an extension runs code inside the daemon.

## The trust model

- An extension runs **in the daemon's process, with the daemon's permissions**: it can read and write every file you can, open network connections, and see every session. Install only code you trust, as you would a shell script.
- Only `$SNOWPEA_HOME/extensions/` (default `~/.snowpea/extensions/`) is scanned. A project folder is **never** scanned, so opening a cloned repository never runs its code.
- What the model does with an extension's tool still goes through the mode and approval rules: a tool says what kind of action it is (`read`, `write`, `exec`, `network`, `send`) and the mode decides whether the person is asked. The approval prompt names the extension the tool comes from.
- `SNOWPEA_EXTENSIONS=0` in the daemon's environment starts it with no extensions at all.

## Quick start

Create an extension from the starter template:

```bash
snowpea extension new hello
```

That writes `~/.snowpea/extensions/hello/` with two files. `plugin.py`:

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

and `plugin.json`:

<!-- example: examples/extensions/hello/plugin.json -->
```json
{
  "name": "hello",
  "description": "Greets people: the starting point made by snowpea extension new.",
  "version": "0.1.0",
  "engines": {"snowpea": "^1"}
}
```

Load it into the running daemon (it is started if it is not running) and look for the tool:

```bash
snowpea extension reload hello
snowpea tools list
```

`hello_greet` is listed with `[extension:hello]` after it. Ask the agent to greet someone and it calls the tool. Now change the greeting in `plugin.py`, run `snowpea extension reload hello` again, and the next model call uses the new code — no daemon restart. If you make a mistake, `reload` exits with status 1 and prints the error with the file and line:

```bash
snowpea extension list
snowpea extension info hello
```

## Folder layout and `plugin.json`

An extension is one folder in `$SNOWPEA_HOME/extensions/`. The folder name is the extension's name: letters, digits, `-` and `_`, up to 64 characters.

| Path | Required | What it is |
|---|---|---|
| `plugin.py` | yes | defines `apply(ctx)`; a folder without it is ignored |
| `plugin.json` | no | the manifest below |
| `*.py`, sub-packages | no | your own modules, imported relatively: `from . import helpers`, `from .lib import client` (a sub-folder needs an `__init__.py`) |
| `tests/` | no | your tests; ignored by the daemon |
| anything else | no | data files; find them with `Path(__file__).parent` |

Absolute imports of your own modules (`import helpers`) do not work, on purpose: every extension is imported as a package of its own, so two extensions that both have a `helpers.py` never see each other's. Third-party packages must be installed in the daemon's Python (the one `snowpea` runs with); for a small pure-Python dependency, vendor it into a sub-package and import it relatively.

`plugin.json` fields (all optional; an unknown field is logged as a warning):

| Field | Type | Meaning |
|---|---|---|
| `name` | string | must equal the folder name when given |
| `title` | string | a display name for market indexes |
| `description` | string | one line, shown by `snowpea extension list` |
| `version` | string | your version, shown by `list` and `info` (semver recommended) |
| `engines.snowpea` | string | the extension API major it was written for: `"^1"`, `"1"`, `"1.x"`, `"~1.0"`, or `">=1"` (see [Versioning](#versioning)) |
| `configSchema` | object | a JSON Schema for its configuration (see [Configuration](#configuration-and-configschema)) |

A `plugin.json` that is not valid JSON, has a field of the wrong type, a `name` that differs from the folder, or a `configSchema` that is not a valid JSON Schema keeps the extension from loading, with the reason in `snowpea extension list`.

## Lifecycle

Loading an extension is: **discover** (a folder with `plugin.py`) → **validate** (`plugin.json`, `engines.snowpea`, the config against `configSchema`) → **import** `plugin.py` → **apply** (`apply(ctx)` registers everything) → **run** (the daemon calls what was registered) → **dispose** (everything registered is undone, newest first).

- **At daemon start** every enabled extension is loaded, after the built-ins and the skills, so a name a built-in or a skill already holds is refused with a clear error rather than silently replaced.
- `apply(ctx)` may be a plain function or `async def`; an async one is awaited for at most 30 seconds.
- If any step fails, the extension is in state `error` (or `incompatible`), the reason is in `snowpea extension list`, the traceback is in the daemon log, and **nothing it registered stays registered**: whatever `apply` registered before it raised is undone.
- **Reload** (`snowpea extension reload <name>`) disposes everything, re-reads the folder, `plugin.json` and the settings, imports every module of the extension again from the source on disk (never a cached `.pyc`), and applies again. A disabled extension stays disabled.
- **Disable** and **enable** dispose and apply; the choice is saved in `settings.extensions.<name>.enabled`.
- **Remove** the folder: the next `snowpea extension list` (or any `extension.*` call) unloads it.
- **Settings edits** to `settings.extensions` — by hand, by `settings_set`, by another client — are followed as soon as the daemon reads the changed file (before the next turn, or on any `extension.*` call): a changed `enabled` loads or unloads, a changed `config` reloads.
- **Daemon stop** disposes every extension, so `on_dispose` functions get to close what they opened.

What running sessions see: tools, prompt sections, runtime-context providers, hooks and commands are looked up live. A change takes effect **at the next model call** of every session, including one in the middle of a turn; a tool call already running finishes with the code it started with. A model that calls a tool which has just been removed gets an "unknown tool" error and adapts.

## The API

Import everything from `snowpea_core.extensions`. Those names are the stable API; the modules they come from may move between releases.

| Name | What it is |
|---|---|
| `PluginContext` | what `apply(ctx)` receives |
| `Tool`, `ToolResult`, `ToolContext`, `PermissionTag` | tools |
| `PromptSection`, `PromptInputs`, `SECTION_ORDERS` | system-prompt sections |
| `ContextProvider`, `CONTEXT_ORDERS` | runtime-context providers |
| `PostDecision` | what a `post_execute` hook returns |
| `StopDecision`, `StopContext`, `STOP_ORDERS` | `turn_stopping` listeners |
| `Command`, `CommandContext` | slash commands |
| `ExtensionError` | raised by a registration that cannot be made |
| `API_VERSION`, `TOOL_PERMISSIONS`, `DISPOSE_TIMEOUT` | constants |

Every registration method returns a **disposer**: a function that removes that one registration early. You rarely need it; everything is removed when the extension goes. Every registration raises `ExtensionError` when it cannot be made — a malformed object, or a name already in use by a built-in, a skill or another extension — which makes `apply` fail and the whole extension stay unloaded.

### `ctx.tools.register(tool, *, timeout=None) -> Disposer`

Offers a tool to the model. Registers into the daemon's tool registry, so it shows in `snowpea tools list` and `tool.list` with `source` = `extension:<name>`.

- `tool`: a `Tool(name, category, description, input_schema, permission, run, parallel_safe=False)`. `name` matches `[A-Za-z][A-Za-z0-9_-]{0,63}` and is not taken; `input_schema` is a JSON Schema with `"type": "object"`; `permission` is one of `read`, `write`, `exec`, `network`, `send`; `run` is `async def run(tool_ctx, args) -> ToolResult`.
- `timeout`: seconds; a call that runs longer fails with a timeout error.

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

See [Writing a tool](#writing-a-tool).

### `ctx.prompt.section(section) -> Disposer`

Adds a piece of the system prompt. Registers into the prompt-section registry.

- `section`: `PromptSection(name, order, render, requires_tool=None)`. `render(inputs: PromptInputs) -> str` runs for every model call and returns the text, or `""` to leave the section out. `order` places it (see [Prompt sections](#prompt-sections)). With `requires_tool`, it renders only while that tool is offered.

<!-- example: examples/extensions/house-style/plugin.py -->
```python
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

### `ctx.prompt.context(provider) -> Disposer`

Adds a runtime fact. Registers into the runtime-context registry.

- `provider`: `ContextProvider(name, order, render, required=False)`. `render(core, session) -> str` runs before every model call; the text is sent to the model only when it changed. `required=True` keeps it for lean delegated agents too.

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

### `ctx.hooks.post_execute(name, listener, *, order=0) -> Disposer`

Sees every finished tool call before the model reads the result. Registers into the post-execute chain under `<extension>:<name>`.

- `listener(tool_ctx, tool_name, args, result)` returns `None`, `PostDecision.accept(output=None, *notes)` or `PostDecision.block(feedback, *notes)`; it may be `async`.
- `order`: listeners run by `order`, then by registration.

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    ctx.hooks.post_execute("redact", redact, order=-100)
```

### `ctx.hooks.turn_stopping(name, listener, *, order=150) -> Disposer`

Runs when a turn is about to end. Registers into the turn-stopping chain.

- `listener(core, session, stop: StopContext)` returns `None` or a `StopDecision(text, producer, ...)`; it may be `async`. The first decision, by `order`, wins and `text` becomes the next instruction.

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

### `ctx.commands.register(command) -> Disposer`

Adds a slash command. Registers into the command table (`snowpea commands list`, `command.list`, the TUI palette — clients are told with `commands.changed`).

- `command`: `Command(name, summary, run, args_schema={})`. `name` has no leading `/`; `run` is `async def run(cmd_ctx: CommandContext, args: str)`.

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

Starts an MCP server for as long as the extension is loaded. Registers into the MCP server list (`snowpea mcp list` shows it with the extension as its plugin) and, once the server is up, its tools into the tool registry.

- `name`: the server's name; its tools are `mcp__<name>__<tool>`.
- `entry`: one `.mcp.json` entry: `command`, `args`, `env`, `cwd` for stdio, or `url`, `headers` for http/sse, plus `timeoutSec`, `toolTimeoutSec`, `tools.include` / `tools.exclude`.

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

Runs `fn()` when the extension is unloaded, in reverse order of registration, together with everything else. `fn` may be `async`; it is awaited for at most `DISPOSE_TIMEOUT` (10) seconds, and one that fails or hangs is logged and the rest still run.

<!-- example: examples/extensions/ticket-workflow/plugin.py -->
```python
    client = tracker.Tracker(ctx.config["baseUrl"], ctx.config.get("token", ""))
    # Registered first, so it runs last: after everything that uses the client is gone.
    ctx.on_dispose(client.close)
```

### `ctx.config`, `ctx.data_dir`, `ctx.log`, `ctx.name`, `ctx.path`

- `ctx.config` (dict): `settings.extensions.<name>.config`, already validated against `configSchema` and completed with its top-level `default` values. A copy; a new config arrives by reloading.

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]
```

- `ctx.data_dir` (Path): `$SNOWPEA_HOME/extension-data/<name>/`, created on first use. Keep your files there, not in the extension's folder, so that updating the extension keeps them.

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    store: Path = ctx.data_dir / "notes.json"
```

- `ctx.log` (logging.Logger): named `snowpea.extensions.<name>`; it writes to the daemon log.
- `ctx.name` (str) and `ctx.path` (Path): the extension's name and folder.
- `ctx.core`: the daemon core. It is **not** part of the stable API; read from it only when nothing above gives you what you need, and expect it to change.

## Writing a tool

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

**The schema and the description** are all the model knows about the tool. Say in the description what it does and when to use it; describe every argument; mark what is required. The model usually, but not always, follows the schema: check what you rely on.

**`ToolContext`** gives `tool_ctx.session` (its `workdir`, `id`, `mode`), `tool_ctx.backend`, `tool_ctx.core`, `tool_ctx.call_id` and `tool_ctx.progress`. `tool_ctx.backend` is where the session's files and shell are: this machine, a Docker container or an SSH host. Reach the project through it — `cwd`, `resolve(path)`, `exists`, `list_dir` (folders end in `/`), `read_file`, `read_bytes`, `write_file`, `write_bytes`, `remove_file`, `run(command, cwd=, timeout=)` — rather than `open()` or `subprocess`, or the tool acts on the daemon's machine when the session runs elsewhere.

**`ToolResult`** fields:

| Field | Who reads it | Use |
|---|---|---|
| `ok` | everyone | `False` makes the call a failure |
| `output` | the model | the result text when `ok` |
| `error` | the model | what went wrong, written so the model can fix its call |
| `meta` | clients (`tool.result`), not the model | structured facts for a UI |
| `contexts` | the model, before its next call | extra notes beside the result |
| `path`, `diff` | clients | the file it changed and a unified diff to show |

Returning a plain `str` counts as `ToolResult(ok=True, output=...)`. Output longer than `tools.maxResultChars` (100 000 by default) is cut to a head and a tail, and the whole text is saved where the model can read it.

**Permission tags** decide whether the person is asked, by mode:

| Tag | Means | plan | accept | auto |
|---|---|---|---|---|
| `read` | reads, changes nothing | allow | allow | allow |
| `write` | changes files in the project | deny | allow | allow |
| `exec` | runs programs | ask | ask | allow |
| `network` | goes online | allow | ask | allow |
| `send` | sends messages for the user | deny | ask | allow |

Pick the tag of the most dangerous thing the tool can do. For a `write` tool, files named in the arguments `path`, `paths` or `edits[].path` are checkpointed before the call, so the person can undo the change. The approval prompt says `from extension <name>`.

**Errors.** Return `ToolResult(ok=False, error=...)` for anything the model can do something about. An exception is caught for you: the call fails with `<tool> (extension <name>) failed: <Type>: <message> (at plugin.py:<line>)`, the traceback goes to the daemon log, and the turn and the daemon carry on — but write your own errors; a traceback means nothing to the model.

**Progress.** `await tool_ctx.progress.emit("stdout", text)` streams a line to the clients while the call runs; `progress` is `None` when nobody is watching.

**Timeouts and interrupts.** There is no timeout unless you give `timeout=` to `register` or bound the work yourself (`asyncio.wait_for`). When the person interrupts the turn, the task running your tool is cancelled: `asyncio.CancelledError` is raised at the `await` it is waiting on; clean up in `finally` and let it propagate.

**Concurrency.** `run` executes on the daemon's event loop, shared by every session. Never block it: move file or CPU work to `asyncio.to_thread`, use async clients for the network. Several sessions may call your tool at the same time, so guard shared state. Calls of one model response run one after another unless the tool is `parallel_safe=True` (only allowed for `read` and `network` tools).

## Prompt sections

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

Sections are sorted by `order`, then by name. The order also decides the **band**, which matters for cost: providers cache the beginning of the prompt, and a section that changes invalidates the cache from its position onwards.

| Band | Orders | Changes | Built-in sections (order) |
|---|---|---|---|
| stable | below 1000 | only with the mode, model, role or reply language | identity (−1000), execution (−900), plan-continuity (−800), vendor (0), config-rule (100), search-honesty (200), memory-guidance (300), mode (400), role-preamble (500), role (510), persona (600), reply-language (700) |
| context | 1000–1999 | once per session and after a compaction | environment (1000), context-files (1100), skills (1200), tools (1300) |
| volatile | 2000 and up | every turn | memory (2000), plan-line (2100), context-pressure (2200) |

Put stable text in the stable band and make its `render` return the same text every time. For a fact that changes during a session, use a runtime-context provider instead. `PromptInputs` carries `mode`, `role`, `subagent`, `tools` (and `tool_names`), `reply_language`, `root` (the project folder) and more. A section that raises is skipped for that call and logged once. Prefix your section names with the extension's name.

## Runtime-context providers

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

All providers render before every model call into one ordered list. That list is sent to the model as a single `<system-reminder>` that replaces the previous one — at the start of a turn, or in the middle of one before the next model call — **only when the list differs from what was last sent**, and again after a compaction summarised the old one away. So a provider costs nothing while its text stays the same. Keep `render` fast (no processes, no network: it runs every model call) and deterministic. Built-in orders: goal (50), plan (100), plan-auto (110), context-pressure (900).

## Hooks

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

Every tool call — built-in, MCP, host or extension — passes through the chain after it ran and before its output is trimmed and shown to the model. Each listener returns one of:

| Return | Effect |
|---|---|
| `None` | no change |
| `PostDecision.accept(output=None, *notes)` | replace the text the model reads (when `output` is given) and add notes, sent before the model's next call |
| `PostDecision.block(feedback, *notes)` | the call becomes a failure whose error is `feedback`; later listeners do not run |

Listeners run by `order`, then by registration; there are no built-in ones. A listener that raises is logged and skipped.

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

When the model answers without calling a tool, the turn would end. First the listeners run in `order`; the first one that returns a `StopDecision` wins, and its `text` is sent as the next instruction. Built in:

| Order | Listener | Keeps the turn going when |
|---|---|---|
| 100 | `verify-gate` | code was edited with no passing check since, a UI was never looked at, or to-dos are open |
| 150 | `unfinished-work` | the answer lists work it could still do now |
| 200 | `stop-hooks` | a Claude Code `Stop` hook asks |

`StopContext` gives `last_message`, `stop_continuations` and `at_budget` (the answer is the reply to the round-budget probe: let it end). `StopDecision(text, producer, via="message", shown="", count=None, limit=None)`: `producer` names you in the event clients show; `count`/`limit` let them show "2 of 3". **Always cap** how often you keep one turn going — the model may be unable to satisfy you, and nothing else stops the loop.

## Commands

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

A command is a turn of its own that does not call the model. `run(cmd_ctx, args)` gets everything after the name as one string. `cmd_ctx.say(text)` answers in the transcript (and the text is kept in the conversation, so the model knows about it on the next turn); `cmd_ctx.session` and `cmd_ctx.core` are there. An exception ends the command's turn with an error shown to the user. A built-in command's name cannot be taken, nor one a skill or another extension holds. A skill with the same name installed later takes the name over, and the extension's next reload then fails with the clash.

## MCP servers

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

The server starts in the background, so `apply` does not wait for it; a server that fails to start is logged and the extension stays loaded. The name must not be declared anywhere else (`.mcp.json`, settings, a plugin). `snowpea mcp list` shows the server with the extension as its owner; on unload its tools are removed and the process is stopped. Use this when the capability already exists as an MCP server; write a tool when it is a few lines of Python.

## Configuration and `configSchema`

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

The configuration lives in `settings.json` under `extensions.<name>.config`, next to `enabled`. Before `apply` runs, the config is validated against `configSchema` (any JSON Schema draft the `jsonschema` package knows; `$schema` picks one). An invalid config keeps the extension from loading, and the error names the failing path, for example `settings.extensions.redact-secrets.config.patterns[0]: '' should be non-empty`. Missing top-level properties get their `default`. Clients set the config with `extension.configure` (validated, saved, then the extension reloads); `extension.list` returns the schema so a client can render a form from it. Keep secrets such as tokens in the config only if `settings.json` is private enough for them on that machine.

## Logging and debugging

- `ctx.log` and anything you log under `snowpea.extensions` go to `$SNOWPEA_HOME/logs/daemon.log`, as do the daemon's own lines about your extension: what it registered when it loaded, why it failed (with the full traceback), a tool, section, provider or hook that raised.
- `snowpea extension list` shows each extension's state — `loaded`, `unloaded`, `disabled`, `error`, `incompatible` — and, for a failure, `Type: message (at file:line)` pointing into your own code.
- `snowpea extension info <name>` shows the folder, error, config, schema, and the tools, commands and MCP servers it registered.
- `snowpea extension reload <name>` exits with status 1 when the extension did not load, so an edit-reload loop in a script stops on a mistake.

Common mistakes:

| Symptom | Cause |
|---|---|
| a tool is never called | the description does not say when to use it, or the permission is denied in the current mode |
| `run must be an async function` | `def run` instead of `async def run` |
| the daemon stalls while a tool runs | blocking work on the event loop; use `asyncio.to_thread` |
| `ModuleNotFoundError` for your own module | `import helpers` instead of `from . import helpers` |
| `ModuleNotFoundError` for a package | not installed in the daemon's Python |
| a reload shows the old behaviour | the code keeps state outside the extension (a module of the core, a global registry); keep state in closures or your own modules |
| the prompt cache stops helping | a stable-band section whose text changes; move the fact to a runtime-context provider |

## Testing an extension

`snowpea_core.extensions.testing.load_extension` starts a private daemon in a temporary folder, copies your extension into it, loads it through the same manager the real daemon uses and opens a session. On leaving the block it unloads the extension and fails the test if anything stayed registered. No model is called and nothing goes to the network. The test that ships with the `hello` example:

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

`load_extension(folder, *, config=None, home=None, check=True)` yields an object with:

| Member | Does |
|---|---|
| `call_tool(name, args)` | calls a tool as the agent loop does, then the `post_execute` hooks; returns the `ToolResult` |
| `system_prompt()` | the system prompt the session's next model call would get |
| `runtime_context()` | `{provider name: text}` right now |
| `stop_turn(last_message)` | what the `turn_stopping` chain decides |
| `run_command(name, args)` | runs a slash command; returns what it said |
| `reload(config=None)` | reloads, optionally with a new config |
| `registrations`, `extension`, `session`, `core` | what it registered, its state and error, the session, the core |

With `check=True`, a load failure raises `ExtensionLoadError` carrying the extension's error, so a test of a bad config is `pytest.raises(ExtensionLoadError, match=...)`.

## Versioning

`snowpea_core.extensions.API_VERSION` is the major version of this API (now `1`). It changes only when something an extension relies on is removed or changes meaning: a `ctx` member or argument, a field or method of a name exported by `snowpea_core.extensions`, the call signature of a `run`, `render` or listener, or an ordering promise on this page. Adding members, optional arguments, fields and new built-in sections does not change it. `ctx.core` and every module other than `snowpea_core.extensions` are outside this promise.

Put the major you wrote for in `plugin.json` as `"engines": {"snowpea": "^1"}`. A daemon with another API major lists the extension as `incompatible`, with both versions in the reason, and does not load it. Version your own extension in `version`.

## Distributing

There is no marketplace for extensions yet. To share one, publish its folder — a git repository whose root holds `plugin.py`, or a zip of the folder — and have people put it in `$SNOWPEA_HOME/extensions/<name>/` (for example `git clone <repository> ~/.snowpea/extensions/<name>`) and run `snowpea extension reload <name>`. Updating is `git pull` plus a reload; data in `ctx.data_dir` is kept. Because an extension runs with the daemon's permissions, tell your users what it does, what it sends where, and which permission each tool declares. `scripts/gen_extension_index.py` in the repository writes a static `index.json` listing extension folders, for anyone who wants to host a list.

## CLI and RPC reference

```bash
snowpea extension list
snowpea extension info hello --json
snowpea extension enable hello
snowpea extension disable hello
snowpea extension reload hello
snowpea extension reload
snowpea extension new hello
```

| Command | Does | Exit status |
|---|---|---|
| `snowpea extension list [--json]` | every extension, its state and error | 0 |
| `snowpea extension info <name> [--json]` | one extension, with its tools, commands, MCP servers, config and schema | 1 if it is in `error` or `incompatible` |
| `snowpea extension enable <name>` | enables and loads | 1 if it did not load |
| `snowpea extension disable <name>` | unloads and keeps it off | 0 |
| `snowpea extension reload [<name>]` | reloads one, or every enabled one | 1 if any did not load |
| `snowpea extension new <name>` | writes the starter template; needs no daemon | 2 if the folder exists |

All but `new` talk to the daemon with these methods (capability `extensions`):

| Method | Params | Result |
|---|---|---|
| `extension.list` | `{}` | `{extensions: ExtensionInfo[]}`; rescans the folder |
| `extension.enable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.disable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.reload` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.configure` | `{name, config}` | `{extension: ExtensionInfo}`; error `invalid_params` naming the failing path when the config does not match `configSchema` |

`ExtensionInfo`: `name`, `path`, `description`, `version`, `enabled`, `state` (`loaded`, `unloaded`, `disabled`, `error`, `incompatible`), `error` (or null), `effects` (how many registrations it holds), `configSchema` (or null), `config`. The full schemas are in the [protocol reference](../../protocol.md).

## Troubleshooting

| What you see | What to do |
|---|---|
| the extension is not in `snowpea extension list` | the folder must be directly in `$SNOWPEA_HOME/extensions/` and contain `plugin.py`; check `SNOWPEA_HOME` |
| state `unloaded` | it was added after the daemon started: `snowpea extension reload <name>` |
| state `error`, `plugin.json:…` | fix the manifest as the message says |
| state `error`, `settings.extensions.<name>.config…` | fix the config at that path, or `extension.configure` it |
| state `incompatible` | the extension was written for another API major; get a matching version |
| `… is already registered by builtin` | pick another name; built-in tools and commands cannot be replaced |
| `… is already registered by extension:<other>` | two extensions want the same name; rename one or disable the other |
| a tool fails with `(extension <name>) failed:` | an exception in your tool; the location and the traceback in the daemon log say where |
| a tool fails with `timed out after` | the `timeout` you gave `register` was reached |
| the MCP server's tools never appear | the daemon log says why the server did not start; try the entry with `snowpea mcp test` |
| nothing happens and no error | the daemon was started with `SNOWPEA_EXTENSIONS=0`, or the extension is disabled in `settings.json` |

## Next

[Plugins](plugins.md) — skills, Claude Code and Codex plugins, hooks and MCP servers.

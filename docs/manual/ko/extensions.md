# 확장 만들기

확장은 snowpea 데몬이 자기 안에 불러들이는 Python 코드입니다. 모델에게 새 도구를 주고, 시스템 프롬프트에 내용을 더하고, 바뀌는 사실을 모델에게 계속 알려 주고, 모든 도구 결과를 살펴보고, 턴이 너무 일찍 끝나지 않게 하고, 슬래시 명령을 더하고, MCP 서버를 띄울 수 있습니다. 이 문서만 읽으면 snowpea 소스를 보지 않고도 확장을 만들 수 있습니다.

다른 언어: [English](../en/extensions.md) · [日本語](../ja/extensions.md) · [简体中文](../zh-CN/extensions.md) · [Español](../es/extensions.md)

이 문서에 나오는 파일은 모두 저장소의 [`examples/extensions/`](../../../examples/extensions/README.md)에 있는, 그대로 동작하는 완전한 확장이며 테스트가 하나하나 실제로 불러 봅니다.

## 확장이란, 그리고 언제 만드는가

snowpea의 부가 기능은 세 가지입니다([플러그인](plugins.md#what-is-what) 참고).

| 하고 싶은 것 | 만들 것 |
|---|---|
| 필요할 때 모델이 읽는 절차, 체크리스트, 팀 스타일을 가르치기 | **스킬**(`SKILL.md`) — 코드 없음 |
| 스킬·에이전트·명령·훅·MCP 서버를 Claude Code나 Codex 형식으로 묶어 남에게 나눠 주기 | **플러그인** |
| 에이전트가 일하는 방식 자체를 바꾸기: Python으로 쓴 도구, 프롬프트 섹션, 바뀔 때 다시 보내는 사실, 모든 도구 결과에 대한 검사, 턴을 이어 갈 이유 | **확장** |

일을 해낼 수 있는 가장 가벼운 것을 고르세요. 스킬은 믿어 줄 것이 없지만, 확장은 데몬 안에서 코드를 실행합니다.

## 신뢰 모델

- 확장은 **데몬 프로세스 안에서 데몬의 권한으로** 실행됩니다. 사용자가 읽고 쓸 수 있는 모든 파일을 읽고 쓸 수 있고, 네트워크에 연결할 수 있으며, 모든 세션을 볼 수 있습니다. 셸 스크립트를 대하듯, 믿을 수 있는 코드만 설치하세요.
- 스캔하는 곳은 `$SNOWPEA_HOME/extensions/`(기본값 `~/.snowpea/extensions/`)뿐입니다. 프로젝트 폴더는 **절대** 스캔하지 않으므로, 클론한 저장소를 연다고 그 안의 코드가 실행되지는 않습니다.
- 모델이 확장의 도구로 하는 일은 여전히 모드와 승인 규칙을 거칩니다. 도구는 자신이 어떤 종류의 동작인지(`read`, `write`, `exec`, `network`, `send`) 밝히고, 사람에게 물을지는 모드가 정합니다. 승인 요청에는 그 도구를 등록한 확장의 이름이 표시됩니다.
- 데몬 환경에 `SNOWPEA_EXTENSIONS=0`을 주면 확장 없이 시작합니다.

## 빠른 시작

시작용 템플릿으로 확장을 만듭니다.

```bash
snowpea extension new hello
```

`~/.snowpea/extensions/hello/`에 파일 두 개가 생깁니다. `plugin.py`:

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

그리고 `plugin.json`:

<!-- example: examples/extensions/hello/plugin.json -->
```json
{
  "name": "hello",
  "description": "Greets people: the starting point made by snowpea extension new.",
  "version": "0.1.0",
  "engines": {"snowpea": "^1"}
}
```

실행 중인 데몬에 불러들이고(데몬이 없으면 띄웁니다) 도구를 확인합니다.

```bash
snowpea extension reload hello
snowpea tools list
```

`hello_greet`가 뒤에 `[extension:hello]`를 달고 나옵니다. 에이전트에게 누군가에게 인사해 달라고 하면 이 도구를 부릅니다. 이제 `plugin.py`에서 인사말을 고치고 `snowpea extension reload hello`를 다시 실행하면, 데몬을 재시작하지 않아도 다음 모델 호출부터 새 코드가 쓰입니다. 실수가 있으면 `reload`는 종료 코드 1로 끝나며 파일과 줄 번호가 붙은 오류를 보여 줍니다.

```bash
snowpea extension list
snowpea extension info hello
```

## 폴더 구성과 `plugin.json`

확장 하나는 `$SNOWPEA_HOME/extensions/` 아래의 폴더 하나입니다. 폴더 이름이 곧 확장의 이름이며, 영문자·숫자·`-`·`_`로 64자까지 쓸 수 있습니다.

| 경로 | 필수 | 내용 |
|---|---|---|
| `plugin.py` | 예 | `apply(ctx)`를 정의합니다. 이 파일이 없는 폴더는 무시합니다 |
| `plugin.json` | 아니요 | 아래의 매니페스트 |
| `*.py`, 하위 패키지 | 아니요 | 직접 만든 모듈. 상대 경로로 가져옵니다: `from . import helpers`, `from .lib import client` (하위 폴더에는 `__init__.py`가 필요합니다) |
| `tests/` | 아니요 | 테스트. 데몬은 무시합니다 |
| 그 밖의 것 | 아니요 | 데이터 파일. `Path(__file__).parent`로 찾습니다 |

자기 모듈을 절대 경로로 가져오는 것(`import helpers`)은 일부러 막혀 있습니다. 확장마다 독립된 패키지로 가져오기 때문에, 두 확장에 모두 `helpers.py`가 있어도 서로의 것을 보지 않습니다. 서드파티 패키지는 데몬의 Python(`snowpea`를 실행하는 Python)에 설치되어 있어야 합니다. 작은 순수 Python 의존성이라면 하위 패키지로 복사해 넣고 상대 경로로 가져오세요.

`plugin.json` 필드(모두 선택. 모르는 필드는 경고로 기록됩니다):

| 필드 | 타입 | 의미 |
|---|---|---|
| `name` | 문자열 | 쓴다면 폴더 이름과 같아야 합니다 |
| `title` | 문자열 | 마켓 인덱스에 보일 표시 이름 |
| `description` | 문자열 | 한 줄 설명. `snowpea extension list`에 나옵니다 |
| `version` | 문자열 | 확장의 버전. `list`와 `info`에 나옵니다(semver 권장) |
| `engines.snowpea` | 문자열 | 이 확장이 기준으로 삼은 확장 API 메이저 버전: `"^1"`, `"1"`, `"1.x"`, `"~1.0"`, `">=1"` ([버전 관리](#버전-관리) 참고) |
| `configSchema` | 객체 | 설정을 검사할 JSON Schema ([설정과 configSchema](#설정과-configschema) 참고) |

JSON 문법이 틀렸거나, 필드 타입이 맞지 않거나, `name`이 폴더 이름과 다르거나, `configSchema`가 올바른 JSON Schema가 아닌 `plugin.json`은 확장을 불러오지 못하게 하며, 그 이유가 `snowpea extension list`에 나옵니다.

## 생애 주기

확장을 불러오는 과정은 **발견**(`plugin.py`가 있는 폴더) → **검증**(`plugin.json`, `engines.snowpea`, `configSchema`에 대한 설정) → `plugin.py` **가져오기** → **적용**(`apply(ctx)`가 모든 것을 등록) → **실행**(등록한 것을 데몬이 호출) → **정리**(등록한 것을 최근 것부터 모두 되돌림)입니다.

- **데몬이 시작할 때** 켜져 있는 확장을 모두 불러옵니다. 내장 기능과 스킬 다음에 불러오므로, 내장 기능이나 스킬이 이미 쓰는 이름을 요청하면 조용히 바꿔치기하지 않고 분명한 오류로 거절합니다.
- `apply(ctx)`는 일반 함수여도 되고 `async def`여도 됩니다. 비동기 함수는 최대 30초까지 기다립니다.
- 어느 단계든 실패하면 확장은 `error`(또는 `incompatible`) 상태가 되고, 이유는 `snowpea extension list`에, 트레이스백은 데몬 로그에 남으며, **등록한 것은 하나도 남지 않습니다**. `apply`가 예외를 내기 전에 등록한 것도 되돌립니다.
- **다시 불러오기**(`snowpea extension reload <이름>`)는 모든 것을 정리하고, 폴더와 `plugin.json`과 설정을 다시 읽고, 확장의 모든 모듈을 디스크의 소스에서 다시 가져온 뒤(캐시된 `.pyc`는 쓰지 않습니다) 다시 적용합니다. 꺼진 확장은 꺼진 채로 둡니다.
- **끄기**와 **켜기**는 정리하고 적용하며, 선택은 `settings.extensions.<이름>.enabled`에 저장됩니다.
- 폴더를 **지우면** 다음 `snowpea extension list`(또는 어떤 `extension.*` 호출이든)에서 언로드합니다.
- `settings.extensions`를 손으로, `settings_set`으로, 다른 클라이언트로 **고치면**, 데몬이 바뀐 파일을 읽는 즉시(다음 턴 전, 또는 `extension.*` 호출 때) 따라갑니다. `enabled`가 바뀌면 불러오거나 내리고, `config`가 바뀌면 다시 불러옵니다.
- **데몬이 멈출 때** 모든 확장을 정리하므로, `on_dispose` 함수가 연 것을 닫을 기회를 얻습니다.

실행 중인 세션이 보는 것: 도구, 프롬프트 섹션, 런타임 컨텍스트 제공자, 훅, 명령은 매번 그 자리에서 찾습니다. 변경은 모든 세션의 **다음 모델 호출부터** 적용되며, 턴 한가운데여도 마찬가지입니다. 이미 실행 중인 도구 호출은 시작할 때의 코드로 끝납니다. 방금 사라진 도구를 모델이 부르면 "unknown tool" 오류를 받고 알아서 바꿉니다.

## API

모든 것은 `snowpea_core.extensions`에서 가져옵니다. 이 이름들이 안정된 API이며, 실제로 정의된 모듈은 릴리스마다 옮겨질 수 있습니다.

| 이름 | 무엇인가 |
|---|---|
| `PluginContext` | `apply(ctx)`가 받는 것 |
| `Tool`, `ToolResult`, `ToolContext`, `PermissionTag` | 도구 |
| `PromptSection`, `PromptInputs`, `SECTION_ORDERS` | 시스템 프롬프트 섹션 |
| `ContextProvider`, `CONTEXT_ORDERS` | 런타임 컨텍스트 제공자 |
| `PostDecision` | `post_execute` 훅이 돌려주는 것 |
| `StopDecision`, `StopContext`, `STOP_ORDERS` | `turn_stopping` 리스너 |
| `Command`, `CommandContext` | 슬래시 명령 |
| `ExtensionError` | 할 수 없는 등록이 일으키는 예외 |
| `API_VERSION`, `TOOL_PERMISSIONS`, `DISPOSE_TIMEOUT` | 상수 |

모든 등록 메서드는 **disposer**, 곧 그 등록 하나를 미리 없애는 함수를 돌려줍니다. 확장이 내려갈 때 어차피 모두 없어지므로 쓸 일은 드뭅니다. 등록할 수 없을 때 — 객체 형식이 잘못됐거나, 내장 기능·스킬·다른 확장이 이미 쓰는 이름일 때 — 모든 등록은 `ExtensionError`를 내며, 그러면 `apply`가 실패하고 확장 전체가 불러와지지 않은 채로 남습니다.

### `ctx.tools.register(tool, *, timeout=None) -> Disposer`

모델에게 도구를 줍니다. 데몬의 도구 레지스트리에 등록되므로 `snowpea tools list`와 `tool.list`에 `source` = `extension:<이름>`으로 보입니다.

- `tool`: `Tool(name, category, description, input_schema, permission, run, parallel_safe=False)`. `name`은 `[A-Za-z][A-Za-z0-9_-]{0,63}` 형식이고 아직 쓰이지 않은 이름이어야 합니다. `input_schema`는 `"type": "object"`인 JSON Schema, `permission`은 `read`, `write`, `exec`, `network`, `send` 중 하나, `run`은 `async def run(tool_ctx, args) -> ToolResult`입니다.
- `timeout`: 초. 이보다 오래 걸린 호출은 시간 초과 오류로 실패합니다.

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

[도구 만들기](#도구-만들기)를 보세요.

### `ctx.prompt.section(section) -> Disposer`

시스템 프롬프트에 한 조각을 더합니다. 프롬프트 섹션 레지스트리에 등록됩니다.

- `section`: `PromptSection(name, order, render, requires_tool=None)`. `render(inputs: PromptInputs) -> str`은 모델 호출마다 실행되어 텍스트를 돌려주거나, `""`를 돌려주어 섹션을 뺍니다. 위치는 `order`가 정합니다([프롬프트 섹션](#프롬프트-섹션) 참고). `requires_tool`을 주면 그 도구가 제공될 때만 렌더링합니다.

<!-- example: examples/extensions/house-style/plugin.py -->
```python
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

### `ctx.prompt.context(provider) -> Disposer`

런타임 사실을 더합니다. 런타임 컨텍스트 레지스트리에 등록됩니다.

- `provider`: `ContextProvider(name, order, render, required=False)`. `render(core, session) -> str`은 모델 호출 전마다 실행되며, 텍스트가 바뀌었을 때만 모델에게 보냅니다. `required=True`이면 가볍게 위임된 에이전트에게도 보냅니다.

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

### `ctx.hooks.post_execute(name, listener, *, order=0) -> Disposer`

모델이 결과를 읽기 전에, 끝난 도구 호출을 모두 봅니다. post-execute 체인에 `<확장>:<name>`으로 등록됩니다.

- `listener(tool_ctx, tool_name, args, result)`는 `None`, `PostDecision.accept(output=None, *notes)`, `PostDecision.block(feedback, *notes)` 중 하나를 돌려주며, `async`여도 됩니다.
- `order`: 리스너는 `order` 순, 같으면 등록 순으로 실행됩니다.

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    ctx.hooks.post_execute("redact", redact, order=-100)
```

### `ctx.hooks.turn_stopping(name, listener, *, order=150) -> Disposer`

턴이 끝나려 할 때 실행됩니다. turn-stopping 체인에 등록됩니다.

- `listener(core, session, stop: StopContext)`는 `None` 또는 `StopDecision(text, producer, ...)`를 돌려주며, `async`여도 됩니다. `order` 순으로 처음 나온 결정이 이기고, 그 `text`가 다음 지시가 됩니다.

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

### `ctx.commands.register(command) -> Disposer`

슬래시 명령을 더합니다. 명령 표(`snowpea commands list`, `command.list`, TUI 팔레트)에 등록되며, 클라이언트에는 `commands.changed`로 알립니다.

- `command`: `Command(name, summary, run, args_schema={})`. `name`에는 앞의 `/`를 붙이지 않으며, `run`은 `async def run(cmd_ctx: CommandContext, args: str)`입니다.

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

확장이 불러와져 있는 동안 MCP 서버를 띄웁니다. MCP 서버 목록(`snowpea mcp list`에 그 확장이 플러그인으로 표시됩니다)에, 서버가 뜨면 그 도구들이 도구 레지스트리에 등록됩니다.

- `name`: 서버 이름. 도구 이름은 `mcp__<name>__<tool>`이 됩니다.
- `entry`: `.mcp.json` 항목 하나. stdio는 `command`, `args`, `env`, `cwd`, http/sse는 `url`, `headers`, 그리고 `timeoutSec`, `toolTimeoutSec`, `tools.include` / `tools.exclude`.

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

확장을 내릴 때 다른 모든 것과 함께, 등록의 역순으로 `fn()`을 실행합니다. `fn`은 `async`여도 되며, 최대 `DISPOSE_TIMEOUT`(10)초까지 기다립니다. 실패하거나 멈춘 것은 기록하고 나머지는 계속 실행합니다.

<!-- example: examples/extensions/ticket-workflow/plugin.py -->
```python
    client = tracker.Tracker(ctx.config["baseUrl"], ctx.config.get("token", ""))
    # Registered first, so it runs last: after everything that uses the client is gone.
    ctx.on_dispose(client.close)
```

### `ctx.config`, `ctx.data_dir`, `ctx.log`, `ctx.name`, `ctx.path`

- `ctx.config`(dict): `settings.extensions.<이름>.config`. 이미 `configSchema`로 검사했고, 최상위 속성의 `default`로 빈 곳을 채웠습니다. 복사본이므로, 새 설정은 다시 불러올 때 들어옵니다.

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]
```

- `ctx.data_dir`(Path): `$SNOWPEA_HOME/extension-data/<이름>/`. 처음 쓸 때 만들어집니다. 파일은 확장 폴더가 아니라 여기에 두어야 확장을 업데이트해도 남습니다.

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    store: Path = ctx.data_dir / "notes.json"
```

- `ctx.log`(logging.Logger): 이름이 `snowpea.extensions.<이름>`이며 데몬 로그에 씁니다.
- `ctx.name`(str)과 `ctx.path`(Path): 확장의 이름과 폴더.
- `ctx.core`: 데몬 코어. 안정된 API에 **포함되지 않습니다**. 위의 것으로 안 될 때만 읽고, 바뀔 수 있다고 생각하세요.

## 도구 만들기

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

**스키마와 설명**이 모델이 도구에 대해 아는 전부입니다. 설명에는 무엇을 하는지와 언제 쓰는지를 쓰고, 인자마다 설명을 달고, 필수인 것을 표시하세요. 모델은 대개 스키마를 따르지만 늘 그렇지는 않으니, 기대는 값은 확인하세요.

**`ToolContext`**는 `tool_ctx.session`(그 `workdir`, `id`, `mode`), `tool_ctx.backend`, `tool_ctx.core`, `tool_ctx.call_id`, `tool_ctx.progress`를 줍니다. `tool_ctx.backend`는 세션의 파일과 셸이 있는 곳 — 이 컴퓨터, Docker 컨테이너, SSH 호스트 — 입니다. 프로젝트는 `open()`이나 `subprocess`가 아니라 이것으로 다루세요: `cwd`, `resolve(path)`, `exists`, `list_dir`(폴더는 `/`로 끝남), `read_file`, `read_bytes`, `write_file`, `write_bytes`, `remove_file`, `run(command, cwd=, timeout=)`. 그러지 않으면 세션이 다른 곳에서 돌 때 도구가 데몬의 컴퓨터에서 동작합니다.

**`ToolResult`** 필드:

| 필드 | 읽는 쪽 | 용도 |
|---|---|---|
| `ok` | 모두 | `False`이면 호출이 실패한 것입니다 |
| `output` | 모델 | `ok`일 때의 결과 텍스트 |
| `error` | 모델 | 무엇이 잘못됐는지. 모델이 호출을 고칠 수 있게 씁니다 |
| `meta` | 클라이언트(`tool.result`), 모델은 아님 | UI를 위한 구조화된 사실 |
| `contexts` | 모델, 다음 호출 전 | 결과 옆에 붙는 추가 메모 |
| `path`, `diff` | 클라이언트 | 바뀐 파일과, 보여 줄 unified diff |

평범한 `str`을 돌려주면 `ToolResult(ok=True, output=...)`로 칩니다. `tools.maxResultChars`(기본 100 000)보다 긴 출력은 앞과 뒤만 남기고 잘리며, 전체 텍스트는 모델이 읽을 수 있는 곳에 저장됩니다.

**권한 태그**는 모드에 따라 사람에게 물을지를 정합니다.

| 태그 | 뜻 | plan | accept | auto |
|---|---|---|---|---|
| `read` | 읽기만 하고 바꾸지 않음 | 허용 | 허용 | 허용 |
| `write` | 프로젝트의 파일을 바꿈 | 거부 | 허용 | 허용 |
| `exec` | 프로그램을 실행 | 질문 | 질문 | 허용 |
| `network` | 네트워크에 접속 | 허용 | 질문 | 허용 |
| `send` | 사용자 대신 메시지를 보냄 | 거부 | 질문 | 허용 |

도구가 할 수 있는 가장 위험한 일의 태그를 고르세요. `write` 도구라면 인자의 `path`, `paths`, `edits[].path`에 적힌 파일을 호출 전에 체크포인트로 남겨, 사람이 변경을 되돌릴 수 있게 합니다. 승인 요청에는 `from extension <이름>`이 붙습니다.

**오류.** 모델이 손쓸 수 있는 일에는 `ToolResult(ok=False, error=...)`를 돌려주세요. 예외는 대신 잡아 줍니다. 호출은 `<도구> (extension <이름>) failed: <타입>: <메시지> (at plugin.py:<줄>)`로 실패하고, 트레이스백은 데몬 로그에 가며, 턴과 데몬은 계속 갑니다. 그래도 오류 문구는 직접 쓰세요. 트레이스백은 모델에게 아무 의미가 없습니다.

**진행 상황.** `await tool_ctx.progress.emit("stdout", text)`는 호출이 도는 동안 클라이언트로 한 줄을 흘려보냅니다. 보는 쪽이 없으면 `progress`는 `None`입니다.

**시간 제한과 중단.** `register`에 `timeout=`을 주거나 직접 작업에 한도를 걸지(`asyncio.wait_for`) 않으면 시간 제한은 없습니다. 사람이 턴을 중단하면 도구를 실행하던 태스크가 취소되어, 기다리던 `await` 자리에서 `asyncio.CancelledError`가 납니다. `finally`에서 정리하고 예외는 그대로 올려 보내세요.

**동시성.** `run`은 모든 세션이 함께 쓰는 데몬의 이벤트 루프에서 실행됩니다. 절대 막지 마세요. 파일이나 CPU 작업은 `asyncio.to_thread`로 옮기고, 네트워크는 비동기 클라이언트를 쓰세요. 여러 세션이 동시에 도구를 부를 수 있으니 공유 상태는 보호하세요. 한 모델 응답의 호출들은 차례로 실행되며, 도구가 `parallel_safe=True`이면(`read`와 `network` 도구만 가능) 동시에 실행됩니다.

## 프롬프트 섹션

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

섹션은 `order` 순, 같으면 이름 순으로 정렬됩니다. order는 **대역**도 정하는데, 이것이 비용을 좌우합니다. 제공자는 프롬프트의 앞부분을 캐시하므로, 내용이 바뀌는 섹션은 그 위치부터 뒤쪽 캐시를 무효로 만듭니다.

| 대역 | order | 바뀌는 때 | 내장 섹션(order) |
|---|---|---|---|
| stable | 1000 미만 | 모드, 모델, 역할, 응답 언어가 바뀔 때만 | identity (−1000), execution (−900), plan-continuity (−800), vendor (0), config-rule (100), search-honesty (200), memory-guidance (300), mode (400), role-preamble (500), role (510), persona (600), reply-language (700) |
| context | 1000–1999 | 세션마다 한 번, 그리고 압축 후 | environment (1000), context-files (1100), skills (1200), tools (1300) |
| volatile | 2000 이상 | 턴마다 | memory (2000), plan-line (2100), context-pressure (2200) |

변하지 않는 텍스트는 stable 대역에 두고, `render`가 매번 같은 텍스트를 돌려주게 하세요. 세션 중에 바뀌는 사실이라면 런타임 컨텍스트 제공자를 쓰세요. `PromptInputs`에는 `mode`, `role`, `subagent`, `tools`(와 `tool_names`), `reply_language`, `root`(프로젝트 폴더) 등이 있습니다. 예외를 낸 섹션은 그 호출에서 빠지고 한 번 기록됩니다. 섹션 이름 앞에는 확장 이름을 붙이세요.

## 런타임 컨텍스트 제공자

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

모든 제공자는 모델 호출 전마다 렌더링되어 하나의 정렬된 목록이 됩니다. 이 목록은 이전 것을 대체하는 `<system-reminder>` 하나로 모델에게 가는데 — 턴 시작 때, 또는 턴 중간이라면 다음 모델 호출 전에 — **마지막으로 보낸 것과 다를 때만** 보내고, 압축이 이전 것을 요약해 없앴을 때 다시 보냅니다. 그래서 텍스트가 그대로인 동안 제공자는 비용이 들지 않습니다. `render`는 빠르게(프로세스도 네트워크도 쓰지 말 것: 모델 호출마다 돕니다), 그리고 결정적으로 만드세요. 내장 order: goal (50), plan (100), plan-auto (110), context-pressure (900).

## 훅

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

모든 도구 호출 — 내장, MCP, 호스트, 확장 — 은 실행된 뒤, 출력이 잘리고 모델에게 보이기 전에 이 체인을 지납니다. 리스너는 다음 중 하나를 돌려줍니다.

| 반환값 | 효과 |
|---|---|
| `None` | 변경 없음 |
| `PostDecision.accept(output=None, *notes)` | 모델이 읽을 텍스트를 바꾸고(`output`을 준 경우), 모델의 다음 호출 전에 보낼 메모를 더합니다 |
| `PostDecision.block(feedback, *notes)` | 호출을 실패로 만들고 오류를 `feedback`으로 합니다. 뒤의 리스너는 실행되지 않습니다 |

리스너는 `order` 순, 같으면 등록 순으로 실행되며 내장 리스너는 없습니다. 예외를 낸 리스너는 기록하고 건너뜁니다.

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

모델이 도구를 부르지 않고 답하면 턴이 끝나려 합니다. 그 전에 리스너가 `order` 순으로 실행되고, 처음 `StopDecision`을 돌려준 것이 이기며, 그 `text`가 다음 지시로 보내집니다. 내장 리스너:

| order | 리스너 | 턴을 이어 가는 경우 |
|---|---|---|
| 100 | `verify-gate` | 코드를 고친 뒤 통과한 검사가 없거나, UI를 한 번도 보지 않았거나, 할 일이 남아 있을 때 |
| 150 | `unfinished-work` | 답이 지금 더 할 수 있는 일을 늘어놓을 때 |
| 200 | `stop-hooks` | Claude Code `Stop` 훅이 요청할 때 |

`StopContext`는 `last_message`, `stop_continuations`, `at_budget`(라운드 예산 확인에 대한 답이니 끝내게 둘 것)을 줍니다. `StopDecision(text, producer, via="message", shown="", count=None, limit=None)`: `producer`는 클라이언트가 보여 주는 이벤트에 당신의 이름을 남기고, `count`/`limit`은 "3번 중 2번째" 같은 표시를 가능하게 합니다. 한 턴을 몇 번까지 이어 갈지 **반드시 한도를 두세요**. 모델이 요구를 채우지 못할 수도 있고, 그 반복을 멈출 것은 달리 없습니다.

## 명령

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

명령은 모델을 부르지 않는 독립된 턴입니다. `run(cmd_ctx, args)`는 이름 뒤의 모든 것을 문자열 하나로 받습니다. `cmd_ctx.say(text)`는 대화 기록에 답하며(그 텍스트는 대화에 남아 다음 턴에 모델도 압니다), `cmd_ctx.session`과 `cmd_ctx.core`도 있습니다. 예외가 나면 명령의 턴이 사용자에게 보이는 오류로 끝납니다. 내장 명령의 이름은 쓸 수 없고, 스킬이나 다른 확장이 쓰는 이름도 쓸 수 없습니다. 나중에 같은 이름의 스킬을 설치하면 그 스킬이 이름을 차지하고, 확장의 다음 다시 불러오기는 이름 충돌로 실패합니다.

## MCP 서버

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

서버는 백그라운드에서 뜨므로 `apply`는 기다리지 않습니다. 시작에 실패한 서버는 기록되고 확장은 불러온 채로 남습니다. 이름은 다른 어디에도(`.mcp.json`, 설정, 플러그인) 선언되어 있지 않아야 합니다. `snowpea mcp list`는 그 서버를 확장 소유로 보여 주고, 언로드하면 도구를 지우고 프로세스를 멈춥니다. 기능이 이미 MCP 서버로 있다면 이것을, Python 몇 줄이면 되는 일이라면 도구를 쓰세요.

## 설정과 `configSchema`

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

설정은 `settings.json`의 `extensions.<이름>.config`에, `enabled` 옆에 있습니다. `apply`가 실행되기 전에 설정을 `configSchema`로 검사합니다(`jsonschema` 패키지가 아는 JSON Schema 드래프트면 무엇이든. `$schema`로 고릅니다). 설정이 맞지 않으면 확장을 불러오지 않으며, 오류는 `settings.extensions.redact-secrets.config.patterns[0]: '' should be non-empty`처럼 실패한 경로를 짚습니다. 없는 최상위 속성은 `default`로 채웁니다. 클라이언트는 `extension.configure`로 설정을 바꾸며(검사하고 저장한 뒤 확장을 다시 불러옵니다), `extension.list`가 스키마를 돌려주므로 클라이언트가 그것으로 폼을 그릴 수 있습니다. 토큰 같은 비밀은 그 컴퓨터의 `settings.json`이 그만큼 비공개일 때만 설정에 두세요.

## 로그와 디버깅

- `ctx.log`와 `snowpea.extensions` 아래로 남긴 로그는 `$SNOWPEA_HOME/logs/daemon.log`로 갑니다. 확장에 대한 데몬 자신의 기록 — 불러올 때 무엇을 등록했는지, 왜 실패했는지(전체 트레이스백 포함), 예외를 낸 도구·섹션·제공자·훅 — 도 그곳에 남습니다.
- `snowpea extension list`는 확장마다 상태 — `loaded`, `unloaded`, `disabled`, `error`, `incompatible` — 와, 실패했다면 당신의 코드를 가리키는 `Type: message (at file:line)`을 보여 줍니다.
- `snowpea extension info <이름>`은 폴더, 오류, 설정, 스키마, 그리고 등록한 도구·명령·MCP 서버를 보여 줍니다.
- `snowpea extension reload <이름>`은 확장을 불러오지 못하면 종료 코드 1로 끝나므로, 스크립트로 고치고-다시 불러오기를 반복할 때 실수에서 멈춥니다.

흔한 실수:

| 증상 | 원인 |
|---|---|
| 도구가 한 번도 불리지 않음 | 설명에 언제 쓰는지가 없거나, 현재 모드에서 권한이 거부됨 |
| `run must be an async function` | `async def run`이 아니라 `def run` |
| 도구가 도는 동안 데몬이 멈춤 | 이벤트 루프에서 막히는 작업. `asyncio.to_thread`를 쓰세요 |
| 자기 모듈에서 `ModuleNotFoundError` | `from . import helpers`가 아니라 `import helpers` |
| 패키지에서 `ModuleNotFoundError` | 데몬의 Python에 설치되어 있지 않음 |
| 다시 불러와도 예전처럼 동작 | 코드가 확장 바깥(코어의 모듈, 전역 레지스트리)에 상태를 둠. 상태는 클로저나 자기 모듈에 두세요 |
| 프롬프트 캐시가 소용없어짐 | 텍스트가 바뀌는 stable 대역 섹션. 그 사실은 런타임 컨텍스트 제공자로 옮기세요 |

## 확장 테스트하기

`snowpea_core.extensions.testing.load_extension`은 임시 폴더에 전용 데몬을 띄우고, 확장을 그 안에 복사하고, 실제 데몬과 같은 매니저로 불러온 뒤 세션을 하나 엽니다. 블록을 빠져나가면 확장을 내리고, 무엇이든 등록된 채로 남았다면 테스트를 실패시킵니다. 모델은 부르지 않고 네트워크로는 아무것도 나가지 않습니다. `hello` 예제에 함께 들어 있는 테스트:

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

`load_extension(folder, *, config=None, home=None, check=True)`이 주는 객체:

| 멤버 | 하는 일 |
|---|---|
| `call_tool(name, args)` | 에이전트 루프처럼 도구를 부르고 `post_execute` 훅을 거쳐 `ToolResult`를 돌려줍니다 |
| `system_prompt()` | 세션의 다음 모델 호출이 받을 시스템 프롬프트 |
| `runtime_context()` | 지금의 `{제공자 이름: 텍스트}` |
| `stop_turn(last_message)` | `turn_stopping` 체인의 결정 |
| `run_command(name, args)` | 슬래시 명령을 실행하고, 그 명령이 한 말을 돌려줍니다 |
| `reload(config=None)` | 다시 불러옵니다. 새 설정을 줄 수도 있습니다 |
| `registrations`, `extension`, `session`, `core` | 등록한 것, 상태와 오류, 세션, 코어 |

`check=True`이면 불러오기 실패 때 확장의 오류를 담은 `ExtensionLoadError`를 내므로, 잘못된 설정의 테스트는 `pytest.raises(ExtensionLoadError, match=...)`가 됩니다.

## 버전 관리

`snowpea_core.extensions.API_VERSION`은 이 API의 메이저 버전입니다(지금은 `1`). 확장이 기대는 것이 없어지거나 뜻이 바뀔 때만 올라갑니다: `ctx`의 멤버나 인자, `snowpea_core.extensions`가 내보내는 이름의 필드나 메서드, `run`·`render`·리스너의 호출 형식, 이 문서가 약속한 순서. 멤버, 선택 인자, 필드, 새 내장 섹션을 더하는 것은 해당하지 않습니다. `ctx.core`와 `snowpea_core.extensions` 밖의 모든 모듈은 이 약속 밖에 있습니다.

기준으로 삼은 메이저를 `plugin.json`에 `"engines": {"snowpea": "^1"}`로 적으세요. API 메이저가 다른 데몬은 그 확장을 `incompatible`로 표시하고(이유에 두 버전이 다 나옵니다) 불러오지 않습니다. 확장 자체의 버전은 `version`에 적습니다.

## 배포

아직 확장 마켓플레이스는 없습니다. 확장을 나누려면 폴더를 공개하세요 — 루트에 `plugin.py`가 있는 git 저장소, 또는 폴더를 묶은 zip — 그리고 사람들이 그것을 `$SNOWPEA_HOME/extensions/<이름>/`에 두고(예: `git clone <저장소> ~/.snowpea/extensions/<이름>`) `snowpea extension reload <이름>`을 실행하게 하면 됩니다. 업데이트는 `git pull`과 다시 불러오기이며, `ctx.data_dir`의 데이터는 그대로 남습니다. 확장은 데몬의 권한으로 실행되므로, 무엇을 하는지, 무엇을 어디로 보내는지, 도구마다 어떤 권한을 선언하는지 사용자에게 알려 주세요. 저장소의 `scripts/gen_extension_index.py`는 확장 폴더 목록을 담은 정적 `index.json`을 만들어, 목록을 호스팅하려는 사람이 쓸 수 있습니다.

## CLI와 RPC 레퍼런스

```bash
snowpea extension list
snowpea extension info hello --json
snowpea extension enable hello
snowpea extension disable hello
snowpea extension reload hello
snowpea extension reload
snowpea extension new hello
```

| 명령 | 하는 일 | 종료 코드 |
|---|---|---|
| `snowpea extension list [--json]` | 모든 확장과 그 상태·오류 | 0 |
| `snowpea extension info <이름> [--json]` | 확장 하나와 그 도구·명령·MCP 서버·설정·스키마 | `error`나 `incompatible`이면 1 |
| `snowpea extension enable <이름>` | 켜고 불러옴 | 불러오지 못하면 1 |
| `snowpea extension disable <이름>` | 내리고 꺼 둠 | 0 |
| `snowpea extension reload [<이름>]` | 하나를, 또는 켜진 것을 모두 다시 불러옴 | 하나라도 불러오지 못하면 1 |
| `snowpea extension new <이름>` | 시작용 템플릿을 씀. 데몬 불필요 | 폴더가 이미 있으면 2 |

`new`를 뺀 나머지는 다음 메서드로 데몬과 이야기합니다(capability `extensions`).

| 메서드 | 파라미터 | 결과 |
|---|---|---|
| `extension.list` | `{}` | `{extensions: ExtensionInfo[]}`. 폴더를 다시 스캔합니다 |
| `extension.enable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.disable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.reload` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.configure` | `{name, config}` | `{extension: ExtensionInfo}`. 설정이 `configSchema`에 맞지 않으면 실패한 경로를 짚는 `invalid_params` 오류 |

`ExtensionInfo`: `name`, `path`, `description`, `version`, `enabled`, `state`(`loaded`, `unloaded`, `disabled`, `error`, `incompatible`), `error`(또는 null), `effects`(가진 등록 수), `configSchema`(또는 null), `config`. 전체 스키마는 [프로토콜 레퍼런스](../../protocol.md)에 있습니다.

## 문제 해결

| 보이는 것 | 할 일 |
|---|---|
| `snowpea extension list`에 확장이 없음 | 폴더가 `$SNOWPEA_HOME/extensions/` 바로 아래에 있고 `plugin.py`가 있어야 합니다. `SNOWPEA_HOME`을 확인하세요 |
| 상태가 `unloaded` | 데몬이 뜬 뒤에 추가된 것: `snowpea extension reload <이름>` |
| 상태가 `error`, `plugin.json:…` | 메시지대로 매니페스트를 고치세요 |
| 상태가 `error`, `settings.extensions.<이름>.config…` | 그 경로의 설정을 고치거나 `extension.configure`로 바꾸세요 |
| 상태가 `incompatible` | 다른 API 메이저용으로 만든 확장입니다. 맞는 버전을 구하세요 |
| `… is already registered by builtin` | 다른 이름을 고르세요. 내장 도구와 명령은 바꿀 수 없습니다 |
| `… is already registered by extension:<다른 것>` | 두 확장이 같은 이름을 원합니다. 하나의 이름을 바꾸거나 다른 하나를 끄세요 |
| 도구가 `(extension <이름>) failed:`로 실패 | 도구 안의 예외. 위치와 데몬 로그의 트레이스백이 어디인지 알려 줍니다 |
| 도구가 `timed out after`로 실패 | `register`에 준 `timeout`에 걸렸습니다 |
| MCP 서버의 도구가 나타나지 않음 | 서버가 왜 시작하지 못했는지 데몬 로그에 있습니다. 항목을 `snowpea mcp test`로 시험해 보세요 |
| 아무 일도, 아무 오류도 없음 | 데몬이 `SNOWPEA_EXTENSIONS=0`으로 시작했거나, `settings.json`에서 확장이 꺼져 있습니다 |

## 다음

[플러그인](plugins.md) — 스킬, Claude Code와 Codex 플러그인, 훅, MCP 서버.

# M1 Core Contract (binding for US-004 … US-008)

이 문서는 M1 수직 슬라이스를 병렬로 구현하는 에이전트들이 공유하는 **인터페이스 계약**이다. 여기 적힌 이름·시그니처·이벤트 형태를 바꾸려면 이 문서를 먼저 고친다. 세부 근거는 `.omc/plans/snowpea-agent-consensus-plan.md` §2, §3.

## 0. Runtime facts
- Python 3.11+, `asyncio` 단일 이벤트 루프, 패키지 `snowpea_core` (`core/snowpea_core`).
- 홈 디렉터리: `SNOWPEA_HOME` (기본 `~/.snowpea`). 파일: `daemon.json`, `token`(0600), `settings.json`, `state.db`, `logs/`.
- 프로젝트 설정: `<workdir>/.snowpea/settings.json` (`defaultMode`, `allowlist`, `backend`, `agents.max_concurrent` 오버라이드).
- 로깅: `logging.getLogger("snowpea.<module>")`, 파일 핸들러 `$SNOWPEA_HOME/logs/daemon.log`.

> **Transport note (US-004):** 데몬은 aiohttp 애플리케이션 하나로 HTTP와 WebSocket을 같은 포트에서 서비스한다. WS 엔드포인트는 `ws://127.0.0.1:<port>/ws`, HTTP는 `GET /health`, `GET /version`, `GET /protocol.json`.

## 1. `server/protocol.py` — SSOT
```python
PROTOCOL_VERSION = "0.1.0"          # semver; M8에서 1.0.0
SERVER_VERSION = snowpea_core.__version__

class RpcMethod(BaseModel):          # 메서드 레지스트리 항목 (스키마 덤프용)
    name: str; params: type[BaseModel]; result: type[BaseModel]; direction: Literal["c2s","s2c"]

METHODS: dict[str, RpcMethod]        # 아래 목록 전부 등록
EVENTS: dict[str, type[BaseModel]]   # 알림 페이로드 스키마
```
메서드 이름/필드 (camelCase, JSON-RPC `params`는 항상 object):

| method | params | result |
|---|---|---|
| system.hello | token, clientVersion, protocolVersion | protocolVersion, serverVersion, capabilities: list[str] |
| system.info | – | version, protocolVersion, pid, port, startedAt, home |
| system.health | – | status: "ok" |
| system.shutdown | – | ok: bool |
| session.create | workdir, mode?: Mode, provider?, model?, agent?, maxConcurrent?, originSurface?: str | sessionId |
| session.resume | sessionId, afterSeq?: int | sessionId, events: list[SessionEvent] |
| session.list | – | sessions: list[SessionSummary] |
| session.close | sessionId | ok |
| session.prompt | sessionId, text, attachments?: list[Attachment] | turnId |
| session.interrupt | sessionId | ok |
| session.setMode | sessionId, mode | mode |
| command.list | sessionId? | commands: list[CommandInfo{name, summary, argsSchema, source}] |
| command.run | sessionId, name, args: str | turnId |
| tool.list | sessionId? | tools: list[ToolInfo{name, category, permissionTag, state, source, description}] |
| approval.list | sessionId? | requests: list[ApprovalRequest] |
| approval.respond | requestId, decision: "allow"\|"deny", scope: "once"\|"session"\|"project"\|"always" | ok |
| permission.allowlist.add / list / remove | pattern, scope / scope? / patternId | patternId / patterns / ok |
| provider.list / provider.configure / provider.loginWeb | – / vendor, config / vendor, method | providers / ok / ok |
| backend.set | sessionId, kind: "local"\|"docker"\|"ssh", config: dict | ok |
| agent.*, team.*, job.*, gateway.*, memory.*, skill.* | 플랜 §3.5 그대로 (M1은 스키마만 정의, 구현은 `error{code:"not_implemented"}`) | |

서버→클라이언트 요청: `approval.request(requestId, sessionId, tool, args, risk, timeoutSec, scopeHint) -> {decision, scope}`.

알림: `session.event(sessionId, seq, kind, payload, ts)`; `approval.resolved(requestId, decision, by)`; `job.event`; `gateway.event`.

`session.event.kind` ∈ `message.delta{text}` · `message.done{text, role}` · `tool.call{callId, name, args}` · `tool.result{callId, name, ok, output, error?}` · `diff{path, patch}` · `subagent.spawn/update/done{agentId, ...}` · `team.task.update` · `mode.changed{mode}` · `usage{inputTokens, outputTokens}` · `error{code, message}` · `turn.done{turnId, reason: "complete"|"interrupted"|"error"|"denied"|"timeout"}`.

에러 코드(문자열, JSON-RPC `error.data.code`): `unauthorized`, `protocol_incompatible`, `not_found`, `invalid_params`, `mode_denied`, `approval_denied`, `approval_timeout`, `tool_inactive`, `not_implemented`, `login_unsupported`, `internal`.

## 2. `server/rpc.py`
```python
class RpcConnection:            # 연결 1개 = 클라이언트 1개 (WS)
    async def call(self, method: str, params: dict, timeout: float | None = None) -> dict   # s2c 요청
    async def notify(self, method: str, params: dict) -> None
    surface_id: str             # originSurface 식별 (uuid), 인증 후 부여
    authenticated: bool
class RpcDispatcher:
    def register(self, name: str, handler: Callable[[RpcConnection, BaseModel], Awaitable[BaseModel]])
    async def handle_message(self, conn, raw: str)   # 요청/응답/알림 분기, id 상관관계
```
JSON-RPC 2.0 형식 엄수. `system.hello` 이전 다른 메서드 → `unauthorized`.

## 3. Core singletons (`server/app_server.py`)
```python
@dataclass
class Core:
    settings: Settings; paths: Paths; store: Store
    sessions: SessionManager; tools: ToolRegistry; commands: CommandRegistry
    providers: ProviderRegistry; approvals: ApprovalQueue; policy: PermissionPolicy
    hub: EventHub          # session.event 브로드캐스트 (구독: connection별)
    lifecycle: Lifecycle
async def run_daemon(port: int = 0, home: Path | None = None) -> None
```
`python -m snowpea_core --port 0 [--home DIR]`, 콘솔 별칭 `snowpea-core`. 기동 시 `daemon.json = {port, pid, token, startedAt, protocolVersion}`.

## 4. Session / events (`session/`)
```python
Mode = Literal["plan","accept","auto"]
class Session: id, workdir, mode, provider, model, origin_surface, created_at, history: History, seq: int
class SessionManager:
    async def create(...)->Session; get(id); list(); async close(id)
    def next_seq(session)->int
class EventHub:
    async def emit(session_id, kind, payload)      # seq 부여 → store 저장 → 구독 연결에 notify
    def subscribe(conn, session_id|None)
```
이벤트는 `store`에 append 되어 `session.resume(afterSeq)`가 재전송한다.

## 5. Providers (`providers/base.py`)
```python
@dataclass
class ToolSpec: name: str; description: str; input_schema: dict
@dataclass
class ChatMessage: role: Literal["system","user","assistant","tool"]; content: str | list[dict]; tool_call_id: str|None=None; tool_calls: list[ToolCall]|None=None
@dataclass
class ToolCall: id: str; name: str; arguments: dict
class StreamEvent:   # kind: "text_delta"|"tool_call"|"usage"|"done"
class ChatProvider(Protocol):
    vendor: str; model: str
    async def stream(self, messages: list[ChatMessage], tools: list[ToolSpec], *, max_tokens: int) -> AsyncIterator[StreamEvent]
class ProviderRegistry:
    def get(vendor: str|None, model: str|None) -> ChatProvider
```
`SNOWPEA_PROVIDER=fake:<script>` 환경변수가 있으면 `tests/fixtures/providers/fake/scripted.py`(M3) 또는 M1 임시 `providers/fake.py`의 스크립트 프로바이더를 반환한다. 스크립트 형식: JSON 파일, `steps: [{"match": "<substring of last user text>", "tool_calls": [...], "text": "..."}]`, 순서대로 소비.

## 6. Tools (`tools/registry.py`)
```python
PermissionTag = Literal["read","write","exec","network","send"]
@dataclass
class Tool: name: str; category: str; description: str; input_schema: dict; permission: PermissionTag
           state: Literal["active","inactive"] = "active"; source: str = "builtin"
           run: Callable[[ToolContext, dict], Awaitable[ToolResult]]
class ToolContext: session: Session; core: Core; backend: ExecutionBackend
class ToolResult: ok: bool; output: str; error: str|None=None; diff: str|None=None
class ToolRegistry: register(tool); get(name); list(session=None) -> list[ToolInfo]
```
M1 툴: `read_file`, `write_file`, `edit_file`(old/new 문자열 치환, diff 이벤트 발생), `list_dir` (read/write), `shell`(exec, `command`, `cwd?`, `timeout?`).

## 7. Permissions (`permissions/`)
```python
class PermissionPolicy:
    def decide(self, mode: Mode, tag: PermissionTag, tool: Tool, args: dict, session) -> Literal["allow","deny","ask"]
```
| mode \ tag | read | write | exec | network | send |
|---|---|---|---|---|---|
| plan | allow | deny | deny | allow | deny |
| accept | allow | allow | ask | ask | ask |
| auto | allow | allow | allow | allow | allow |
allowlist(M4)는 `ask`→`allow`로 승격만 한다.
```python
class ApprovalQueue:
    async def request(self, session, tool, args, *, risk: str, unattended: bool) -> Decision   # 대기(타임아웃 → deny)
    def list(session_id=None); async def respond(request_id, decision, scope, by: str)
```
대화형 턴(`unattended=False`)은 세션의 `origin_surface` 연결에만 `approval.request`를 보낸다. 응답 없이 `approvals.timeoutSec`(기본 300) 경과 시 deny. 결과는 `logs/approvals.jsonl`에 append.

## 8. Agent loop (`agent/loop.py`)
```
turn = prompt → messages(history+system) → provider.stream
  text_delta → session.event message.delta
  tool_call  → policy.decide → deny: event error{mode_denied} + turn.done{denied}
                             → ask: approvals.request → deny: 같은 처리
                             → allow: event tool.call → tool.run → event tool.result (+diff) → 메시지에 tool 결과 추가 → 다시 provider.stream
  done(no tool calls) → message.done → usage → turn.done{complete}
```
최대 반복 `agent.max_tool_rounds`(기본 50). `session.interrupt` → `turn.done{interrupted}`.

## 9. Commands (`commands/registry.py`)
```python
@dataclass
class Command: name; summary; args_schema: dict; source: Literal["builtin","skill","plugin"]; run: Callable[[CommandContext, str], Awaitable[None]]
class CommandRegistry: register(cmd); list(session=None); parse(text) -> (name, args) | None; async run(session, name, args) -> turn_id
```
텍스트가 `/`로 시작하면 `session.prompt` 핸들러도 `commands.parse`를 거쳐 `command.run`으로 위임한다(TUI·헤드리스·게이트웨이 공통). M1 내장: `help`, `plan`, `accept`, `auto`, `mode` (`/mode [plan|accept|auto|save]`), `tools`.

## 10. CLI (`cli/main.py`) — 플랜 §3.6 그대로
종료 코드 0/1/2/3/4/5. `snowpea tools list --json`, `snowpea commands list --json`, `snowpea daemon status|stop`, `snowpea --version`.

## 11. SDK (`sdk/src`)
`connect({port, token, clientVersion})` → `Client` with `call<M>(method, params)`, `on("session.event", cb)`, `onRequest("approval.request", handler)`, `close()`, auto-reconnect + `resume(sessionId, afterSeq)`. `protocol.ts`는 생성물(수기 편집 금지).

## 12. Tests (M1)
- `tests/test_rpc_roundtrip.py`, `tests/test_session_loop.py`, `tests/test_headless_exit_codes.py` — 모두 `SNOWPEA_PROVIDER=fake:...`와 임시 `SNOWPEA_HOME`(conftest) 사용, 실 네트워크 없음.
- `sdk/test/contract.test.ts --grep base` — 데몬을 `uv run python -m snowpea_core --port 0 --home <tmp>`로 띄우고 3건 검증.

## Implemented early (by lead, binding)
- `providers/base.py` — ToolSpec, ToolCall, ChatMessage, Usage, StreamEvent, ChatProvider, ProviderError. Use these; do not redefine.
- `providers/fake.py` — `FakeProvider.from_env("fake:<script.json>")`; sample script `tests/fixtures/providers/fake/basic.json` (steps: match "hello" → text; match "run ls" → shell tool call; after_tool "shell" → "done"). `ProviderRegistry.get()` MUST return it when `SNOWPEA_PROVIDER` starts with `fake`.

## 13. Deviations

### Core daemon (US-004)

구현하면서 계약 대비 달라진 점. 다음 스토리들은 이 문서 기준으로 작업한다.

1. **단일 aiohttp 서버.** `websockets` 서버와 aiohttp 서버는 같은 소켓을 공유할 수 없어, aiohttp 하나만 `127.0.0.1:<port>`에 바인딩하고 WS는 `web.WebSocketResponse`로 `/ws` 경로에 얹었다. `server/transport_ws.py`는 그 경로 핸들러와 `system.hello`를, `server/transport_http.py`는 앱 구성과 `/health`·`/version`·`/protocol.json`을 담당한다. 런타임 의존성에서 `websockets`는 아직 사용하지 않는다.
2. **에러 코드 위치.** 계약대로 스노우피 코드는 `error.data.code`에 실린다. 숫자 `error.code`는 JSON-RPC 규약을 따르며 `not_found` → `-32601`, `invalid_params` → `-32602`, `internal` → `-32603`, 나머지는 `-32000`이다. `error.data.details`에 부가 정보(pydantic 검증 오류 등)가 들어간다.
3. **핸들러 시그니처.** `RpcDispatcher.register(name, handler)`의 핸들러는 계약의 `(conn, params)`가 아니라 `(conn, params_model, core)`를 받는다. `Core` 싱글턴을 전역 없이 전달하기 위함이다. 프로토콜에 없는 메서드를 등록할 때만 `params_model`을 직접 넘긴다.
4. **요청은 태스크로 처리.** 서버가 핸들러 안에서 `conn.call("approval.request", ...)`로 클라이언트를 호출하는 동안 읽기 루프가 막히면 교착이 생기므로, 각 요청은 `RpcConnection.spawn`으로 별도 태스크에서 실행된다. 따라서 한 연결 안에서 응답 순서는 요청 순서와 다를 수 있다(JSON-RPC `id` 상관관계로 매칭).
5. **`Core` 추가 필드.** 계약의 필드 외에 `token`, `pid`, `port`, `started_at`, `request_shutdown`이 있다. `system.info`와 `system.hello`가 전역 상태 없이 동작하기 위한 것이다. `store`는 아직 `None`(US-005가 채운다).
6. **`Daemon` 클래스.** `run_daemon(port, home)`은 계약 그대로지만, 테스트에서 포트를 알아내고 인프로세스로 띄우기 위해 `Daemon.start()` / `wait_closed()` / `stop()`을 공개한다. `run_daemon`은 그 위의 얇은 래퍼(시그널 핸들러 설치 + 대기)다.
7. **`EventHub` 위치.** 계약 §4는 `session/`이라고만 적어 두었으므로 M1 스텁은 `session/manager.py`에 `SessionManager`와 함께 둔다. US-005가 옮겨도 무방하다.
8. **`Lifecycle` 카운터.** `sessions` / `jobs` / `gateway_bindings` / `named_agents` 네 개만 존재하며 `set_counter(name, value)`로만 갱신한다. 넷 다 0인 상태가 `daemon.idleTimeoutSec`(기본 1800초) 지속되면 종료한다. `status()`는 `{counters, willExit, reason, secondsUntilExit}`를 돌려준다.
9. **테스트 전용 메서드.** `SNOWPEA_TEST=1`일 때만 `system.echoRequest`가 등록된다. 서버→클라이언트 요청 경로를 검증하기 위한 것으로 `METHODS`에는 포함되지 않는다.
10. **`dump_schema()` 형태 (US-007과 합의).** 반환 dict의 키는 `version`·`protocolVersion`(같은 값의 별칭)·`serverVersion`·`methods`·`events`·`sessionEventKinds`·`errorCodes`·`capabilities`·`transport`. `methods[name]`은 `{direction, params, result}`(params/result는 pydantic `model_json_schema()` 원본이라 로컬 `#/$defs/...` 참조는 소비자가 푼다), `events[name]`과 `sessionEventKinds[kind]`는 스키마 객체 그 자체다. `transport`는 `{"ws": "/ws", "http": {...}}`. `GET /protocol.json`이 이 dict를 그대로 돌려준다.
11. **M1 구현 범위.** `system.hello|info|health|shutdown` 외의 모든 c2s 메서드는 스키마만 등록되고 `error{code:"not_implemented"}`를 돌려준다(`protocol.IMPLEMENTED_METHODS` 참조).

### TUI (US-008)
계약을 어기지는 않되, 계약서에 없던 클라이언트 국소 결정들:

1. **연결 상태 표시는 SDK 라이프사이클 이벤트에 의존한다.** `StatusLine`의 connected/reconnecting/closed는 프로토콜 알림이 아니라 `@snowpea/sdk`의 `disconnected{code, reason, willRetry}` / `reconnected{attempt, resumedSessions}`에서 파생된다. `session.resume(afterSeq)` 재전송은 SDK가 수행하고 TUI는 표시만 한다.
2. **중복 이벤트 방어(이중 안전장치).** `tui/src/rpc/client.ts`는 세션별 최고 `seq`를 추적해 그 이하의 `session.event`를 버린다. SDK도 자체적으로 `seq`를 추적해 이미 전달한 이벤트를 다시 주지 않으므로(US-007 확인) 이 방어는 중복이지만, 화면에 두 번 그려지는 사고를 막는 값싼 보험으로 남긴다.
3. **`approval.list`는 조언적(advisory)이다.** 실패해도 세션을 막지 않는다. `ApprovalQueue`는 M1에서 읽기 전용(미처리 승인 표시만)이고, 큐에서 직접 응답하는 UI는 M4 allowlist 작업과 함께 온다. 대화형 승인(`approval.request`)만 `y`/`n` + scope로 promise를 resolve 한다.
4. **슬래시 라우팅은 클라이언트에서도 한 번 일어난다.** §9는 서버 `session.prompt` 핸들러도 `/`를 `command.run`으로 위임한다고 정하지만, TUI는 자동완성·도움말을 위해 이미 `command.list`를 갖고 있으므로 `/`로 시작하는 입력을 직접 `command.run`으로 보낸다. 명령 테이블은 하드코딩하지 않는다(`tui/src/slash/registry.ts`).
5. **TUI 바이너리 종료 코드.** `node dist/snowpea-tui.js` 는 인자 오류 `2`, 데몬 연결/세션 생성 실패 `1`, 정상 종료 `0`. §10의 CLI 종료 코드 체계와 같은 의미를 재사용한다.
6. **SDK 타입 재노출 모듈.** `tui/src/rpc/sdk.ts`는 `@snowpea/sdk`의 생성 타입에서 `SessionEvent`·`ApprovalRequestParams`·`Mode`·`CommandInfo` 등을 **파생**해 로컬 이름으로 재노출한다(별도 선언 아님). 프로토콜 재생성이 TUI와 조용히 어긋나는 것을 막고, TUI의 import 경로를 한 모듈로 모으기 위한 것이다. `ApprovalRequestParams.args`·`risk`는 프로토콜상 optional이므로 UI에서 각각 `{}`·`"unknown"`으로 폴백한다.
7. **키 바인딩(계약 외).** `Enter` 전송, `↑/↓` 히스토리(팔레트 열림 시 후보 선택), `Tab` 명령 완성, `Esc` → `session.interrupt`, `F1` 도움말 토글(터미널 escape 시퀀스로 감지), `Ctrl+O` 마지막 툴 출력 펼치기, `Ctrl+C` 종료. Ink 5에 텍스트 입력 컴포넌트가 없어 입력 줄은 `useInput`으로 직접 구현했다(번들 의존성 추가 회피).
8. **~~SDK 이슈~~ (해결됨).** 최초 `connect()` 실패 시 재연결 루프가 남아 프로세스가 종료되지 않던 문제는 US-007이 수정했다(`openSocket()`의 `everOpen` 플래그 + `connect()` 실패 시 자체 `close()`). TUI의 `process.exit` 우회는 제거했고, 시작 실패는 일반적인 `process.exitCode` 설정만으로 정상 종료한다.

## 14. Deviations (US-006, M1 CLI)

계약 §10과 플랜 §3.6을 구현하면서 생긴 국소 결정들.

1. **`system.info`에 `counters`·`lifecycle` 추가(가산적).** `snowpea daemon status`가 세션/잡/게이트웨이/네임드 에이전트 수와 유휴 종료 예정을 보여주려면 필요하다. `InfoResult`에 `counters: dict[str,int]`(기본 `{}`)와 `lifecycle: LifecycleStatus|null`(`willExit`, `reason`, `secondsUntilExit`)을 더했고, `LifecycleStatus`는 §13.8 `Lifecycle.status()`에서 카운터만 뺀 모양이다. 기존 필드는 그대로이므로 구버전 클라이언트는 영향을 받지 않는다.
2. **`-c`의 긴 이름은 `--prompt`.** argparse에서 `-c`만으로는 도움말이 불친절해 `-c/--prompt`로 묶었다. 계약이 정한 `-c "<prompt>"` 형태는 그대로 동작한다.
3. **`--home` 전역 플래그.** 테스트와 CI가 `SNOWPEA_HOME` 환경변수 없이도 홈을 고를 수 있도록 더했다. 지정하지 않으면 기존대로 `SNOWPEA_HOME` → `~/.snowpea` 순서다.
4. **`SNOWPEA_DAEMON_CMD` 탈출구.** `ensure_daemon`이 데몬을 띄울 때 쓰는 명령을 덮어쓴다(`shlex.split` 후 `--port 0 --home <home>`을 덧붙임). 기본값은 `sys.executable -m snowpea_core`. 테스트에서 기동 실패(→ 종료 코드 3)를 만들고, 스텁 데몬으로 CLI 경로를 검증하는 데 쓴다. 프로덕션 경로에는 영향이 없다.
5. **데몬 기동 실패의 조기 감지.** 스폰한 프로세스가 즉시 죽으면 15초를 기다리지 않고 바로 `DaemonError`(종료 코드 3)를 낸다. 표준 출력·오류는 `$SNOWPEA_HOME/logs/daemon.out`에 append 된다.
6. **승인 프롬프트는 stderr에 쓴다.** `--json`이 stdout을 JSON Lines 전용으로 쓰기 때문이다. 답은 stdin 한 줄(`y` / `a` / 그 외=거부)을 스레드에서 읽는다. `a`는 그 실행 동안의 모든 `approval.request`를 `{"decision":"allow","scope":"session"}`으로 답한다.
7. **종료 코드 결정 규칙.** 기본은 `turn.done.reason` → 코드(`complete`0 / `error`1 / `denied`4 / `interrupted`5 / `timeout`5). 다만 이번 턴에 거부가 있었으면(클라이언트가 deny로 답했거나 `error{mode_denied|approval_denied|approval_timeout}` 이벤트가 왔으면) 0·1은 4로 승격한다. RPC 에러는 `not_implemented`/`invalid_params`/`not_found`/`protocol_incompatible` → 2, `*_denied`/`approval_timeout` → 4, 나머지 → 1.
8. **`turn.done` 없이 연결이 끊기면 1.** 데몬이 턴 도중 죽은 경우다.
9. **`daemon stop`은 데몬이 없으면 0.** "멈춰 있음"이 요청의 목표 상태이므로 실패로 보지 않는다. 프로세스가 10초 안에 사라지지 않으면 1.
10. **`daemon status`는 데몬을 띄운다.** 조회만 하고 싶어도 `system.info`가 필요하므로 `ensure_daemon`을 거친다. 설치 스모크 테스트에서 이 동작이 오히려 유용하다.
11. **TUI 번들 탐색 순서.** `SNOWPEA_TUI_ENTRY`(`.tsx`면 `npx tsx`, 아니면 `node`) → 패키지 데이터 `snowpea_core/tui/dist/snowpea-tui.js` → 소스 체크아웃의 `tui/dist/snowpea-tui.js`. 셋 다 없으면 stderr에 빌드 방법을 안내하고 2로 끝낸다. TUI에는 `--port --token --cwd`를 항상 넘기고 `--mode`는 `--mode`가 주어졌을 때만 넘긴다(프로젝트 `defaultMode`가 이기도록).
12. **M2+ 자리표시자.** `setup`, `skill`, `service`, `agents`, `team`, `job`은 "not yet implemented"를 stderr에 찍고 2로 끝낸다.
13. **US-005 대기 테스트.** `tests/test_headless_exit_codes.py`의 세션·레지스트리 의존 케이스는 `not_implemented`를 만나면 건너뛴다. 폴링 예산은 `SNOWPEA_US005_WAIT_SEC`(기본 `0`)로, CI가 15분 멈추지 않도록 기본값을 0으로 두었다. 스토리가 요구한 15분 폴링은 `SNOWPEA_US005_WAIT_SEC=900`으로 얻는다.
14. **`--timeout` → 5 테스트는 보류.** `tests/fixtures/providers/fake/basic.json`에 턴을 지연시킬 단계가 없어 결정적으로 만들 수 없다. 스킵 사유에 적어 두었고, M3에서 스크립트 프로바이더에 `delaySec` 같은 필드가 생기면 켠다. 타임아웃 경로 자체는 스텁 데몬으로 수동 검증했다(`session.interrupt` 후 5).
15. **`tests/test_smoke.py`의 M0 자리표시자 테스트 교체.** `main([])`가 더 이상 `SystemExit(2)`가 아니라 데몬+TUI를 띄우므로, 사용법 오류(2)와 TUI 번들 부재(2) 두 케이스로 바꿨다.

## 15. Deviations (US-007, SDK)

생성기(`scripts/gen_protocol.py`)·SDK(`sdk/src`)·계약 테스트(`sdk/test`)를 구현하며 계약 대비 달라지거나 계약에 없어 새로 정한 점.

1. **`dump_schema()` 형태는 US-004와 합의한 §13-10 그대로다.** 다만 생성기는 방어적으로 정규화한다: `protocolVersion`/`version`, `methods`가 dict이든 list이든, `sessionEventKinds`가 `{kind: schema}`이든 이름 리스트 + 판별 유니온(`sessionEventPayloads`)이든 모두 같은 산출물을 낸다. `transport`가 없으면 `{"ws": "/ws", "http": {"health","version","schema"}}`를 기본값으로 쓴다. 스키마가 바뀌어도 생성기가 조용히 깨지지 않게 하기 위함이다.
2. **여러 메서드가 공유하는 `summary`는 버린다.** 공용 pydantic 모델(`EmptyParams` 등)의 docstring이 `params.description`을 거쳐 메서드 요약으로 새어 나온 적이 있다("A method that takes no parameters…"). 2개 이상의 메서드가 같은 요약을 가지면 그 요약은 메서드 고유 정보가 아니므로 생성기가 제거한다. US-004가 44개 메서드에 고유 summary를 넣은 뒤로는 발동하지 않지만, 같은 사고의 재발 방지 장치로 남긴다.
3. **`session.event.payload` 타입은 `kind` 판별자를 포함한다.** `sessionEventKinds[kind]` 스키마가 `kind` const 필드를 갖고 데몬이 모델 전체를 `payload`에 덤프하므로, 생성된 `SessionEventKindMap`은 TypeScript 판별 유니온으로 좁혀진다. `kind`는 optional(`kind?`)이라 데몬이 빼고 보내도 타입은 성립한다. 문서 표에서 description이 비는 유일한 필드가 이 13개 `kind`다(의도된 것).
4. **`--diff <refA> <refB>`는 커밋된 산출물을 비교한다.** 두 ref에서 `protocol.py`를 체크아웃해 재생성하는 대신 `git diff --stat refA refB -- docs/protocol.md sdk/src/protocol.ts`를 돌리고, 비어 있지 않으면 통계 + 전체 diff를 출력하고 exit 1. 프로토콜 freeze gate(AC-21)에서 "릴리스 사이에 프로토콜이 바뀌었는가"를 묻는 용도라 이것으로 충분하다.
5. **생성기 추가 플래그(계약 외).** `--schema <file>` (JSON 덤프에서 생성 — `protocol.py` 없이 생성기 자체를 테스트), `--print-schema` (정규화된 스키마 출력). 기본 동작·`--check`·`--diff`는 계약대로다.
6. **마크다운 표의 타입 칸에서는 JSDoc 주석을 제거한다.** 중첩 객체 타입은 필드 설명을 JSDoc으로 달고 인라인되는데, 한 줄짜리 표 칸에 들어가면 읽을 수 없다. 설명은 옆의 description 칸이 담당한다. TypeScript 산출물에는 JSDoc이 그대로 남는다.
7. **SDK 라이프사이클 이벤트.** 프로토콜 알림 외에 `reconnected{attempt, resumedSessions}`, `disconnected{code, reason, willRetry}`, `error{error}`를 `client.on()`으로 함께 노출한다.
8. **`session.resume` 재전송은 SDK가 `session.event`로 다시 뿌린다.** 재연결 시 추적 중인 각 세션에 대해 `session.resume(sessionId, afterSeq)`를 호출하고, 돌아온 이벤트를 `session.event` 리스너에 그대로 흘린 뒤 `reconnected`를 낸다. 세션 추적은 `session.create`/`session.resume` 결과에서 자동으로 시작되고 `session.close`에서 끝난다.
9. **호출 타임아웃(계약 외).** `call()`은 기본 30초, `system.hello`는 15초 뒤 reject 한다. `callTimeoutMs: 0`으로 끌 수 있다.
10. **계약 테스트의 fake 스크립트 위치.** 계약 §5는 `tests/fixtures/providers/fake/basic.json`을 예로 들지만, SDK 테스트는 자기 픽스처를 `sdk/test/fixtures/fake-basic.json`에 두고 `SNOWPEA_PROVIDER=fake:<abs path>`로 넘긴다. 내용 형식은 §5 그대로다.
11. **`--grep base` 3건은 모두 엄격하다.** US-005(에이전트 루프) 이전에는 테스트 2·3이 `not_implemented`를 만나면 `SKIP:` 사유를 출력하고 `this.skip()` 하도록 두었으나, US-005가 들어와 3건 모두 통과한 뒤 그 탈출구를 제거했다. 이제 어떤 메서드가 `not_implemented`로 퇴행하면 조용히 skip 되지 않고 실패한다. `--grep subagent`는 AC-15b 자리표시자 `it.skip` 1건이며 데몬을 띄우지 않는다.
12. **CI `sdk-contract` 잡에 uv를 추가했다.** 계약 테스트가 실제 데몬을 `uv run python -m snowpea_core`로 띄우므로 Node만으로는 돌지 않는다. `protocol-check` 잡의 명령은 그대로다.
13. **생성물은 커밋된다.** `sdk/src/protocol.ts`와 `docs/protocol.md`는 커밋되고 CI가 `--check`로 신선도를 검사한다. `protocol.py`(METHODS·EVENTS·모델·필드 설명 포함)를 고치면 `uv run python scripts/gen_protocol.py`를 돌려 두 산출물을 같은 변경에 포함시켜야 한다.

> 이 절은 US-006이 자기 절을 14번으로 붙이면서 한 번 통째로 사라졌다가 복구되었다. 이 문서에 절을 추가할 때는 마지막 절 번호를 먼저 확인할 것.

## 16. Deviations (US-005, sessions / agent loop / tools / permissions / commands)

1. **핸들러 위치.** §7의 RPC 핸들러들은 `server/app_server.py`가 아니라 `server/session_handlers.py`에 있다. `app_server.py`는 US-004·US-006과 동시 편집 중이라 충돌 면적을 줄였다. `build_dispatcher`는 `register_session_handlers(dispatcher)` 한 줄로 13개 메서드를 등록하고, `Daemon.start`는 `wire_core(core)` 한 줄로 `store`/`sessions`/`hub`/`approvals`/`providers`를 연결하고 내장 툴·명령을 등록한다. `protocol.IMPLEMENTED_METHODS`에 이 13개가 추가됐다.
2. **`ProviderInfo.authMethods` 추가.** 플랜 §2.8의 웹 로그인 2종(OpenAI `device_code`, OpenRouter `oauth_pkce`)을 `provider.list`가 실어 나르려면 필드가 필요하다. `authMethods: list[str] = ["api_key"]`를 `server/protocol.py`의 `ProviderInfo`에 더했다(추가 전용이라 기존 소비자는 영향 없음). 벤더 11종의 정적 목록은 `providers/presets.py`에 있다.
3. **`ExecutionBackend`는 파일 접근까지 포함한다.** 계약 §6은 `ToolContext.backend`만 정하고 모양은 열어 뒀다. `exec/backend.py`의 Protocol은 `run`·`read_file`·`write_file`·`list_dir`·`resolve`·`cwd`를 갖는다. fs 툴도 전부 이 백엔드를 거치므로 US-010의 docker/ssh 백엔드가 파일 툴까지 그대로 가져간다. M1 구현은 `exec/local.py`의 `LocalBackend` 하나.
4. **`diff` 이벤트의 `path`.** `ToolResult`에 계약에 없는 `path` 필드를 더해, 루프가 `diff{path, patch}`를 만들 때 툴이 실제로 건드린 경로를 쓴다(인자 파싱을 루프가 다시 하지 않기 위함).
5. **turn 종료 이유.** 승인 타임아웃도 `turn.done{reason:"denied"}`로 끝난다. 구분은 직전 `error` 이벤트의 코드(`approval_timeout` vs `approval_denied`)가 한다. `TurnReason`의 `"timeout"`은 M1에서 쓰이지 않는다.
6. **`session.interrupt`는 대기 중인 승인도 깨운다.** `ApprovalQueue.request(cancel_event=...)`가 세션의 `asyncio.Event`를 함께 기다린다. 인터럽트가 이기면 `error` 없이 곧장 `turn.done{interrupted}`가 나간다. 그렇지 않으면 승인 하나가 최대 `approvals.timeoutSec`(기본 300초) 동안 턴을 붙잡는다.
7. **대화형 승인의 이중 대기.** 원본 연결에는 `conn.call("approval.request", timeout=timeoutSec)`로 묻고, 바깥에서 `timeoutSec + 2초`를 더 기다린다(`GRACE_SECONDS`). 안쪽이 먼저 만료되어 `approval_timeout`을 확정하게 하기 위한 여유이며, 바깥 만료는 원본 태스크가 죽었을 때의 안전망이다.
8. **승인 scope 캐시.** `session`·`project`·`always`로 허용하면 `(sessionId, tool)` 조합이 세션 메모리에 캐시되어 다시 묻지 않는다. 세 scope는 M1에서 동작이 같고 `logs/approvals.jsonl`에는 요청된 scope 그대로 기록된다. 영속 allowlist는 M4.
9. **`unattended` 판정.** `session.prompt`는 세션에 `origin_conn`이 없을 때만 `unattended=True`로 턴을 돌린다. M1에서 WS 클라이언트가 만든 세션은 항상 대화형이다. 무인 승인 요청은 인증된 아무 연결이나 `approval.respond`로 답할 수 있고, 대화형 요청은 원본 연결만 답할 수 있다(그 외에는 `unauthorized`).
10. **프로바이더 인스턴스는 턴 단위.** `ProviderRegistry.get()`을 턴마다 호출한다. 스크립트 프로바이더의 "스텝 1회 소비" 의미가 턴 안에서만 유지되고 턴이 바뀌면 리셋된다 — 테스트 픽스처를 짤 때 전제.
11. **히스토리 압축은 자리만.** `History.compact()`는 `max_messages`(기본 200)를 넘으면 오래된 메시지를 버리되 tool 결과가 그 호출과 떨어지지 않게만 한다. 요약 압축은 메모리 작업과 함께 온다.
12. **`session.resume`의 원본 승계.** 원본 연결이 없거나 닫혔으면 resume 한 연결이 `origin_conn`·`originSurface`를 넘겨받는다. 재접속한 TUI가 승인 프롬프트를 다시 받을 수 있게 하기 위함이다.
13. **테스트 픽스처 추가.** `tests/fixtures/providers/fake/session.json` (write/edit/shell 시나리오). `basic.json`은 US-006이 쓰고 있어 건드리지 않았다.

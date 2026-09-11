# M1 Core Contract (binding for US-004 … US-008)

이 문서는 M1 수직 슬라이스를 병렬로 구현하는 에이전트들이 공유하는 **인터페이스 계약**이다. 여기 적힌 이름·시그니처·이벤트 형태를 바꾸려면 이 문서를 먼저 고친다. 세부 근거는 `.omc/plans/snowpea-agent-consensus-plan.md` §2, §3.

## 0. Runtime facts
- Python 3.11+, `asyncio` 단일 이벤트 루프, 패키지 `snowpea_core` (`core/snowpea_core`).
- 홈 디렉터리: `SNOWPEA_HOME` (기본 `~/.snowpea`). 파일: `daemon.json`, `token`(0600), `settings.json`, `state.db`, `logs/`.
- 프로젝트 설정: `<workdir>/.snowpea/settings.json` (`defaultMode`, `allowlist`, `backend`, `agents.max_concurrent` 오버라이드).
- 로깅: `logging.getLogger("snowpea.<module>")`, 파일 핸들러 `$SNOWPEA_HOME/logs/daemon.log`.

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

## Deviations

### TUI (US-008)
계약을 어기지는 않되, 계약서에 없던 클라이언트 국소 결정들:

1. **연결 상태 표시는 SDK 라이프사이클 이벤트에 의존한다.** `StatusLine`의 connected/reconnecting/closed는 프로토콜 알림이 아니라 `@snowpea/sdk`의 `disconnected{code, reason, willRetry}` / `reconnected{attempt, resumedSessions}`에서 파생된다. `session.resume(afterSeq)` 재전송은 SDK가 수행하고 TUI는 표시만 한다.
2. **중복 이벤트 방어.** `tui/src/rpc/client.ts`는 세션별 최고 `seq`를 추적해 그 이하의 `session.event`를 버린다. resume 재전송이 화면에 두 번 그려지는 것을 막기 위한 클라이언트 방어이며 서버 동작을 가정하지 않는다.
3. **`approval.list`는 조언적(advisory)이다.** 실패해도 세션을 막지 않는다. `ApprovalQueue`는 M1에서 읽기 전용(미처리 승인 표시만)이고, 큐에서 직접 응답하는 UI는 M4 allowlist 작업과 함께 온다. 대화형 승인(`approval.request`)만 `y`/`n` + scope로 promise를 resolve 한다.
4. **슬래시 라우팅은 클라이언트에서도 한 번 일어난다.** §9는 서버 `session.prompt` 핸들러도 `/`를 `command.run`으로 위임한다고 정하지만, TUI는 자동완성·도움말을 위해 이미 `command.list`를 갖고 있으므로 `/`로 시작하는 입력을 직접 `command.run`으로 보낸다. 명령 테이블은 하드코딩하지 않는다(`tui/src/slash/registry.ts`).
5. **TUI 바이너리 종료 코드.** `node dist/snowpea-tui.js` 는 인자 오류 `2`, 데몬 연결/세션 생성 실패 `1`, 정상 종료 `0`. §10의 CLI 종료 코드 체계와 같은 의미를 재사용한다.
6. **SDK 표면의 국소 타입 선언.** `tui/src/rpc/sdk.ts`가 §11의 `connect`/`Client` 모양을 구조적으로 선언하고 `@snowpea/sdk`를 지연 import 한다. SDK 빌드 산출물 유무와 무관하게 `tsc -p tui`와 번들이 성립하도록 하기 위한 것이며, 실제 SDK가 이 모양을 만족해야 한다.
7. **키 바인딩(계약 외).** `Enter` 전송, `↑/↓` 히스토리(팔레트 열림 시 후보 선택), `Tab` 명령 완성, `Esc` → `session.interrupt`, `F1` 도움말 토글(터미널 escape 시퀀스로 감지), `Ctrl+O` 마지막 툴 출력 펼치기, `Ctrl+C` 종료. Ink 5에 텍스트 입력 컴포넌트가 없어 입력 줄은 `useInput`으로 직접 구현했다(번들 의존성 추가 회피).

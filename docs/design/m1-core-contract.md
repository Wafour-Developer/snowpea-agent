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
PROTOCOL_VERSION = "1.4.0"          # semver; 기능이 늘 때마다 minor. v1.0 freeze gate는 별도
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
| session.list | includeClosed?: bool=false, workdir?: str, kinds?: list[str] | sessions: list[SessionSummary] |
| session.deleteSaved | sessionId?, workdir?, all?: bool=false | deleted: int |
| session.close | sessionId | ok |
| session.prompt | sessionId, text, attachments?: list[Attachment] | turnId |
| session.interrupt | sessionId | ok |
| session.setMode | sessionId, mode | mode |
| session.setModel | sessionId, model? | provider, model, pinned |
| command.list | sessionId? | commands: list[CommandInfo{name, summary, argsSchema, source}] |
| command.run | sessionId, name, args: str | turnId |
| tool.list | sessionId? | tools: list[ToolInfo{name, category, permissionTag, state, source, description}] |
| approval.list | sessionId? | requests: list[ApprovalRequest] |
| approval.respond | requestId, decision: "allow"\|"deny", scope: "once"\|"session"\|"project"\|"always" | ok |
| permission.allowlist.add / list / remove | pattern, scope / scope? / patternId | patternId / patterns / ok |
| provider.list / provider.configure / provider.loginWeb | – / vendor, config / vendor, method | providers / ok / ok |
| backend.set | sessionId, kind: "local"\|"docker"\|"ssh", config: dict | ok |
| agent.*, team.*, job.*, gateway.*, memory.*, skill.* | 플랜 §3.5 그대로 (M1은 스키마만 정의, 구현은 `error{code:"not_implemented"}`) | |

`PROTOCOL_VERSION`은 현재 `1.4.0`이다 (`server/protocol.py`의 `PROTOCOL_VERSION`). 위 코드 블록이 M1 시점에 적어 둔 `"0.1.0"`과 M8의 `1.0.0` 계획은 모두 폐기되었다 — 프로토콜은 추가 변경마다 minor를 올려 왔고(1.0.0 → 1.1.0 update → 1.2.0 context/models/login → 1.3.0 `config` 권한 태그 → 1.4.0 `turn.queued`/`turn.dequeued`/`model.changed`와 `session.setModel`), v1.0 freeze gate는 v0.2 IDE 이전에 별도로 잡는다.

`SessionSummary`에는 v0.2에서 네 필드가 더 붙었다(가산적, 프로토콜 `1.5.0` 유지 — CORE-session-kind): `kind: "chat"|"scheduled"|"subagent"|"agent"` (기본 `"chat"`), `parentSessionId: str|None`, `jobId: str|None`, `agent: str|None`. 스케줄러가 만든 무인 세션(`origin_surface="scheduler"`)이 `session.list`에서 사용자의 스레드와 구분되지 않던 문제를 고친다 — 예약 실행은 `kind="scheduled"`에 `parentSessionId = job.originSessionId`, `jobId = job.id`를, 스폰된 자식은 `kind="subagent"`에 부모 세션 id를, 상주 named agent는 `kind="agent"`를 갖는다. 세 값은 `sessions` 테이블의 `parent_session_id`/`kind`/`job_id` 컬럼에 저장되며, 예전 `state.db`는 열릴 때 `ALTER TABLE … ADD COLUMN`으로 멱등하게 이관되고 기존 행은 `chat`/NULL로 읽힌다 (`session/store.py`). `session.list`는 `kinds?: list[str]` 필터를 받는다(생략하면 전부). 예약 실행이 끝나면 그 작업을 만든 **원래 세션**(살아 있을 때)에 `job.done` / `job.failed` `session.event`가 추가로 전달되어 `{jobId, sessionId, status, text}`로 "예약 실행이 끝났다 — s-xxxx 열기"를 그릴 수 있다; 모든 클라이언트가 받는 `job.event` 알림은 그대로다.

`SessionSummary`는 계약 이후 세 필드가 추가되었다(모두 가산적): `contextUsed`, `contextWindow` (CORE-context), `lastPrompt: str|None` — 그 세션에 마지막으로 저장된 **user** 메시지의 텍스트 (`server/protocol.py`의 `SessionSummary`). `session.list` 결과는 `createdAt` **내림차순**으로 정렬된다 (`server/session_handlers.py`의 `session_list_handler`). (v0.1.x에서 추가)

서버→클라이언트 요청: `approval.request(requestId, sessionId, tool, args, risk, timeoutSec, scopeHint) -> {decision, scope}`.

알림: `session.event(sessionId, seq, kind, payload, ts)`; `approval.resolved(requestId, decision, by)`; `job.event`; `gateway.event`.

`session.event.kind` ∈ `message.delta{text}` · `message.done{text, role}` · `tool.call{callId, name, args}` · `tool.result{callId, name, ok, output, error?}` · `tool.progress{callId, name, stream, chunk, seq, truncated}` · `diff{path, patch}` · `subagent.spawn/update/done{agentId, ...}` · `team.task.update` · `mode.changed{mode}` · `backend.changed{kind}` · `model.changed{provider, model}` · `usage{inputTokens, outputTokens}` · `context{used, window, estimated, …}` · `compaction.started{reason, before}` · `compaction{…}` · `audio.spoken{…}` · `turn.started{turnId, prompt, queued}` · `turn.queued{turnId, position, queued}` · `turn.dequeued{turnId, reason: "started"|"dropped", queued}` · `error{code, message}` · `turn.done{turnId, reason: "complete"|"interrupted"|"error"|"denied"|"timeout"|"budget"}`(`"budget"`은 v0.2 추가 — 도구 라운드 예산을 다 쓰고 보고한 뒤 끝난 턴, CORE-subagent-budget). v0.1.x에서 `context`·`compaction`·`audio.spoken`·`turn.queued`·`turn.dequeued`·`backend.changed`·`model.changed`가, v0.2에서 `turn.started`·`tool.progress`·`compaction.started` 세 종류가 **추가로** 붙었다(IDE-PROGRESS; 프로토콜 `1.5.0` 유지, additive). `turn.started`는 턴이 실제로 돌기 시작하는 순간(큐 대기가 끝난 뒤, 첫 `message.delta` 앞) 정확히 한 번 나가고, `tool.progress`는 실행 중인 도구의 출력 꼬리를 흘려보낸다 — **권고적이며 `tool.result`가 여전히 정본이다**. 현재 목록의 정본은 `server/protocol.py`의 `SESSION_EVENT_MODELS`다.

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
              # v0.1.x에서 추가된 필드 (session/session.py): team, team_agents, queued_turns,
              # turn_task, current_turn, context_used, context_estimated, context_window,
              # backend, allowed_tools, is_subagent, prompt_role, system_prompt, memory_namespace
class SessionManager:
    async def create(...)->Session; get(id); list(); async close(id)
    async def restore(id, *, origin_conn=None) -> Session|None   # v0.1.x: store에서 되살림 (§17-3)
    async def close_all(*, timeout=5.0) -> None                  # Daemon.stop이 호출
    def next_seq(session)->int
class EventHub:
    async def emit(session_id, kind, payload)      # seq 부여 → store 저장 → 구독 연결에 notify
    def subscribe(conn, session_id|None)
```
이벤트는 `store`에 append 되어 `session.resume(afterSeq)`가 재전송한다. 히스토리도 매 턴 `finish_turn()`에서 `store.replace_messages()`로 증분 저장되므로, 데몬을 재시작한 뒤에도 `session.resume`이 대화를 복원한다(`agent/loop.py`의 `finish_turn`). (v0.1.x에서 추가)

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
| mode \ tag | read | write | exec | network | send | config |
|---|---|---|---|---|---|---|
| plan | allow | deny | deny | allow | deny | deny |
| accept | allow | allow | ask | ask | ask | ask |
| auto | allow | allow | allow | allow | allow | **ask** |

`config`는 CORE-search-fix가 더한 여섯 번째 태그다(이 변경이 `PROTOCOL_VERSION`을 1.3.0으로 올렸다). auto 모드에서도 `ask`인 것이 의도된 부분이다: 감시자가 없는 에이전트가 `settings.json`을 고쳐 쓰는 것이 고치려던 실패이므로, "아무도 안 본다"는 건너뛸 이유가 아니라 물어볼 이유다. 태그는 호출마다 `Tool.permission_for` → `tools/config_guard.py`가 해석한다: 해석된 경로가 `$SNOWPEA_HOME` 아래이거나 프로젝트의 `.snowpea/settings.json`·`.snowpea/credentials.json`이면 `config`, 아니면 `write`다(`.snowpea/worktrees/` 같은 작업 상태는 `write`로 남는다). allowlist(M4)는 `ask`→`allow`로 **승격만** 하되 `UNPROMOTABLE` 태그(=`config`)는 건너뛰며(`permissions/policy.py`), 승인 scope 캐시도 `cacheable=False`로 비활성이다(`agent/loop.py`가 `cacheable=tag not in UNPROMOTABLE`로 넘긴다). (v0.1.x에서 추가)
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
  tool_call  → policy.decide → deny: event error{mode_denied} + 거부 사유를 실패한 tool.result로 append → 루프 계속
                             → ask: approvals.request → deny: 같은 처리
                             → allow: event tool.call → tool.run → event tool.result (+diff) → 메시지에 tool 결과 추가 → 다시 provider.stream
  done(no tool calls) → message.done → (auto-speak 시 audio.spoken) → context → turn.done{complete}
```
최대 반복은 `agent/loop.py`의 `tool_rounds_for()`가 정한다(CORE-subagent-budget). 높은 순서대로 ① 에이전트 정의의 `tool_rounds:` 프론트매터, ② `agents.toolRounds[<에이전트 이름>]`(매핑일 때), ③ `agents.toolRounds`(숫자이거나 매핑의 `"default"`/`"*"` 키), ④ `agent.max_tool_rounds`(기본 200)이며, 위임된 자식 세션은 ④에 한해 `SUBAGENT_TOOL_ROUNDS = 80`을 하한으로 받는다.

**한도에 닿아도 턴은 절대 조용히 끝나지 않는다**(v0.2, CORE-subagent-budget). 한도에 닿는 즉시 **도구를 끈 채로** provider를 한 번 더 불러 "무엇을 했고, 무엇을 찾았고, 무엇이 남았고, 어떤 파일을 고쳤는지" 보고하게 하고, 그 답을 history에 넣고 `message.done`으로 낸다. 그 다음에야 보고 있는 사람에게 계속할지 묻고(`unattended`도 `is_subagent`도 아닐 때만), 묻지 않거나 "멈춤"이면 `turn.done{reason:"budget"}`으로 끝난다 — 예전의 `error{internal, "stopped after N tool rounds"}` 이벤트는 더 이상 나가지 않는다. 계속을 고르면 `BUDGET_CONTINUE_INSTRUCTION`이 user 메시지로 붙고 예산이 다시 채워진다. 보고 요청 자체는 history에 남지 않는다(그 턴의 마지막 user 메시지가 바뀌면 언어 감지와 체크포인트 질문이 함께 흔들린다).

**거부 한 번이 턴을 끝내지 않는다**(v0.1.x에서 변경): 거부는 tool 결과로 모델에 돌아가 모델이 이름·인자를 고칠 수 있고, 한 턴에 `MAX_DENIALS_PER_TURN = 3`(`agent/loop.py`) 번째 거부에서야 `turn.done{denied}`로 끝난다. `mode_denied`/`approval_denied` error 이벤트는 그대로이므로 어떤 surface도 새로 배울 것이 없다.

모든 턴 종료는 `agent/loop.py`의 `finish_turn()` 하나를 거친다. 순서는 **history 영속화 → `context` 이벤트 → `turn.done`** 이며, `Core.stopping` 중에는 앞의 둘을 건너뛴다(CORE-session-race). `turn.done`은 여전히 terminal이다.

**턴은 데몬이 죽어도 반드시 닫힌다**(v0.2, CORE-dangling-turns). 위의 "`Core.stopping` 중에는 건너뛴다"는 규칙 때문에, 턴이 도는 중에 데몬이 멈추면 이벤트 로그에 `turn.started`만 남고 `turn.done`이 없었다 — 히스토리를 리플레이하는 모든 surface(`session.resume`, IDE hydrate)가 영원히 "생각 중"인 턴을 그렸다. 이제 두 지점에서 닫는다. (1) **정상 종료**: `Daemon.stop`이 `core.stopping = True` 직후, 아직 store와 hub가 살아 있을 때 `SessionManager.finish_open_turns()`로 진행 중인 턴마다 `turn.done{reason:"interrupted", synthetic:true}`를 내보내고 영속화한다. 턴 id를 세션에서 먼저 걷어내므로 뒤이은 취소 경로와 겹쳐 두 번 쓰이지 않는다. (2) **크래시·`kill -9`**: 데몬이 뜰 때 `Store.repair_dangling_turns()`가 `turn.started`/`turn.done`을 **turnId로 짝지어** 짝 없는 턴마다 같은 synthetic 이벤트를 다음 seq로 덧붙인다(마지막 마커만 보지 않는 이유는 §17-4의 버려진 프롬프트가 실행 중인 턴보다 뒤에 `turn.done`을 쓰기 때문이다). 스캔 이후에 생긴 행은 `SessionManager.restore`가 그 세션 하나만 다시 수선한다. 두 경로 모두 멱등이다. `TurnDone`에는 `synthetic: bool = false`가 추가됐고(가산적, 프로토콜 `1.5.0` 유지), `SessionSummary`에는 `running: bool`이 붙어 **지금 정말 턴이 도는지**를 데몬이 직접 말한다 — 저장된 행은 항상 `false`이므로 클라이언트가 리플레이의 마지막 이벤트로 추측할 필요가 없다.

`session.interrupt`는 진행 중인 턴을 취소하고(`turn.done{interrupted}`), 그 턴이 끝나면 뒤에 쌓여 있던 큐도 비운다(§17-4).

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
7. **종료 코드 결정 규칙.** 기본은 `turn.done.reason` → 코드(`complete`0 / `error`1 / `budget`1 / `denied`4 / `interrupted`5 / `timeout`5). 다만 이번 턴에 거부가 있었으면(클라이언트가 deny로 답했거나 `error{mode_denied|approval_denied|approval_timeout}` 이벤트가 왔으면) 0·1은 4로 승격한다. RPC 에러는 `not_implemented`/`invalid_params`/`not_found`/`protocol_incompatible` → 2, `*_denied`/`approval_timeout` → 4, 나머지 → 1.
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

## 17. Post-v0.1.1 amendments (session listing, deletion, restore, prompt queueing, teams)

계약 §1·§4를 HEAD(v0.1.7) 기준으로 맞추는 절. 여기 적힌 것이 구현이다.

1. **`session.list`는 파라미터를 받는다.** `SessionListParams{includeClosed: bool=false, workdir: str|None}` (`server/protocol.py`). `includeClosed=false`(기본)는 살아 있는 세션만, `true`는 `store.list_sessions(include_closed=True)`의 영속 행을 live 행에 병합한다(같은 id는 live가 이긴다). `workdir`이 주어지면 그 디렉터리에 뿌리내린 행만 남긴다. `EmptyParams`를 보내던 구버전 클라이언트는 두 필드가 모두 기본값이라 영향이 없다. (`server/session_handlers.py`의 `session_list_handler`)

2. **`SessionSummary.lastPrompt`.** store가 있으면 모든 행에 대해 `store.messages(sessionId)`를 뒤에서부터 훑어 마지막 `role == "user"` 메시지의 텍스트를 채운다. store가 없으면 `None`이다. 정렬은 `createdAt` 내림차순.

3. **`session.deleteSaved` (신규 RPC).** `SessionDeleteParams{sessionId?, workdir?, all: bool=false}` → `SessionDeleteResult{deleted: int}` (`server/protocol.py`; `METHODS`와 `IMPLEMENTED_METHODS` 양쪽에 등록). 핸들러는 먼저 `core.sessions.list()`의 live id 집합을 빼므로 **살아 있는 세션은 절대 지우지 않는다**(`session_handlers.py`의 `session_delete_saved_handler`). 셋 중 아무것도 주지 않으면 아무것도 지우지 않는다. `Store.delete_sessions`는 `messages`·`events`·`sessions` 세 테이블의 행을 한 트랜잭션으로 지운다(`session/store.py`). 삭제된 세션이 디스크에 갖고 있던 바이트(`<SNOWPEA_HOME>/attachments/<id>/`, `<SNOWPEA_HOME>/audio/<id>/`)도 `_purge_session_files`가 best-effort로 함께 지운다 — 파일 삭제 실패는 경고 로그일 뿐 RPC를 실패시키지 않는다.

   `deleted`는 실제로 지워진 `sessions` 행 수다 (`session/store.py`의 `delete_sessions`가 `DELETE` 커서의 `rowcount`를 돌려준다). 삭제는 DB 행에서 끝나지 않는다: `session_delete_saved_handler`가 `<home>/attachments/<id>/`와 `<home>/audio/<id>/`도 함께 지운다 (best-effort — 파일이 안 지워져도 RPC는 실패하지 않는다). (CORE-fixes-v017 R4/R6)

4. **`session.resume`의 의미가 넓어졌다.** 계약 §4는 살아 있는 세션의 이벤트 재전송만 정했다. 이제 데몬을 재시작해 메모리에서 사라진 세션도 `SessionManager.restore()`가 store에서 되살린다(`session/manager.py`): `sessions` 행에서 workdir·mode·provider·model을 읽고, `store.messages()`로 `History`를 재구성하고, workdir의 활성 팀을 다시 계산하고, `store.reopen_session()`으로 `closed_at`을 지운다. 이것이 가능한 이유는 `finish_turn()`이 매 턴 `store.replace_messages()`로 히스토리를 증분 영속화하기 때문이다. 원본 연결 승계(§16-12)는 그대로다.

5. **프롬프트 큐잉.** 턴이 도는 중에 온 `session.prompt`는 거부되지도, 동시에 실행되지도 않는다. `start_turn`은 항상 `QueuedTurn{turn_id, text, unattended, attachments}`를 만들고, `session.turn_task`가 아직 살아 있으면 `Session.queued_turns`(`session/session.py`, **메모리 전용**)에 넣고 `turn.queued{turnId, position, queued}` 이벤트를 낸 뒤 `turnId`만 돌려준다(`agent/loop.py`의 `start_turn`). 하나의 `_drain_turns` 태스크가 FIFO로 소비하며, 큐에서 꺼낼 때마다 `turn.dequeued{turnId, reason:"started", queued}`를 낸다. 첨부는 큐에 넣는 시점에 동기적으로 `pending.take()` 되므로 뒤 프롬프트의 이미지가 앞 턴으로 새지 않는다.

6. **인터럽트는 큐까지 비운다.** `session.interrupt`는 `session.interrupt`를 set 하고 **그 자리에서** `flush_queued_turns()`를 돌려 대기 중이던 프롬프트를 전부 버린다(`server/session_handlers.py`의 `session_interrupt_handler`). 비우기는 첫 `await` 전에 동기적으로 일어나므로, Stop을 누른 그 순간 큐에 있던 것만 버려지고 그 직후에 새로 친 프롬프트는 살아남아 그대로 실행된다(`_drain_turns`는 턴이 끝난 뒤 큐를 다시 비우지 않는다). 버려진 프롬프트마다 `turn.dequeued{reason:"dropped"}`와 `turn.done{interrupted}`가 나가므로, 그 `turnId`를 기다리던 클라이언트가 영영 매달리지 않는다(`agent/loop.py`의 `flush_queued_turns`).

   **알려진 한계.** `queued_turns`는 영속화되지 않는다. 데몬이 재시작되면 대기 중이던 프롬프트는 사라지고, 클라이언트는 이미 받은 `turnId`에 대한 `turn.done`을 받지 못한다.

6-1. **프로젝트 지시 파일**(v0.2, CORE-context-files). 탐색·캡·절단 의미론은 Hermes `agent/prompt_builder.py`에서 **포팅**했다(MIT, Nous Research — 벤더링이 아니라 재구현이며 근거와 차이는 `docs/design/deviations/CORE-context-files.md`). `prompts/environment.py`의 `build_project_context`가 **먼저 맞는 한 종류만** 읽는다: `.snowpea/instructions.md`·`SNOWPEA.md`(git 루트까지 올라가며 가장 가까운 것) → **`AGENTS.md` 체인**(git 루트 → workdir, 디렉터리마다 `AGENTS.override.md`·`AGENTS.md`·`agents.md` 중 첫 번째, 동일 내용은 중복 제거, 각 섹션은 workdir 기준 상대경로로 출처 표시) → `CLAUDE.md`/`claude.md`(workdir만) → `.cursorrules` + `.cursor/rules/*.mdc`(workdir만). `.git` 조상이 없으면 체인은 workdir 하나뿐이다 — `$HOME`이나 `/tmp`에 놓인 파일이 프롬프트 권위를 얻어선 안 된다. **캡**은 `settings.agent.contextFileMaxChars`가 있으면 그 값, 없으면 세션 컨텍스트 윈도우에서 `clamp(window×4×0.06, 20_000, 500_000)`(윈도우를 모르면 20_000)이고, 합쳐진 블록에도 같은 수를 한 번 더 적용한다. 초과분은 **앞 70% + 뒤 20%**를 남기고 가운데에 "`read_file`로 전체를 읽으라"는 마커를 넣으며, 같은 경고가 블록 끝에 `Note:` 한 줄로 한 번 더 나간다. 블록 머리는 `# Project Context`다. `settings.agent.ignoreContextFiles`는 전체를 끈다(Hermes `--ignore-rules`).

   **Hermes에 없는 추가**: 세션은 평생 저장소 루트에 앉아 있으므로 체인만으로는 `/deepinit`이 쓴 `src/AGENTS.md`에 닿지 않는다. 그래서 체인 뒤에 workdir 아래의 중첩 `AGENTS.md`(깊이 ≤ 4, 최대 40개, `.git`·`node_modules`·`.venv`·`dist`·`build`·`__pycache__`·숨김 디렉터리 제외)를 **합산 예산이 허락하는 만큼 미리** 같은 블록에 싣는다 — 새 세션·`session.resume`·같은 workdir의 subagent 모두 처음부터 계층 전체를 갖는다. 못 실은 것은 `Nested instructions not loaded (read_file when you work there): …`로 이름만 알리고, `agent/context_files.py`가 그 디렉터리를 건드리는 첫 툴 결과(`read_file`·`write_file`·`edit_file`·`list_dir`·`glob`·`grep`, `cwd`가 있거나 `cd <dir>`로 시작하는 `shell`)에 `<context file="src/AGENTS.md">…</context>`로 덧붙인다 — LSP `Diagnostics` 블록과 같은 자리다. `Session.loaded_context_files`(프롬프트에 이미 인용된 것)와 `Session.seen_context_files`(툴 결과로 이미 붙은 것) 두 집합 덕분에 같은 파일이 두 번 나가지 않는다. **무효화**: 깊이에 상관없이 지시 파일을 `write_file`/`edit_file` 하면 환경 블록 캐시 전체를 비우고(`/init`·`/deepinit`은 **subagent** 세션에서 쓰므로 부모의 캐시까지 비워야 한다) 그 경로의 두 표시를 지운다. `/init`·`/deepinit`·`/skill`은 끝날 때 `CommandRegistry.run`이 한 번 더 비운다.

7. **`Session.team` / `Session.team_agents`.** `session.create`와 `SessionManager.restore`가 `agent/team_config.active_team(settings, workdir)`으로 채운다. 둘이 채워져 있고 세션이 subagent가 아니면 시스템 프롬프트에 팀 제한 규칙이 덧붙는다(`agent/agent.py`):

   > `Active delegation team: <name>. Delegate only to these agents: <a, b, c>. Every delegate_task call must include one of those names in its agent field.`

   규칙은 조언이 아니라 강제다 — 실제 거부는 M6/M7 계약 §3.1을 볼 것.

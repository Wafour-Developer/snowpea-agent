# 프로토콜

전체 생성 레퍼런스는 [docs/protocol.md](../../protocol.md)입니다. 이 페이지는 그걸 읽기 전에 먼저 봐 둘 만한 길잡이입니다.

코어가 할 수 있는 모든 일은 UI 기능이 되기 전에 먼저 JSON-RPC 메서드입니다. 데몬과 터미널 UI 사이에 뒷문 같은 전용 채널은 없습니다 — TUI도 그냥 평범한 클라이언트 하나일 뿐이고, TUI가 할 수 있는 일이면 내가 만든 클라이언트도 똑같이 할 수 있습니다.

## 진실의 원천

`core/snowpea_core/server/protocol.py`가 `PROTOCOL_VERSION`과 모든 메서드의 파라미터·결과 스키마, 모든 이벤트 payload를 정의합니다. `scripts/gen_protocol.py`가 여기서 `docs/protocol.md`와 `sdk/src/protocol.ts`를 생성합니다. 이 두 생성 파일은 절대 손으로 고치지 않으며, 어긋나면 CI가 빌드를 막습니다:

```bash
uv run python scripts/gen_protocol.py --check
```

## 전송

데몬은 기동 시 선택한 루프백 포트를 인증 토큰과 함께 `$SNOWPEA_HOME/daemon.json`에 기록합니다.

| 표면 | 엔드포인트 | 용도 |
|---|---|---|
| WebSocket | `ws://127.0.0.1:<port>/ws` | JSON-RPC 2.0, 양방향 |
| HTTP GET | `/health` | 생존 확인 |
| HTTP GET | `/version` | 서버·프로토콜 버전 |
| HTTP GET | `/protocol.json` | 전체 스키마를 JSON으로 |

HTTP 엔드포인트는 프로브, 설치 스크립트, 디버깅을 위한 읽기 전용 편의 기능입니다. 상태를 바꾸는 호출은 전부 WebSocket에만 있습니다.

## 핸드셰이크

연결 직후 클라이언트는 `$SNOWPEA_HOME/token`의 토큰, 자기 버전, 빌드 시점의 프로토콜 버전을 실어 `system.hello`를 호출합니다. 그 전에 다른 메서드를 호출하면 `unauthorized`로 실패합니다. major 버전이 어긋나면 `protocol_incompatible`로 거부됩니다.

```json
{"jsonrpc":"2.0","id":1,"method":"system.hello",
 "params":{"token":"…","clientVersion":"0.1.0","protocolVersion":"0.1.0"}}
```

결과에는 `protocolVersion`, `serverVersion`, `capabilities` 목록이 실려 오는데, 버전이 고정된 뒤 기능을 협상하는 방식이 바로 이것입니다.

## 영역별 메서드

| 영역 | 메서드 |
|---|---|
| system | `system.hello`, `system.info`, `system.health`, `system.shutdown` |
| session | `session.create`, `session.list`, `session.resume`, `session.close`, `session.prompt`, `session.interrupt`, `session.compact`, `session.deleteSaved`, `session.setMode`, `session.setModel` |
| commands and tools | `command.list`, `command.run`, `tool.list` |
| approvals | `approval.list`, `approval.respond`, `permission.allowlist.add` / `list` / `remove` |
| providers | `provider.list`, `provider.configure`, `provider.loginWeb` |
| agents | `agent.list`, `agent.create`, `agent.spawn`, `agent.bindChannel`, `agent.delete` |
| team | `team.start`, `team.status` |
| jobs | `job.schedule`, `job.list`, `job.cancel`, `job.runNow` |
| gateway | `gateway.bind`, `gateway.list`, `gateway.unbind` |
| memory | `memory.search`, `memory.write` |
| skills | `skill.search`, `skill.install`, `skill.list`, `skill.remove`, `skill.reload` |
| backend | `backend.set` |

방향이 반대인 메서드가 하나 있습니다. `approval.request`는 알림이 아니라 서버가 클라이언트에게 보내는 *요청*입니다 — 데몬이 클라이언트에게 결정을 묻고 답을 기다립니다. 프로토콜이 HTTP에 스트림을 얹은 형태가 아니라 WebSocket JSON-RPC인 이유가 바로 이 양방향성입니다.

## 이벤트

알림은 데몬에서 구독 중인 클라이언트로 흘러갑니다.

- `session.event(sessionId, seq, kind, payload, ts)`는 한 턴 안에서 일어나는 모든 일을 담습니다. 종류는 `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `backend.changed`, `model.changed`, `usage`, `context`, `compaction`, `audio.spoken`, `error`, `turn.queued`, `turn.dequeued`, `turn.done`입니다.
- 무인 승인 큐를 위한 `approval.pending`과 `approval.resolved`.
- `job.event`와 `gateway.event`.
- 스킬이나 플러그인을 리로드한 뒤의 `commands.changed`.

모든 `session.event`는 해당 세션 안에서 단조 증가하는 `seq`를 싣고 있습니다. 연결이 끊긴 클라이언트는 재연결한 뒤 `session.resume(sessionId, afterSeq)`를 호출해 놓친 부분만 정확히 다시 받습니다. 이벤트가 그냥 방송되는 게 아니라 영속되는 이유가 이것입니다.

## 오류

오류는 `error.data.code`에 문자열 코드가 담긴 JSON-RPC 오류입니다: `unauthorized`, `protocol_incompatible`, `not_found`, `invalid_params`, `mode_denied`, `approval_denied`, `approval_timeout`, `tool_inactive`, `not_implemented`, `login_unsupported`, `search_provider_unavailable`, `browser_provider_unavailable`, `internal`.

## SDK 쓰기

```ts
import { connect } from "@snowpea/sdk";

const client = await connect({ port, token, clientVersion: "1.0.0" });
const { sessionId } = await client.call("session.create", { workdir: process.cwd(), mode: "accept" });

client.on("session.event", (e) => {
  if (e.kind === "message.delta") process.stdout.write(e.payload.text);
});

client.onRequest("approval.request", async (req) => ({ decision: "deny", scope: "once" }));

await client.call("session.prompt", { sessionId, text: "what does this repo do?" });
```

클라이언트는 알아서 재연결하고 마지막으로 본 `seq`부터 재개합니다. `sdk/src/protocol.ts`가 모든 메서드와 이벤트의 타입을 제공합니다. 이 파일은 생성된 것이라, 데몬에 없는 메서드를 담고 있을 수는 없습니다.

## 버전 관리와 동결 게이트

`PROTOCOL_VERSION`은 semver를 따릅니다. v0.2 데스크톱 IDE가 시작되기 전에 프로토콜은 동결 게이트를 통과해야 합니다 — 버전 `1.0.0`, 그리고 연속 세 릴리즈 태그 동안 `docs/protocol.md`나 생성 스키마에 변경이 없고, 세 태그 모두에서 SDK 계약 테스트가 통과해야 합니다.

```bash
uv run python scripts/gen_protocol.py --check
git log --oneline -- docs/protocol.md sdk/src/protocol.ts
```

동결 이후의 추가 변경은 minor 버전을 올리고 `system.hello`의 `capabilities` 협상을 거쳐야 합니다. major 변경은 모든 클라이언트를 동시에 릴리즈해야 한다는 뜻이고, 그 비용을 눈에 보이게 만드는 것이 바로 이 게이트의 존재 이유입니다.

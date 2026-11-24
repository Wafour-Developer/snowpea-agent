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

클라이언트는 `locale`도 함께 보내는 것이 좋습니다(프로토콜 1.65.0, capability `clientLocale`). 사용자의 언어를 `ko`, `ko-KR`, `ja`, `es-ES`, `zh-CN` 같은 BCP 47 태그로 보내면, 검증 문제와 거절 메시지(`settings.set`의 문제, `security.get`의 문제, 브라우저가 보여 주는 사람 전용 거절)가 그 언어로 돌아옵니다. 영어, 한국어, 일본어, 스페인어, 중국어 간체를 지원하며, 지역 태그는 언어로, 그 밖의 값은 영어로 대체됩니다. 메서드에 자체 `locale` 매개변수가 있으면 그 호출에서는 그것이 우선합니다. 태그는 정규화되고(`ko_kr` → `ko-KR`), 언어 태그가 아닌 값은 거부하지 않고 무시하므로 오래되었거나 특이한 클라이언트도 접속할 수 있습니다. 번역되는 것은 사람이 읽는 문장뿐이며 `details.reason`과 `problemItems[].reason`/`code`는 어느 언어에서나 같습니다. TUI는 `LC_ALL`, `LC_MESSAGES`, `LANG` 값을 보내고, SDK의 `connect({locale})`는 받은 값을 그대로 보내며, 이 필드를 거부하는 1.65.0 미만 데몬에는 이 필드 없이 다시 hello를 보냅니다.

프로토콜 1.66.0(capability `securityDefaultOff`)부터 보안 검사의 기본값은 꺼짐입니다. 사용자가 켜기 전까지(`settings.set {scope: 'global', patch: {security: {enabled: true}}}`) `security.get` 은 `enabled: false`, `byDefault: true` 를 돌려줍니다. 이 상태는 보안 설정 화면에서만 담담하게 보여 주세요. 검사가 꺼져 있으면 데몬의 토큰과 저장된 로그인을 읽는 것도 막지 않고 모드 규칙을 따릅니다. 1.65.0 이하의 클라이언트를 위해 `problems` 는 여전히 담담한 한 줄(문제 코드 `security-disabled`)로 시작합니다.

프로토콜 1.68.0(capability `jobApprovals`)부터 예약 루틴의 승인 요청이 사람에게 닿습니다. 요청에는 `ApprovalRequest.job {id, title}`이 붙고, 모든 화면에 `approval.pending`으로, 연결된 메신저의 개인 대화로 가며, `job.event` `approval_needed`(payload `{requestId, sessionId, tool, summary, title, timeoutSec}`)로 알려지고, `approvals.timeoutSec`이 아니라 설정 `scheduler.approvalTimeoutSec`(기본 900초) 또는 작업의 `approvalTimeoutSec`만큼 기다립니다. 카드는 scope `task`("이 루틴에서 항상 허용")로 답할 수 있으며, 그 호출의 형태가 작업 id에 묶여 이후 모든 실행에 적용됩니다. 미리 허용할 수도 있습니다: `job.schedule`은 `grants`와 `approvalTimeoutSec`을, `job.update`는 `mode`, `addGrants`, `removeGrants`, `approvalTimeoutSec`을 받습니다(grant는 `{tool, pattern}`, `{tool, line}` 또는 셸이 아닌 `{tool}`; 모든 것을 허용하는 패턴은 거절; 로컬 사람의 연결, 또는 `actor: "person"`과 `personConfirm: "window"`를 보낸 로컬 브라우저 0.4.0+만 추가 가능). `JobInfo`에는 `lastError`(응답 없는 요청이면 "approval needed: <tool> `<command>` …"), `lastSessionId`, `waitingApproval`, `grants`, `approvalTimeoutSec`이 추가됩니다.

프로토콜 1.67.0(capability `externalModelSwitch`)부터 외부 CLI 세션의 모델을 바꿀 수 있습니다. `external.setModel {ref, model}`(그 CLI의 `external.clis` `models` 중 하나, 또는 CLI 기본값이면 `null`)은 `{ref, model, appliesNextTurn?}`을 돌려주며 다음 턴부터 적용됩니다. 진행 중인 턴은 이전 모델로 끝납니다. 선택은 데몬을 다시 시작해도 유지되고, `ExternalSessionRow.model`과 `external.state` 이벤트(새 필드 `model`)에 나타나며, `ExternalCliInfo.defaultModel`은 CLI가 알려 줄 때(Codex) 기본 모델을 담습니다. Claude Code는 전체 모델 id도 받습니다. tmux 창에서 실행 중인 세션은 거절되며(`external_unavailable`, `details.reason` `tmux_transport`) 그 창에서 `/model`로 바꿉니다.

프로토콜 1.68.0(capability `lspInstallReasons`)부터 `lsp.catalog` 행과 `lsp.install` 결과가 언어 서버를 설치할 수 없는 이유를 알려 줍니다. `reason` `needs_toolchain`은 `toolchain`(`dotnet-sdk`, `go`, `rustup`, `node`, `python`), `hint`, `url`과 함께, `npm_failed`는 `npmCode`(`ERESOLVE`, `EACCES`, `E404` 등)와 `detail`(npm의 마지막 오류 줄)과 함께 옵니다. .NET 서버에는 SDK가 필요하며 런타임만 있는 `dotnet`은 없는 것으로 봅니다. capability `lspToolchainInstall`이 있으면 .NET SDK의 `needs_toolchain` 결과에 `needsToolchain {name, sizeMb, source, version}`이 붙고, 사용자가 확인한 뒤 `lsp.install {ids, withToolchain: true}`가 Microsoft에서 SDK를 받아(https, 릴리스 메타데이터의 sha512로 검증) 사용자별 디렉터리에 설치한 다음 서버를 설치합니다. `lsp.install.progress`에 `phase`, `percent`, `toolchain`이 추가되고, `lsp.toolchain.cancel {name}`은 다운로드를 멈추며 `lsp.toolchain.remove {name}`은 SDK를 지웁니다. 로컬의 사용자만 할 수 있으며, 브라우저는 `actor: 'person'`과 `personConfirm: 'window'`를 보냅니다. `lsp.catalog` 행에도 같은 `needsToolchain` 제안이 붙어 서버와 SDK를 한 번에 설치하도록 제안할 수 있고, `toolchainInstalled`(`toolchainVersion`, `toolchainSizeMb`와 함께)는 지울 수 있는 core 자체 SDK가 있는지 알려 줍니다.
프로토콜 1.69.0(capability `hostToolKind`)부터 `tool.register` 스펙에 `kind`를 붙일 수 있습니다. `"act"`는 대화 밖의 무언가를 바꾸는 도구(클릭, 폼 입력, 페이지에서 동작하는 repl)로, 호스트가 직접 확인하므로 권한은 `read`로 둡니다. `"read"`는 보기만 하는 도구입니다. 없으면 알 수 없는 것으로 보고, 이름이 `repl`이거나 category가 `browser`인 호스트 도구는 동작하는 도구로 칩니다. 동작하는 도구를 부른 턴에는 "지금 답하라" 안내가 붙지 않습니다.

프로토콜 1.69.0(capability `jobMissedRuns`)부터 놓친 예약 실행이 조용히 넘어가지 않습니다. 실행 예정 시각보다 `scheduler.catchUpSec` 넘게 늦게 데몬이 시작되었거나, 대기 중인 실행의 브라우저 호스트가 끝내 연결되지 않으면, `job.runs`에 status `missed`와 `error` "missed: <이유>"인 행이 남고, `job.event` `missed`가 `{scheduledAt, reason, error, title, nextRunAt}`(`reason`은 `daemon_down` 또는 `host_absent`)을 싣고, 작업의 채널(없으면 연결된 사람의 개인 대화, 루틴 이름 표시)에 알리며, `JobInfo.lastError`에 이유가 보입니다.

프로토콜 1.70.0(capability `reasoningOnlyFallback`)부터 답을 생각(reasoning) 안에만 쓰는 모델 때문에 턴이 빈손으로 끝나지 않습니다. 모델에게 그 사실을 알리고 최대 두 번 다시 묻는데, 공급자가 지원하면 생각을 끈 채로 묻습니다. 그래도 보이는 답이 없으면 턴은 `complete`로 끝나고 `message.done`의 `reasoningFallback`이 `true`입니다. 본문은 답 언어로 된 안내 한 줄과 그 생각 내용(마지막 4000자)입니다. 생각조차 없는 빈 응답은 예전처럼 한 번만 다시 묻고 `empty_response`로 끝납니다.

프로토콜 1.71.0(capability `providerLoginOpenBrowser`)부터 `provider.loginWeb`에 선택 항목 `openBrowser`가 있습니다. `false`이면 데몬은 어떤 로그인 방식에서도 시스템 브라우저를 열지 않고, 클라이언트가 결과와 `provider.loginProgress` `await_user` 이벤트에 함께 실린 `verificationUri` / `verificationUriComplete`(device code이면 `userCode`도)로 동의 페이지를 직접 엽니다. 이때 `google_adc`(gcloud가 직접 브라우저를 열고 넘겨줄 주소가 없음)는 `login_unsupported`로 거절하고, 자동 선택이 그쪽으로 가면 `google_oauth`를 씁니다. 생략하면 예전처럼 데몬이 브라우저를 엽니다. 또 1.71.0부터 `provider.loginProgress`의 `done` 단계는 자격 증명이 저장된 뒤에만 보내므로, `done`을 받고 `provider.list`를 새로 고치면 공급자가 연결된 것으로 보입니다. 저장에 실패하면 `done` 대신 `failed`를 보냅니다.

프로토콜 1.72.0(capability `sessionPaths`)부터 에이전트가 만들거나 내려받은 파일 중 임시 파일이 아닌 것은 모두 대화의 프로젝트 폴더에, 프로젝트가 없으면 세션 자신의 폴더에 둡니다. `session.create`는 workdir가 사람이 고른 프로젝트가 아닐 때(브라우저의 기본 폴더) `noProject: true`를 받습니다. 이 값은 재시작 후에도 유지되고 위임된 하위 세션과 포크가 따릅니다. `session.paths {sessionId}`는 `{workdir, workspaceDir, resultsDir, downloadsDir, scratchDir, hasProject}`를 돌려줍니다. `resultsDir`는 프로젝트가 있으면 workdir, 없으면 `<workspaceDir>/artifacts`이고, `downloadsDir`는 `<resultsDir>/.snowpea-downloads`(처음 저장하는 쪽이 만들고, core가 만들 때는 `*` 한 줄짜리 `.gitignore`를 넣습니다. 점 폴더라 프로젝트의 테스트·빌드 도구와 `git add -A`가 건드리지 않습니다), `scratchDir`는 `<workspaceDir>/tmp`입니다. `session.create` 결과와 모든 `tool.invoke`에도 같은 필드가 실리므로, 호스트는 내려받은 파일을 OS 다운로드 폴더가 아니라 `downloadsDir`에 저장합니다. 프로젝트가 있으면 `session.artifacts`가 `<workdir>/.snowpea-downloads/**`(그 `.gitignore` 제외)도 `.snowpea-downloads/<경로>` 이름으로 나열합니다. 플래그 없이 만든 세션과 예전 세션은 `resultsDir` = workdir 그대로입니다.

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
| jobs | `job.schedule`, `job.list`, `job.cancel`, `job.runNow`, `job.update`, `job.runs` |
| gateway | `gateway.bind`, `gateway.list`, `gateway.unbind` |
| memory | `memory.search`, `memory.write` |
| skills | `skill.search`, `skill.install`, `skill.list`, `skill.remove`, `skill.reload`, `skill.create`, `skill.read`, `skill.write` |

방향이 반대인 메서드가 하나 있습니다. `approval.request`는 알림이 아니라 서버가 클라이언트에게 보내는 *요청*입니다 — 데몬이 클라이언트에게 결정을 묻고 답을 기다립니다. 프로토콜이 HTTP에 스트림을 얹은 형태가 아니라 WebSocket JSON-RPC인 이유가 바로 이 양방향성입니다.

## 이벤트

알림은 데몬에서 구독 중인 클라이언트로 흘러갑니다.

- `session.event(sessionId, seq, kind, payload, ts)`는 한 턴 안에서 일어나는 모든 일을 담습니다. 종류는 `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `backend.changed`, `model.changed`, `usage`, `context`, `compaction`, `audio.spoken`, `error`, `turn.queued`, `turn.dequeued`, `turn.done`입니다.
- 무인 승인 큐를 위한 `approval.pending`과 `approval.resolved`.
- `job.event`와 `gateway.event`.
- 스킬이나 플러그인을 리로드한 뒤의 `commands.changed`.

모든 `session.event`는 해당 세션 안에서 단조 증가하는 `seq`를 싣고 있습니다. 연결이 끊긴 클라이언트는 재연결한 뒤 `session.resume(sessionId, afterSeq)`를 호출해 놓친 부분만 정확히 다시 받습니다. 이벤트가 그냥 방송되는 게 아니라 영속되는 이유가 이것입니다.

1.51.0부터는 클라이언트가 영어 문장을 읽어 내던 이벤트에 코드가 함께 실려, 클라이언트가 자기 언어로 문장을 만들 수 있습니다: `provider.retry`에는 `reason`(`unreachable`, `silent`, `transient`)과 `waitLeftSec`이 있고, 한도가 시간일 때 `max`는 null입니다. `hook.continue`에는 `kind`(누가 턴을 이어 갔는지), `count`, `limit`이, `step.done`에는 `errorCode`가 있습니다. 목표의 `blockedReason.code`는 `round-limit`, `needs-input`, `no-credentials`, `no-access`, `external`, `other` 중 하나이고, `session.trajectory`의 context·system·compacted 행에는 `data.kind`가 있습니다. 예전 클라이언트를 위해 영어 문장도 그대로 남으며, 데몬은 그것을 번역하지 않습니다.

1.53.0부터 모든 `message.user` 이벤트에 `origin` — `{kind, surface}` — 이 실립니다. `kind`는 `human`, `jarvis`, `schedule`, `gateway`, `subagent`, `system` 중 하나이며(capability `messageOrigin`), `session.trajectory`의 user 행에도 `data.origin`으로 들어갑니다. 세션의 `lastPrompt`와 `lastUserAt`은 사람이 직접 보낸 입력(origin `human`)만 따르므로 목표 라운드, 복귀(handback), 예약 작업, 위임 메시지가 더는 그것을 바꾸지 않으며, `session.fork`는 원래 세션의 제목을 그대로 둡니다. 1.53.0은 자비스 모드(베타)의 계약도 선언합니다: `jarvis.*`, `external.*`, `mcp.preset.*` 메서드, `jarvis.*`·`external.*` 이벤트, 세션의 `role`·`jarvis` 필드, 승인의 `external`·`route` 필드. 기능이 나오기 전까지 이 메서드들은 `not_implemented`로 답하고, 해당 capability(`jarvis`, `jarvisDelegation`, `externalSessions`, `mcpPresets`, `jarvisBriefing`)는 `system.hello`에 나오지 않습니다.

## 오류

오류는 `error.data.code`에 문자열 코드가 담긴 JSON-RPC 오류입니다: `unauthorized`, `protocol_incompatible`, `not_found`, `invalid_params`, `mode_denied`, `approval_denied`, `approval_timeout`, `tool_inactive`, `not_implemented`, `login_unsupported`, `skill_exists`, `mcp_auth_failed`, `search_provider_unavailable`, `browser_provider_unavailable`, `fork_after_compaction`, `fork_turn_not_found`, `fork_no_finished_turn`, `fork_child_session`, `fork_running` (`session.fork`, 1.50.0), `steer_slash_command` (`session.steer`, 1.50.0), `goal_not_allowed` (`goal.set`/`goal.resume`, 1.50.0), `jarvis_disabled`, `jarvis_person_only`, `external_view_only`, `external_unavailable` (자비스 모드, 1.53.0), `internal`.

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

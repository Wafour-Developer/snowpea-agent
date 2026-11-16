# M19 — Jarvis mode (beta)

Status: approved by the owner 2026-11-15; not started. Target protocol 1.53.0 (additive). Ships in one release, documented as beta.

This file has two parts: the plan as the owner approved it (Korean), and the technical design the work packages follow (English). Where they differ, the owner's decisions in Part 1 win; the known differences are listed right below.

## Decisions that override Part 2

- **Grants from a messenger are allowed by default** (`jarvis.messenger.allowGrants: true`), still through a one-press confirmation that only the person can answer. Part 2 says default false.
- **Browser-host sessions:** Jarvis reads, summarises and may send instructions (prompt/steer). Deciding their approvals stays with the browser connection until the browser team opens it; coordinate and include it in the same release if ready. Part 2 says read-only.
- **Hand-back:** authority pauses when the person types into a managed session and returns automatically after `jarvis.quietMinutes` (default 10) without input, or at once on hand back.
- **All four CLIs ship in the first release**, on IDE, web and TUI.
- Grants are proposed by Jarvis and confirmed by the person with one press; nothing Jarvis reads can change them.
- **External sessions are told apart by where their process lives** (`host`): `none` (no live process; snowpea can continue it), `terminal` (the person runs it in a plain terminal: view-only live tail, available once that process exits), `tmux` (in a tmux pane: view, and snowpea may type into the same process pending spike S5; its approvals are answered by the person, never by Jarvis), `snowpea` (the daemon runs it over the structured transport: unified approvals, Jarvis may decide). Rows also carry `canSend`, `approvals` and, for tmux, `tmux {session, pane}`. Part 2's `open_elsewhere` status is replaced by `host`.
- **Sessions are loaded by working directory:** `external.list` takes `cwd` (default: the calling session's workdir) and returns that folder's sessions for the requested CLIs, newest first, paged; `allProjects` lists across exposed projects for the Jarvis overview. `external.open` of a `host: none` session continues it under snowpea (`host: snowpea`).
- **Unrestricted ("yolo") mode for every external CLI** (overrides Part 2's "never pass dangerous flags"): a session's `mode` is `default | readonly | unrestricted`, chosen on `external.create`, on `external.open` when continuing, or with `external.setMode`. Only a human connection (IDE, web, TUI, the bound approver in a messenger) may choose `unrestricted`; Jarvis can only raise a person-only confirmation naming the CLI, the session, the project folder and "snowpea does not control what this session runs; Jarvis's guarded-class rules do not apply inside it". Per session, never remembered unless the person sets `jarvis.external.clis.<id>.defaultMode`. Every unrestricted open or switch is a ledger entry. `external/proc.py` emits a bypass flag only for a session whose mode is `unrestricted` and was set by a human connection; any other bypass-like argument is refused. The same three modes apply to a Claude session started in tmux.
- **Spike outcomes S1–S5** (`docs/design/m19-spikes/`): claude — structured stream-json with bridged approvals (`control_request can_use_tool`), plus a tmux shared-terminal transport for Claude only (Linux/macOS, beta: `host: tmux`, `canSend` with restrictions, approvals `person`); codex — one shared `codex app-server`, bridged approvals (`item/commandExecution/requestApproval`), open elsewhere = a held flock on `~/.codex/thread-writer-locks/<id>.lock`, no tmux; agy — `agy acp` per open session, approvals follow agy's own settings (`approvalPolicy: asks | auto` in `external.clis`; bridged when it asks, none when it does not), modes default and unrestricted only (its `plan` mode still writes files, so there is no readonly), no tmux; cursor — spawn per message, no permission mechanism at all, so `readonly` (`--mode ask`) by default, `default` (edits and sandboxed shell without asking, approvals `degraded`) when chosen, and `unrestricted` only on the person's confirmation (the same switch gates edit work on agy with policy `auto`); rows carry `mode` and `controlled`.
- **Transcript privacy:** Claude transcript records other than user/assistant (attachment, session-owner, …) hold the owner's email and org ids; adapters drop them before anything reaches a client or Jarvis.
- **Projects have names** in the Jarvis store (`jarvis.project.list|rename|alias`; default name = workspace title, else folder name).
- **Notifications:** `jarvis.escalation` / `jarvis.decision` carry `importance` and a short `summary`; settings `jarvis.notify.{inApp, os, messenger}` default true. MCP presets in v1: Google (Gmail, Calendar) and Microsoft 365 (Outlook mail, Calendar).
- **No "open everything in tmux":** sessions Jarvis manages are opened over the structured transport. `external.create` takes `transport: "structured" | "tmux"` (default structured; tmux only once S5 confirms it and the host has tmux).

---

# Part 1 — approved plan


## Context

snowpea의 모든 세션을 한 곳에서 총괄하는 **자비스 세션**을 만든다. 지금은 세션마다 사람이 직접 들어가 승인하고, 질문에 답하고, 멈춘 것을 "계속"이라고 이어 줘야 한다(luffy에서 겪은 문제와 같은 종류). 자비스는 이 일을 대신한다. 전체 상황을 대화로 요약·보고하고, 맡겨진 범위 안에서는 스스로 결정해 세션을 계속 진행시키고, 사람이 정해야 할 것만 가져온다. PC에 설치된 Claude Code, Codex, Antigravity(agy), Cursor agent의 세션도 snowpea 화면에서 만들고, 불러오고, 대화하고, 자비스가 지시할 수 있게 한다.

릴리스 방식: 전부 완성한 뒤 한 번에 릴리스한다. 문서에는 **베타**로 표기한다. 프로토콜은 추가 전용으로 **1.53.0**.

## 소유자와 확정한 요구사항

| 항목 | 결정 |
|---|---|
| 구성 | 데몬당 자비스 세션 하나. 자비스 모드는 기본 꺼짐. 켜면 snowpea에서 활성화된 모든 프로젝트를 총괄. 프로젝트별 "자비스에 노출" 설정(기본 켬). 자비스용 모델은 따로 지정. |
| 결정 권한 | 기본은 단계별 위임(안전한 승인과 진행 방식 질문만 자비스가 결정). 세션·프로젝트 단위로 "다 맡김"을 주면 멈춤·질문·불완전한 종료까지 자비스가 판단해 계속 진행시키거나 보고. |
| 일임 예외 | 맡긴 세션에서도 (a) 배포·릴리스·푸시, (b) 프로젝트 밖 파일·삭제, (c) 비밀정보·외부 전송은 사람에게 묻는다. 항목별로 자비스에게 따로 허용 가능. |
| 권한 확정 | 대화로 요청하면 자비스가 범위를 적은 확인 창을 띄우고, 사람이 한 번 눌러 확정. 메신저에서도 기본 허용. |
| 개입 | 사람이 맡긴 세션에 직접 말하면 자비스의 대리 결정이 멈추고, 일정 시간(기본 10분, 설정 가능) 입력이 없으면 자동 복귀. "다시 맡김"으로 즉시 복귀. |
| 능동성 | 중요한 일(완료, 오류로 멈춤, 사람 결정 필요, 자비스가 대신 결정함)만 먼저 알림. 주기 브리핑은 요청할 때 켜고 대화로 조정. |
| 자비스의 역할 | 총괄과 읽기. 파일 쓰기는 자기 작업 폴더(기본 `~/Snowpea/Jarvis`, 변경 가능) 안에서만. 프로젝트 변경은 항상 작업 세션에 시킨다. 새 세션은 사람의 요청을 처리할 때와 맡긴 일 안에서만 만든다. |
| 메모리 | 전역과 노출된 모든 프로젝트의 메모리를 읽고, 자비스 전용 공간에만 쓴다. |
| 외부 CLI | Claude Code, Codex, agy, Cursor agent 네 가지를 첫 릴리스에 모두. 세션 목록·생성·불러오기, snowpea 대화 화면으로 보기와 직접 대화, 자비스의 지시. 다른 터미널에서 열려 있으면 보기만, 닫히면 이어받기. 승인은 snowpea 승인으로 통합. |
| 브라우저 세션 | 읽기·요약·지시. 승인 대리는 브라우저 개발 세션과 협의해 가능하면 같은 릴리스에. |
| 화면 | 데스크톱, 웹, TUI, 메신저(켜면 자비스와만 대화하고 모든 세션의 소통이 자비스를 거침), 음성(누르고 말하기). |
| 비서 기능 | 예약·반복 작업 관리, 정기 보고서, 메일 정리, 일정·할 일. 메일·일정은 MCP 서버로 연결하되 쉬운 설정 단축(프리셋)을 제공. |

## 설계 요점

경로 약어: `CORE` = `snowpea/core/snowpea_core`, `IDE` = `snowpea-ide/src`, `TUI` = `snowpea/tui/src`.

### 1. 자비스 세션 (`CORE/jarvis/`)

- 기존 이름 있는 에이전트(`CORE/agent/named.py`) 위에 예약 이름 `jarvis`로 만든다. 지속되는 `kind="agent"` 세션, 메모리 공간 `agent:jarvis`. 세션 요약에 `role: "jarvis"` 필드를 추가한다(구버전 클라이언트는 보통 에이전트로 본다).
- `service.py`: 켜면 생성, 재시작 시 복원, 끄면 정지. 자체 모델, 작업 폴더.
- `intake.py`: 기존 이벤트 허브(`EventHub.subscribe(conn, None)`)와 승인·질문 관찰자(`add_observer`)로 모든 세션을 본다. 세션별 상태 요약(상태, 대기 항목, 마지막 답 끝부분, 바뀐 파일)은 **모델 호출 없이** 유지한다.
- 자비스를 깨우는 경우만 정한다: 자비스에게 배정된 승인·질문(2초 묶음), 맡긴 세션의 종료·오류·멈춤. 그 밖의 완료·오류는 30초 단위로 한 번에 알림. 안전한 승인은 모델 없이 자동 허용하고 기록만 남긴다.
- 예산: 시간당 깨움 횟수와 하루 토큰 상한. 넘으면 전부 사람에게 넘긴다.
- 매 턴에는 전체 세션 현황 표(최대 40행)만 넣고, 나머지는 도구로 읽는다(`CORE/agent/runtime_context.py`).

### 2. 위임 엔진 (`jarvis/delegation.py`, `jarvis/classify.py`)

보안 경계는 모델이 아니라 이 엔진이다. 자비스는 엔진이 허용한 선택지 안에서만 고른다.

- 권한 기록은 `$SNOWPEA_HOME/jarvis/delegation.db`에 둔다. 설정 파일 밖이라 작업 세션의 설정 수정으로 바꿀 수 없다. 범위는 전역 / 프로젝트 / 세션, 수준은 단계별 / 전체, 허용한 예외 항목, 누가 언제 줬는지, 만료 시각.
- 요청 분류: 안전 / 위험 / 예외 (a)(b)(c) / 불가. 기존 권한 태그(`CORE/permissions/policy.py`)와 명령 패턴으로 판정한다. 해석이 안 되는 복합 명령은 예외로 취급한다(안전 쪽으로 실패).
- 배정: 안전은 자동 허용. 위험은 단계별이면 사람, 전체 위임이면 자비스. 예외는 허용 기록이 있을 때만 자비스.
- 자비스의 결정은 서버에서 엔진이 다시 검증한 뒤 기존 경로 `ApprovalQueue.respond(..., by="jarvis")`로 처리한다. 모든 결정은 `logs/approvals.jsonl`과 `jarvis/ledger.jsonl`에 남는다.
- 권한 변경(`delegation_set`)은 사람만 답할 수 있는 확인 요청을 띄운다. 자비스가 읽은 글(대화 기록, 메일, 외부 CLI 출력)로는 권한이 바뀌지 않는다.
- 개입 감지: 맡긴 세션에 사람이 직접 보낸 메시지(새 `origin` 필드로 구분)가 오면 대리 결정을 멈추고 타이머로 복귀.

### 3. 자비스 도구 (`jarvis/tools.py`, 자비스 세션에만 등록)

- 읽기: `session_list`, `session_read`(요약 또는 궤적 일부, `CORE/session/trajectory.py` 재사용), `external_list`, `external_read`, `ledger_read`.
- 지시: `session_prompt`, `session_steer`, `session_interrupt`, `session_create`(snowpea 또는 외부), `approval_decide`, `question_answer`.
- 권한 제안: `delegation_set`(사람 확인 필요).
- 쓰기(작업 폴더 안으로 제한): `briefing_set`, `report_write`, 할 일 관리.
- 메모리: `CORE/memory/access.py`를 추가해 읽기는 전체, 쓰기는 `agent:jarvis`로 강제.

### 4. 외부 CLI 계층 (`CORE/external/`)

공통 인터페이스: 찾기, 목록, 기록 읽기(snowpea 타임라인 항목으로 변환), 실시간 따라 읽기, 다른 곳에서 열려 있는지, 생성, 전송(스트리밍), 중단, 닫기.

| CLI | 연결 방식 | 목록·기록 | 승인 통합 | 안 될 때 |
|---|---|---|---|---|
| Claude Code | 양방향 스트림(`-p`, stream-json, `--resume`) | `~/.claude/projects/*.jsonl`, 실행 중 표시 `~/.claude/sessions/*.json` | 승인 요청을 스트림으로 받아 snowpea 승인으로 | 수정만 자동 허용 모드 |
| Codex | `codex app-server` 하나를 공유 | `thread/list`, `thread/read` | app-server의 승인 요청 | `codex exec --json` |
| agy | 세션별 `agy acp` | 요약 DB(읽기 전용), `session/load` | ACP 권한 요청 | `-p` 방식 |
| Cursor agent | 메시지마다 실행(`--resume`) | `meta.json`, 기록 jsonl | 없음. 수정만 자동 허용, 나머지 차단 | 해당 없음 |

- 클라이언트에는 별도의 `external.*` RPC로 노출한다(세션 목록에 섞지 않는다. 외부 세션은 snowpea 세션의 규칙을 지킬 수 없고 구버전 클라이언트가 깨진다). 이벤트는 기존 세션 이벤트와 같은 모양이라 화면 코드를 재사용한다.
- 실행 안전(`external/proc.py`): 인자 목록으로만 실행, 실행 파일 절대 경로 고정, 프롬프트는 표준 입력으로, 세션 ID·모델 형식 검증, 작업 폴더는 등록된 프로젝트만, 환경 변수에서 데몬 토큰 제거, 위험 플래그(`dangerously`, `yolo`, `bypass`) 거부, 동시 프로세스 상한, 데몬 종료 시 정리.

### 5. 메신저, 비서 기능, 브라우저

- 메신저(`CORE/gateway/router.py`): 자비스 모드가 켜지면 모든 연결의 대상을 자비스 세션으로 돌린다(저장된 설정은 그대로라 끄면 원복). 사람에게 배정된 승인은 어느 세션 것이든 세션 이름과 함께 전달한다.
- 비서 기능: MCP 프리셋(메일·캘린더 서버 사양, 로그인 방식, 읽기/전송 도구 구분, 예약 템플릿)을 `CORE/tools/mcp_catalog.py`에 추가하고 `mcp.preset.list/apply`로 한 번에 설정. 메일 정리는 **격리된 예약 작업 세션**이 하고 자비스는 요약만 받는다(메일 내용이 자비스의 결정에 섞이지 않게). 보고서와 할 일은 자비스 폴더에 저장.
- 브라우저 세션: 읽기·요약·지시는 포함. 승인 대리는 브라우저 연결만 답할 수 있게 잠겨 있으므로(`CORE/permissions/approval_queue.py`) 브라우저 개발 세션과 협의해 연다.

### 6. 설정과 프로토콜

- 설정: `jarvis.{enabled, model, folder, delegation.default, quietMinutes, proactive.*, briefing, budget, messenger.*, external.*}`, 프로젝트별 `jarvis.expose`.
- 프로토콜 1.53.0(추가 전용): 메서드 `jarvis.*`(상태, 현황, 기록, 위임 목록·설정·회수, 개입·복귀), `external.*`, `mcp.preset.*`. 이벤트 `jarvis.state/decision/escalation/delegation.changed`, `external.event/state`. 필드 `message.user`의 `origin`(브라우저 세션에 약속한 항목), 세션 요약의 `role`과 위임 상태, 승인 요청의 배정 대상.

### 7. 클라이언트

- IDE·웹: 사이드바에 자비스와 외부 세션 항목(`IDE/renderer/shell/sidebar/SidebarRoot.vue`). `views/JarvisView.vue`(자비스 대화 + 전체 세션 현황 + "자비스가 결정함 / 사용자를 기다림" 패널 + 개입·복귀). `views/ExternalChat.vue`(기존 `MessageStream.vue` 재사용 + 변환기 + 전용 입력창 + "다른 터미널에서 사용 중" 표시), dock 탭과 새 창 지원. 설정 화면(권한 목록과 회수, 프리셋, CLI 경로). 누르고 말하기(`stores/audio.ts`).
- TUI: `snowpea jarvis`와 `/jarvis` 화면, `/external` 선택기와 대화 화면(`AgentTranscript.tsx`, `state/history.ts` 재사용).
- 구버전 데몬: 기능 표시(capability)로 가리고 "core 1.53 필요" 안내 하나만 표시.

## 진행 순서

**0. 사전 확인(스파이크, 각 반나절):** 실제 CLI로 프로토콜을 확인하고 기록을 남겨 가짜 CLI의 재료로 쓴다. S1 Claude의 승인 내보내기 형식, S2 Codex app-server의 승인 흐름과 실행 중 판정, S3 agy ACP의 불러오기와 권한, S4 Cursor의 제한. 실패하면 위 표의 "안 될 때" 방식으로 간다.

| WP | 범위 | 선행 | 모델 |
|---|---|---|---|
| 1 | 프로토콜 1.53, `message.user.origin`, SDK 타입 | 없음 | sonnet |
| 2 | 분류기, 위임 저장소, 기록 | 1 | opus |
| 3 | 자비스 서비스, 이벤트 수집, 현황 주입 | 1 | opus |
| 4 | 자비스 도구, 메모리 접근 | 2, 3 | opus |
| 5 | 외부 CLI 공통 계층, 프로세스 관리, 가짜 CLI | 1 | opus |
| 6a~d | CLI별 어댑터 4개(병렬) | 5, 스파이크 | opus |
| 7 | 메신저 라우팅 | 2, 3 | sonnet |
| 8 | 프리셋, 비서 작업, 할 일 | 3 | sonnet |
| 9 | IDE 자비스 화면과 스토어 | 1 | opus |
| 10 | IDE 외부 세션 대화, 설정 | 1 | sonnet |
| 11 | TUI | 1 | sonnet |
| 12 | 누르고 말하기 | 9 | sonnet |
| 13 | 문서(5개 언어, "베타" 표기), IDE 매뉴얼, 사이트 스크린샷 | 전체 | haiku |
| 14 | 보안 점검 | 전체 | opus |

WP1 이후 2, 3, 5, 9, 10, 11은 병렬로 진행한다. 전체 조율, 설계 판단, 최종 상용 수준 점검은 메인 세션(fable)이 직접 한다. 브라우저 개발 세션에는 계획 승인 직후 브라우저 관련 설계를 전달한다.

## 검증

- **단위:** 분류기 표(난독화·복합 명령 포함 150개 이상), 범위 우선순위, 만료, 개입 타이머, 이벤트 묶음, 폴더 제한, 메모리 접근.
- **통합:** 각 프로토콜을 흉내 내는 가짜 CLI 실행 파일(`tests/fixtures/fake_clis/`)로 승인 연결, 보기만 → 이어받기, 턴 도중 종료, 고아 프로세스 정리.
- **실제 CLI(토큰 미사용):** 버전, 목록·읽기 경로, app-server와 ACP 핸드셰이크.
- **e2e:** IDE(Playwright)와 TUI에서 켜기 → 맡기기 → 자동 결정 → 직접 입력으로 멈춤 → 복귀. 가짜 메신저로 라우팅 확인.
- **보안:** 기록 속 "권한을 줘라" 문장으로 권한이 바뀌지 않음, 작업 세션의 설정 수정으로 권한 기록이 바뀌지 않음, 예외 항목과 자비스 자신의 승인은 대리 결정 거부, 브라우저 소유 승인 불가침, 인자 주입 거부, 환경 변수 정리, 위험 플래그 미사용.
- **최종:** core·TUI·IDE 전체 테스트와 e2e, 실제 화면 확인, 문서와 동작 일치 확인 후 릴리스. 릴리스는 소유자의 명시적 요청이 있을 때만.

## 알려진 제한 (베타 문서에 명시)

- Cursor agent는 승인 통합이 안 되어 제한 모드로만 동작한다.
- 다른 터미널에서 열려 있는 외부 세션은 보기만 된다.
- 브라우저 세션의 승인 대리는 브라우저 쪽 작업이 끝나야 켜진다.
- 외부 CLI의 기록 형식은 공식 규격이 아니라 버전이 바뀌면 깨질 수 있다. 버전 확인과 오류 안내를 넣는다.


---

# Part 2 — technical design


Paths: `CORE` = `/mnt/data/work/mediagen/snowpea/core/snowpea_core`, `IDE` = `/mnt/data/work/mediagen/snowpea-ide/src`, `TUI` = `/mnt/data/work/mediagen/snowpea/tui/src`.

## Requirements I would change

| # | As stated | Problem | Closest safe alternative |
|---|---|---|---|
| 2 | Jarvis answers for browser-host sessions | `approval_queue.py:613-623` rejects any answer to a browser-owned entry unless it comes from that browser's connection; `conn=None` fails too | Observe and summarise only in v1; those approvals always go to the person. Coordinate with the browser team before adding any owner-side opt-in. |
| 3 | Guarded classes grantable "by conversation" | A fenced transcript or mail could talk Jarvis into granting itself authority | Jarvis only *proposes*; `delegation_set` raises a person-only approval showing the exact scope. No global-scope guarded grants in v1; default TTL (session: until it ends, project: 24h). |
| 9 | Messenger can do everything | A remote chat could widen authority | `jarvis.messenger.allowGrants` defaults to false; grants are confirmed on IDE/TUI/web. |
| 10 | Jarvis triages mail | Untrusted mail content plus approval authority plus send tools in one context | Triage runs in an isolated scheduled worker with no delegation or session tools; Jarvis receives a fenced digest. |
| 8 | Cursor prompts unified | Cursor exports no prompts | Fixed degraded mode; never pass `-f/--yolo`; always `--sandbox`. |

## A. Core

**1. `CORE/jarvis/`.** Jarvis is a reserved named agent `jarvis` built on `agent/named.py`: a persistent `kind="agent"` session with namespace `agent:jarvis`. `SessionSummary` gains an additive `role: "jarvis"` rather than a new `kind` literal, so old clients still see a normal agent session.

- `service.py`: lifecycle (create on enable, resume on boot, park on disable), own model, workdir = `jarvis.folder`.
- `intake.py`: a hub pseudo-connection (`subscribe(conn, None)`) plus `add_observer`. It keeps a per-session state row with no LLM call: status, pending items, last assistant tail (600 chars at most), files touched, test result.
- Wake rules:

| Trigger | Action |
|---|---|
| Approval or question the engine routes to `jarvis` | Wake after a 2s debounce, coalesced across sessions |
| Turn finished, error or stall in a fully delegated session | Wake |
| Finished or error elsewhere | Batch for 30s into one notice |
| Engine auto-allowed something | No wake; ledger entry, mentioned in the next batch |

- Budget: `jarvis.budget.{wakesPerHour,dailyTokens}`; when exhausted, everything escalates to the person.
- The runtime-context provider (`agent/runtime_context.py`) injects a compact overview table of at most 40 rows. Anything else is fetched by tools.

**2. `jarvis/delegation.py` and `jarvis/classify.py`.** The engine is deterministic and is the security boundary; the model only chooses among options the engine permits.

- Store: `$SNOWPEA_HOME/jarvis/delegation.db`, deliberately outside settings.json so a worker's `config` write cannot touch it. Row: `{scope: global|project:<root>|session:<id>, level: staged|full, allow: [deploy|outside_destructive|secret_send], grantedBy, grantedAt, expiresAt, sourceTurn}`. The most specific scope wins.
- `classify(request)` returns `safe | risky | guarded:<class> | never`:

| Class | Rule |
|---|---|
| `never` | Tag `config` (already in `UNPROMOTABLE`), delegation or memory-permission prompts, browser-owned entries; **anything under `$SNOWPEA_HOME` but `plans/` and session workspaces, read or written, by any tool** — named, globbed (`~/.snowpe?/token`), `~user`-relative, behind a variable (`~/.snow${X}pea`, or any variable path while `$SNOWPEA_HOME` lies in the user's home) or inside a recursive read of an ancestor (`grep -r x ~`) |
| (a) deploy | `exec` matching `git push`, `gh release` / `gh pr merge`, `npm`/`cargo`/`twine publish`, `docker push`, `kubectl`/`terraform apply`, `ssh`/`scp`/`rsync` to a remote, `deploy*`/`release*` scripts |
| (b) outside / destructive | Tag `outside`; `rm -r`, `git reset --hard`, `git clean`, `--force`, `DROP`/`TRUNCATE`, `find -delete`; every file-writing flag aimed outside the project (`git … --output`, `tree -o`, `sort -o`, `pytest --basetemp`, `cp -t`, `env -C dir`, `git -C dir commit`, `git worktree add|init <dir>`, …) |
| (c) secret / send | Tags `secret` and `send`; **every** HTTP request and DNS lookup (a GET sends its URL), web-fetch and web-search tools; MCP tools without `readOnlyHint`; payment-like tool names; secret paths (globs expanded against credential names, `~user`, `/proc/*/environ`, `ps e`, recursive reads of their ancestors, files only a variable names) |
| `safe` | **The exact table** (`jarvis/inspect_table.py`, security review 3 — see C. Security, "Third review"): a shell line from snowpea's own `shell` tool whose every command and option is listed, on literal paths that stay inside the project. git is never `safe`. A read tool is `safe` only inside the project |
| read-like (`risky` + `readOnly`) | Read-only by the same table but not eligible for `safe`: read-only `git` at the project's top level, an external CLI's shell, a glob, a recursive or symlink-following option, an expansion in `echo`/`printf` |
| Unparseable shell, brace expansion in the program's place, `case`, `${…$(…)}`, DB shell escapes, awk/sed program files, editors/pagers with commands, `.git/config` or `.git/hooks` written by any tool | Every guarded class (fail closed) |

- Routing: `safe` is auto-allowed without the LLM. Read-like goes to Jarvis at either level (owner's decision: under `staged` Jarvis decides safe approvals and how-to questions). Any other `risky` goes to the person under `staged` and to Jarvis under `full`. `guarded` goes to the person unless a grant covers it. Paused, over budget or hidden: the person.
- `approval_decide` re-runs the engine server-side and then calls `ApprovalQueue.respond(..., by="jarvis", conn=None, reason=...)`. Every decision lands in `logs/approvals.jsonl` and `jarvis/ledger.jsonl`.
- Pause: a `message.user`, steer or interrupt whose new `origin.kind == "human"` in a managed session sets `pausedUntil = now + quietMinutes`, extended on each further input. `jarvis.handback` clears it. Answering an escalated approval does not pause.

**3. Tools, `jarvis/tools.py`, registered only for `role=jarvis`.**

| Tag | Tools |
|---|---|
| `read` | `session_list`, `session_read` (digest or trajectory slice via `session/trajectory.py`, fenced), `external_list`, `external_read`, `ledger_read` |
| `delegate` | `session_prompt`, `session_steer`, `session_interrupt`, `session_create` (snowpea or external), `approval_decide`, `question_answer` |
| `config` (person-only approval) | `delegation_set` |
| `write` (path-jailed to `jarvis.folder` with realpath check) | `briefing_set`, `report_write`, `todo_*` |

`session_create` and `delegation_set` require the current turn's origin to be `human`, or a delegated session. They are refused in turns woken by intake.

**4. `CORE/external/`** (`base.py`, `proc.py`, `timeline.py`, `claude.py`, `codex.py`, `agy.py`, `cursor.py`, `registry.py`). The interface is `discover, list, read(page), tail, open_elsewhere, create, send (stream), interrupt, close`.

| CLI | Transport | List / transcript | Liveness | Permissions | Fallback |
|---|---|---|---|---|---|
| claude | Long-lived `-p` with stream-json both ways, `--resume`, `--permission-prompts host` | `~/.claude/projects/*.jsonl` | `~/.claude/sessions/<pid>.json` plus pid alive | Host prompts into ApprovalQueue | `--permission-mode acceptEdits`, rest blocked |
| codex | One shared `codex app-server` over stdio | `thread/list`, `thread/read` | Process scan plus rollout mtime (spike) | App-server approval requests | `codex exec --json -s workspace-write` |
| agy | `agy acp` per open session | Summaries sqlite opened `mode=ro`; `session/load` | `status` column plus process scan | ACP permission requests | `-p --mode accept-edits` |
| cursor | Spawn per message, `--resume --workspace --sandbox` | `meta.json` plus agent-transcripts jsonl | Process scan | None; degraded mode | n/a |

- **Client surface:** a separate `external.*` RPC family, not rows in `session.list`. Those rows carry invariants (mode, store seq, compaction, `session.prompt`) that external sessions cannot honour, and old clients would render broken rows. `external.event` reuses the session event kinds and payloads, so the timeline reducers work unchanged.
- **Approvals:** a duck-typed proxy session with id `ext:<cli>:<id>` goes through `ApprovalQueue.request`; `ApprovalRequest.external` is additive.
- **`proc.py`:** argv lists only; the absolute binary path is pinned at discovery; prompts go over stdin; ids and models are regex-validated; cwd must be a registered project root; env is scrubbed of daemon tokens; each child has its own process group and is killed on daemon exit; idle timeout; `maxProcesses` defaults to 4. Any flag matching `dangerously|yolo|bypass` is refused.

**5. Gateway.** When Jarvis is enabled, the router's session resolution (`gateway/router.py`, around lines 1280-1345) overrides every binding's target to the Jarvis session at resolve time. Stored targets are untouched, so disabling restores them. `_share_pending` drops the `active_id` filter and forwards only what the engine routed to `person`, from any session, with the session label. Buttons work as today.

**6. Assistant.**
- A preset is an entry in `tools/mcp_catalog.py` extended with `{id, server spec, auth: oauth|env, readTools[], sendTools[] (tagged send), recipes[]}`. A recipe is a schedule template such as "mail digest 08:00".
- RPCs: `mcp.preset.list` and `mcp.preset.apply`.
- A scheduler job with target `jarvis-task` runs the isolated worker in `jarvis.folder` and notifies Jarvis through `_notify_origin_session`.
- To-dos live in `jarvis/todo.py`, stored as JSON in the folder.

**7. Memory.** New `memory/access.py`. Jarvis reads `default`, `project:<exposed roots>` and `agent:jarvis`. `save_memory` is forced to `agent:jarvis`; a project write raises a `never`-class approval.

**8. Settings.** `jarvis.{enabled, model, folder, delegation.default, quietMinutes, proactive.{finished,error,needsYou,decided}, briefing, budget, messenger.{route,allowGrants}, external.{enabled,clis.<name>.{enabled,path},maxProcesses,idleSec}}`, plus per-project `jarvis.expose` in `config/project.py`.

**9. Protocol 1.53.0, all additive.**
- Capabilities: `jarvis`, `jarvis.delegation`, `external.sessions`, `external.<cli>`, `mcp.presets`, `message.user.origin`.
- Methods: `jarvis.status`, `jarvis.overview`, `jarvis.ledger`, `jarvis.delegation.list|set|revoke` (human connections only), `jarvis.takeover`, `jarvis.handback`, `external.clis|list|open|read|send|interrupt|create|close`, `mcp.preset.list|apply`.
- Events: `jarvis.state`, `jarvis.decision`, `jarvis.escalation`, `jarvis.delegation.changed`, `external.event`, `external.state`.
- Fields: `MessageUser.origin {kind: human|jarvis|schedule|gateway|subagent, surface}`, `SessionSummary.role`, `SessionSummary.jarvis {level, pausedUntil}`, `ApprovalRequest.external`, `ApprovalRequest.route`.

## B. Clients

**IDE and web**
- `renderer/shell/sidebar/SidebarRoot.vue`: add Jarvis and External panels to `PANELS`.
- New `views/JarvisView.vue`: `Chat.vue` bound to the Jarvis thread, a sessions overview, a "Decided by Jarvis / Waiting for you" panel fed by `jarvis.ledger` and `approvalQueue`, and take-over / hand-back chips in `ConversationHeader`.
- New `stores/jarvis.ts` and `stores/external.ts`.
- New `views/ExternalChat.vue`: `MessageStream.vue` plus `lib/externalTimeline.ts`, with its own composer calling `external.send` and a view-only banner. Registered in `dock/builtinTabs.ts` with an `external://<cli>/<id>` address in `lib/resourceAddress.ts`; pop-out support.
- Settings pages: grants table with revoke, presets, CLI paths.
- Push-to-talk: hold-key in the Jarvis composer through `stores/audio.ts`.
- Web: no extra bridge work beyond capability gating; external CLIs run on the daemon host, and the UI says so.

**TUI**
- `snowpea jarvis` and `/jarvis` open a screen with chat plus overview rows, extending `layout/agents.ts` and `AgentPanel.tsx`.
- `/external` picker modelled on `state/history.ts`, opening `ExternalTranscript.tsx` (from `AgentTranscript.tsx`, plus an input line).
- `ApprovalQueue.tsx` shows the `by` and `route` badges.

**Degradation.** Every entry point is gated on capabilities. Against an older daemon the panels are hidden with a single "requires core 1.53" hint.

## C. Security

- **Injection.** Transcript, mail and external text reaches Jarvis only inside nonce-delimited `<untrusted source=…>` fences, with a prompt section in `prompts/sections.py`. The real control is structural: authority lives in `delegation.db`, which is writable only through a human-connection RPC or a person-answered approval, and grant tools require a human-origin turn.
- **Privilege.** `approval_decide` cannot exceed the engine verdict. `config` is never delegable. Jarvis writes only inside its folder. It cannot answer its own approvals: the engine rejects any request whose sessionId is the Jarvis session.
- **External CLIs.** As in `proc.py` above. Degraded mode is announced in the window header.
- **Audit.** The ledger records the classification, the matching rule and the grant id for each decision.
- **Remote web login.** `jarvis.delegation.set` for guarded classes requires a local connection, or a re-authenticated remote one. `external.create` is limited to registered roots. Since security review 5 (`m20-project-trust.md` §4–5) a remote login never holds the daemon token (its page gets a per-login capability only the remote listener accepts), is always `remote_login` whatever its hello says, and calls only `settings_gate.REMOTE_LOGIN_ALLOWED`.
- **Browser sessions.** Read-only for Jarvis, as above.
- **Who is the person (known limit).** There is one daemon credential, `$SNOWPEA_HOME/token`; the desktop app, IDE, TUI, browser hosts (snowpea-browser) and the remote connector all present it in `system.hello`, so `clientKind` is self-declared — only a remote-listener login carries a mark the transport sets (`remote_login`). Pragmatic beta rules: (1) a worker can never read the token or anything else of snowpea's (`never` for every tool and path form, above), and Jarvis's own reads exclude `$SNOWPEA_HOME`; (2) `clientKind` is pinned by a connection's first successful hello — a later hello that changes it (or drops it, which would count as a person) is refused with `invalid_params`. Any local process that can read the token file is the person; separate per-client credentials are future work.
- **External sessions snowpea cannot gate.** `controlled: false` sessions (unrestricted, agy with `always-proceed`, Cursor outside readonly) run without snowpea's approvals: Jarvis may read and summarise them, but `external_send` into one and `session_create` of one are refused, exactly as for unrestricted mode. The person still uses them directly.
- **External "for this session".** A CLI prompt allowed for the session is remembered per exact call (tool + whole command line, or every argument) and never for a request the classifier calls guarded or `never`; everything else goes through the queue and the engine.
- **Jarvis's own reach.** No URL fetch (`web_extract` is not among its built-ins); `web_search` queries are capped at 200 characters. `grep`/`glob` results leave out every line from a secret file or from `$SNOWPEA_HOME`; reads under `$SNOWPEA_HOME` are refused; `jarvis.folder` may not be, contain or lie inside `$SNOWPEA_HOME` or a credential directory (`settings.set` and `jarvis.enable` refuse it, and Jarvis does not start on one).
- **Questions.** A question whose text or options mention a guarded action (deploy/push/release/publish/merge, delete/remove/outside, secrets/tokens/passwords, send/upload/mail/share, payment) or that asks for a secret goes to the person at every level; a question Jarvis answers reaches the worker labelled "answered by Jarvis … not by the person".
- **Fences and memory.** Fenced bodies cannot contain `<untrusted` or `</untrusted` (escaped to `&lt;…`), in tool reads and the overview table alike; memories recalled into Jarvis's prompt or returned by `search_memory` are fenced too.
- **Files.** `delegation.db` (and its journal files), the ledger, open escalations and the presets book are made owner-only (0600) on every open or write, not only when created.
- **Messenger.** An unrestricted confirmation shows the whole first message, or is refused when it is too long to show (over 3,000 characters); `/jarvis status` answers only the person bound to the chat.

Second security review (2026-11-16):

- **What `never` means.** `never` = never delegable: Jarvis and the engine never decide it; it always goes to the person, who may still approve it. It is not "blocked".
- **`$SNOWPEA_HOME` areas** (owner's rule; `jarvis/classify.py` `HOME_SECRET_TOP`, `HOME_SECRET_PATTERNS`, `HOME_CONTENT_DIRS`, `home_area`). The daemon's home and the user's default `~/.snowpea` are both judged by this table, for every tool, shell or not:

  | Area | Paths | Read | Write |
  |---|---|---|---|
  | secret (authority and secrets) | `token`, `settings.json`, `credentials*.json`, `mcp-oauth.json`, `daemon.json`, `escalations.json`, `budget.json`, `jarvis/`, `external/`, `attachments/`, `.claude/`, `.codex/`, `gateway*`, any `*.db` (+ `-journal`/`-wal`/`-shm`), `*.sqlite*`, `presets.json`, `ledger.jsonl*`, anything `is_secret_path` calls a secret | `never` | `never` |
  | own | `plans/`, session workspaces `sessions/<day>_<id>/` | as in the project | as in the project (not outside) |
  | content | `skills/`, `agents/`, `extensions/`, `plugins/`, `docs/`, `memory/`, `reports/`, `commands/`, `prompts/`, `templates/` | as in the project (a literal read is `safe`) | `guarded[outside_destructive]` |
  | logs | `logs/` | `risky` (command lines) | `never` |
  | other | anything else, and the home folder itself | `risky` | `never` |

  A glob takes the strictest area it could match (`~/.snowpea/*` is secret; the glob samples include every secret name), `~user` and `$HOME` forms too; a path behind a variable or `$SNOWPEA_HOME` is `never`. A recursive read of the home, an ancestor of it (`grep -r x ~`) or an `other` folder is `never`; of a content folder it is not `safe`. A shell line that is not a plain inspection and names a content path (an interpreter's argument) is `outside_destructive`. A write the permission layer tags `config` that lands only in content/own folders is judged by this table instead of being `never` outright. Jarvis's own read tools still exclude `$SNOWPEA_HOME` (its reach, above).
- **Orphan reaping (H1).** A pid file is trusted only when every check holds: a regular file this user owns in a 0700 directory this user owns (no symlink, the directory re-tightened), its name equals the recorded pid, the start time is recorded and matches, the executable snowpea configured and pinned for the record's `cli` (discovery at start, every spawn — never the record's own `exe` field, review 3 N15) is what `/proc/<pid>/exe` or the first two command-line words name, and the recording daemon is gone (or is this daemon); descendants are killed only after that root check. Never pid ≤ 1, this daemon or its parent; a group is signalled only when the recorded pgid equals the pid, is > 1, is not the daemon's group and the process still leads it. Files are created `O_EXCL|O_NOFOLLOW`, 0600. Without a start time (Windows) nothing is reaped.
- **Who may change what over RPC (H2, M9, L6).** One person rule (`server/person_gate.py`): IDE, web, TUI, CLI, desktop app, no `clientKind`; `local` methods also refuse a remote web login. `settings.set` from anyone but a local person may change only the keys listed in "Third review" below (everything else, and every project-scope key, is local-person-only); `jarvis.delegation.default` may only be `staged`, `defaultMode` never `unrestricted`, `confirmedPath` is never settable; changes to `delegation.default` and `clis.*.{path,defaultMode,passEnv}` are ledgered. `mcp.add`/`mcp.update` with `permission: "read"` is local-person-only. The dispatcher refuses `jarvis_person_only` per `PERSON_METHODS` (table in Part 3). `jarvis.*`, `external.state` and `external.clis.changed` events reach people's connections only; a browser host or the connector gets `external.state` for sessions it watches and `jarvis.access` for its own sessions (`hostToolsFrom`); `approval.*` is unchanged.
- **Delegating everything.** `jarvis.delegation.default: "full"` is gone: "everything delegated" is a global `full` grant the person makes with `jarvis.delegation.set`. A stored `full` (and a stored `defaultMode: "unrestricted"`) loads as the safe value, the file is rewritten, and one `high` escalation explains how to grant it properly. `unrestricted` comes only from an explicit `mode` in the person's own request: a continued session never comes back unrestricted, and only a person sends to an unrestricted session.
- **CLI path overrides.** A `path` override is never run, not even `--version`, until the person confirmed its resolved real path with `external.clis.confirmPath` (stored as `confirmedPath`); until then the CLI shows `pendingPath` and is not installed.
- **External CLI trust (M4).** A CLI loads the project's own settings and hooks, so the first external session in a project is opened by a person with `trust: true`; trust is kept in `$SNOWPEA_HOME/jarvis/external-trust.json`, never in the repository's `.snowpea/settings.json` (which the repository itself could ship). Anyone else gets `external_unavailable` `untrusted_project_person_only` there.
- **Messenger (H3).** While Jarvis routing is on, only the bound person's messages reach Jarvis; anyone else in a chat gets "Jarvis only talks to its owner here" (at most every 10 minutes per chat) and nothing they write reaches Jarvis or a chat command. Notices, briefings, grant proposals, unrestricted confirmations and forwarded requests go only to the bound person's private chat (Telegram: the user id; Slack `im`, Discord DM: learned from the person's first DM, kept across restarts in `$SNOWPEA_HOME/jarvis/private-chats.json`, 0600, with the person it was learned for; the chat type comes only from the platform's payload fields); none known → nothing is sent and an in-app escalation "messenger: no private chat" is raised (hourly at most). Requests from projects hidden from Jarvis are not forwarded. A message id handled once never starts a second turn; platform edits are not delivered; a forwarded message (Telegram `forward_*`, Discord forwards) reaches Jarvis fenced as quoted untrusted content and never ties a one-press confirmation. Defence in depth: in a turn another member started, every Jarvis tool is refused.
- **External text (M7).** Core strips terminal escape sequences (OSC incl. 52 and 8, CSI, DCS/APC/PM/SOS, 8-bit C1) and C0/C1 controls except `\n`/`\t` from every timeline payload, row title, preview, error and CLI approval note and diff (an approval's arguments stay raw and are written out instead: review 3, N3); the TUI does the same at its one input boundary (`TuiClient`). Core does **not** HTML-escape: renderers must still treat the text as text.
- **Approvals that read files (M1).** A Claude `Write` approval shows the file's old contents only for a file inside the session's folder that is neither a secret nor snowpea configuration; otherwise only the new contents, with a note.
- **Exposure (M2).** A project is hidden when its own `jarvis.expose` is false, when its settings file exists but cannot be read, or when any registered project above it is hidden; a `jarvis-task` is re-checked at every firing.
- **Listing (M3).** `external.list` keeps a row only when its real cwd is under the scope by the wide-root rule (home and `/` match only themselves); "all projects" leaves wide roots out.
- **tmux adoption (M5).** The pane's process must be the pinned Claude Code (`/proc/<pid>/exe` or its command line) with a recorded start time, checked at adoption and again before every send (and the pane still holds it); a pane started with a bypass flag becomes `mode: unrestricted`, `controlled: false`; `tmux.adopt` args add `paneCommand`, `paneCwd`, `unrestricted`. A prompt starting with a format character, or with `/`/`!` after invisible characters, is refused.
- **Mail workers (M6).** Microsoft 365 `get-mail-message-mime`, `get-mail-attachment-content` and `download-bytes` are not read tools; a `jarvis-task` worker's transcript is deleted after it ran (one `access`/`purge` ledger line), and while it exists only people's connections can list, resume or read it; `job.update`/`job.runNow` of a `jarvis-task` job is person-only.
- **Supply chain (M8, known limit).** The Microsoft 365 preset runs `npx -y --ignore-scripts @softeria/ms-365-mcp-server@<pin>`: the community server is pinned, its dependencies are not; it runs with the person's account. `mcp.preset.remove` cannot clear that server's own sign-in and says so (`notice`).
- **Child environment (L3).** An allowlist (PATH, HOME, locale, terminal, temp, certificates, credential-free proxies, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, Windows basics); provider keys only when named in `jarvis.external.clis.<id>.passEnv`.
- **Smaller ones.** Transcript lines over 32 MB (Claude) / 16 MB (Cursor) or nested too deeply are skipped (L1); Cursor's live tail reads only new complete lines from a byte offset (L2); preset snapshots keep references to the credential store, not values (L4); `escalations.json` and pid files are created exclusively without following links, `ps`/`lsof`/`systemd-run` come from `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin` only (L5); a CLI starts in the validated real path, re-checked at launch (L8).
- **Codex and agy channels.** Codex: server requests other than the two approval methods, or for a thread snowpea does not run, are refused; an approval id reused while open is ignored (the first ask answers it once); at most 16 open asks per thread (more are declined unasked), none on a closed channel; file-change items kept are bounded. agy: every agent→client request but `session/request_permission` (`fs/*`, `terminal/*`, …) is `Method not found`, as `clientCapabilities` declares; a permission for another session id is cancelled; a reused open id is ignored; at most 16 open.

### Third review (2026-11-16): the final design

Three reviews each found a new way to get a shell line called `safe`. Patching flags did not converge, so the auto-allow class was shrunk and rebuilt so that unknown input can never be `safe`.

**Where the router sits (F1).** For a snowpea session the permission policy runs first (`agent/loop.py` → `PermissionPolicy.decide`: the mode matrix, the allowlist and shell-remember rules, plan mode's read-only exception), then the queue's session cache (`ApprovalQueue.request` → `cached`); only what would otherwise ask a person reaches `DelegationEngine.route`. In `accept` mode a read-only command is asked (no automatic allow), so `safe` is the only thing that adds unasked execution. An external CLI's prompt is its own; the bridge (`external/manager._ask_approval`) answers a remembered exact call only when it is neither guarded nor `never`, everything else goes through the same queue.

**What is `safe` (auto: nobody asked).** A shell line only when all of these hold:

- it comes from snowpea's own `shell` tool (a fresh `sh -c` per call in `args.cwd` or the project, with the daemon's environment: no earlier line can change its folder, `PATH`, aliases or functions — `exec/local.py`). An external CLI's shell (Claude's Bash keeps its folder), an extension's, a background process, Jarvis's own: never `safe`;
- the classifier saw the raw text (below);
- every command is in `inspect_table.TABLE` and every option is in that program's exact allowlist: short options letter by letter (a valued letter's value validated), long options only by their full listed name (`--name`, or `--name=value` validated; optional-value ones only with `=`). Anything else — an unknown, abbreviated or attached form — is not `safe`. No deny-lists. Options whose value is a file are simply not listed;
- every operand is a literal path that stays inside the project before and after resolving symlinks, not a secret, not snowpea's, not a CLI control file; no glob; a directory operand of `diff`/`cmp` is read-like;
- no assignment, no `printf` option (so no `printf -v` in any spelling), no variable-setting builtin, redirections only into `/dev/null` (or a read from a file in the project), no here-document, no wrapper, no expansion.

The table (programs and their options): `pwd -LP`; `true`, `false`, `whoami`, `arch`; `id -ugGnrz NAME`; `groups NAME`; `uname -asnrvmpio` and the long forms; `nproc --all`; `uptime -ps --pretty --since`; `date -uR -d TEXT --utc --universal --rfc-email --date=TEXT +FORMAT`; `hostname -sfiIdA` and long forms; `which -a`, `type -atpP` names; `basename -az -s SUF`, `dirname -z`; `tty -s`, `locale -am`, `getconf -a`; `tr -dscCt` sets; `echo` (text), `printf FORMAT …` (no option), `test`/`[` (paths in the project), `expr`; `ls -aAlh1dFptrSisngoGcuUXvCmxQNkB` (`-R` read-like), `stat -tf -c FMT`, `realpath -emsqz`, `readlink -femnqsvz`, `file -bikh`, `du -shacbkmx -d N`, `tree -adfisughDpFCnNQ -L N -I/-P PAT`, `df -hHkTilaP`; `cat -AbeEnstTuv`, `tac -b`, `head`/`tail -qvz -n/-c N` and `-NUM` (no `-f`/`-F`), `wc -cmlwL`, `nl -b/-n/-w/-v/-i`, `md5sum`/`sha*sum`/`b2sum -btz`, `cksum`, `cmp -bls -n N` (two files), `diff -uqsywbBiaNcEZtTp -U/-C/-W N` (no `-r`), `cut`, `column -tx`, `fold`, `rev`, `comm -123z`, `paste -sz -d`, `expand`/`unexpand`, `od`, `hexdump -bcCdoxv -n/-s N`, `strings -a -n/-t`, `grep`/`egrep`/`fgrep -iynHhclLoqsvwxEFGPaIbZzTU -m/-A/-B/-C N -e PAT` (`-r` read-like; no `-f`, `-R`, `-d`), `sort -bdfgiMhnRrVsucmz -k/-t` (no `-o`, `-T`, `-S`, `--files0-from`, `--compress-program`), `uniq -cdDuiz -f/-s/-w N` (one operand), `xxd` (exact words `-l -c -g -s -o N`, `-p -ps -u -b -e`; one operand), `sed -n 'N[,M]p'` (`-E`, `-r`, `-e`), `find` roots in the project with `-name -iname -path -ipath -wholename -iwholename -regex -iregex -lname -ilname -type -xtype -maxdepth -mindepth -size -perm -user -group -uid -gid -mtime -mmin -atime -amin -ctime -cmin -used -inum -links -newer/-anewer/-cnewer/-samefile FILE-in-project -printf -regextype` and `-print -print0 -ls -prune -quit -empty -true -false -not -a -and -o -or -depth -d -mount -xdev -noleaf -readable -writable -executable -nouser -nogroup -daystart` (`-L`, `-H`, `-follow` read-like); `cd DIR` inside the project. The long-option lists are in the code. `yes`, `sleep`, `seq`, `tail -f` are not `safe`.

**What is read-like (`risky` with `readOnly: true`, rule `exec:read-like`).** Read-only by the table's knowledge but not eligible for `safe`: the read-only `git` subcommands with exact option allowlists (`status`, `diff`, `log`, `show`, `shortlog`, `blame`, `grep`, `ls-files`, `ls-tree`, `rev-parse`, `rev-list`, `describe`, `merge-base`, `show-ref`, `count-objects`, `check-ignore`, `version`, branch/tag listing, `for-each-ref`, `cat-file`, `remote -v`, `worktree list`, `stash list|show`) run at the project's top level with no `-C` (repo config, attributes, pagers and diff drivers can make them run code; a bare repository in a subfolder is never consulted); the table's lines from an external CLI's shell (a relative `..` is refused where the CLI's folder is unknown; git only when the CLI sent the call's folder and it is the top level); `rg`, `jq` (no `env`/`$ENV`/`input_filename`/`import`/`include`), `seq`; globs; `ls -R`, `grep -r`, `find -L`; an expansion in `echo`/`printf`; a directory operand of `diff`/`cmp`.

**What Jarvis can decide.** `staged`: read-like requests (allow once, or leave them to the person), how-to questions. `full`: also any other `risky` request. Guarded classes only with a grant naming every class of the request. `never` (and paused, over budget, hidden, person-only, Jarvis's own, browser-owned): the person. Jarvis's prompt: allow a `[read-like]` request only when the command is plainly read-only and every path stays inside the project; otherwise leave it to the person.

**Raw text (N3).** Approvals are classified and cached on their raw arguments. A tool name or argument holding a C0/C1 control, ESC/OSC/CSI, NUL, carriage return or a bidi/invisible format character is guarded in every class (`exec:opaque:control-chars`); the queue writes those characters out for the card (`\x9d`, `‮`) and sets `ApprovalRequest.argsEscaped`; such a request is never cached or remembered (`shell_remember.plain_words` refuses it too). The external bridge no longer cleans the arguments before classifying them (its note and diff are still cleaned for display).

**Abbreviations (N1, N2, N10).** Every exec-, output-, directory- and file-option check outside the table treats any prefix of a listed long option (`--o=`, `--compress-prog=`, `--open-files=`, `--to-com=`, `--rsync-p=`) as that option, so an opaque line stays guarded in every class. Options that name a file the program reads (`diff --from-file/--to-file`, `date -f/--file/-r`, `file -m/-f`, `grep -f`, `rg --file/--ignore-file`, `jq -f/-L/--rawfile/--slurpfile/--library-path`, `git blame -S/--contents/--ignore-revs-file`, `-O` order files, …) are judged as reads; a list of names (`--files0-from`, `--files-from`) reads unknown files (`secret_send`, and `never` while `$SNOWPEA_HOME` is in the user's home).

**Paths (N4, N5, N6, D4).** Writing an agent CLI's control file is `never` for every tool and redirect: `.claude/settings*.json`, `.claude/{hooks,commands,agents,skills,plugins}/**`, `.mcp.json`, `.claude.json`, `.codex/**`, `.cursor/**`, `.cursorrules`, `.gemini/**` (agy's settings live there), `.agy/**`, `~/.config/{claude,cursor,codex}/**`, in the project and the home alike; reading one is `risky`, never `safe`. Writing `.gitattributes`, `.gitconfig`, `.gitmodules`, `.git/info/attributes`, any `HEAD`/`commondir`/`gitdir` outside `.git`, a `config`/`packed-refs` next to `objects`/`refs`/`HEAD`, or anything under an `objects`/`refs` folder next to a `HEAD`/`config` is guarded in every class. The secret list adds `~/.codex/auth.json`, `~/.claude/.credentials.json`, `~/.claude.json`, gemini/cursor credential files, shell and REPL histories, cloud CLIs' folders (`.oci`, `.fly`, doctl, hcloud, heroku), keyrings, browser profiles and `~/.config/snowpea-secrets/**` (on top of `.netrc`, `.npmrc`, `.pypirc`, `.docker/`, `.kube/`, `.config/gh/`, `.git-credentials`, …). A shell read outside the project of a non-secret path is `risky`, never `safe` or read-like — the snowpea home's content folders included (a read tool still reads them like the project).

**Smaller ones.** `printf -v` in any spelling, `read`/`mapfile`/`getopts` into a code variable, `hash -p`, and `NAME+=`/`NAME[i]=` assignments of code variables are guarded in every class (N8). A classifier exception is `never` (N12). Orphans are matched to the executable pinned for their CLI (N15, above).

**Settings and config RPCs (N7, browser team's inventory).** All rules are data in `server/settings_gate.py`. A `settings.set` patch is judged leaf by leaf (each key it sets or deletes; `{"mcp": null}` is the `mcp` key; a table entry covers its subtree; deleting a parent of an entry is not covered). (1) `PERSON_LOCAL_ONLY` — `jarvis.*`, `security.*` (reserved), `daemon`, `web`, `extensions`, `allowlist`, `mcp.permissions`, `mcp.allowVars` (project scope also `jarvis.*`, `allowlist`) — only a local person. (2) A `clientKind: "browser"` connection also carries the person's Settings/setup pages. The marker is a top-level param `actor: "person"` that the browser's C++ gate sets only for Settings/setup-page calls that passed its click gate and for clicked approval answers; core honours it only on browser connections (a person-kind connection already is the person; any other kind gains nothing from it). `BROWSER_PERSON_METHODS` need it: `settings.set` (keys `BROWSER_PERSON_KEYS` = `agent`, `browser`, `gateway`, `lsp`, `memory`, `models`, `providers`, `scheduler`, `search`, `skills`, global scope), `setup.applyDefaults`, `provider.configure|loginWeb|remove|visionReset`, `mcp.add|update|remove|reload|authorize|deauthorize` (`permission: "read"` only while Jarvis is off), `skill.create|learn|reload|remove|write`, `skill.install` (but from below the daemon's plugin roots `$SNOWPEA_HOME/plugins` and `/extensions`: the server cannot otherwise tell the browser's bundled plugin from any local folder), `memory.update`, `permission.allowlist.remove`, `team.guide.set`, `audio.install`, `lsp.install`, `system.reloadSettings`; and an allow wider than `once` (session, project, always, site) — by `approval.respond` or as the reply to `approval.request` (without the marker the reply's scope counts as `once`). `BROWSER_HOST_METHODS` need none: `memory.delete`, `gateway.sync`, `mcp.authorize.complete`, `site.put|mark|markPart|delete`, `job.schedule|update|runNow|cancel|delete` (never a `jarvis-task` job: every action on one is a person's), `workspace.add|remove|rename|reorder`, `command.run`, `tool.register`; denies, `once` answers and every `question.respond`; every unlisted method (`session.*`, `goal.*`, …) keeps its own rules, and `system.shutdown` stays open to every authenticated local connection. `BROWSER_HOST_KEYS` (settings keys without the marker) is empty. Never from a browser: `jarvis.*` (but `jarvis.takeover` and `jarvis.delegation.revoke`, which only take authority away), `mcp.preset.*` (but `mcp.preset.list`), `security.*`, `permission.allowlist.*` but `remove`, `gateway.bind` (1.55.0: the person's binding identity; the browser refuses it on every page), and the local-only keys. **Legacy browsers.** A browser whose `system.hello` `clientVersion` is below `BROWSER_ACTOR_CUTOVER` = 0.3.0 (numeric compare; missing or unparseable counts as below; every shipped browser sends 0.2.0) gets the marker's lists without the marker (`settings_gate.legacy_browser`, the one cut-over rule); the never list and the Jarvis-on rule still apply. A remote web login gets `REMOTE_LOGIN_KEYS` (the browser person keys plus `mcp.servers`, `approvals`, `ide`, `appearance`, `audio`, and the project scope but its local-only keys); the connector `CONNECTOR_KEYS` (empty); an in-process caller no `settings.set` key. (3) While `jarvis.enabled`, Jarvis's authority chain is a local person's even with the marker (`jarvis_person_only`, `details.reason: "jarvis_on"`): `gateway.<platform>.{allowed_user_id, user_id, channel_id, token, app_token, shareActive}`, deleting a platform, `gateway.bind`; the profile `jarvis.model` resolves to (`models.default` when it is null) and its provider (`providers.<p>`, `provider.*` of it); `mcp.add|update` with `permission: "read"`; the MCP servers presets added (`mcp.*` of them). With Jarvis off they follow (2). Turning Jarvis on raises one `normal` notice listing the MCP tools tagged `read` and the number of allowlist rules (what already runs unasked). (4) Every refused call or key, and every Jarvis-relevant change (gateway, models, providers, mcp, jarvis) from a non-local connection, is a ledger line.

**Capability and `mcp.test`.** `system.hello` advertises `browserActor`: the browser stamps `actor: "person"` only on a core that does. Every marker method's params model declares `actor` (`ActorParams` for `skill.reload` and `system.reloadSettings`, which took no params); from a non-browser connection it is accepted and ignored; any value but `"person"` is `invalid_params`. `mcp.test` (it runs a draft server's command) needs the marker like `mcp.add` and is judged like it (Jarvis-on only when it names a preset's server).

**`command.run` from a browser.** Judged before it runs, by what the command would persist (`commands/persists.COMMAND_PERSISTS`, one entry per built-in; a test fails for an unclassified built-in and an unknown one counts as marker-required): `session` (no marker: `/accept`, `/auto`, `/plan`, `/mode` without `save`, `/effort`, `/delegation`, `/review`, `/goal`, `/ralph`, `/ultrawork`, `/workers`, `/delegate`, `/compact`, `/help`, `/tools`, `/models`, `/approvals`, `/memory` reads, `/mcp` and `/allowlist` listings, `/agent list`, `/skill list`, `/model` and `/busy` without arguments), `host` (no marker, like `job.*` / `memory.delete`: `/schedule`, `/memory forget`), `person` (marker: `/model <name>`, `/model default <id>`, `/vision`, `/setup`, `/login`, `/mcp add|remove|…`, `/allow`, `/allowlist remove`, `/skill learn|create`, `/mode … save`, `/team`, `/init`, `/deepinit`, `/agent create`, `/busy <value>`, `/desktop`, `/update`). Jarvis-on: `/allow` (rules pre-approve before routing), `/mcp add … --permission read`, and a `/model`/`/vision` change under `models.*` while Jarvis runs on a profile there (`details.reason: "jarvis_on"`). Commands from skill or command files are prompts: `session`.

**Who sees and answers which request.** Facts before this review (`ApprovalQueue.respond`, `question.respond`): an interactive approval (sent as `approval.request` to its origin) could be answered over RPC only by that origin connection — by design, a person's other surface too is refused (the messenger answers in-process); a browser-owned job's only by its browser; a shared-queue approval by any authenticated connection with any scope (a person-only one allowed only by a person); a question by any surface; every list and event went to every connection. Now (`settings_gate.list_visible` / `may_answer`, browser 0.3.0+ and the connector; a legacy browser keeps the old behaviour but never sees Jarvis's routing): **lists and events** (`approval.list`, `question.list`, `*.pending`, `*.resolved`) show requests of sessions this connection hosts (`hostToolsFrom` = its client id) and of unhosted sessions (IDE, CLI, scheduler), never another browser client's, never a person-only one (switch `settings_gate.FILTER_ANSWER_LISTS`, on). A browser of any version never sees a Jarvis-routed request and never gets `*.route.changed`: a request it showed that is routed to Jarvis becomes `approval.resolved {requestId, decision: null, by: "rerouted", outcome: "withdrawn"}` (`question.resolved {requestId, by: "rerouted", outcome: "withdrawn"}`; `decision` became optional and `outcome` is new, both additive); one routed back to the person arrives as a normal `*.pending` with the request. **Answers**: its own hosted session's request (or one delivered to it) with or without the marker (a widening allow still needs it); an unhosted session's approval or question only with `actor: "person"` (`jarvis_person_only` otherwise; a connector never); another browser client's, or a Jarvis-routed one until it is the person's: `not_found`; a person-only one: `jarvis_person_only`. `question.respond` takes `actor` too.

**Known limits.** (1) Between classification and execution a path can change (N13): the engine re-classifies — re-resolving every operand — when it answers an `auto` request (`validate_decision(expect="auto")` right before `ApprovalQueue.respond`), but a symlink swapped after that answer is not seen. (2) `safe` trusts the daemon's own `PATH` and environment: a program planted in a `PATH` folder outside the project is an outside write (guarded) but a daemon started with a project-relative `PATH` entry would defeat this. (3) The external CLIs' shells are assumed to keep their folder inside the project (Claude Code resets it there); relative paths with `..` are refused for them. (4) The `actor` marker is the browser host's word: a browser host that lies about it gets the browser person keys (never the local-only ones, never Jarvis's chain while Jarvis is on). `commands/persists.py` classifies the built-in commands by hand; a command file or skill command is judged by its tool calls, not here. (5) `clientKind` is self-declared (above).

### Review 6, part B (2026-11-16): what a remote login cannot start or leave behind

A remote web login is the person for running work, on a channel that is weaker than a local one (a link or a password over plain HTTP). Review 6 found it could still start a program of its choosing and leave access behind. Every refusal below is `jarvis_person_only` with `details.reason: "local_only"` (new in the enumeration; `details.what` says which part), written to the ledger. The tables are in `server/settings_gate.py`; a local person is never narrowed.

| Finding | Fix |
|---|---|
| **H-1** `mcp.add`, `mcp.update`, `mcp.test` ran a remote login's `command`/`args`/`env`; `force`/`acceptFindings` overrode the security findings; `skill.install` took any path or git URL. | `remote_mcp_refusal`: an entry that runs a program (`command`, `args`, `env`, `cwd` — `MCP_LAUNCH_FIELDS`) is refused unless it is the saved entry's launch, unchanged — so a url-only server, probing a saved server, and switching a saved command server off and on stay; `force` and `acceptFindings` are refused. `skill.install`: only `registry:<id>` and `<marketplace>/<plugin>` (`remote_skill_source_problem`); never a link, a path, a git URL, `github:`/other schemes or a shortcut. `provider.loginWeb` from a remote login opens no browser on the daemon's machine. |
| **H-1 / M-4** settings keys of `REMOTE_LOGIN_KEYS` named programs and identities. | `REMOTE_LOGIN_NEVER_KEYS` (either scope, Jarvis on or off): `mcp.servers.*.{command,args,env,cwd}`, `lsp.servers`, `audio.stt.command`, `audio.tts.command`, `audio.player`, `audio.recorder`, `gateway.*.{token,app_token,allowed_user_id,user_id,channel_id,enabled}`, `providers.*.base_url`, `skills.registry`. Deleting a whole parent (an MCP server, a messenger block) only takes away and stays allowed. |
| **M-4** `gateway.bind`, a provider's endpoint and jobs outlived the login. | `gateway.bind` is a local person's (no longer in `REMOTE_LOGIN_ALLOWED`). `provider.configure` with `base_url` is refused. `job.schedule` makes prompt jobs only (a `jarvis-task` target cannot be set over RPC; there is no job kind that runs a shell line); from a remote login a `sessionTemplate.hostToolsFrom` is refused, and the job is stored with `createdBy: "remote_login"` (`JobInfo.createdBy`) and counted in `web.remote.status.remoteJobs`. A remote login's job delivers in-app only: `channel` absent or `"log"` (`REMOTE_LOGIN_JOB_CHANNELS`); a messenger channel is refused, and `job.update` cannot change a channel at all, so one a local person saved stays as saved. A remote login changes, disables or re-enables, cancels and deletes (`job.update`, `job.cancel`, `job.delete`) only jobs it created; a job made by a local person, a browser or with no recorded creator it may read (`job.list`, `job.runs`) and run as saved (`job.runNow`, the same class as prompting a session). |
| **M-1** an expired login kept its WebSocket; logging everyone out closed only the listed logins' sockets. | `RemoteAuth.session` closes an expired login's sockets like a logout; the listener owns the set of every socket it opened (`RemoteAuth.sockets`), and `end_all` (rotate, set or clear the password, stop, a moved listener) closes all of them. |
| **M-2** slash commands reached local-only handlers (`/update` → `system.update`, `/desktop`, `/mcp add` with no connection). | `commands/persists.py`: level `local` (`/desktop`; and by derivation every command whose RPC twin is a local person's in `person_gate.PERSON_METHODS`: `/update`, `/allow`) and `COMMAND_TWINS` (command → the RPC methods it performs). `registry.gate_command` refuses a `local` command, and any command whose twin the dispatcher's tables refuse, to every connection that is not a local person's (remote login, browser with or without the marker, connector). `/mcp` passes a remote login's connection to the handlers, so H-1 applies to it too. The owner-bound messenger chat (`caller="messenger"`) keeps what it ran before (`/update`, `/desktop`, `/mcp …`); `/allow` refuses it itself. |
| **M-3** guesses were limited per address only, and an IPv6 host has a whole /64. | The per-address limit counts by /64 for IPv6 (`rate_key`); at most two scrypt checks run at once; while the everyone-together bucket is full every attempt first waits (1 s doubling per failure over the limit, capped at 30 s) and is then checked. |
| **L-5** with `trustProxy` anyone who reaches the port directly can claim an address and lock its person out. | The per-address limit no longer refuses unchecked: over it, an address's attempts go one at a time, each after the same growing wait, and a right credential gets in (a wrong one is `429`; more than four queued are `429` unchecked; an address over its own limit no longer adds to the everyone-together bucket). The manual says `trustProxy` needs the port closed to everything but the proxy. |
| **L-1** a connector that became a session's origin by `session.resume` answered its approvals. | A connector's `session.resume` never sets `origin_conn`; an origin connector's `allow` is not an answer (the request moves to the shared queue for a person, as a refused person-only allow does), and `may_answer` no longer lets an origin that is not a browser answer by `approval.respond`. |
| **L-2** `session.create {hostToolsFrom}` was unchecked. | `host_choice_refusal`, in `session.create`, `session.attach` and a job's `sessionTemplate`: a local person, or a browser naming itself (its `clientId`, its surface id, or the alias `"browser"` its standing orders and Tasks store); never a remote login, a connector, or one browser for another. A job's template is judged when its creator makes it and the job records the creator's kind (`created_by`; stored jobs without it load as before), so the session the scheduler later creates in-process inherits exactly that. |
| **L-3** `tool.progress` ownership was by tool name. | The in-flight call records the surface it was sent to; only that connection reports. `message` is capped at 65536 characters. |
| **L-4** host text reached the model unbounded. | `HostToolSpec`: `description` ≤ 65536, `descriptionBy` and `sections` ≤ 50 entries with names ≤ 64 and texts ≤ 65536 (a section is still kept up to 20000), `helpers` names match an identifier pattern (dotted allowed). |
| **L-6** `fs.*` ignored who called. | A browser (0.3.0+) reads with `fs.*` only on a session it hosts, or an unhosted one it opened (`not_found` otherwise). A browser of any version, marker or not, writes with `fs.write` only into a session it hosts (`hostToolsFrom` is its `clientId`, surface id or `"browser"` — having opened it is not enough; `jarvis_person_only`/`person_only` otherwise), inside the session's folders as before (`outside_root`), and never a control file; a remote login sees every session, so nothing narrower applies. `fs.write` of a control file (`trust.projects.control_path`, `.git/hooks`, `.git/config*`, the agent CLIs' config) is a local person's: `protected` for every other kind (the method cannot ask). |
| **L-7** `llm.sample` decoded images on the event loop. | The check runs in a thread; an image declaring more than 50 million pixels is refused from its header. |
| **Info** an unhandled handler error returned the exception text. | Every kind but a local person gets `internal error (id …)` with `details.errorId`; the text is in the daemon log under that id. |
| **Classifications** unmarked browser `fs.write`, `checkpoint.restore`, `session.deleteSaved`, `agent.create`. | From the browser team's source check: `agent.create` and `agent.delete` are never a browser's (`BROWSER_NEVER_METHODS`, marker or not, any version); `fs.write` is its host's own path (saving a download into `<workdir>/artifacts`, the repl's `project.writeFile`) and follows L-6's hosted-session rule. `checkpoint.restore`, `session.deleteSaved` and `session.close` stay open with no marker, but only for the browser's own sessions (`BROWSER_OWN_SESSION_METHODS`, `browser_session_refusal`): one it hosts (`hostToolsFrom` is its `clientId`, surface id or `"browser"`) or one a browser opened (`originSurface: "browser"`), live or stored. Any other session is `person_only` — for `checkpoint.restore` even with the marker; for `session.deleteSaved` (a bulk delete included) and `session.close` the marker opens it as before. |

**Every RPC that starts a process, for a remote login** (the table and its test are `tests/test_security_review6_limits.py`): *refused* — `system.update|restart|shutdown`, `extension.*`, `external.clis.confirmPath`, `project.trust.set`, `mcp.preset.apply`, `gateway.bind`, `web.remote.*`, `tool.register`, `setup.applyDefaults`; *narrowed* (the handler refuses the part that names a program) — `mcp.add|update|test`, `settings.set`, `skill.install`, `provider.configure`, `job.schedule`; *fixed* (only what snowpea ships or the person saved) — `mcp.reload|remove`, `lsp.install|restart`, `audio.install|speak|transcribe|record.*`, `provider.loginWeb`, `external.create|open|send`, `checkpoint.*`, `file.complete`, `gateway.sync`, `system.reloadSettings`, `session.create|resume`; *a turn* (every command the model runs meets the permission system) — `session.prompt|steer|continue`, `command.run`, `job.runNow`, `agent.spawn|create`, `team.start`, `skill.create|learn`.

**Known limits.** The waits bound how fast one address guesses, not how many addresses guess: a caller with many addresses still gets two scrypt checks at a time.

### Review 7, part B (2026-11-16): what a remote login is, and who asked

**The model (lead's decision).** A remote web login may, by design, create a session in any folder, raise it to `auto` (`session/mode_guard.py`), prompt it and answer its own approvals: that is the daemon user's shell. So the per-RPC limits on a remote login are defence in depth, not a boundary — whoever holds the access link or the password can run programs as the owner — and the product says exactly that: the manual (`docs/manual/*/web.md`, top of the remote-access section) and the warning every client shows (`web_remote.REMOTE_ACCESS_WARNING`, carried in `web.remote.status.warning`). What a remote login cannot do through snowpea's own screens is described as "kept on this computer so a mistake or a borrowed session cannot change it by a click". The findings below are fixed all the same: several are plain bugs for every caller, and "by a click" limits must hold. Protocol stays 1.55.0; refusals use the existing codes and enumerated `details.reason` values (`local_only`, `mode_raise`, `person_only`), plus one new reason, `snowpea_home` (`invalid_params`), and one new `details.kind`, `closed`.

**One owner control: `web.remote.maxMode`** (`plan` | `accept` | `auto`, default `auto` = no limit; a local person's key like all of `web.*`; `snowpea web max-mode`; reported in `web.remote.status.maxMode`; read at every call). A remote login cannot raise a session above it (`session.setMode`, `/auto`, `plan` with `autoAfterPlan`, a person-only mode question it answers), create a session or a job above it (also by the folder's default mode), prompt, steer or run a `turn` command in a session already above it, run or change a job above it, or set a project's `defaultMode` above it; a job it scheduled does not run above it either (the run ends `mode_above_remote_max`), also after the limit is lowered. Refusal: `jarvis_person_only`, `details {reason: "mode_raise", kind: "remote_login", to, max}`. It may still read such a session, lower its mode and answer its approvals — so `maxMode` stops unattended runs, not the holder of the link, and the manual says so.

**The caller snapshot (H-1, the structural fix).** `person_gate.CallerSnapshot` — kind, the browser's `actor` marker, client kind, client id, surface id, client version, the in-process identity, the turn origin, and the connection itself for identity only — is taken when work is ACCEPTED and is what every later judgement reads; `connection_kind()` and every gate that takes a connection take a snapshot in its place. It is carried by: a queued slash command (`QueuedCommand.who`; the command is gated AGAIN when its turn comes, `CommandRegistry.run(who=…)`), an instant command run beside a turn, `CommandContext.who`, a queued or folded prompt (`QueuedTurn.who`), and every turn (`Session.turn_callers`, `agent.loop.note_caller` / `turn_caller`). `session.continue`, `goal.set|resume`, `agent.spawn` and `team.start` take their caller in one place (`person_gate.accept_work`). A delegated child, a team worker and a goal round answer with the caller of the turn that started them (for children an RPC started in an idle session: who asked, `note_spawner`); a steer or a prompt folded into a running turn makes that turn the new caller's too when that caller is not a local person (never the reverse); a scheduled run of a job a remote login made is a remote login's turn. Tools that act for the turn's caller read it (`schedule_create`, `schedule_cancel`, `mcp_add`). `connection_kind` now tells `closed` (a connection that has closed or never signed in — nobody, refused everywhere, `settings_gate.refuse_closed`) from `none` (an in-process caller, `conn is None`): the two were the same value, which is how a queued `/mcp add` lost the RPC's limits when its remote login disconnected.

| Finding | Fix |
|---|---|
| **H-1** a queued `/mcp add` ran after the remote login disconnected; a closed connection was judged as in-process. | The snapshot above. `/mcp` passes the snapshot to the handlers (`mcp_cmd._remote`), so a queued command is a remote login's whatever its connection is by then. Every `connection_kind(...) == "none"` / `conn is None` trust decision was read again: `guard_call`, `kind_method_refusal`, `host_choice_refusal`, `remote_mcp_refusal`, `gate_command`, `may_answer`/`list_visible`, `require_person`, `delegation.Caller.from_connection`, `ModeCaller.from_conn` and the scheduler entry point refuse `closed`. |
| **H-2** `settings.set {scope: "project"}` with the parent of `$SNOWPEA_HOME` as `workdir` rewrote the global `settings.json`. | `config.project.home_problem` / `server.project_gate.require_project` — one helper for every RPC that takes a project folder: the home, a folder inside it, and (for the methods that use `<workdir>/.snowpea` as the project scope) a folder whose `.snowpea` IS the home are `invalid_params` "this folder is snowpea's own home, not a project" (`details.reason: "snowpea_home"`), for every caller. `ProjectSettings.load`/`save` never read or write the global file as a project's, whoever forgets the check. `media.*` joined `PERSON_LOCAL_ONLY`; a remote login's project-scope patch is judged against an ALLOW table (`PROJECT_REMOTE_LOGIN_KEYS`: `defaultMode`, `agents`, `models`). Every settings leaf is classified in `server/settings_leaves.py` (plain, or something the daemon runs or calls, with who may write it), and `tests/test_security_review7_settings.py` walks the schema so a new command-like key cannot be added unclassified. `Settings.load` degrades (failing sections back to defaults, the file copied to `settings.json.invalid`, an error in the log) instead of stopping the daemon. |
| **H-3** a remote login could make the daemon send stored credentials to a URL it chose (`${NAME}` filled from `credentials.json`, then the environment, into headers). | For a remote login (`settings_gate.remote_mcp_refusal`, `remote_mcp_settings_keys`): an entry with a `${VAR}` is refused (`details.what: "variable"`), and the `url`, `headers` or `oauth` of a SAVED entry cannot change (`"target"`) — `mcp.add` with `replace`, `mcp.update`, `mcp.test` with overrides, `settings.set mcp.servers.*`, `/mcp`, the `mcp_add` tool in a turn it prompted; probing or switching a saved entry stays. For everyone: an entry never expands a `SNOWPEA_*` environment variable, and an entry a remote login added is saved with `savedBy: "remote_login"` and never expands a `credentials.json` value (only `settings.mcp.allowVars` names, from the environment); a local person's edit adopts the entry. |
| **M-1** `skill.write` / `skill.create {content}` wrote a remote login's text verbatim. | `server/skill_gate.py`: a remote login writes only a GLOBAL SKILL whose front matter has none of `allowed-tools`, `autoInject`, `permission(s)`, `hooks`, `model`, `tools` (and the other keys the two loaders act on), under a name no built-in command, agent, role or skill has. The project scope is `protected` (the control-file rule of `fs.write`); a global agent or command is refused. The same judgement for `skill.learn` and for the document a model writes for `skill.create {description}` / `/skill create|learn`. `agent.create` and `agent.bindChannel` are a local person's; `team.guide.delete` is the class of `team.guide.set`. |
| **M-2** `/schedule` and the `schedule_create` tool bypassed the remote-login job rules. | `scheduler/access.py` — ONE entry point (`schedule_job`, `guard_job`) for `job.schedule`, `/schedule` and the tool, taking the caller snapshot: `createdBy` from it, the channel rule, the mode (a raise is a person's; `maxMode`), ownership (a remote login changes only its own jobs). `schedule` has its RPC twins in `COMMAND_TWINS`. |
| **M-3** a remote login's `approval.respond` with `always` / `project` stored allowlist rules. | `approval_queue.clamp_scope`: from a remote login, a connector or a closed connection `project`, `always` and `site` apply as `session` — in `approval.respond` and in the reply to `approval.request`; the result and `approval.resolved` report the applied scope. No question answer stores a rule or a setting; a remote login's answer to a mode question keeps `maxMode`. |
| **Low 2** four queued wrong attempts from one address made every further one `429` unchecked. | An attempt past the queue waits the longest penalty and is then checked: no credential is refused unchecked. |
| **Low 3** an expired login kept its socket up to 60 s. | The remote WebSocket asks whether its login still stands before every frame. |
| **Low 5** exception text reached subscribers. | A failed command reports `internal error (id …)` (the `error` event and `command.result`), an MCP probe failure reaches non-local callers as an id, a settings validation error lists fields without values, and a typed refusal raised inside a command keeps its own words. |
| **Low 6** a browser's "own session" matched any browser. | The server names a session's `originSurface` (`session_handlers.origin_surface_for`: a browser's is `browser`, the core's own surfaces are refused) and records the creating connection's `clientId` with the session (`sessions.origin_client_id`). Own (`settings_gate.owns_session`) = hosted by this browser's id, or created by it; the alias `"browser"` means this browser only while it is the single browser host connected. |
| **Low 9** `fs.write` resolved the path twice. | Once: the path the control-file rule judges is the path written. |
| **Info** | The never-registered `provider.configure` handler that wrote `media.mcp` is deleted. `audio.transcribe {path}` from anyone but a local person takes only a recording or an attachment (`outside_root`). A remote login may still set a provider's key or sign a provider in: the manual documents it and the IDE's remote page offers it (only the base URL is withheld) — left as is, to be decided. |

**Known limits.** (1) The decision "environment expansion limited to an explicit allow-list per entry, for every caller" is NOT implemented for the user's own entries: a global or `settings` entry a local person wrote still expands any environment variable but `SNOWPEA_*` (changing that breaks every existing `${VAR}` configuration and needs a migration decision). Entries have no record of the host a credential was saved for, so there is no "variable header sent to another host" finding. (2) `savedBy` is a mark inside the entry: whoever can write the file can remove it (part A's control-plane rule covers `$SNOWPEA_HOME/.mcp.json`). (3) A session in the folder that holds `.snowpea` (the user's home directory with the default home) is still an ordinary session; only its project scope is refused, and its project settings read as empty. (4) The sign-in limits bound one address, not many (Low 1, documented): about twenty password guesses a second in total. (5) Low 7: a remote login's writes into a project change pinned files, so the project's trusted content stops applying until a local person trusts it again — expected, documented. (6) The owner-bound messenger chat is an in-process caller: it is not narrowed by M-3 or `maxMode`.

### Review 6 (part A): the control plane, interpreter code, project trust

Root cause of the three Highs: in `auto` mode the only gate on a shell line is the security lists (`policy.py` allows `exec`; the loop asks only when the lists say blocked or opaque; the delegation classifier is not consulted when the mode already allows). Everything below is therefore in `security/` — the gate that runs before the mode, the allowlist, the session cache and Jarvis's routing — and holds in every mode. Protocol stays 1.55.0.

**H-1 — snowpea's own control plane (always on).** One table, `security/control_plane.CONTROL`, names the files that decide what snowpea trusts and who may talk to the daemon. It is not a preset: no list, setting or mode removes it, and it is judged even when both lists are empty.

| Under `$SNOWPEA_HOME` | Level | Meaning |
|---|---|---|
| `daemon.json`, `token*`, `credentials*.json` (incl. the web password hash), `mcp-oauth.json` | `all` | never read, listed, copied, written, deleted or moved by a tool |
| `settings.json`, `trust/**` (`projects.json`, `migrated.json`), `jarvis/**` (delegation db, ledger, presets), `security/**` (written-paths record), `state.db*`, `install.json`, `update-plan.json`, `update-helper.ps1`, `gateway-chats.json`, `external/**`, `logs/approvals.jsonl`, `logs/security.jsonl` | `write` | never written, deleted, moved or re-moded; reading is an ordinary read |
| `hooks.json`, `hooks/**`, `plugins/**`, `extensions/**`, `skills/**`, `commands/**`, `agents/**`, `.claude/**`, `.codex/**` | `ask` | a shell line or code that writes there is put to a person in every mode |
| `plans/`, `sessions/`, `attachments/`, `tool-content/`, `audio/`, `cache/`, `checkpoints/`, the other `logs/`, `update-check.json`, `teams/` | working | not in the table |

Both the daemon's home and the user's default `~/.snowpea` are judged; symlinks are followed; deleting or moving the home or a folder above it is blocked; a recursive copy or extraction onto a folder above the home asks. A test holds the table against `config/paths.Paths`: a new path property that is neither in the table nor in the working list fails it. The same table answers everywhere a path reaches a tool: shell effects and code effects (`check._control_verdict`), the file tools (the same check; `config_guard._secret_path` reads its secrets from the table), and `control_plane_effect(path, access, *, home, user_home, cwd)` for the `fs.*` methods (part B). `settings.json` stays editable by a file tool behind the `config` question; its `security` section stays out of every tool's reach as before.

A line whose program is snowpea's own CLI — `snowpea …`, a path to it, `uv run`/`uvx`/`npx snowpea`, `python -m snowpea_core…`, a script inside `snowpea_core/`, code that imports `snowpea_core` or spawns `snowpea` — is opaque with reason `acts on snowpea's own controls`: asked in every mode, person-only, never Jarvis, never cached or remembered, and not switched off by any setting. The CLI acts with the token from `daemon.json`, so it is the local person; `snowpea project trust` from an agent's shell was scenario (a).

**H-2 — interpreter code, however it is fed.** A here-document or here-string fed to an interpreter is analysed exactly like `-c` code. A pipe is read when its source is a literal `printf`/`echo` or `cat <file>`, a `< file` redirect is read as a script; any other stdin, `-`, or no script at all is opaque (`code:stdin`). `python -m <module>`: a module found in the project (`<cwd>/` or `<cwd>/src/`) is read as a script; `timeit` operands are analysed as code; `cProfile`/`pdb`/`trace`/`runpy`/`coverage run` read the script they are given; `uv`/`poetry`/`hatch`/`pdm` are judged as the command runner of that name; the app servers (`uvicorn main:app`, `gunicorn`, `flask`, `streamlit run app.py`, …) have the project module or file they are pointed at read as a script; modules in `security/shell.SAFE_MODULES` (the standard library's tools that run no code of the caller's — `json.tool`, `venv`, `compileall`, `unittest`, … — and test runners, linters, formatters and builders; `http.server`, `pydoc` and `inspect` are not there) run as before; any other module is opaque (`code:module`). Test runners run the project's tests, including ones the agent just wrote, unread — as plain `pytest` does. Clustered flags are read letter by letter (`-pe CODE`, `-ne`, `-uc`; `-MFile::Path` is a module name). node `--eval=`/`-p` are code; a preload (`-r`, `--require`, `--import`, `--loader`) that is a path is read as a script, one that is a package asks only when the agent wrote into that package. A line that sets a code-carrying variable (`NODE_OPTIONS` beyond runtime tuning flags, `BASH_ENV`, `ENV`, `PYTHONSTARTUP`, `PERL5OPT`, `RUBYOPT`, `LD_PRELOAD`, `GIT_SSH_COMMAND`, … — `security/shell._CODE_ENV`; before a command, after `env`, or with `export`) is opaque; `PYTHONPATH`/`NODE_PATH`/`PERL5LIB`/`RUBYLIB` only when it names a folder the agent wrote into. The classifier's `exec:opaque:*` hits are copied into the view (git `-c` of a non-harmless key, editor and pager commands, awk `system`/pipes, sed `e`/`w`, `rsync -e`, agent CLIs, …) except those the security walk reads itself and more exactly (`shell._NOT_COPIED`). `trap '<line>'` is parsed as a line. `read_script` reads a file whose name holds `$` or a backtick. A script in a language nothing reads (lua, R, awk, sed, julia, …) is opaque when it is the agent's own.

`make <target>` and `npm|yarn|pnpm|bun run <name>` read the project's own line (the Makefile target's recipe with its prerequisites; the `package.json` script with its `pre`/`post` scripts). A project file that was there before the session is the project's own — that is the boundary project trust draws: its literal lines are judged (a blocked command in them blocks), what they compute is not held against the line. A file the agent wrote this session counts everything: a recipe with make variables, a script that does not parse, a missing target are opaque.

**H-3 — Python is read as a syntax tree.** `security/pycode.py` uses `ast.parse` only (nothing is compiled, imported or run). Imports, aliases, from-imports, `f = shutil.rmtree`, `__import__("x")`, `importlib.import_module("x")` and `getattr(mod, "name")` resolve to the dotted name they are; literals are decoded by the parser; constant paths are folded (concatenation, f-strings of literals, `os.path.join`, `Path(...) / ...`, `Path.home()`, `.parent`, `__file__` of a script, a name assigned once, a loop over a literal `glob`). Opaque: a parse failure; `exec`/`eval`/`compile` of a computed string; `getattr`/`vars()`/`globals()`/`__dict__` used to reach a call; a subscript or call-result callee while a dangerous module is imported; a star import of `os`/`shutil`/`subprocess`/`pathlib`/`sys`; a dangerous callable or module used as a value the tree does not follow; `ctypes`, `pty`; a spawn whose command is computed (a list of words with computed members is walked like a shell command whose computed words are `$X`). Node, Perl, Ruby and PHP keep their tables with the uncertainty flipped: a call the tables match in the text but not once strings are blanked, next to a construct the blanking does not understand (a regex literal, a comment, a here-document, a template expression, an unclosed quote), is opaque; in JavaScript also a bracketed lookup on `fs`/`child_process`/`process`, an alias or a renamed destructuring of one of their calls, and `require`/`import()`/`eval`/`Function` of anything but a literal. "No finding" is allowed only when the code was read whole and nothing uncertain was seen; code past 256 KB is opaque. Project files the code pulls in are read like the script itself — Python `import helper` (looked for beside the script, in the project and in `src/`), JavaScript `require('./x')` / `import './x'` — up to 48 files and six levels; past that, a file the agent wrote that stays unread makes the line opaque, the project's own are let be. Installed packages are not read.

**M-1 — unresolved targets, and `security.unknownCode`.** A writing, truncating, copying, moving, deleting or re-moding call whose target does not resolve is opaque. `security.unknownCode: "ask" | "allow"` (global settings only, a local person's key like the rest of `security.*`; refused in the project scope with `details.reason: "global_only"`; shown by `security.get.unknownCode`) decides what happens to code the check cannot resolve — every reason with the `code:` prefix. `allow` lets it through; blocked findings stay blocked and what acts on the control plane still asks. `ApprovalRequest.reason` is `cannot tell what this code changes` when only such reasons are in the way.

**M-8 — the agent's own files.** `security/written.py` records every path a session writes — a file tool's paths, a shell line's write effects the check saw, and what the checkpoint scan found changed after a shell call — in `$SNOWPEA_HOME/security/written.log` (append-only, in the control-plane table). A recorded path is the agent's own for the whole session and across daemon restarts; the 600 s age window is one criterion of two, no longer the only one.

**Project trust (M-2 … M-6, L-1 … L-5).** No digest cache: a trust decision hashes the disk, and `TrustStore.read_pinned` returns the verified bytes so hooks, MCP servers and prompt overrides are parsed from what was verified (a skill's `allowed-tools` and `autoInject` are checked against the digest of the bytes the skill was loaded from). Trust pins what a control file runs: path-like words of hook commands and of MCP `command`/`args` that are files inside the project, and every other file in a skill's folder (`status.runs`); a changed one switches its control file off, at load and again before each hook run. Commands whose code cannot be pinned are listed (`status.notPinned`, reasons `computed`, `project-script`, `package-runner`, `module`, `missing`, `too-large`, `too-many`, `unreadable`). `project.trust.set` takes the `digests` the status returned and fails with `trust_changed` when the disk differs. The migration marker is its own file (`trust/migrated.json`, written first, never rewritten); with the marker present a missing or unreadable record means nothing is trusted, said by `security.get.problems`, the first session notice and `project.trust.changed {reason: "store_lost"}`. A project's skills, commands and agents exist only for sessions inside that project, a project entry never takes the place of a global, plugin or built-in one of the same name (the simpler of the two options: it is left out, not namespaced), and a project skill's `autoInject` needs the `commands` trust. A project agent definition with a `permission`, or with a built-in agent's name, needs the `agents` trust. Only a local person's answer re-pins a project's settings trust when it stores a project rule. The prompt cache is cleared on every trust change. The trust card data shows a command's head, tail and length. A `custom` list that is not a list is a problem line. Path names are compared folded (NFC, casefold) on macOS and Windows in the security check, the config guard and the control-path table through one helper (`util/pathcase.py`).

**The limit, stated plainly.** All of this is static analysis of the text of a call, and it is best effort. A process running as the same user that computes a path, a command or code while it runs — from a network answer, from a file it reads, by arithmetic on strings — is not stopped by it; the check asks when it can see that it cannot tell, and it cannot always see that. Library calls that write where they are told (`numpy.save`, `DataFrame.to_csv`, a compiled binary) are not in any table. The real boundary for "this process cannot touch those files" is OS-level enforcement (Linux Landlock or bubblewrap, a macOS sandbox profile); that is separate work awaiting the owner's decision and nothing here claims to replace it. Also not in this round: a nonce confirmation on a person's surface for local-only RPCs (so that holding the daemon token is not enough to trust a project or change delegation) — recorded as a follow-up; today the token is protected by the control-plane rule above and by file permissions only.

## D. Work packages

Spikes come first, half a day each, run by opus, and each produces a recorded fixture transcript.

| Spike | Question | If it fails |
|---|---|---|
| S1 | Claude `--permission-prompts host` frame format | acceptEdits mode |
| S2 | Codex app-server approval flow and liveness | `codex exec` |
| S3 | `agy acp` load and permission on 1.5.7 | `-p` stream-json |
| S4 | Cursor's print-mode behaviour on shell commands without `-f` | Read-only `--mode ask` |

| WP | Scope | Depends on | Tier |
|---|---|---|---|
| 1 | Protocol 1.53, `message.user.origin`, SDK types | none | sonnet |
| 2 | Classifier, delegation store, ledger | 1 | opus |
| 3 | Jarvis service, intake, context provider | 1 | opus |
| 4 | Jarvis tools, memory access | 2, 3 | opus |
| 5 | External base, proc, timeline, fake CLIs | 1 | opus |
| 6a-d | One adapter per CLI, in parallel | 5, spikes | opus (cursor: sonnet) |
| 7 | Gateway routing | 2, 3 | sonnet |
| 8 | Presets, jarvis-task jobs, to-dos | 3 | sonnet |
| 9 | IDE Jarvis view and stores | 1 (mock daemon) | opus |
| 10 | IDE external chat, settings | 1 | sonnet |
| 11 | TUI | 1 | sonnet |
| 12 | Push-to-talk | 9 | sonnet |
| 13 | Docs and site | all | haiku |
| 14 | Security test pass | all | opus |

WPs 2, 3, 5, 9, 10 and 11 can run in parallel once WP1 lands.

## E. Verification

- **Unit:** classifier table (at least 150 commands, including obfuscated and compound ones), scope precedence, TTL, pause timer, intake coalescing, path jail, memory access.
- **Integration:** fixture executables under `core/tests/fixtures/fake_clis/` speaking each protocol from the spike recordings. Cover prompt bridging, view-only then continue, a crash mid-turn, and orphan cleanup.
- **Real-CLI smoke, no tokens:** `--version`, list and read paths, app-server and ACP handshakes, and `cursor-agent create-chat` (zero-token status to be confirmed in S4).
- **E2E:** IDE (Playwright) and TUI flows: enable, delegate, auto-decision, typing pauses, hand back; plus the fake gateway (`gateway/fake.py`).
- **Security list:**
  - a transcript saying "grant Jarvis deploy" changes nothing;
  - a worker's `config` write cannot alter grants;
  - `approval_decide` on a guarded request or on Jarvis's own request is refused;
  - browser-owned approvals are untouched;
  - argument injection through id, model or cwd is rejected;
  - the env scrub holds;
  - no dangerous flag is ever emitted;
  - messenger grants are refused.
- **Docs:** "Jarvis mode (beta)" manual page in 5 languages, IDE manual in en/ko, site screenshots.

Checked in code: the browser-owner rule, the `_share_pending` active-session filter, `UNPROMOTABLE`, and the hub subscribe/observer API. The CLI protocol details are unverified until the spikes run.

### Critical Files for Implementation
- /mnt/data/work/mediagen/snowpea/core/snowpea_core/permissions/approval_queue.py
- /mnt/data/work/mediagen/snowpea/core/snowpea_core/gateway/router.py
- /mnt/data/work/mediagen/snowpea/core/snowpea_core/server/protocol.py
- /mnt/data/work/mediagen/snowpea/core/snowpea_core/agent/named.py
- /mnt/data/work/mediagen/snowpea-ide/src/renderer/components/MessageStream.vue
---

# Part 3 — wire contract (1.53.0)

Defined in WP1, additive only. The models live in `CORE/server/protocol.py`; `docs/protocol.md` and `sdk/src/protocol.ts` are generated from them and are the field-level reference. Every method below is declared and, until its WP lands, answers `not_implemented` (the dispatcher registers that stub for any declared method without a handler). Capabilities follow the existing camelCase list; while a WP was pending its name sat in `PENDING_CAPABILITIES` (name → WP) so `system.hello` never advertised something that answers `not_implemented`. Every WP has landed: `PENDING_CAPABILITIES` is empty and all six names below are in `CAPABILITIES`.

## Capabilities

| Capability | Covers | Advertised from |
|---|---|---|
| `messageOrigin` | `message.user.origin`, trajectory `data.origin`, human-only `lastPrompt`/`lastUserAt` | WP1 (now) |
| `jarvis` | `jarvis.status/enable/disable/overview`, `jarvis.project.*`, `jarvis.escalations.*`, `jarvis.state`, `jarvis.escalation.dismissed`, `jarvis.access`, `SessionSummary.role/jarvis` | WP3/WP4 |
| `jarvisDelegation` | `jarvis.ledger`, `jarvis.delegation.*`, `jarvis.takeover/handback`, `jarvis.decision/escalation/delegation.changed`, `ApprovalRequest.route`, `ApprovalRequest.personOnly` | WP2 |
| `externalSessions` | `external.*`, `ApprovalRequest.external`; per-CLI availability is `external.clis`, not capability strings | WP5 (+ WP6a–d) |
| `mcpPresets` | `mcp.preset.list/apply/check/remove` | WP8 |
| `jarvisBriefing` | `jarvis.briefing.get/set` | WP8 |

## Methods

| Method | Params → result | Semantics | WP |
|---|---|---|---|
| `jarvis.status` | – → `JarvisStatusResult` | enabled, running, sessionId, model, folder, delegationDefault, budget, paused sessions, hidden projects, problems | 3 |
| `system.hello` (existing) | unchanged | `clientKind` is pinned by the connection's first successful hello: a later hello with another kind (or none) is `invalid_params` (security review C3) | – |
| `jarvis.enable` | `{model?, folder?}` → status | set `jarvis.enabled`, create/resume the Jarvis session; idempotent. A `folder` that is, contains or lies in `$SNOWPEA_HOME` or a credential directory (`~/.ssh`, …) is `invalid_params`; so is `/`, the home folder or above, a registered project root, or a folder holding a credential directory (security review M9); so is such a `jarvis.folder` in `settings.set`. Local person only (`jarvis_person_only`) | 3 |
| `jarvis.disable` | – → status | clear it, park the session; everything routes to the person; messenger targets restored | 3 |
| `jarvis.overview` | `{project?, includeIdle, includeExternal, limit}` → `{rows, generatedAt}` | one row per supervised session: sessionId or `external`, project, title, status, host (external), pending approvals/questions, waitingOnPerson, level, pausedUntil, takenOver, lastActivityAt, digest | 3 |
| `jarvis.ledger` | `{sessionId?, decidedBy?, before?, limit}` → `{entries, nextBefore}` | paged decisions: kind, decidedBy `jarvis`/`person`/`auto`, decision, classification, guardedClass, rule, grantId, reason | 2 |
| `jarvis.delegation.list` | `{scope?, target?, includeExpired}` → `{grants, default}` | grants from `jarvis/delegation.db` | 2 |
| `jarvis.delegation.set` | `{scope, target?, level, allow[], expiresAt?, note}` → `{grant, replaced}` | the calling connection is the person; applies at once. Jarvis's `delegation_set` tool never calls it — it raises a person-only approval. Guarded classes: local or re-authenticated connection; from a messenger only with `jarvis.messenger.allowGrants` (default true); no `allow` on a global grant in v1 | 2 |
| `jarvis.delegation.revoke` | `{id}` → `{revoked}` | | 2 |
| `jarvis.takeover` | `{sessionId \| external, minutes?}` → `{state}` | the person takes a session back until hand-back or `minutes`; persisted in `jarvis/delegation.db` (a restart keeps it); any caller may take authority away; Jarvis's own session → `invalid_params` | 2 |
| `jarvis.handback` | `{sessionId \| external}` → `{state}` | authority returns now (ends a takeover or a typing pause); person only (`jarvis_person_only`) | 2 |
| `jarvis.escalations.list` | `{before?, limit}` → `{escalations, nextBefore}` | open escalations that are **not** a waiting request (finished, error, stalled, budget, jarvis), newest first, each as its `jarvis.escalation` params; kept in `$SNOWPEA_HOME/jarvis/escalations.json` until dismissed, so a reload or restart keeps them (waiting requests are `approval.list` / `question.list`) | core |
| `jarvis.escalations.dismiss` | `{ids[], all}` → `{dismissed}` | drop open escalations; every client gets `jarvis.escalation.dismissed {ids}`; neither ids nor all → `invalid_params` | core |
| `jarvis.project.list` | – → `{projects}` | root, name, aliases, exposed, named; names live in the Jarvis store, default workspace title or folder name | 3 |
| `jarvis.project.rename` / `.alias` | `{root, name?}` / `{root, add[], remove[]}` → `{project}` | | 3 |
| `jarvis.briefing.get` / `.set` | – / `{enabled?, schedule?, instructions?, timezone?, sections?}` → `JarvisBriefing {enabled, schedule, instructions, timezone, sections, jobId, nextRunAt}` | periodic briefing; off by default; an omitted field keeps its value, `timezone: ""` clears it (daemon-local); `sections` of sessions/waiting/todos/mail/calendar | 8 |
| `external.clis` | – → `{clis}` | per CLI `claude`/`codex`/`agy`/`cursor`: installed, enabled, version, path, capabilities `{streaming, approvals: bridged\|degraded, liveView, tmux, modes[], steer}`, `approvalPolicy` (agy: `asks`\|`auto`), `defaultMode`, `modes[]` (also when not installed), `models[]` (listed in the background without tokens, cached; empty until the CLI answered, then `external.clis.changed`), `pendingPath` (a `path` override not confirmed yet: not run, shown as not installed), problems | 5 / core |
| `external.clis.confirmPath` | `{cli, path}` → `{clis}` | the person confirms the resolved real path of a `path` override (`path` = the `pendingPath` they saw; anything else `invalid_params`); stored as `confirmedPath`, then probed. Local person only (`jarvis_person_only`). Clients that show a path field must add this confirm step | security review H2 |
| `external.list` | `{clis?, cwd?, sessionId?, allProjects, limit, before?}` → `{sessions, nextBefore}` | one working directory's sessions (default the workdir of `sessionId`), or every exposed project's; newest first | 5/6 |
| `external.open` | `{ref, limit, mode?, transport?, trust?}` → `{session, events, hasMore, readOnly}` | subscribe to `external.event`, newest page; a `host: none` session is continued under snowpea in `mode` (default the CLI's `defaultMode`). An adapter that answers "open elsewhere" (Codex "active writer") makes the row `host: terminal`, view only, at once. `transport: "tmux"` on a Claude session the person runs in a tmux pane of their own adopts that pane after a **person-only** approval (`tool: tmux.adopt`, `personOnly`; never Jarvis, the engine or an unattended rule): the call waits for it; declined → it stays view only. Starting a CLI in a project nobody trusted yet: a person without `trust: true` gets `external_unavailable` `details.reason: "untrusted_project"` (show the one-time confirmation, retry with `trust: true`); anyone else (Jarvis, browser host, connector, remote web login) `untrusted_project_person_only`. Without `mode` a session continues in its last mode unless that was `unrestricted` (then the CLI default) | 5/6, core |
| `external.get` | `{ref}` → `{session}` | one `ExternalSessionRow` without opening or reading it (deep links, pop-outs, restored tabs); `not_found`, `outside_root` | core |
| `external.unwatch` | `{ref}` → `Ok` | stop `external.event` to this connection; the process keeps running (an idle one is stopped after `jarvis.external.idleSec`). A screen that only goes away calls this, not `external.close` | core |
| `external.read` | `{ref, beforeSeq?, limit}` → `{events, hasMore}` | older timeline page | 5/6 |
| `external.send` | `{ref, text}` → `{turnId}` | prompt; the sender is subscribed to the ref (idempotent, as on open), so the turn streams to it as `external.event` without an `external.open`; no attachments in v1; `external_view_only` while `canSend` is false. A `host: none` session is continued first in its current mode or the CLI's `defaultMode` (`unrestricted` only for a person, else `jarvis_person_only`). During a running turn: added to it when `capabilities.steer` (Codex `turn/steer`; the same `turnId` back), else `session_busy` (a tmux pane that is not ready: see *Busy tmux pane*). A send from a human connection pauses Jarvis there | 6, core |
| `external.interrupt` / `external.close` | `{ref}` → `Ok` | stop the turn (`host: tmux`: Escape while Claude is busy, whoever started the turn, never C-c; `host: terminal`: `external_view_only`; nothing running: no-op) / stop snowpea's process and unsubscribe | 6 / 5 |
| `external.create` | `{cli, project, model?, mode?, transport, trust?}` → `{session}` | new session in a registered root, the caller subscribed as on open; `mode` from the CLI's `modes` (claude, codex, cursor: default\|readonly\|unrestricted; agy: default\|unrestricted); `transport` `structured` (default) or `tmux` (Claude only); `trust` as on `external.open` | 6 |
| `external.setMode` | `{ref, mode}` → `{session}` | switch mode from the next turn; `unrestricted` only from a human connection (`jarvis_person_only` otherwise), ledger entry | 6 |
| `mcp.preset.list` | `{locale?}` → `{presets, locale}` | id, title, description, category, auth (`McpCatalogAuth`), transport, entry, inputs, readTools, sendTools, recipes, homepage, applied, servers, appliedServers; texts in `locale` (see *Presets: check and language*) | 8 |
| `mcp.preset.apply` | `{id, inputs, recipes, scope, workdir?, name?}` → `{server, add: McpAddResult, servers, adds, signIn, already, jobs, needsAuth}` | write the server entries (`server`/`add` the first, `servers`/`adds` every one), schedule chosen recipes as `jarvis-task` jobs; `signIn` what still needs a sign-in per server; `already` when it was applied the same way | 8 |
| `mcp.preset.check` | `{id}` → `{id, applied, servers}` | is the person signed in, per server (below) | core |
| `mcp.preset.remove` | `{id}` → `{removed, servers, jobs, notice?}` | undo `apply`: put back what was there before (entries, tags, credentials, the OAuth sign-in), delete its recipe jobs; `removed: false` when it was not applied; `notice` says what the person must still do (a device-code server keeps its own sign-in) | 8 |

**Who may call (security review 2, `server/person_gate.PERSON_METHODS`, checked by the dispatcher).** P = passes the person rule, R = `jarvis_person_only`, N = `not_found` (another connection's external session does not exist for it). Columns: local person (IDE, web, TUI, CLI, desktop, no `clientKind`) · remote web login · browser host · connector/other.

| Methods | person | remote | browser | connector |
|---|---|---|---|---|
| `jarvis.status/overview/project.list/escalations.list/ledger/delegation.list/briefing.get`, `mcp.preset.check` | P | P | R | R |
| `jarvis.disable/project.rename/project.alias/escalations.dismiss/briefing.set` | P | P | R | R |
| `jarvis.enable`, `mcp.preset.apply/remove`, `external.clis.confirmPath`, `settings.set` of `PERSON_LOCAL_ONLY` keys (Part 2, "Third review"), `mcp.add/update` with `permission: "read"` | P | R | R | R |
| `settings.set` of other keys and the config RPCs above | P | `REMOTE_LOGIN_KEYS`; RPCs P | `BROWSER_PERSON_METHODS`/`_KEYS` with `actor: "person"` or `clientVersion` < 0.3.0; `BROWSER_HOST_METHODS` always | `CONNECTOR_KEYS` (settings); RPCs P |
| the same while Jarvis mode is on, for Jarvis's authority chain | P | R (`jarvis_on`) | R (`jarvis_on`) | R (`jarvis_on`) |
| `jarvis.delegation.set/revoke`, `jarvis.handback` | P | engine rule (no guarded classes) | R | R |
| `jarvis.takeover` | P | P | P | P |
| `mcp.preset.list`, `external.clis/list/unwatch`, `job.schedule/preview/list/cancel/delete/nextRunForClient/runs` | P | P | P (`external.list`: own rows only) | P |
| `external.get/read/send/setMode/interrupt/close` | P | P | N unless it watches the session | N unless it watches |
| `external.open/create` | P (trust: see above) | `untrusted_project_person_only` in an untrusted project | as remote | as remote |
| `job.update/runNow` of a `jarvis-task` job | P | P | R | R |
| `session.list/resume/events/trajectory` of a `jarvis-task` worker | P | P | hidden / N | hidden / N |

`jarvis.*` events (all of them), `external.state` and `external.clis.changed` go to people's connections only; a browser host or connector gets `external.state` for sessions it watches and `jarvis.access` lines whose `hostToolsFrom` is its `clientId` (L6).

## Events

| Event | Delivery | Payload | WP |
|---|---|---|---|
| `jarvis.state` | daemon-wide, like `sessions.changed` | `{reason: enabled\|disabled\|started\|stopped\|budget\|paused\|resumed\|settings, status: JarvisStatusResult}` | 3 |
| `jarvis.decision` | daemon-wide (the "Decided by Jarvis" panel is not tied to a subscribed session) | `{entry: JarvisDecision}` — entry has `importance` and a short `summary` | 2 |
| `jarvis.escalation` | daemon-wide; badge, OS notification and messenger per `jarvis.notify.*` | `{id, ts, sessionId \| external, project, title, reason: guarded\|risky\|never\|question\|finished\|error\|stalled\|budget\|jarvis, summary, importance, requestKind?, requestId?, guardedClass?}` | 2/3 |
| `jarvis.delegation.changed` | daemon-wide | `{reason: set\|revoked\|expired, grant}` | 2 |
| `jarvis.escalation.dismissed` | daemon-wide | `{ids}` — escalations a client dismissed | core |
| `jarvis.access` | daemon-wide | `{id, ts, sessionId, external?, project?, hostToolsFrom?, action: read\|prompt\|steer\|interrupt\|create\|approval\|question, summary}` (an external session: `sessionId` `ext:<cli>:<id>` and `external` its ref) — Jarvis read or instructed a session; the session's own surfaces (its origin, the owning browser host) show it as an audit line; also a ledger record of type `access` | 4 |
| `external.event` | only connections subscribed to the ref: those that called `external.open`, `external.create` or `external.send` on it, until `external.unwatch`, `external.close` or the connection closes | `{ref, seq, kind, payload, ts, turnId?}` — kinds and payloads are the session-event ones (`message.delta`, `tool.call`, `turn.done`, …), own `seq` per ref | 5/6 |
| `external.state` | people's connections, plus a browser host or connector that watches the session (security review L6) | `{ref, status, previous?, host, previousHost?, canSend, approvals, tmux?, mode?, controlled, title?, error?}` | 5 |
| `external.clis.changed` | people's connections (L6) | `{clis}` — `external.clis` as it would answer now (e.g. a CLI's `models` arrived) | core |
| `approval.route.changed` | the original request's audience: only the asking connection for an interactive approval (the one sent `approval.request`), every authenticated surface for one in the shared queue (`approval.pending`) | `{requestId, sessionId, route: person\|jarvis\|auto\|null, reason, hostToolsFrom?}` — a waiting approval's `route` changed; `approval.list` answers with the new route from then on | core |
| `question.route.changed` | daemon-wide, like `question.pending` (the asking connection included) | `{requestId, sessionId, route: person\|jarvis\|null, reason}` | core |

`reason` of a route change: `jarvis_enabled` (it was waiting when Jarvis mode came on and was just routed), `jarvis_disabled` (route back to null), `paused` (the person took the session over or typed in it: its `jarvis` requests become `person`), `resumed` (handback: routed again, possibly back to `jarvis`), `budget` (allowance spent), `declined` (Jarvis's wake turn ended without deciding it; a `high` escalation with its `requestId` goes out too), `refused` (the engine refused Jarvis's or its own decision on the re-check). A request that becomes the person's also reaches the bound messenger chat, once. The quiet-minutes pause running out on its own is not announced (the engine notices it lazily); the next request there is routed normally.

**Person-only approvals raised inside an RPC** (`tmux.adopt` while `external.open {transport: "tmux"}` runs, Jarvis's `delegation_set` while its turn runs): both are asked the same way — into the shared queue, `unattended`, so they go out as `approval.pending` to every authenticated surface (and to the bound messenger chat), never as a server→client `approval.request` to the calling connection, which waits on its RPC meanwhile. Any person's surface answers with `approval.respond`; `allow` counts only from a person (`jarvis_person_only` otherwise), `deny` from anyone who sees it. Neither is ever routed to Jarvis or the engine (`route: person`).

`tmux.adopt`'s `ApprovalRequest.args` are `{cli: "claude", sessionId, tmux: {session, pane}, cwd, paneCommand, paneCwd, unrestricted}` (the last three added by the security review: what tmux says runs in the pane, its folder, and whether it was started with a permission bypass) (`personOnly: true`, `risk: high`); `delegation_set`'s are the `jarvis.delegation.set` fields (above).

**Presets: check and language.** `mcp.preset.check {id}` → `{id, applied, servers: [{server, connected, authenticated: true|false|null, account?, detail}]}`: OAuth servers by the stored sign-in (and no 401 since), a device-code server by calling its read-only `verifyTool` (Microsoft 365: `verify-login`), `null` when the server offers no way to tell or is not connected; `account` is masked (`j***@example.com`), never a token or a full account id; an unknown preset is `invalid_params`, one not applied answers `applied: false`. `mcp.preset.list {locale?}` (`en` default, `ko`; `ko-KR` → `ko`; anything else → `en`) returns the human texts — title, description, input labels, server and recipe titles — in that language and says which in `locale`; ids, tools, entries and recipe prompts are never translated (`tools/mcp_catalog.PRESET_TEXTS`).

**Remote web OAuth return.** The IDE's host-caught redirect URI `<origin>/web/mcp-oauth` is a client-side route: both the loopback listener and the remote one (`web.remote.*`) answer any unknown `/web/*` path with the app (`index.html`, `Cache-Control: no-cache`; the remote one with its security headers, `Referrer-Policy: no-referrer`), so the page reads `code`/`state` and finishes with `mcp.authorize.complete`.

**Wakes.** Every message Jarvis receives from core — the intake's wakes, the briefing, a scheduled task's hand-over — that starts a turn reaches the Jarvis session's subscribers as two session events, in this order:

1. `turn.started {turnId, prompt: null, initiator: "system", reason: "jarvis-intake", queued}`;
2. `message.user {text, attachments: [], refs: [], steered: false, origin: {kind: "system", surface: null}}`, where `text` is the wake exactly as Jarvis's model gets it: a first line such as `Jarvis intake (automatic; nobody typed this).`, the session-derived part inside its `<untrusted source="…" nonce="…">…</untrusted nonce="…">` fence, and the closing instruction. Clients show it as a quiet system line (not a user bubble) and must render the fence as text, never as markup or instructions.

It is replayed by `session.resume` / `session.events` like any `message.user`. It never counts as the person's message: `lastPrompt` and `lastUserAt` skip it (human-only), it is not the session's `user_turn` (what `tool.invoke` and host tools judge intent by), it never triggers takeover detection (only `kind: "human"` pauses Jarvis), and the messenger bridge never echoes a `message.user` to the chat. Only a wake carries it: other system turns (`session.continue`, goal rounds, hand-backs) still emit no `message.user`. A wake that arrives while Jarvis's turn is running is not a turn: it is folded into that turn as a notice before its next model call, with no event of its own. The person's own messages to Jarvis are `human` (with the messenger platform as `surface` from a chat).

**Busy tmux pane.** `external.send` into a Claude session shared through tmux is refused with `session_busy` and the reason in `error.data.details.reason` (every error's extra fields live under `data.details`, as `mcp_auth_failed`'s `details.stage` and `unavailable`'s `details.reason` do; `data.code` is `session_busy`): `terminal_prompt` (a permission prompt is open in the terminal), `terminal_busy` (Claude Code is not idle there), `terminal_typing` (a person attached to the pane typed within the last 3 s).

None of the Jarvis events is a session event on the Jarvis session: clients need them without subscribing to it, and the Jarvis transcript already shows its own tool calls.

## Fields on existing models

| Field | Semantics | WP |
|---|---|---|
| `message.user.origin {kind, surface?}` | `human` (the session's person on any surface, incl. the bound messenger user; `surface` = client kind or platform), `jarvis` (Jarvis's `session_prompt` / `session_steer` into a worker, from WP4: never the person's words, so no `lastPrompt`, no pause), `schedule`, `gateway` (another member of a messenger chat), `subagent` (lead's brief/message in a child, a child's hand-back announce in the lead), `system` (core's or a model's text: queued commands, Jarvis wakes; goal rounds and `session.continue` turns emit no `message.user` at all, their `turn.started` says `initiator: system` and `reason`). Null on events stored before 1.53.0 = human | 1 (done) |
| trajectory `user` rows `data.origin` | copied from the event | 1 (done) |
| `SessionSummary.lastPrompt` / `lastUserAt` | only human-origin `message.user` (or pre-1.53 rows without origin); a session with only non-human prompts has none | 1 (done) |
| `session.fork` title | the fork keeps the source's title unchanged (no " (fork)"); untitled stays null | 1 (done) |
| `SessionSummary.role?: "jarvis"`, `SessionSummary.jarvis?: {exposed, level, pausedUntil, takenOver, levelScope, grantId}` | also on `sessions.changed` and the `session.create` result; `levelScope` `session\|project\|global\|default` (which scope supplied `level`), `grantId` the grant in force (null for the default) | 3 |
| `JarvisOverviewRow.levelScope?`, `.grantId?` | as `SessionJarvisState.levelScope` / `.grantId` | core |
| `ExternalCliInfo.modelsLoaded` | true once the CLI answered the model listing (`models` may still be empty: it lists none); false while it has not answered, or the CLI is not usable | core |
| `JarvisOverviewRow.exposed` | false for a browser-host session while `jarvis.exposeBrowser` is off: status only, empty `digest`, no transcript, no instructions | 4 |
| `JarvisOverviewRow.mode?`, `.controlled?`, `.approvals?` | for an external session: its mode, whether snowpea gates it (false: unrestricted, agy with policy `auto`, cursor outside readonly — Jarvis's guarded-class rules do not apply inside, so Jarvis only reads such a session and never sends it work), and how its prompts are answered (`person` for a tmux pane) | core |
| `ApprovalRequest.personOnly?` | true on Jarvis's `delegation_set` proposal: `tool: "delegation_set"`, `args: {scope, target, level, allow, expiresAt, note}` (the `jarvis.delegation.set` fields), from the Jarvis session, `route: person`. Answered with `approval.respond {decision: allow\|deny, scope: "once"}`: `allow` only from a person's connection (or the bound messenger person while `jarvis.messenger.allowGrants`), any other answerer gets `jarvis_person_only`; `deny` from anyone who sees it. On allow the engine applies the grant with the answering person as grantor (`proposedBy: jarvis`) | 4 |
| `ApprovalRequest.external?`, `QuestionRequest.external?` `{cli, id}` | requests from a hosted external session (`sessionId` `ext:<cli>:<id>`). An external CLI's approval is never an `approval.request` to a connection: it is queued (`unattended`) and announced as `approval.pending` (with `route`) to every authenticated surface, listed by `approval.list`, answered with `approval.respond` from any of them (or by Jarvis / the engine per `route`); the answer goes back to the CLI | 5 |
| `ApprovalRequest.route?`, `QuestionRequest.route?` `person\|jarvis\|auto` | where the engine routed it; null with Jarvis mode off. A read-like approval (security review 3) is routed `jarvis` under `staged` too (`approval.route.changed` / the engine's reason `readlike:staged\|full`) | 2 |
| `ApprovalRequest.argsEscaped?` | true when the call's tool name or arguments held characters a surface strips or that change how text reads (C0/C1 controls, ESC sequences, NUL, CR, bidi/invisible format characters): `args`/`tool` then show them written out (`\x9d`, `‮`), what runs is the raw text, the answer is never cached or remembered, and the engine reports it in every guarded class (`exec:opaque:control-chars`). Clients show `args` as given and should mark the card ("contains hidden characters") | review 3 |
| `actor?: "person"` (top-level param) on every method of `settings_gate.ACTOR_METHODS`, and on `ApprovalAnswer` / `ApprovalRespondParams` | set by a browser host (`clientKind: "browser"`) only for calls its Settings/setup pages make after its click gate, and for clicked approval answers; ignored for other kinds. Declared on the models of `settings.set`, `provider.configure`, `mcp.add|update|remove`, `job.schedule|update|delete` and the `JobIdParams` methods, the approval answer and `approval.respond`; on the other listed methods the dispatcher accepts it as a top-level param and takes it off before the handler's params are validated. A browser with `clientVersion` 0.3.0 or later must send it for `BROWSER_PERSON_METHODS` (refused `jarvis_person_only`, `details.reason: "person_only"`) | review 3 |
| `JarvisDecision.readOnly?` | true on a read-like approval (`classification: "risky"`, `rule: "exec:read-like"`); also on `jarvis.decision`. `classification` values are unchanged | review 3 |
| `approval.resolved.by` / `question.resolved.by` | new values `jarvis` and (approvals) `jarvis:auto` | 2 |
| `ExternalSessionRow` | `ref, title, cwd, project, updatedAt, status idle\|running\|error, host none\|terminal\|tmux\|snowpea, canSend, approvals bridged\|person\|degraded\|none, tmux? {session, pane}, mode? default\|readonly\|unrestricted, controlled, preview, model` | 5 |
| `JarvisDecision.kind: "unrestricted"`, `importance` (`low\|normal\|high\|urgent`) | ledger entry for every unrestricted open/switch (decidedBy person) | 2/6 |
| Error codes | `jarvis_disabled`, `jarvis_person_only`, `external_view_only`, `external_unavailable` | – |

Settings: `jarvis.{enabled=false, model=null, folder="~/Snowpea/Jarvis", delegation.{default="staged" (the only value; "full" is a global grant), projectTtlHours=24}, quietMinutes=10, stallMinutes=15 (a fully delegated session with a running turn and no event, or idle after an answer saying work remains, for this long wakes Jarvis as stalled), exposeBrowser=true (owner decision; false: browser-host sessions are listed with status only — no transcript, no prompt/steer/interrupt; their approvals are never Jarvis's either way), proactive.{finished,error,needsYou,decided}=true, briefing.{enabled=false, schedule, instructions}, budget.{wakesPerHour=30, dailyTokens=2000000}, messenger.{route=true, allowGrants=true}, notify.{inApp, os, messenger}=true, external.{enabled=true, clis.<claude|codex|agy|cursor>.{enabled, path, confirmedPath (set only by external.clis.confirmPath), defaultMode=null (= the CLI's own default: readonly for cursor, default for the others; agy may not be "readonly"; never "unrestricted"), passEnv=[] (extra environment variable names)}, maxProcesses=4, idleSec=900}}` in `config/settings.py`; per project `jarvis.expose=true` in `config/project.py`.

## Spike outcomes the contract expresses

| CLI | Transport | `approvals` (host snowpea) | `tmux` | Modes | Notes for the adapter |
|---|---|---|---|---|---|
| claude | `claude -p` stream-json both ways, kept alive; `--resume` | `bridged` (`control_request can_use_tool`) | yes, beta, Linux/macOS: `host: tmux`, `canSend` (single-line and bracketed paste), approvals `person` | default, readonly (`--permission-mode plan`), unrestricted (`--dangerously-skip-permissions`) | history from `~/.claude/projects/*.jsonl`; liveness `~/.claude/sessions/<pid>.json`; drop every transcript record that is not user/assistant (attachment, session-owner, … carry the owner's email and org ids) |
| codex | one shared `codex app-server` (stdio JSON-RPC) | `bridged` (`item/commandExecution/requestApproval`, file-change approvals) | no | default, readonly (read-only sandbox), unrestricted (approvalPolicy never + danger-full-access; `--yolo` for exec) | `host: terminal` when `~/.codex/thread-writer-locks/<id>.lock` is flock-held by another process |
| agy | `agy acp` per open session | `bridged` when `approvalPolicy: asks`; `none` (and `controlled: false`) when `auto` | no | default and unrestricted (`--dangerously-skip-permissions`) only: **no readonly** — agy's `plan` mode writes the plan and then edits files, so it is not offered; with policy `auto`, edit work needs unrestricted | the policy comes from agy's own settings and cannot be set per session |
| cursor | spawn per message (`--resume`, `--workspace`, `--sandbox`, prompt as the last argv element) | `degraded` / none: no permission mechanism; print mode runs edits, shell and network unprompted | no | readonly (`--mode ask`, the default), default (`--sandbox enabled`: edits and sandboxed shell with no prompt, approvals `degraded`) and unrestricted (`--yolo`) | `controlled: false` outside readonly |

MCP presets in v1: `google` (Gmail, Google Calendar) and `microsoft365` (Outlook mail, calendar).

## Deviations from Part 2 and why

- Capability names are camelCase (`jarvisDelegation`, `externalSessions`, `mcpPresets`, `messageOrigin`) to match the existing list; `jarvisBriefing` was added so briefing (WP8) is not gated by `jarvis` (WP3). No `external.<cli>` capabilities: `external.clis` reports them, since installation changes at runtime.
- `jarvis.enable` / `jarvis.disable` instead of one setter, matching `extension.enable/disable`; `jarvis.briefing.get/set` added for the requested conversational briefing.
- `origin.kind` adds `system` (Part 2 had none for core- or model-written text); a messenger message from the bound person is `human` with `surface` set to the platform, and `gateway` is reserved for other chat members, so the pause rule (`kind == "human"`) holds on every surface.
- Takeover without a limit is `takenOver: true` with `pausedUntil: null` rather than a sentinel date.
- `ExternalSessionRow.status` dropped `open_elsewhere`; `host` says where the process lives (owner clarification above). The matching error is `external_view_only`.
- Preset `auth` reuses `McpCatalogAuth` (`oauth | token | none`), not `env`, so presets and catalog entries share one shape.
- Part 2's "never pass `dangerously|yolo|bypass` flags" is replaced by the owner's unrestricted mode: those flags are emitted only for a session a human connection put in `unrestricted`.
- External `mode` is its own literal (`default|readonly|unrestricted`), not the snowpea session `Mode`, since the CLIs' modes do not map onto plan/accept/auto.
- `lastPrompt`'s pre-1.53 fallback skips every injected history entry by its `source` (skill notes, notices, runtime context, continuations, lead relays); a core-started turn's `[system]` note now carries `source {producer: system, form: notice}` so it is skipped too.
- `lastUserAt` became human-only as well, the same rule as `lastPrompt`; before, a hand-back announce or queued command moved it.

## Jarvis's tools (WP4, core only — not wire)

Registered only on the Jarvis session (`Session.session_tools`): read `session_list`, `session_read` (`digest` | `trajectory`, ≤ 100 rows, ≤ 12k chars, fenced), `ledger_read`, `project_list`; instruct `session_prompt`, `session_steer`, `session_interrupt`, `session_create`; decide `approval_decide`, `question_answer`; propose `delegation_set` (the `personOnly` approval above); folder `report_write`, `todo_add|list|done`. Built-ins are narrowed to read-only ones plus `ask_user`, memory and `tool_search` — no URL fetch, `web_search` queries at most 200 characters, `grep`/`glob` with secret files and `$SNOWPEA_HOME` left out of their results; the agent loop refuses anything else, any `config` call, any path into a hidden project and any path under `$SNOWPEA_HOME` for the Jarvis session and every sub-agent below it. Instructions need a turn the person started, or a grant / level `full` on the target; never into a paused session, never a slash command; no tool takes a mode, model or agent. Every read or instruction emits `jarvis.access` and an `access` ledger line. Memory: reads `default`, `agent:jarvis` and every exposed project's namespace; writes only `agent:jarvis`. Hooks for later packages: `JarvisService.hooks.tools` (WP5/6 `external_list` / `external_read`, WP8 `briefing_set`), `hooks.overview_sources` (external rows), `hooks.notify` (WP7 messenger).

The Jarvis session's file-reading built-ins (`read_file`, `list_dir`, `view_image`, `glob`, `grep`, `git_*`) read only Jarvis's folder and the exposed project roots; a secret path (`classify.is_secret_path`) is refused everywhere; its mode is pinned (`session.setMode` → `invalid_params`, `/mode` refuses).

## External sessions under Jarvis (core integration, `jarvis/external.py`)

- **One person rule.** Unrestricted mode, takeover origin and the delegation engine use the same check (`delegation.person_refusal` on `Caller.from_connection`): IDE, web, TUI, CLI, the desktop app and clients that send no `clientKind` are a person; a remote web login may not open unrestricted mode (as it may not hand over guarded classes). The gateway mints its own confirmation through `external.proc.person_confirmation_from_gateway(platform, bound_person=…, settings=…)` (refused unless the binding's own person, and while `jarvis.messenger.allowGrants` is off); `external.create/open/setMode` take it as `confirmation=`. One ledger: an unrestricted grant is a `kind: unrestricted` decision in the engine's ledger (announced as `jarvis.decision`); no ledger write, no grant.
- **Approvals.** A CLI's prompt goes into the shared queue as `ApprovalRequest.external` from a proxy session that carries the registered project; the engine routes it (`auto` / `jarvis` / `person`) like any other (`ClassifyRequest` knows the CLIs' tool shapes); Jarvis answers only what was routed to it, once. `personOnly` asks (tmux adoption) are always `person`.
- **Intake and takeover.** The manager's observers get every new, non-streaming timeline entry (not the history read on open); Jarvis keys an external session `ext:<cli>:<id>`. A `message.user` with origin `human` — `external.send` from a human connection, or what the person typed in the tmux pane or terminal as the transcript shows it — pauses Jarvis there (`jarvis.quietMinutes`). A transcript copy of a prompt snowpea itself sent is matched to it (text, oldest first) and dropped, so it is neither shown twice nor read as the person typing.
- **Overview.** One row per external session snowpea has open: host, mode, `controlled`, `approvals`; uncontrolled sessions and tmux panes (prompts answered by the person) are named as such in Jarvis's table.
- **Tools.** `external_list`, `external_read` (≤ 200 entries, ≤ 12k chars, fenced), `external_send` (the person's request or delegated work, never into a paused session, never into an `unrestricted` one or any other `controlled: false` one, plain text with Jarvis's prefix, origin `jarvis`), `external_interrupt`; `session_create {cli, model?, mode?: default|readonly}` starts one in the CLI's controlled default and is refused when that mode would be uncontrolled (agy with `always-proceed`, Cursor's `default`) — Jarvis can never request or keep `unrestricted`. Each read or instruction is a `jarvis.access` line with `external`.
- **Unrestricted from a messenger.** `session_create {cli, mode: "unrestricted"}` works only in a Jarvis turn the bound person's own chat message started (`MessengerBridge.note_turn`), once per message, while `jarvis.messenger.allowGrants`: the bridge (`Hooks.unrestricted` → `unrestricted_for_turn`) posts the one-press warning (CLI, project, the first message) to the person's chat; their allow mints `person_confirmation_from_gateway` and the session opens unrestricted with its `kind: unrestricted` ledger entry (`reason: "chosen on messenger:<platform>"`). Jarvis alone, the app, another member, a decline or no answer in `CONFIRM_TIMEOUT_SEC` open nothing; `external_send` never takes a mode.
- **Glue.** Requests already waiting when Jarvis mode turns on are routed then (`JarvisService.route_pending`); disable clears every route. A messenger handback does not need `allowGrants` (`person_refusal(grant=False)`). `jarvis-task` workers (`origin_surface: "jarvis-task"`) are not supervised: no digest or overview row, refused by Jarvis's session tools, their requests are the person's. MCP tools the person tagged `read` (an assistant preset's read tools) classify `safe`; their send tools stay `secret_send`. The briefing's text reaches the messenger through notify hook kind `briefing {text, report}` (trimmed to one message with a pointer to the saved report). A stalled delegated session wakes Jarvis and leaves an open `stalled` escalation (importance `normal`).
- **Processes.** `ExternalAdapter.shutdown()` runs at daemon stop (Codex stops its shared app-server gracefully); helper processes (`claude auth status`, `agy models`, a token-free `agy acp`, `cursor-agent --list-models`) run through `ProcessSupervisor.spawn_helper` / `run_helper`: counted, in their own process group, pid files, reaped at stop. A process tree is killed whether or not its pid leads a group, and the daemon's own group is never signalled.

## The assistant features (WP8)

**Presets** (`tools/mcp_catalog.PRESETS`, `jarvis/presets.py`; `mcp.preset.list|apply|remove`, capability `mcpPresets`). A preset is one or more servers, the sign-in method, which tools only read, and the recipes (schedule templates, never preselected). `apply` is one transaction: validate inputs, store a client secret in `credentials.json` (the entry says `${NAME}`), set `mcp.permissions` (the server `send`, each read tool `read` through the per-tool key `"<server>/<tool>"` that `permission_for` now honours), add the servers through the `mcp.add` code (probe first), restart them so the tags apply, create the recipe jobs. A failure undoes every step. What was there before is kept in `$SNOWPEA_HOME/jarvis/presets.json` (0600) and `remove` puts it back (entries, tags, credentials, the stored OAuth sign-in, jobs). The same call twice answers `already: true`; a server name the person already uses is never taken over (`mcp_exists`). A tool the server grows later stays `send` (asks) until a release lists it as read.

| Preset | Servers | Sign-in | Source |
|---|---|---|---|
| `google` | `<name>-gmail` = `https://gmailmcp.googleapis.com/mcp/v1`, `<name>-calendar` = `https://calendarmcp.googleapis.com/mcp/v1` (Google's own remote servers, Developer Preview) | `mcp.authorize` (OAuth; needs the person's own OAuth client id and secret, marked "for use by an AI-powered agent", and the Gmail MCP / Calendar MCP APIs enabled in their Cloud project) | developers.google.com/workspace/gmail/api/guides/configure-mcp-server, .../calendar/api/guides/configure-mcp-server |
| `microsoft365` | `<name>` = `npx @softeria/ms-365-mcp-server@0.185.0 --preset mail,calendar` (community server over Microsoft Graph; Microsoft publishes no personal mail/calendar MCP server) | device code: run `--login` once on the daemon's machine; optional tenant id (`consumers` for a personal account) and client id | github.com/Softeria/ms-365-mcp-server (README, PRESETS.md) |

Google's Gmail server has no send tool (only `create_draft` and labels), so nothing can be sent through it; its calendar documents read scopes only, so event changes need a write scope the person adds themselves.

**Assistant tasks** (`scheduler` target `jarvis-task`, `jarvis/tasks.py`). `Job.target`, `target_data` and `timezone` (new, additive; `JobInfo.target/timezone`) say a job is run by Jarvis mode instead of an ordinary scheduled session. Each firing opens its own worker session and closes it; the result is saved under `<jarvis folder>/reports/` (fenced on disk too) and put to Jarvis as one wake message inside an `<untrusted>` fence. The scheduler gets only a status line, so a digest never reaches `jobs.log` or a `job.event`. Kinds: `mail` and `calendar` (the isolated worker), `briefing`, `task` (an ordinary unattended worker in an exposed project or the Jarvis folder). The isolated worker has exactly the preset's read tools of that kind for that server (`Session.exclusive_tools` stops `narrowed` adding `read_file`, `grep`, `tool_search`), an empty scratch folder, no delegation, memory or session tools, and a prompt that says mail is data. When none of its tools is connected the run fails without calling a model. A run while Jarvis mode is off fails the same way (three failures pause a repeating job).

**Briefing** (`jarvis/briefing.py`; `jarvis.briefing.get|set`, tool `briefing_set`, capability `jarvisBriefing`). Off until asked; one `jarvis-task` job of kind `briefing` kept in step with `settings.jarvis.briefing` (`enabled`, `schedule` cron, `timezone`, `sections` of sessions/waiting/todos/mail/calendar, `instructions`). The facts are gathered by code, mail and calendar by the isolated worker, saved as `reports/briefing-<date>.md`, and put to Jarvis, each block fenced, to write as its reply; a `high` escalation reaches the notification hooks. The cron is read in `timezone` (`Spec.tz`).

**Jarvis's tools**: `briefing_set`, `schedule_list|create|update|remove|run`. They change what runs in the person's name, so they refuse in a turn a wake message started. To-dos gained `repeat` (daily|weekly|monthly, needs an ISO due date): done moves the due date on.

# M19 — Jarvis mode (beta)

Status: approved by the owner 2026-11-15; not started. Target protocol 1.53.0 (additive). Ships in one release, documented as beta.

This file has two parts: the plan as the owner approved it (Korean), and the technical design the work packages follow (English). Where they differ, the owner's decisions in Part 1 win; the known differences are listed right below.

## Decisions that override Part 2

- **Grants from a messenger are allowed by default** (`jarvis.messenger.allowGrants: true`), still through a one-press confirmation that only the person can answer. Part 2 says default false.
- **Browser-host sessions:** Jarvis reads, summarises and may send instructions (prompt/steer). Deciding their approvals stays with the browser connection until the browser team opens it; coordinate and include it in the same release if ready. Part 2 says read-only.
- **Hand-back:** authority pauses when the person types into a managed session and returns automatically after `jarvis.quietMinutes` (default 10) without input, or at once on hand back.
- **All four CLIs ship in the first release**, on IDE, web and TUI.
- Grants are proposed by Jarvis and confirmed by the person with one press; nothing Jarvis reads can change them.

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
| `never` | Tag `config` (already in `UNPROMOTABLE`), delegation or memory-permission prompts, browser-owned entries |
| (a) deploy | `exec` matching `git push`, `gh release` / `gh pr merge`, `npm`/`cargo`/`twine publish`, `docker push`, `kubectl`/`terraform apply`, `ssh`/`scp`/`rsync` to a remote, `deploy*`/`release*` scripts |
| (b) outside / destructive | Tag `outside`; `rm -r`, `git reset --hard`, `git clean`, `--force`, `DROP`/`TRUNCATE`, `find -delete` |
| (c) secret / send | Tags `secret` and `send`; mutating network calls; MCP tools without `readOnlyHint`; payment-like tool names |
| `safe` | Tag `read`, `permissions/safe_commands.py`, test runners |
| Unparseable or compound shell | Treated as guarded (fail closed) |

- Routing: `safe` is auto-allowed without the LLM. `risky` goes to the person under `staged` and to Jarvis under `full`. `guarded` goes to the person unless a grant covers it.
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
- **Remote web login.** `jarvis.delegation.set` for guarded classes requires a local connection, or a re-authenticated remote one. `external.create` is limited to registered roots.
- **Browser sessions.** Read-only for Jarvis, as above.

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
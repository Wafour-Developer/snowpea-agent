# snowpea-agent 구현 계획 (omc-plan consensus)

Status: pending approval (consensus reached, iteration 3)
Plan ID: plan-snowpea-agent-20260911
Input spec: `.omc/specs/deep-interview-snowpea-agent.md` (di-snowpea-agent-20260911, Ambiguity 12%, PASSED)
Scope of this document: v0.1 (core + CLI) 전체 실행 계획. v0.2/v0.3은 전망 문단만.
Repo root (public, MIT): `snowpea-agent/` = 현재 작업 디렉터리 `/mnt/data/work/mediagen/snowpea`
Revision note: iteration 1의 19건, iteration 2의 8건, 최종 편집 6건을 §10 Changelog대로 반영. Architect approve-with-changes + Critic APPROVE로 합의 종료. 결정 근거 요약은 §9 ADR. 마일스톤 번호는 실행 순서에 맞춰 재배열되었다(구 M2 ↔ 구 M3 교환).

---

## 1. Requirements Summary

### 1.1 제품 정의
Hermes Agent 코드를 **vendoring(복사 후 수정, git fork 아님)** 한 Python 코어가 로컬 WebSocket JSON-RPC 서버(Codex app-server 방식)로 상주하고, 그 위에 Node/Ink 풀 TUI(1차 표면)와 Electron/Vue 앱(2차 표면, v0.2)이 **동일한 TypeScript 클라이언트 SDK**로 붙는 오픈소스 멀티벤더 AI 에이전트 플랫폼. 본질은 코딩 에이전트이며, 메신저 게이트웨이·장기 메모리·스케줄러·웹검색·미디어 생성이 코어에 포함되어 "나만의 AI 비서"로도 쓰인다.

### 1.2 v0.1 필수 기능 (모두 코어)
| 영역 | 요구 |
|---|---|
| 프로토콜 | JSON-RPC 2.0 over WebSocket, 토큰 인증, semver `version` 필드, 문서화, TS SDK |
| 벤더 | Anthropic(API key only), OpenAI, OpenAI 호환 로컬(vLLM/Ollama/LM Studio), OpenRouter, Gemini, xAI, GLM, MiniMax, Kimi, DeepSeek, Qwen (11종). 네이티브 어댑터 2개(Anthropic, Gemini) + OpenAI 호환 어댑터 + 벤더 프리셋. 웹 토큰 로그인은 §2.8에서 확정한 2종(OpenAI, OpenRouter), 나머지 9종은 API 키 |
| 모드 | plan / accept(기본) / auto. `snowpea --mode <m>`, TUI `/plan` `/accept` `/auto`, 프로젝트 설정에 기본 모드 저장 |
| 승인 | accept = Claude Code acceptEdits (read/write/edit 자동, shell·git push·schedule 등록·메신저 발송·외부 API는 프롬프트, allowlist 예외). 승인 큐는 코어 소유, TUI·메신저 양방향 응답, 타임아웃 시 거부 |
| 툴셋 | 파일/셸/git/검색/브라우저/delegate_task/스케줄/메모리/웹검색(무료 제공자 포함)/이미지·TTS·비디오. MCP 서버(`.mcp.json`) 연결 |
| 멀티 에이전트 | (1) 일회성 서브에이전트 동시 N(`agents.max_concurrent`, 기본 3), (2) 팀 모드(공유 태스크 리스트 + 에이전트 간 메시지 + worktree 분리), (3) 영속 이름 에이전트(세션·메모리·채널·스케줄 보유, 데몬 재시작 후 유지) |
| 실행 백엔드 | local(기본) / docker / ssh, 동일 툴셋 |
| 메모리 | SQLite FTS 장기 메모리 + 사용자 프로필, 세션 간 검색·인용 |
| 스케줄러 | cron + 자연어 예약, 데몬 실행, 결과를 지정 채널로 전달, 등록 시 모드 지정 |
| 게이트웨이 | Telegram / Discord / Slack 중 최소 1개 |
| 확장 | Claude Code 플러그인 포맷(plugin.json, skills/SKILL.md, agents/*.md, commands, hooks, .mcp.json) + agentskills.io 표준. opencode TS 플러그인 비지원 |
| 내장 명령 | OMC급 ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview + `/agent create`, `/skill learn`, `snowpea skill search` |
| 헤드리스 | `snowpea -c "<prompt>"` 1회성 실행(§3.6), `--json`, 결정적 종료 코드 |
| 설치 | `curl \| sh` (uv + Node 처리, `snowpea` 명령 등록), brew/npm 보조, mac/win/linux |

### 1.3 Non-Goals (v0.1에서 만들지 않는 것)
Monaco 기반 코드 편집기, opencode TS 플러그인 런타임, Anthropic 구독 OAuth, 단일 바이너리 번들, 원격 다중 머신 코어 접속, 클라우드 샌드박스(Modal/Daytona/E2B).

### 1.4 레포·라이선스
`snowpea-agent`(public MIT, 이 계획의 대상) / `snowpea-ide`(private) / `snowpea-registry`(private) / `snowpea-site`(private). 세 비공개 레포는 M0에서 빈 스켈레톤으로 생성한다(§4 M0). 복사한 Hermes·OMC 코드는 MIT 고지 유지.

---

## 2. RALPLAN-DR

### 2.1 Principles
1. **Protocol-first** — `docs/protocol.md`와 `sdk/src/protocol.ts`가 단일 진실. 코어 기능은 RPC 메서드로 노출된 뒤에야 "존재"한다. TUI 전용 뒷문 없음.
2. **Core owns all state, and the protocol is versioned** — 세션·승인 큐·메모리·스케줄·게이트웨이 바인딩은 전부 데몬이 소유. 클라이언트는 무상태 렌더러. 모든 핸드셰이크·스키마 덤프는 semver `version` 필드를 싣고, v0.2 IDE 착수 전에 **protocol v1.0 freeze gate**(§4.9, AC-21)를 통과해야 한다. IDE가 붙을 때 코어 수정이 0이어야 한다.
3. **Vertical slice before breadth** — M1에서 "설치 없는 개발 환경에서 프롬프트 → 파일 편집 → TUI 표시 → SDK 계약 테스트 통과"가 끝까지 돌아간 뒤에만 툴 30개·벤더 11개로 넓힌다.
4. **Vendored code lives behind an adapter** — Hermes에서 가져온 코드는 `snowpea_core/vendor/hermes/`에 원형 보존 계층으로 두고, 코어는 `snowpea_core/tools/*.py` 등 자체 인터페이스로만 호출한다. 원본 파일 SHA256과 **우리가 가한 수정분 패치**가 함께 기록되고, CI가 `(원본 + 패치) == 작업본`을 검증한다. 바이트 동일성이 아니라 *추적 가능한 차이*가 기준이다.
5. **Every behavior is observable** — 모든 툴 호출·승인·서브에이전트 이벤트는 `session.event`로 흘러 TUI와 로그(`~/.snowpea/logs/`)에 동시에 남는다. 디버깅 불가능한 경로를 만들지 않는다.

### 2.2 Decision Drivers (top 3)
1. **v0.1 범위가 비정상적으로 넓다** (툴셋·11벤더·메모리·스케줄러·게이트웨이·플러그인·팀모드가 모두 1차). → 재사용 극대화와 조기 실행 가능성이 다른 모든 기준을 이긴다.
2. **3 OS × 2 런타임(Python+Node) 설치 신뢰성**이 첫 사용자 이탈 지점이다. → 설치 경로를 하나로 줄이는 선택이 우대된다.
3. **표면 2개가 같은 코어를 공유**하므로 프로토콜이 v0.2 IDE 착수 전에 얼어야 한다. → 프로토콜 표현력·스트리밍·양방향성이 성능보다 중요하다.

### 2.3 Decision A — Core protocol shape

| Option | Pros | Cons |
|---|---|---|
| **A1. JSON-RPC 2.0 over WebSocket** (Codex app-server 방식) | 양방향 네이티브 → `approval.request`를 서버→클라이언트 호출로 자연 표현. 단일 연결에 세션 다중화. 토큰 인증 단순(핸드셰이크 1회). Electron/Ink 양쪽 WS 클라이언트 성숙 | 재연결·재구독 로직 직접 구현. curl 디버깅 불편. 프록시/방화벽 환경 취약(로컬이라 영향 적음) |
| **A2. HTTP + SSE** (opencode 방식) | curl/브라우저로 디버깅 용이. 재연결이 SSE 표준으로 해결. 캐시·프록시 친화 | 승인 요청 같은 서버→클라이언트 *요청*이 부자연스러움(폴링 또는 별도 채널 필요). 요청/스트림 2채널 관리 |
| **A3. 둘 다** | 통합 테스트 스크립트를 HTTP로, 실사용을 WS로 | 표면 2배, 프로토콜 드리프트 위험, v0.1 범위에서 순손실 |

**Recommendation: A1 + 최소 HTTP 부수 표면.** 사양이 명시한 WS JSON-RPC를 주 프로토콜로 확정한다. 단, 디버깅·헬스체크·설치 검증을 위해 같은 포트에 읽기 전용 HTTP 엔드포인트 3개만 둔다: `GET /health`, `GET /protocol.json`(메서드 스키마 + `version` 덤프), `GET /version`. 이것은 A3의 "두 개의 API"가 아니라 운영 보조 표면이며, 상태 변경 메서드는 전부 WS에만 존재한다.

### 2.4 Decision B — Hermes vendoring strategy

| Option | Pros | Cons |
|---|---|---|
| **B1. 전체 복사 후 가지치기** | 초기 속도 최고. 숨은 의존성 누락 없음. upstream diff 적용 쉬움 | 죽은 코드·Hermes 고유 개념(Desktop 웹 대시보드 등)이 영구 잔류. 라이선스 고지 범위 비대. 아키텍처가 Hermes를 그대로 물려받아 snowpea 고유 요구(3모드·팀모드·영속 에이전트)와 충돌 |
| **B2. 선별 모듈 이식** (tools, gateway, scheduler, memory, setup) | 코어 구조를 snowpea 요구에 맞춰 설계 가능. 고지 대상 파일이 명시적. 리뷰 가능한 크기 | 초기 속도 저하. 모듈 간 숨은 결합 발견 시 재작업. Hermes 내부 유틸을 따라 들어가며 복사해야 함 |
| **B3. 참조 전용 (읽고 새로 작성)** | 복사 코드 0 → 라이선스·드리프트 리스크 소멸. 아키텍처가 100% snowpea 것. 테스트·타입을 처음부터 우리 규격으로 | 가장 느림. Hermes가 이미 해결한 실무 함정(메신저 API quirk, 스케줄 재기동, FTS 스키마)을 다시 밟음. v0.1 범위에서 일정 위험 최대 |

**Recommendation: B2 + B3 하이브리드 (명시적 경계).**
- **B2로 이식**: 실무 함정이 값비싼 영역 — `tools/`(셸·파일·git·브라우저 어댑터), `gateway/`(Telegram·Discord·Slack 클라이언트), `scheduler/`(cron 파싱·재기동 복구), `memory/`(SQLite FTS5 스키마·인덱싱), `setup/`(런타임 탐지).
- **B3로 신규 작성**: snowpea 고유 개념이라 Hermes에 대응물이 없거나 구조가 다른 영역 — `server/`(WS JSON-RPC, 승인 양방향), `session/`, `agent/`(3모드·팀·영속 에이전트), `providers/`(11벤더 정규화), `permissions/`, `skills/`(Claude Code 플러그인 포맷), `commands/`.
- **절차**: 1단계(M0) Hermes를 `/tmp/hermes-ref`에 읽기 전용 클론(레포 커밋 금지)하고 `docs/vendoring-map.md`에 "원본 경로 ↔ 원본 커밋 SHA ↔ **원본 파일 SHA256** ↔ 목적지 경로 ↔ 패치 파일 경로 ↔ 이식 사유" 표를 먼저 작성. 2단계(M2·M5) 표에 적힌 파일만 `core/snowpea_core/vendor/hermes/<module>/`로 복사하고 상단에 `# Vendored from hermes-agent @ <sha>, MIT` 헤더를 넣는다.
- **수정 추적(iteration 3 확정)**: vendored 파일은 **수정해도 된다.** 대신 수정할 때마다 원본 대비 diff를 `core/snowpea_core/vendor/patches/<목적지 상대경로>.patch`로 커밋한다(수정이 없으면 패치 파일도 없음). CI 잡 `vendor-integrity`(`scripts/verify_vendor_integrity.py`)는 다음을 검사한다: ① 맵의 원본 SHA256이 `/tmp/hermes-ref` 재클론본과 일치 ② 원본에 패치를 적용한 결과가 작업 사본과 **바이트 단위로 동일** ③ 고지 헤더 존재 ④ 맵에 없는 vendored 파일 0건. 즉 검증 기준은 원본과의 바이트 동일성이 아니라 **(원본 + 커밋된 패치) == 작업본**이다.

### 2.5 Decision C — Monorepo layout & `snowpea` launch path

| Option | Pros | Cons |
|---|---|---|
| **C1. uv 관리 Python 엔트리포인트가 Node TUI를 spawn** | `snowpea`가 하나의 진입점. 데몬 수명 관리 주체가 명확. TUI를 esbuild 번들로 wheel에 동봉하면 사용자 머신에 npm install 불필요 | Node 런타임 존재를 Python 쪽이 보장해야 함. TUI 개발 시 번들 재생성 필요 |
| **C2. npm 설치 TUI가 Python 데몬을 spawn** | Node 생태계 배포(`npx snowpea`) 자연스러움. TUI 개발 루프 빠름 | Python 환경(uv) 부트스트랩을 Node가 해야 함 → Windows에서 실패율 상승. 코어 단독 사용(IDE·헤드리스)에 Node 의존 발생 |
| **C3. 두 진입점 모두 제공** | 사용자 선택권 | 수명 관리 규칙이 두 벌 → 좀비 데몬·포트 충돌 디버깅 비용. v0.1에서 불필요 |

**Recommendation: C1.** `snowpea`는 Python console script(`snowpea_core.cli.main:main`). 동작 순서: ① 설정·포트 파일 확인 → ② 데몬 없으면 detach 기동 → ③ `node <package_data>/tui/dist/snowpea-tui.js --port <p> --token <t>` spawn → ④ TUI 종료 시 데몬은 §2.6 정책에 따라 유지/종료. TUI는 CI에서 esbuild로 단일 JS 번들을 만들어 wheel의 package data로 넣는다(사용자 머신 npm install 없음).
- 개발 탈출구: 환경변수 `SNOWPEA_TUI_ENTRY`가 설정되면 번들 대신 그 경로(예: `tui/src/index.tsx` + tsx 러너)를 spawn한다.
- npm 패키지 `@snowpea/tui`와 `@snowpea/sdk`는 **v0.1에서 함께 발행**한다(M8). SDK는 비공개 레포 3종이 npm으로 소비하는 유일한 경로이고, TUI 패키지는 v0.2 IDE가 같은 렌더러를 재사용하기 위한 사전 포석이다.

**확정 레이아웃**
```
snowpea-agent/
  pyproject.toml            uv.lock            README.md  LICENSE  NOTICE
  core/snowpea_core/        # Python 패키지 (§3.1)
  tui/                      # Node/Ink (§3.2)
  sdk/                      # TS client SDK (§3.3)
  installer/                # install.sh, install.ps1, brew/, npm/
  scripts/gen_protocol.py   # protocol.py → sdk/src/protocol.ts + docs/protocol.md
  docs/                     # protocol.md, vendoring-map.md, omc-porting-map.md, manual/
  core/snowpea_core/vendor/patches/   # 수정한 vendored 파일의 원본 대비 diff (§2.4)
  tests/                    # pytest + node e2e + fixtures
  .github/workflows/        # ci.yml (jobs: unit, vendor-integrity, e2e-<os>), release.yml
```

### 2.6 Decision D — Daemon lifecycle

| Option | Pros | Cons |
|---|---|---|
| **D1. 항상 켜진 사용자 서비스** (systemd user / launchd / Windows Service) | 스케줄러·게이트웨이가 로그인 직후부터 동작. 재부팅 후 영속 에이전트 자동 복구 | OS 3종 서비스 등록 코드 + 권한 이슈. 설치 즉시 백그라운드 상주 → 개발자 반감·보안 리뷰 부담. 업데이트 시 서비스 재시작 로직 필요 |
| **D2. 첫 클라이언트에서 lazy-start + idle 종료** | 설치가 파일 복사로 끝남. 예측 가능. 개발 중 재시작 자유 | 예약 작업·메신저 수신이 데몬 미기동 시 누락 → v0.1 수용 기준(스케줄러·게이트웨이) 불충족 |
| **D3. 하이브리드: lazy-start + 바인딩 기반 keepalive + 선택적 서비스 등록** | 기본 경험은 D2, 스케줄·게이트웨이·영속 에이전트가 하나라도 등록되면 idle 종료를 끈다. 재부팅 영속이 필요한 사용자만 `snowpea service install` | 종료 조건 로직이 상태 의존 → 테스트 케이스 증가 |

**Recommendation: D3.** 기본은 lazy-start. `~/.snowpea/daemon.json`에 port/token/pid/startedAt 기록. idle 종료 타이머(기본 1800초)는 "활성 세션 0 **그리고** 활성 스케줄 잡 0 **그리고** 게이트웨이 바인딩 0 **그리고** 영속 에이전트 0"일 때만 작동한다. `snowpea daemon status`는 네 카운터와 함께 **종료 예정 여부와 그 이유**를 출력한다(예: `will not exit: 2 gateway bindings, 1 named agent`). 재부팅 영속은 M8의 `snowpea service install|uninstall|status`로 제공하되 기본 비활성.

### 2.7 Decision E — Built-in OMC command porting

| Option | Pros | Cons |
|---|---|---|
| **E1. 전부 Python workflow 플러그인으로 포팅** | ralph의 반복·완료 판정, ultrawork의 병렬 팬아웃, team의 태스크 클레임처럼 **제어 흐름**이 본질인 명령은 코드로 정확히 표현됨. 테스트 가능 | deep-interview·deep-research처럼 본질이 긴 프롬프트인 것을 코드로 옮기면 충실도 손실, 수정 비용 상승 |
| **E2. SKILL.md 마크다운 유지 + snowpea 네이티브 agents/tools** | 원본 OMC 문구를 그대로 보존 → 포팅 충실도 최고. 사용자 스킬과 동일 로더 재사용 | 루프·병렬·상태 지속이 필요한 명령을 마크다운으로 강제하면 비결정적. Claude Code 전용 의존(executor 에이전트, state/notepad MCP 툴) 치환 필요 |

**Recommendation: 하이브리드 E1+E2, 경계를 명시적으로 긋는다.**
- **Python workflow (`core/snowpea_core/commands/`)**: `ralph.py`, `ultrawork.py`, `team_cmd.py`, `deepinit.py` — 루프·병렬·완료 판정·파일 산출이 있는 것.
- **번들 SKILL.md (`core/snowpea_core/builtin_skills/<name>/SKILL.md`)**: `deep-interview`, `deep-research`, `ralplan` — 프롬프트가 본질인 것. 사용자 설치 스킬과 **동일한 로더**로 읽어 로더 경로를 하루라도 빨리 실사용 검증한다.
- Claude Code 전용 의존은 치환 표로 관리: `executor 에이전트 → agent.spawn(role="executor")`, `state/notepad MCP → memory.write/search + .snowpea/notepad.md`, `Task 툴 → delegate_task`. 표는 `docs/omc-porting-map.md`.

### 2.8 Decision F — Web token login vendor set (iteration 3에서 확정)
사양의 "지원 벤더에 한해"를 v0.1 구현 대상으로 확정한다(iteration 3 기준). 기준: 공개 문서화된 플로우가 있고, 로컬 CLI가 브라우저를 열어 토큰을 회수하는 것이 제공자 ToS에 어긋나지 않는 것.

| 벤더 | v0.1 인증 방식 |
|---|---|
| OpenAI | **디바이스 코드 플로우**(ChatGPT 로그인 스타일). CLI가 코드 표시 → 브라우저 승인 → 토큰 회수 |
| OpenRouter | **OAuth PKCE**. 로컬 콜백 포트로 code 수신 → 키 교환 |
| 그 외 9종 (Anthropic, Gemini, xAI, GLM, MiniMax, Kimi, DeepSeek, Qwen, OpenAI 호환 로컬) | **API 키 입력만**. Anthropic은 사양상 구독 OAuth 비대상 |

벤더 총수는 사양대로 **11종**이며 새 벤더를 추가하지 않는다(iteration 2의 Nous Portal 항목은 사양 범위 밖이므로 삭제). 구현은 `providers/auth_web.py` 하나에 두 플로우를 두고, `presets.py`의 `auth_methods: ["api_key"] | ["api_key","device_code"] | ["api_key","oauth_pkce"]` 선언으로 벤더에 연결한다. 새 벤더의 웹 로그인 추가는 프리셋 한 줄 + 플로우 재사용으로 끝나야 한다.

---

## 3. Architecture

### 3.1 `core/snowpea_core/` 모듈 맵
```
snowpea_core/
  __main__.py                 # python -m snowpea_core → 데몬
  cli/main.py                 # `snowpea` 콘솔 스크립트: ensure daemon → TUI spawn 또는 -c 헤드리스(§3.6)
  cli/commands.py             # snowpea setup|skill|service|daemon|agents|team|job|tools|commands
                              #   (비TUI 서브커맨드. `tools list`/`commands list`는 세션을 만들지 않고
                              #    각각 tool.list / command.list RPC만 호출한다)

  server/
    app_server.py             # 데몬 부트스트랩, 포트 바인딩, ~/.snowpea/daemon.json 기록
    transport_ws.py           # websockets 서버, 연결당 auth 핸드셰이크(+ protocol version 교환)
    transport_http.py         # /health /version /protocol.json (읽기 전용)
    rpc.py                    # JSON-RPC 2.0 디스패처(요청/응답/알림/양방향)
    protocol.py               # PROTOCOL_VERSION(semver) + 메서드·이벤트 pydantic 스키마 (SSOT)
    auth.py                   # 토큰 발급·검증 (~/.snowpea/token, 0600)
    lifecycle.py              # idle 타이머, keepalive 조건(§2.6), 종료 사유 리포트, graceful shutdown

  session/
    session.py                # Session/Thread 엔티티, mode, workdir, provider 바인딩, originSurface
    store.py                  # SQLite 영속 (~/.snowpea/state.db)
    history.py                # 메시지·툴 호출 기록, 압축(compaction)
    events.py                 # session.event 페이로드 생성기, 세션별 단조 seq 발급

  agent/
    loop.py                   # 에이전트 루프: prompt → LLM → tool calls → 결과 → 반복
    agent.py                  # Agent 실행 컨텍스트(시스템 프롬프트, 툴 허용 집합)
    definition.py             # agents/*.md frontmatter 파서 + 생성기(/agent create)
    subagent.py               # delegate_task. 동시성 = settings `agents.max_concurrent` (기본 3,
                              #   전역 설정·프로젝트 설정·session.create 인자 순으로 오버라이드)
    team.py                   # 공유 태스크 리스트, 클레임, 에이전트 간 메시지, worktree 할당·병합.
                              #   충돌 재큐잉 횟수는 `team.max_conflict_retries`(기본 2)로 제한하고
                              #   초과 시 태스크를 state="failed"로 확정한다(§5 AC-16)
    named.py                  # 영속 이름 에이전트 레지스트리(세션·메모리·채널·스케줄 바인딩)

  providers/
    base.py                   # ChatProvider 인터페이스(stream, tool schema 변환, usage)
    registry.py               # 벤더 등록·모델 목록·기본 모델 선택
    anthropic_native.py       # Anthropic Messages API (API key only)
    gemini_native.py          # Google Gemini
    openai_compat.py          # OpenAI /v1/chat/completions 계열 공통 구현
    presets.py                # openai, openrouter, xai, glm, minimax, kimi, deepseek, qwen,
                              #   local-vllm, local-ollama, local-lmstudio + auth_methods 선언(§2.8)
    auth_web.py               # device_code(OpenAI) / oauth_pkce(OpenRouter) 2종 플로우
    normalize.py              # tool-call·스트리밍 delta 포맷 정규화 단일 지점(§6 리스크 3)
    replay.py                 # tests/fixtures/providers/<vendor>/*.json 재생 트랜스포트(CI 전용)

  tools/
    registry.py               # 툴 등록·스키마·권한 태그(read/write/exec/network/send) + 활성 상태
    fs.py shell.py git.py     # 파일·셸·git
    grep.py glob.py           # 코드 검색
    web_search.py             # search_providers/ 레지스트리 위의 web_search·web_extract 툴 (기본: DuckDuckGo ddgs, 키 없음)
    search_providers/         # 제공자 플러그인: ddgs, brave_free, exa_free, keenable_free, parallel_free, tavily(keyless), searxng, firecrawl_selfhost, [paid] exa, keenable, parallel, firecrawl, xai_grok
    browser.py                # browser_providers/ 위의 navigate/click/type/scroll 툴 (기본: Local headless Chromium)
    browser_providers/        # local_chromium(기본), camoufox, browser_use_local, [paid] browserbase, firecrawl_cloud
    delegate.py               # delegate_task → agent/subagent.py
    schedule.py memory.py     # 스케줄·메모리 툴 표면
    media.py                  # snowpea-studio MCP 이미지/영상/음악/TTS.
                              #   **항상 registry에 등록**되며, 자격증명 부재 시 state="inactive"로
                              #   노출되고 호출하면 error{code:"tool_inactive", hint:"..."}를 반환한다.
                              #   자격증명이 생기면 재시작 없이 state="active"로 전환.
    mcp_client.py             # .mcp.json 로드, stdio/SSE MCP 서버 툴 노출

  exec/
    backend.py                # ExecutionBackend 인터페이스(run, read, write, cwd)
    local.py docker.py ssh.py # 3종 구현

  permissions/
    policy.py                 # 모드별 규칙(plan/accept/auto) × 툴 권한 태그
    allowlist.py              # 명령별 예외(정규식 + 프로젝트/전역 스코프)
    approval_queue.py         # 승인 요청 큐: 생성·브로드캐스트·응답 수신·타임아웃 거부.
                              #   대화형 턴에서 발생한 요청은 originSurface 연결에서만 응답 가능;
                              #   무인(스케줄·게이트웨이) 요청만 전 채널 공유 큐로 브로드캐스트한다.

  memory/
    store.py profile.py retrieval.py    # SQLite FTS5, 사용자 프로필, 관련 기억 주입·인용

  scheduler/
    scheduler.py jobs.py nl_parse.py    # cron + 자연어, 잡별 모드·채널 지정

  gateway/
    base.py router.py telegram.py discord.py slack.py   # 채널 ↔ 세션/에이전트 라우팅

  skills/
    loader.py skill_md.py marketplace.py registry_client.py hooks.py

  builtin_skills/             # deep-interview/, deep-research/, ralplan/ (SKILL.md)
  commands/
    registry.py               # **슬래시 명령의 파싱·해석·디스패치가 여기 산다.** 내장 명령 + 플러그인
                              #   commands + builtin_skills를 하나의 네임스페이스로 병합하고,
                              #   `command.list`(이름·설명·인자 스키마·출처)와
                              #   `command.run(sessionId, name, args)`를 RPC로 노출한다.
                              #   TUI·헤드리스·게이트웨이·IDE가 모두 이 한 곳을 통과한다.
    ralph.py ultrawork.py team_cmd.py deepinit.py
    agent_cmd.py skill_cmd.py mode_cmd.py

  config/
    paths.py                  # ~/.snowpea/ (SNOWPEA_HOME 존중), <project>/.snowpea/
    settings.py               # 전역 설정 (agents.max_concurrent, team.max_conflict_retries,
                              #   providers, search, approvals.timeoutSec)
    project.py                # 프로젝트 설정(.snowpea/settings.json): defaultMode, allowlist, backend

  setup/
    wizard.py detect.py       # Quick/Full/Blank, 런타임·기존 키 탐지
    screens/                  # Hermes 식 단계 화면: providers.py(LLM 벤더), search.py(검색 제공자 단일 선택), browser.py(브라우저 제공자 단일 선택), tools.py(툴 카테고리 다중 토글), gateway.py, done.py
    catalog.py                # 선택지 카탈로그: 각 항목에 label, tags([free|paid|subscription],[no key|key optional|self-hosted]), 기본값 표시(★), [active] 상태
```

### 3.2 `tui/` (Node + Ink)
```
tui/package.json  tsconfig.json  esbuild.config.mjs
tui/src/index.tsx            # 진입점, --port --token --mode
tui/src/rpc/client.ts        # @snowpea/sdk 사용, 재연결 + session.resume(afterSeq) 결손 복구
tui/src/state/store.ts       # 세션·이벤트·승인 큐 상태
tui/src/components/
  Chat.tsx MessageStream.tsx ToolCall.tsx DiffView.tsx
  ApprovalPrompt.tsx ApprovalQueue.tsx SubagentTree.tsx
  ModeBar.tsx StatusLine.tsx SlashCommandPalette.tsx HelpPanel.tsx
tui/src/slash/registry.ts    # **얇은 클라이언트**: 자체 명령 테이블을 갖지 않는다.
                             # 연결 시 command.list를 1회 받아 자동완성·도움말을 렌더하고,
                             # 입력은 그대로 command.run(sessionId, name, args)로 넘긴다.
                             # skill.reload / plugin 설치 후에는 command.list를 재요청한다.
tui/dist/snowpea-tui.js      # esbuild 산출물 (wheel package data로 복사)
```

### 3.3 `sdk/` (TypeScript, TUI·IDE 공유, npm `@snowpea/sdk`)
```
sdk/package.json
sdk/src/protocol.ts   # scripts/gen_protocol.py가 core/.../server/protocol.py에서 생성 (수기 편집 금지)
sdk/src/client.ts     # connect(port, token) → version 협상, call(), on(event), 재연결
sdk/src/sessions.ts sdk/src/approvals.ts sdk/src/agents.ts sdk/src/jobs.ts sdk/src/tools.ts
sdk/test/contract.test.ts   # 실데몬 상대 계약 테스트. mocha grep 태그로 두 묶음을 나눈다:
                            #   `npm -w sdk test -- --grep base`     → AC-15a 3건 (§7.2, M1부터 CI 필수)
                            #   `npm -w sdk test -- --grep subagent` → AC-15b   (§7.8, M7부터 CI 합류)
```

### 3.4 `installer/`
```
installer/install.sh     # mac/linux: uv 설치 → Node 확인/설치 → snowpea wheel 설치 → PATH 등록
installer/install.ps1    # windows (winget 경유 uv/Node)
installer/brew/snowpea.rb
installer/npm/           # 보조: npx snowpea → install.sh 위임
```

### 3.5 JSON-RPC 메서드·이벤트 (high level)
핸드셰이크: 연결 직후 클라이언트가 `system.hello(token, clientVersion, protocolVersion)`를 호출하고, 서버는 `{protocolVersion: "1.0.0", serverVersion, capabilities[]}`를 반환한다. major 불일치는 연결 거부(`error{code:"protocol_incompatible"}`).

클라이언트→서버 요청:
```
system.hello | system.info | system.health | system.shutdown
session.create(workdir, mode, provider?, agent?, maxConcurrent?) -> {sessionId}
session.resume(sessionId, afterSeq?) -> {sessionId, events[]}   # afterSeq 이후 결손 이벤트 재전송
session.list() | session.close(sessionId)
session.prompt(sessionId, text, attachments?) -> {turnId}
command.list(sessionId?) -> [{name, summary, argsSchema, source}]  # source: builtin|skill|plugin
command.run(sessionId, name, args) -> {turnId}   # 슬래시 명령의 유일한 실행 경로
session.interrupt(sessionId) | session.setMode(sessionId, mode)
tool.list(sessionId?) -> [{name, category, permissionTag, state, source}]   # state: active|inactive
approval.list(sessionId?) | approval.respond(requestId, decision, scope)
permission.allowlist.add(pattern, scope) | permission.allowlist.list(scope?)
                                         | permission.allowlist.remove(patternId)
agent.list() | agent.create(description) | agent.spawn(name, task)
agent.bindChannel(name, channel) | agent.delete(name)
team.start(sessionId, n, task) -> {teamId} | team.status(teamId)
job.schedule(spec, task, mode, channel) | job.list() | job.cancel(jobId) | job.runNow(jobId)
gateway.bind(platform, credentialsRef, target) | gateway.list() | gateway.unbind(bindingId)
memory.search(query, limit) | memory.write(text, tags)
skill.search(query) | skill.install(source) | skill.list() | skill.reload()
provider.list() | provider.configure(vendor, config) | provider.loginWeb(vendor, method)
backend.set(sessionId, kind, config)   # local|docker|ssh
```
서버→클라이언트 요청(양방향 JSON-RPC):
```
approval.request(requestId, sessionId, tool, args, risk, timeoutSec, scopeHint) -> {decision, scope}
```
서버→클라이언트 알림:
```
session.event(sessionId, seq, kind, payload)
  kind ∈ message.delta | message.done | tool.call | tool.result | diff
        | subagent.spawn | subagent.update | subagent.done
        | team.task.update | mode.changed | usage | error | turn.done
approval.resolved(requestId, decision, by)   # 다른 채널에서 응답된 경우
job.event(jobId, kind, payload) | gateway.event(bindingId, kind, payload)
```
규칙: 모든 이벤트는 `sessionId`별 단조 증가 `seq`를 갖고, 재연결 시 `session.resume(sessionId, afterSeq)`로 결손을 복구한다. 스키마·`PROTOCOL_VERSION`은 `server/protocol.py` 한 곳에만 정의되고, `scripts/gen_protocol.py`가 `sdk/src/protocol.ts`와 `docs/protocol.md`를 생성한다. CI는 생성물이 커밋본과 동일한지 검사한다.

### 3.6 헤드리스 1회성 실행 (`snowpea -c`)
```
snowpea -c "<prompt>" [--mode plan|accept|auto] [--json] [--cwd DIR]
                      [--timeout SEC] [--provider VENDOR] [--approve-none]
```
매핑: ① 데몬 확보 → ② `session.create(workdir=--cwd 또는 현재 디렉터리, mode=--mode 또는 프로젝트 기본값)` → ③ `session.prompt(sessionId, prompt)` → ④ `session.event`를 소비하며 stdout에 렌더 → ⑤ `turn.done` 수신 시 `session.close` 후 종료. TUI를 띄우지 않는다.
- 출력: 기본은 사람용 평문(툴 호출은 한 줄 요약). `--json`이면 수신한 `session.event`를 **JSON Lines**로 그대로 흘리고 마지막 줄에 `{"kind":"result","exitCode":N,"sessionId":...,"usage":{...}}`.
- 승인: 대화형 TTY면 stdin으로 y/n 프롬프트. 비TTY이거나 `--approve-none`이면 승인 요구 시 즉시 거부하고 종료 코드 4.
- 종료 코드: `0` 정상 완료 / `1` 에이전트가 실패로 종결 / `2` 사용법·설정 오류 / `3` 데몬 연결 실패 / `4` 승인 거부 또는 모드 차단 / `5` `--timeout` 초과(세션은 interrupt 후 close).
- 슬래시 명령: `snowpea -c "/ralph <task>"`는 TUI와 동일하게 **코어의 `commands/registry.py`**를 거친다. CLI가 자체 파싱하지 않고 `command.run(sessionId, "ralph", args)`을 호출한다.
- **세션 없는 조회 서브커맨드**: `snowpea tools list [--json]`은 `tool.list`를, `snowpea commands list [--json]`은 `command.list`를 호출하고 즉시 종료한다. 세션을 만들지 않고 LLM도 호출하지 않으므로 설치 검증·CI 스모크에 쓴다. 종료 코드 규약은 위와 같다.
- 이 경로가 §7.9 E2E와 CI 자동화의 기본 실행 수단이다.

---

## 4. Implementation Steps / Milestones (v0.1)

> 각 마일스톤은 "goal / files / deps / satisfies"로 기술한다. 추정은 **1인 기준 영업일 범위**이며, 범위 하단은 함정이 없을 때, 상단은 벤더·OS 편차를 만났을 때다. iteration 1의 단일값 추정은 §9-11에 따라 폐기했다.

### M0 — Repo skeleton, vendoring map, private repo bootstrap — **2–3일**
- **Goal**: `uv sync` + `npm ci`로 개발 환경이 서고, Hermes 이식 대상이 해시까지 확정되며, 비공개 레포 3종이 SDK 소비 경로와 함께 존재한다.
- **Files**: `pyproject.toml`(uv, python>=3.11, console_scripts `snowpea`), `uv.lock`, `core/snowpea_core/__init__.py`, `tui/package.json`, `sdk/package.json`, `LICENSE`(MIT), `NOTICE`(Hermes·OMC 고지), `docs/vendoring-map.md`(원본 경로·커밋 SHA·**파일 SHA256**·목적지·**패치 경로**·사유), `core/snowpea_core/vendor/patches/README.md`(패치 규약), `docs/omc-porting-map.md`, `.github/workflows/ci.yml`(jobs: `unit`, `vendor-integrity`), `scripts/verify_vendor_integrity.py`, `tests/conftest.py`.
- **Private repo bootstrap** (이 레포 밖 작업, M0의 산출물로 추적):
  - `snowpea-ide` — README(목적·v0.2 범위·코어 계약 `snowpea-core --port --token --state-dir`), `package.json`에 `"@snowpea/sdk": "^0.1"` 의존만 선언한 빈 Electron 스켈레톤.
  - `snowpea-registry` — README(API 계약 초안), 빈 서버 스켈레톤.
  - `snowpea-site` — README(두 메시지 카피 초안), 빈 정적 사이트 스켈레톤.
  - 세 레포 모두 **npm으로 공개 레포의 `@snowpea/sdk`를 소비**한다. 소스 경로 참조·서브모듈·코드 복사 금지. v0.1 발행 전에는 `npm link` 또는 `file:` 프로토콜로 로컬 개발.
- **Deps**: 없음.
- **Satisfies**: (전제) AC-19 라이선스 고지, AC-15a의 CI 훅.

### M1 — Thin vertical slice: daemon + protocol pipeline + 1 provider + fs/shell + TUI + modes — **8–12일**
- **Goal**: 개발 체크아웃에서 `uv run snowpea`로 TUI가 뜨고, Anthropic 모델에 프롬프트를 보내 스트리밍 응답을 보고, 파일 편집 1건이 accept 모드로 자동 적용되며, 셸 명령은 승인 프롬프트를 띄운다. **동시에 프로토콜 생성기·문서·SDK 계약 테스트가 이 시점부터 CI를 지킨다.** `snowpea -c`와 `--mode`가 동작한다.
- **Files**: `server/app_server.py`, `server/transport_ws.py`, `server/transport_http.py`, `server/rpc.py`, `server/protocol.py`(PROTOCOL_VERSION 포함), `server/auth.py`, `session/session.py`, `session/store.py`, `session/events.py`, `agent/loop.py`, `agent/agent.py`, `providers/base.py`, `providers/anthropic_native.py`, `tools/registry.py`(+`tool.list`), `commands/registry.py`(슬래시 파서·디스패처 + `command.list`/`command.run`), `tools/fs.py`, `tools/shell.py`, `permissions/policy.py`, `permissions/approval_queue.py`, `config/paths.py`, `config/settings.py`, `config/project.py`, `cli/main.py`(TUI spawn + `-c` 헤드리스 + `--mode`), **`scripts/gen_protocol.py`**, **`docs/protocol.md`**, `sdk/src/protocol.ts`(생성물), `sdk/src/client.ts`, `sdk/src/sessions.ts`, `sdk/src/approvals.ts`, **`sdk/test/contract.test.ts`**, `tui/src/index.tsx`, `tui/src/rpc/client.ts`, `tui/src/components/{Chat,MessageStream,ToolCall,ApprovalPrompt,ModeBar}.tsx`, `tui/src/slash/registry.ts`(`command.list` 소비형 얇은 클라이언트), `tests/test_rpc_roundtrip.py`, `tests/test_headless_exit_codes.py`.
- **Deps**: M0.
- **Satisfies**: **AC-13**(accept 세부 규칙 중 편집 무프롬프트·셸 프롬프트. allowlist 절은 M4), **AC-15a**(프로토콜 + SDK 계약 기본 3건), AC-03(부분: 데몬 기동·재사용), AC-12(부분: `--mode` 런치 플래그).

### M2 — Full tool suite + MCP + execution backends — **10–15일** (2–3주)
- **Goal**: Hermes급 툴셋이 등록되고, `.mcp.json`의 외부 MCP 서버 툴이 같은 레지스트리에 노출되며, 동일 툴셋이 local/docker/ssh에서 동작한다. (iteration 1의 M3에서 앞당김 — 툴 계약이 벤더 정규화의 입력이기 때문.)
- **Files**: `tools/git.py`, `tools/grep.py`, `tools/glob.py`, `tools/web_search.py`(무료 체인 기본: DuckDuckGo → Brave 무료/SearXNG, 유료 선택), `tools/browser.py`, `tools/media.py`(항상 등록, 자격증명 없으면 inactive), `tools/mcp_client.py`, `exec/backend.py`, `exec/local.py`, `exec/docker.py`, `exec/ssh.py`, `core/snowpea_core/vendor/hermes/`(vendoring-map 1차 이식: tools 계열), `tests/fixtures/ssh/docker-compose.yml`(openssh-server, 키 인증), `tests/fixtures/ssh/id_test`(테스트 전용 키쌍), `tests/test_tools_contract.py`, `tests/test_backends.py`.
- **Deps**: M1, `docs/vendoring-map.md`(M0).
- **Satisfies**: AC-06, AC-18.

### M3 — Providers ×11 + setup wizard + web token login — **6–9일**
- **Goal**: `snowpea setup`의 Quick/Full/Blank 흐름으로 11개 벤더 중 임의 조합을 설정하고, 세션이 벤더를 바꿔가며 **동일한 툴 호출 시나리오**를 통과한다. §2.8의 2종 웹 로그인(OpenAI device-code, OpenRouter OAuth PKCE)이 동작한다. Full 흐름은 Hermes setup과 같은 순서의 단계 화면을 가진다: ① LLM 벤더 → ② 검색 제공자(단일 선택, 기본 ★ DuckDuckGo ddgs, 무료·키 없음 항목이 먼저 나열) → ③ 브라우저 제공자(단일 선택, 기본 ★ Local headless Chromium) → ④ 툴 카테고리 토글(웹검색·브라우저·터미널·파일·코드실행·비전·이미지생성·영상생성·TTS·스킬·todo·메모리·세션검색·clarify·delegate·cron·computer-use 등, 기본 ON/OFF는 §3.1 카탈로그) → ⑤ 게이트웨이 → 완료. 각 항목은 `[free|paid|subscription]`, `[no key|key optional|self-hosted]` 태그와 `[active]` 상태를 표시하고 모든 화면에 `Skip — keep defaults` 가 있다. Quick 흐름은 ①만 묻고 나머지는 기본값(무료·키 없음)을 적용한다.
- **Files**: `setup/screens/{providers,search,browser,tools,gateway,done}.py`, `setup/catalog.py`, `tools/search_providers/*.py`, `tools/browser_providers/*.py`, `providers/registry.py`, `providers/openai_compat.py`, `providers/gemini_native.py`, `providers/presets.py`(11종 + `auth_methods`), `providers/auth_web.py`(device_code / oauth_pkce), `providers/normalize.py`, `providers/replay.py`, `setup/wizard.py`, `setup/detect.py`, `tests/fixtures/providers/<vendor>/*.json`(§6 리스크 3 규약), **`tests/fixtures/providers/fake/scripted.py`**(결정적 스크립트 프로바이더), `tests/test_provider_matrix.py`.
- **Deps**: M1, M2(툴 호출 골든 시나리오가 입력).
- **Satisfies**: AC-02, AC-02b.

### M4 — Modes, permissions, allowlist, approval queue UI, project settings — **4–6일**
- **Goal**: plan/accept/auto 3모드가 툴 권한 태그와 결합해 결정적으로 동작하고, allowlist 예외가 적용되며, 프로젝트 기본 모드가 재실행 시 복원된다.
- **Files**: `permissions/allowlist.py`, `commands/mode_cmd.py`, `server/protocol.py`(+`permission.allowlist.*`), `sdk/src/tools.ts`, `tui/src/components/ApprovalQueue.tsx`, `commands/mode_cmd.py` 등록으로 `/mode` `/approvals` 노출(TUI 코드 변경 없음), `tests/test_permission_matrix.py`.
- **Deps**: M1, M2.
- **Satisfies**: AC-05, AC-12, AC-13(allowlist 절).

### M5 — Memory + scheduler + gateway + unattended approval — **10–15일** (2–3주)
- **Goal**: 장기 기억이 세션을 넘어 인용되고, 예약 작업이 데몬에서 실행되어 메신저로 결과가 오며, 무인 승인 요청이 메신저와 TUI 공유 큐에 동시에 뜬다.
- **Files**: `memory/store.py`(SQLite FTS5), `memory/profile.py`, `memory/retrieval.py`, `tools/memory.py`, `scheduler/scheduler.py`, `scheduler/jobs.py`, `scheduler/nl_parse.py`, `tools/schedule.py`, `gateway/base.py`, `gateway/router.py`, `gateway/telegram.py`, `gateway/discord.py`, `gateway/slack.py`, `server/lifecycle.py`(keepalive + 종료 사유 리포트), `cli/commands.py`(`daemon status`), `commands/schedule_cmd.py`(등록만으로 `/schedule` 노출), `core/snowpea_core/vendor/hermes/`(2차 이식: gateway·scheduler·memory), `tests/test_memory_recall.py`, `tests/test_scheduler.py`, `tests/test_approval_multichannel.py`.
- **Deps**: M4(승인 큐 규칙), M2(툴).
- **Satisfies**: AC-07, AC-08, AC-09, AC-20.

### M6 — Skills/plugins loader + marketplace search + generators — **7–10일**
- **Goal**: Claude Code 플러그인(oh-my-claudecode 마켓플레이스 포함)을 설치하면 skills·agents·commands·hooks·MCP가 로드되고, `/agent create`·`/skill learn`이 파일을 산출한다.
- **Files**: `skills/loader.py`, `skills/skill_md.py`, `skills/marketplace.py`, `skills/registry_client.py`(v0.3 서버용 인터페이스), `skills/hooks.py`, `agent/definition.py`, `commands/agent_cmd.py`, `commands/skill_cmd.py`, `cli/commands.py`(`snowpea skill search|install|list`), **`tests/fixtures/plugins/sample-plugin/`**(`plugin.json`, `skills/hello/SKILL.md`, `agents/fixture-agent.md`, `commands/fixture-cmd.md`, `hooks/hooks.json`(PreToolUse → 마커 파일 기록), `.mcp.json`(stdio echo 서버) — 내용 고정·버전 핀), `tests/test_plugin_load.py`.
- **Deps**: M2(MCP), M4(권한).
- **Satisfies**: AC-10, AC-11, AC-14.

### M7 — Built-in commands + subagents + team mode + named agents — **10–15일** (2–3주)
- **Goal**: `/ralph <task>`가 실제 git 레포를 수정하고 완료 판정까지 자동 진행하며, `/team N <task>`가 worktree 병렬 작업을 정책대로 병합하고, 이름 에이전트 2개가 데몬 재시작을 견딘다.
- **Files**: `agent/subagent.py`(`agents.max_concurrent`), `agent/team.py`(병합 정책 AC-16), `agent/named.py`, `commands/registry.py`(명령 확장), `commands/ralph.py`, `commands/ultrawork.py`, `commands/deepinit.py`, `commands/team_cmd.py`, `builtin_skills/deep-interview/SKILL.md`, `builtin_skills/deep-research/SKILL.md`, `builtin_skills/ralplan/SKILL.md`, `cli/commands.py`(`snowpea agents`, `snowpea team status`), `tui/src/components/SubagentTree.tsx`, `tui/src/components/HelpPanel.tsx`, `tests/test_ralph_e2e.py`, `tests/test_team_worktree.py`, `tests/test_named_agent_persistence.py`.
- **Deps**: M5(메모리·채널·스케줄 바인딩), M6(스킬 로더).
- **Satisfies**: AC-03, AC-04, AC-16, AC-17, **AC-15b**(`subagent.*` 계약).

### M8 — Installer, npm publish, protocol freeze, 3-OS E2E — **7–10일**
- **Goal**: `curl -fsSL https://raw.githubusercontent.com/snowpea-ai/snowpea-agent/main/installer/install.sh | sh` 한 줄로 3개 OS에서 설치되고, `@snowpea/sdk`·`@snowpea/tui`가 npm에 발행되며, §7.9 E2E 스크립트가 전부 통과한다. `PROTOCOL_VERSION`을 `1.0.0`으로 올려 동결 카운트의 기점을 만든다(동결 판정 자체는 §4.9).
- **Files**: `installer/install.sh`, `installer/install.ps1`, `installer/brew/snowpea.rb`, `installer/npm/package.json`, `tui/esbuild.config.mjs`(→ wheel package data), `docs/manual/`, `cli/commands.py`(`snowpea service install|uninstall|status`), `.github/workflows/release.yml`(wheel + npm 2종 발행), `tests/e2e/v01_smoke.sh`, `tests/e2e/v01_smoke.ps1`.
- **Deps**: M1–M7 전부.
- **Satisfies**: AC-01, AC-19. (프로토콜 동결은 마일스톤 산출물이 아니라 §4.9의 게이트로 판정한다.)

**총 추정: 64–95 영업일(1인 기준, 약 13–19주).** 임계 경로 M0→M1→M2→M4→M5→M7→M8. M3와 M6은 각각 M2·M5와 부분 병렬 가능하며, 2인 이상이면 총 기간을 9–13주까지 줄일 수 있다. M2·M5·M7은 각각 2–3주 규모의 독립 서브프로젝트로 취급하고 주 단위 중간 데모를 건다.

### 4.9 v0.2 entry gate — protocol v1.0 freeze
v0.1 완료(M8)와 `snowpea-ide` 착수 사이에 **마일스톤이 아닌 게이트**를 둔다. 이 게이트는 코드를 만들지 않고 통과 여부만 판정하며, 통과 전에는 IDE 레포에 제품 코드를 쓰지 않는다.

- **조건**: ① `server/protocol.py`의 `PROTOCOL_VERSION == "1.0.0"` ② `docs/protocol.md`와 `scripts/gen_protocol.py`가 생성하는 스키마가 **연속된 3개 릴리즈 태그에 걸쳐 변경 0** ③ AC-15a·AC-15b 계약 테스트가 그 3개 태그 전부에서 통과.
- **판정 명령**: `python scripts/gen_protocol.py --diff <tag_n-2> <tag_n>` 및 `git log --oneline <tag_n-2>..<tag_n> -- docs/protocol.md sdk/src/protocol.ts` → 각각 빈 출력.
- **불통과 시**: 변경을 유발한 요구를 v0.1 백로그로 되돌리고, 다음 릴리즈부터 3릴리즈 카운트를 재시작한다.
- **통과 후 변경 규칙**: minor 이상 범프 + `system.hello`의 `capabilities` 협상으로만 허용하고, major 변경은 IDE·TUI 동시 릴리즈를 동반한다.
- 대응 수용 기준은 §5 AC-21이다.

### v0.2 전망 (별도 private repo `snowpea-ide`)
Electron + Vue 다크테마 에이전트 조종 앱. npm의 `@snowpea/sdk`를 그대로 사용하고 코어는 수정 없이 내장 실행 파일로 spawn한다. 착수 전제는 §4.9의 protocol v1.0 freeze 게이트 통과다. 쓰레드 채팅·파일별 diff 승인·서브에이전트 트리·통합 터미널·파일트리·worktree 병렬 세션, 그리고 스킬 마켓 브라우저·스케줄 관리·게이트웨이 설정·메모리 브라우저 화면을 추가한다. 코어 내장 방식(§6 리스크 6)과 `session.resume`을 통한 TUI↔IDE 세션 연속성이 이 단계의 두 기술 축이다.

### v0.3+ 전망 (private repos `snowpea-site`, `snowpea-registry`)
snowpea.ai 랜딩이 "오픈소스 멀티벤더 코딩 에이전트"와 "나만의 AI 비서" 두 메시지, 설치 명령, 매뉴얼, 다운로드를 제공한다. 자체 레지스트리는 스킬 업로드·별점·큐레이션 API를 제공하고, v0.1에 인터페이스만 둔 `skills/registry_client.py`가 실제 엔드포인트에 연결되어 TUI/IDE 통합 검색에 포함된다. 이 시점에 설치 URL이 GitHub raw에서 snowpea.ai로 이동한다.

---

## 5. Acceptance Criteria (v0.1, testable)

| ID | 기준 | 검증 명령 | 기대 관측 |
|---|---|---|---|
| AC-01 | 3 OS 설치 | `curl -fsSL https://raw.githubusercontent.com/snowpea-ai/snowpea-agent/main/installer/install.sh \| sh` 후 `snowpea --version` (win: 동일 raw 경로의 `install.ps1`을 `iwr ... \| iex`) | exit 0, `snowpea 0.1.x` 출력. `uv --version`, `node --version` 모두 성공. 이 URL이 v0.3에서 snowpea.ai로 이동하기 전까지의 정본이다 |
| AC-02 | setup 마법사 + 웹 로그인 | `snowpea setup` → Quick → 벤더 1개 키 입력. 이어서 `snowpea setup --login openai`, `--login openrouter`, 그리고 대조군으로 `--login deepseek` | `~/.snowpea/settings.json`에 벤더 항목 생성. OpenAI는 디바이스 코드가 콘솔에 표시되고 승인 후 토큰 저장, OpenRouter는 OAuth PKCE 콜백으로 키 저장. **나머지 9종은 `--login` 시 `error{code:"login_unsupported"}`와 API 키 입력 안내를 출력**. `provider.list`가 벤더 11종을 반환하고 그중 `auth_methods`에 `device_code`/`oauth_pkce`를 가진 것은 정확히 2종 |
| AC-02b | setup Full 단계 화면 + 무료 기본값 | `snowpea setup --full`을 모든 화면에서 Skip으로 통과 → `snowpea tools list --json` | `settings.json`에 `search.provider:"ddgs"`, `browser.provider:"local_chromium"`, 툴 카테고리 기본 ON 집합이 기록됨. API 키가 하나도 없어도 `web_search`·`web_extract`·`browser_*` 툴이 `state:"active"`. `snowpea setup --full` 화면 ②의 목록 순서가 무료·키 없음 → 무료·키/셀프호스트 → 유료 순이며 첫 항목이 ★ 표시. `--search-provider tavily` 등 비대화형 플래그로 동일 결과 |
| AC-03 | TUI 기동 + 데몬 재사용 + /help | `snowpea` 실행 후 `/help` | 데몬 PID가 `~/.snowpea/daemon.json`과 일치(두 번째 실행 시 동일 PID). `/help` 목록에 ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview, plan, accept, auto 9개 모두 표시 |
| AC-04 | ralph 자동 완주 + 동시 서브에이전트 | 테스트 git 레포에서 `/ralph "add a failing test then make it pass"`. 실행 중 별도 셸에서 `snowpea agents --json` 폴링 | 종료 시 `git diff --stat`이 비어있지 않고 변경 파일 존재. 폴링 결과 중 최소 1개 스냅샷에서 **`status=="running"`인 서브에이전트가 2개 이상 동시** 관측되고 TUI SubagentTree에도 2개 이상 running 노드 표시. 완료 이벤트 `turn.done{reason:"complete"}` 수신 |
| AC-05 | 모드 동작 | plan 모드에서 `snowpea -c "write foo.txt"` / auto 모드에서 동일 프롬프트 | plan: `foo.txt` 미생성, `error{code:"mode_denied"}` 이벤트, 종료 코드 4. auto: 승인 프롬프트 없이 생성, `approval.request` 0건, 종료 코드 0 |
| AC-06 | 툴셋 + MCP + 미디어 | `snowpea tools list --json` (세션 미생성, `tool.list` RPC 직접 호출) 또는 TUI `/tools` | 파일·셸·git·검색·브라우저·delegate_task·스케줄·메모리·웹검색·미디어(image/tts/video) 툴이 **전부 목록에 존재**. 미디어 툴은 자격증명 없으면 `state:"inactive"`로 표시되고 호출 시 `error{code:"tool_inactive"}`, 자격증명 설정 후 재시작 없이 `state:"active"`로 전환되어 실행 성공. `.mcp.json` 배치 후 해당 MCP 툴이 `mcp__<server>__<tool>` 이름으로 추가 |
| AC-07 | 장기 메모리 | 세션A에서 "내 배포 대상은 duho 서버다" → 세션 종료 → 새 세션B에서 "내 배포 대상이 뭐였지?" | 세션B 응답에 `duho` 포함 + 출처 메모리 id 인용. 데몬 재시작 후에도 동일 |
| AC-08 | 스케줄러 | `/schedule "매일 09:00" "레포 상태 요약" --channel telegram:<id>` 후 `job.runNow` | 잡이 데몬 프로세스에서 실행되고(부모 PID = 데몬), 결과 메시지가 지정 Telegram 채널에 도착. `job.list`에 마지막 실행 시각 기록 |
| AC-09 | 메신저 게이트웨이 | Telegram 봇에 "지금 작업 중인 레포 이름" 전송 | 같은 에이전트가 응답. `session.list`에 해당 게이트웨이 세션 표시 |
| AC-10 | 플러그인 로드 (고정 픽스처) | `snowpea skill install ./tests/fixtures/plugins/sample-plugin` 후 `/help`, `tool.list`, 픽스처 명령 1회 실행 | 픽스처의 skill 1개·agent 1개·command 1개가 목록에 등장하고 command 실행이 exit 0. **PreToolUse 훅이 `$SNOWPEA_HOME/fixture-hook.marker` 파일을 생성**하고 내용에 툴 이름 포함. 픽스처 `.mcp.json`의 echo 서버 툴이 `tool.list`에 `mcp__fixture-echo__echo`로 나타나고 호출 시 입력 문자열을 그대로 반환. 이어서 `snowpea skill install oh-my-claudecode`도 로드 성공 |
| AC-11 | 생성기 | `/agent create "릴리즈 노트 작성 전담"` → `/agent list` → 해당 에이전트로 위임. `/skill learn` | `.snowpea/agents/<name>.md` 생성, 즉시 `delegate_task` 대상이 됨. `/skill learn`이 `.snowpea/skills/<name>/SKILL.md` 산출 |
| AC-12 | 모드 전환·기억 | `snowpea --mode plan` 기동 → `/accept` → `/mode save` → 재기동 | 상태줄이 각각 PLAN → ACCEPT. `<project>/.snowpea/settings.json`에 `"defaultMode":"accept"` 기록, 재기동 시 ACCEPT로 시작 |
| AC-13 | accept 세부 규칙 | accept 모드에서 파일 편집 1회, `ls` 1회, `permission.allowlist.add("^ls( .*)?$","project")` 후 `ls` 재실행 | 편집은 `approval.request` 0건. 첫 `ls`는 승인 프롬프트 1건. allowlist 등록 후 `ls`는 프롬프트 0건이며 `permission.allowlist.list("project")`에 패턴 1개 |
| AC-14 | 통합 스킬 검색 | `snowpea skill search "pdf"` | 결과 각 항목에 `source` 필드가 `claude-marketplace` / `agentskills.io` / `hermes-hub` 중 하나로 채워지고 3개 출처가 모두 최소 1건씩. `snowpea skill install <id>` 성공 |
| AC-15a | 프로토콜 + SDK 계약 (기본) | `curl http://127.0.0.1:<p>/protocol.json`, `python scripts/gen_protocol.py --check`, `npm -w sdk test -- --grep base` | `/protocol.json`이 전 메서드 스키마 + `version` 반환. `--check`가 생성물과 커밋본 동일(diff 0, exit 0). 계약 테스트 3건 통과: `session.create`, `session.prompt` 스트리밍 수신, `approval.request` → `approval.respond` 왕복. **M1에서 충족되고 M1 이후 모든 PR에서 CI 필수 잡으로 유지된다** |
| AC-15b | 프로토콜 + SDK 계약 (서브에이전트) | `npm -w sdk test -- --grep subagent` | `subagent.spawn` / `subagent.update` / `subagent.done` 이벤트가 SDK 타입대로 수신되고 부모 `sessionId`·`seq` 순서가 보존된다. **M7에서 충족**되며 이후 CI에 합류 |
| AC-16 | 팀 모드 + 병합 정책 | git 레포에서 `/team 3 "3개 모듈에 docstring 추가"`, 이어서 의도적 충돌 태스크로 1회 더 | 각 에이전트가 `snowpea/team-<teamId>-<n>` 브랜치의 자기 worktree에서 커밋(`git worktree list` 3행 추가). 리드가 태스크 완료 순서대로 **`git merge --no-ff` 순차 병합**. 충돌 시 병합을 중단(`git merge --abort`)하고 해당 태스크를 **충돌 hunk를 첨부해 그 에이전트에게 재큐잉**하며 `snowpea team status`가 그 태스크를 `state:"conflict"`(+`retries:n`)로 표시, 재작업 후 병합 성공 시 `state:"merged"`. **재큐잉은 `team.max_conflict_retries`(기본 2)회까지만**이며, 초과하면 그 태스크는 `state:"failed"`로 확정되어 충돌 hunk가 첨부된 채 `team status`에 남고 리드는 나머지 태스크 병합을 계속한다(팀 전체가 멈추지 않는다). 검증: 항상 충돌하는 태스크를 주입하면 정확히 2회 재시도 후 `failed` 1건 + 나머지 `merged`. 종료 시 worktree·브랜치 정리, 기준 브랜치에 성공한 커밋만 반영 |
| AC-17 | 영속 에이전트 | 이름 에이전트 2개 생성 → 각각 다른 Telegram 채널·스케줄 바인딩 → 데몬 재시작 | 재시작 후 `agent.list`에 2개 유지, 바인딩 유지. 에이전트A에게 준 정보가 에이전트B의 `memory.search`에서 조회되지 않음(결과 0건) |
| AC-18 | 실행 백엔드 | `docker compose -f tests/fixtures/ssh/docker-compose.yml up -d` 후 `backend.set(kind:"ssh", host:"127.0.0.1", port:2222, key:"tests/fixtures/ssh/id_test")`, 그리고 `kind:"docker"` | 각 환경에서 `shell` 툴의 `hostname` 출력이 서로 다르고 ssh 경우 컨테이너 호스트명과 일치. 파일 쓰기가 해당 환경 파일시스템에만 반영(로컬에 미생성). local 복귀 시 원래 값 |
| AC-19 | 라이선스·이식 무결성 | `python scripts/verify_vendor_integrity.py` (CI 잡 `vendor-integrity`). 검증 후 vendored 파일 1개를 수정하고 패치 갱신 없이 재실행 | 정상 시: 맵의 원본 SHA256 불일치 0, **원본에 `vendor/patches/<path>.patch`를 적용한 결과가 작업 사본과 바이트 동일**(불일치 0), 고지 헤더 누락 0, 맵 미등재 vendored 파일 0, exit 0. 패치 미갱신 상태에서는 exit≠0과 해당 파일 경로를 출력한다. `NOTICE`에 Hermes·OMC MIT 고지 포함 |
| AC-20 | 무인 승인 + 타임아웃 | 스케줄 잡이 셸 명령을 요구하도록 등록 후 실행. (a) Telegram에서 승인 (b) 무응답 | (a) Telegram 승인 요청 도착 + TUI 승인 큐에 동일 `requestId` 표시, 어느 쪽에서 응답해도 진행되고 다른 쪽에 `approval.resolved` 수신. (b) `approvals.timeoutSec`(기본 300) 경과 후 자동 거부, `job.list` 결과에 `denied_by_timeout` 기록. 대화형 턴에서 발생한 승인은 이 공유 큐에 나타나지 않고 원 표면에서만 응답 가능 |
| AC-21 | protocol v1.0 freeze gate (§4.9, 마일스톤 아님) | v0.2 IDE 착수 직전 `git log --oneline -- docs/protocol.md sdk/src/protocol.ts` 및 최근 3개 릴리즈 태그 간 `python scripts/gen_protocol.py --diff <tagA> <tagB>` | `PROTOCOL_VERSION == "1.0.0"`이고, **직전 3개 릴리즈 동안 `docs/protocol.md`와 생성 스키마에 변경 0**. 위반 시 v0.2 착수 금지. 이후 변경은 minor 이상 범프 + `capabilities` 협상으로만 허용 |

---

## 6. Risks and Mitigations

| # | 리스크 | 영향 | 완화 |
|---|---|---|---|
| 1 | **Hermes 코드 드리프트 / 라이선스** — 복사본이 upstream과 갈라지고 고지가 누락되면 병합 불가 + 법적 노출 | 높음 | `docs/vendoring-map.md`가 원본 커밋 SHA·**파일별 SHA256**·**패치 파일 경로**를 고정한다. vendored 파일 수정은 허용하되 diff를 `core/snowpea_core/vendor/patches/<path>.patch`로 커밋해야 한다. CI 잡 **`vendor-integrity`**가 ① 원본 SHA256 일치 ② **(원본 + 패치) == 작업본** ③ 고지 헤더 존재 ④ 맵 미등재 vendored 파일 부재를 매 PR에서 검사하고, 하나라도 실패하면 머지 차단. 이 구조 덕에 upstream 재동기화는 "새 원본에 우리 패치 재적용"으로 환원된다. 분기별 upstream diff 리뷰 결과를 같은 문서에 append |
| 2 | **Windows 이중 런타임 설치 실패** — uv + Node 둘 다 필요, PATH·실행 정책·경로 길이 문제 | 높음 | TUI를 esbuild 단일 번들로 wheel에 동봉해 사용자 머신 npm install 제거(§2.5). `install.ps1`이 uv·Node 존재를 검사하고 없으면 winget으로 설치, 실패 시 정확한 수동 명령 출력. 긴 경로 회피를 위해 기본 설치 경로 `%LOCALAPPDATA%\snowpea`. CI에 windows-latest E2E 잡 필수 |
| 3 | **11개 벤더 tool-call 포맷 분기** — OpenAI 호환을 표방해도 tool_choice·parallel tool call·스트리밍 delta 형태가 벤더마다 다름 | 높음 | `providers/normalize.py`를 단일 정규화 지점으로 두고, 벤더 quirk를 `presets.py`의 선언적 플래그(`supports_parallel_tools`, `tool_call_style`, `stream_delta_shape`)로 표현. **리플레이 픽스처 규약**: 메인테이너가 실제 키로 골든 시나리오("툴 1회 호출 → 결과 반영")를 1회 녹화해 `tests/fixtures/providers/<vendor>/<case>.json`에 request/response 쌍으로 커밋한다. 녹화기는 헤더의 `Authorization`·쿠키·`organization`·응답 내 계정 식별자를 마스킹하고, 커밋 전 `scripts/scrub_fixtures.py`가 잔여 시크릿 패턴을 검사한다. **CI는 실키를 쓰지 않고 `providers/replay.py` 재생만 수행**한다.
**스크립트 프로바이더**: 녹화 픽스처로 표현하기 어려운 다단계 에이전트 루프(ralph의 반복, team의 병렬 클레임)를 위해 `tests/fixtures/providers/fake/scripted.py`를 둔다. 이것은 벤더 응답을 재생하는 대신 **입력 상태에 따라 미리 정해진 tool-call 시퀀스를 결정적으로 반환**하는 가짜 ChatProvider이며(`SNOWPEA_PROVIDER=fake:<script>`로 선택), 같은 입력에 항상 같은 출력을 낸다. 덕분에 §7.9의 7단계(ralph)와 8단계(team)가 **실 API 키 없이 CI에서 실행**된다. 10~11단계(스케줄 메신저 전달, 승인 타임아웃)는 실제 채널 자격증명이 필요하므로 릴리즈 파이프라인에만 남는다. 벤더 추가·모델 변경 시 픽스처 재녹화 + 매트릭스 통과가 머지 조건 |
| 4 | **무인 승인의 보안** — 메신저 채널이 곧 원격 실행 승인 경로가 된다 | 높음 | 승인 요청은 `requestId` 1회용, 바인딩된 채널·사용자 ID에서 온 응답만 수락. 기본 `approvals.timeoutSec=300`, 만료 시 거부. 대화형 턴의 승인은 원 표면에서만 응답 가능하고 공유 큐로 새지 않는다. `auto` 모드는 스케줄 잡 등록 시 명시 지정해야 쓸 수 있고 등록 자체가 승인 대상. 승인·거부 전량을 `~/.snowpea/logs/approvals.jsonl`에 append. 게이트웨이 자격증명은 OS 키체인 우선, 평문 저장 시 0600 |
| 5 | **OMC 스킬 포팅 충실도** — Claude Code 전용 개념(executor 에이전트, state/notepad MCP, Task 툴)을 잃으면 명령 동작이 달라짐 | 중간 | `docs/omc-porting-map.md`에 원본 개념 ↔ snowpea 대체 표를 두고, 포팅한 명령마다 "원본 대비 달라진 점" 섹션을 SKILL.md 하단에 명시. 프롬프트가 본질인 3개는 마크다운 원문 보존(§2.7). 명령별 행동 테스트를 최소 1개씩 둔다(`tests/test_ralph_e2e.py` 등) |
| 6 | **v0.2 Electron의 Python 코어 내장** — 코어를 앱 번들에 넣는 방식이 미정이면 v0.1 설계가 막을 수 있다 | 중간 | v0.1에서 코어가 **환경 비의존 실행 파일 경로 + 포트 + 토큰**만으로 기동되도록 계약을 고정한다(`snowpea-core --port --token --state-dir`). `config/paths.py`가 `SNOWPEA_HOME`을 존중하고 전역 경로를 하드코딩하지 않는다. v0.2 번들링은 uv 환경을 앱 리소스에 동봉하거나 **PyInstaller onedir**(디렉터리 배포)를 쓴다. **single-file(onefile)은 사양의 "단일 바이너리 번들 배포" Non-Goal에 해당하므로 채택하지 않는다.** 어느 쪽이든 위 계약만 만족하면 코어 수정 0 |
| 7 | **범위 과대** — v0.1에 9개 영역이 동시에 들어간다 | 높음 | M1 수직 슬라이스를 게이트로 삼아, M1 미완 상태에서 이후 착수 금지. 마일스톤별 AC 매핑(§4)으로 완료 정의를 사전 고정. M2·M5·M7은 2–3주 서브프로젝트로 취급하고 주간 데모 |
| 8 | **데몬 좀비·포트 충돌** | 중간 | `~/.snowpea/daemon.json`(port/pid/token/startedAt) + PID 생존 검사 + `snowpea daemon status|stop`. status는 종료 예정 여부와 이유를 출력. 포트는 0번 바인딩 후 실제 포트 기록(고정 포트 미사용) |
| 9 | **프로토콜 드리프트** — 생성물과 손수정본이 갈라져 IDE가 깨진다 | 중간 | `scripts/gen_protocol.py --check`를 M1부터 CI 필수 잡으로 두고 `sdk/src/protocol.ts` 수기 편집 금지. AC-21 freeze gate가 v0.2 착수 조건 |

---

## 7. Verification Steps

**공통 전제**: 테스트용 git 레포 `/tmp/snowpea-fixture`(초기 커밋 1개). 벤더 키 1개 이상(로컬 개발) 또는 리플레이 픽스처(CI). SSH 백엔드 테스트는 `docker compose -f tests/fixtures/ssh/docker-compose.yml up -d`로 openssh-server 컨테이너(포트 2222, `tests/fixtures/ssh/id_test` 키 인증)를 먼저 띄우고, 종료 시 `down -v`로 정리한다. Docker 백엔드 테스트는 Docker 데몬 가용을 전제한다.

### 7.1 M0
```
uv sync && uv run python -c "import snowpea_core; print(snowpea_core.__version__)"
npm ci --prefix tui && npm ci --prefix sdk
test -s docs/vendoring-map.md && test -s NOTICE
uv run python scripts/verify_vendor_integrity.py   # 등재 0건이어도 성공. 이후 (원본+패치)==작업본 검증
gh repo view snowpea-ai/snowpea-ide snowpea-ai/snowpea-registry snowpea-ai/snowpea-site
```
기대: 버전 출력, exit 0, 무결성 검사 통과(등재 0건이어도 성공), 비공개 레포 3개 존재.

### 7.2 M1
```
uv run python -m snowpea_core --port 0 &
P=$(jq -r .port ~/.snowpea/daemon.json); curl -s http://127.0.0.1:$P/health
curl -s http://127.0.0.1:$P/protocol.json | jq -r .version
uv run python scripts/gen_protocol.py --check
uv run pytest tests/test_rpc_roundtrip.py tests/test_headless_exit_codes.py -q
npm -w sdk test -- --grep base      # AC-15a 3건
cd /tmp/snowpea-fixture && uv run snowpea -c "add one line to README.md" --mode accept; echo $?
uv run snowpea --mode plan -c "write foo.txt"; echo $?
uv run snowpea
```
기대: `/health`가 `{"status":"ok"}`, `/protocol.json`의 `version`이 semver. `--check` diff 0. pytest·SDK 계약 테스트 통과. accept 헤드리스 종료 코드 0 + `git diff` 1파일. plan 헤드리스 종료 코드 4 + `foo.txt` 미생성. TUI에서 토큰 스트리밍, 파일 편집 무프롬프트, `ls` 승인 프롬프트 1회.

### 7.3 M2
```
docker compose -f tests/fixtures/ssh/docker-compose.yml up -d
uv run pytest tests/test_tools_contract.py tests/test_backends.py -q
docker compose -f tests/fixtures/ssh/docker-compose.yml down -v
# TUI에서
/tools            # 전체 카테고리 + 미디어 툴 inactive 확인
/backend docker   # 셸 툴로 hostname 확인 → /backend ssh 반복
```
기대: 툴 목록 완비(미디어 포함, 자격증명 없으면 inactive). docker/ssh 전환 후 `hostname`이 각각 컨테이너·원격 호스트 값.

### 7.4 M3
```
uv run snowpea setup            # Quick → 벤더 1개
uv run snowpea setup --login openrouter
SNOWPEA_PROVIDER_MODE=replay uv run pytest tests/test_provider_matrix.py -q
uv run python scripts/scrub_fixtures.py --check tests/fixtures/providers
```
기대: 설정 파일에 벤더 기록, OAuth 콜백으로 키 저장. 매트릭스가 11개 벤더 전부 replay 통과. 시크릿 스캔 0건.

### 7.5 M4
```
uv run pytest tests/test_permission_matrix.py -q
uv run snowpea --mode plan     # 쓰기 시도 → 차단
```
기대: 매트릭스(3 모드 × 5 권한 태그 = 15 케이스) 전부 통과. plan 모드 쓰기 차단 메시지. allowlist 등록 전후 프롬프트 수 1 → 0.

### 7.6 M5
```
uv run pytest tests/test_memory_recall.py tests/test_scheduler.py tests/test_approval_multichannel.py -q
uv run snowpea daemon status
```
기대: 3개 파일 통과. `daemon status`가 카운터 4종과 종료 사유 문장을 출력. 수동으로 AC-08·AC-09·AC-20 시나리오 1회 실행하여 실제 Telegram 도달 확인.

### 7.7 M6
```
uv run pytest tests/test_plugin_load.py -q
uv run snowpea skill install ./tests/fixtures/plugins/sample-plugin
test -f "$SNOWPEA_HOME/fixture-hook.marker"
uv run snowpea -c "/fixture-cmd" --json | jq -r 'select(.kind=="tool.result")'
uv run snowpea skill search pdf
```
기대: 픽스처에서 skills/agents/commands/hooks/mcp 5종 전부 로드. 훅 마커 파일 존재. `mcp__fixture-echo__echo` 호출 결과가 입력과 동일. 검색 결과에 3개 출처.

### 7.8 M7
```
cd /tmp/snowpea-fixture && uv run snowpea
/ralph "add a failing test then make it pass"     # 실행 중 별도 셸: watch -n1 'snowpea agents --json | jq "[.[]|select(.status==\"running\")]|length"'
/team 3 "3개 모듈에 docstring 추가"
snowpea team status
uv run pytest tests/test_named_agent_persistence.py tests/test_team_worktree.py -q
npm -w sdk test -- --grep subagent   # AC-15b
```
기대: ralph 종료 후 `git diff --stat` 비어있지 않고, 폴링 중 running 서브에이전트 수가 2 이상인 순간이 최소 1회. `git worktree list` 3개 추가 후 정리, `team status`에 `merged` 3건(충돌 시나리오에서는 `conflict` → 재작업 → `merged`, 상한 초과 시 `failed`). 데몬 재시작 후 이름 에이전트 2개 유지. `subagent.*` 계약 테스트 통과(AC-15b).

### 7.9 v0.1 End-to-End (mac / windows / linux 각각 1회)
`tests/e2e/v01_smoke.sh` (win: `v01_smoke.ps1`)가 다음을 순서대로 실행하고 각 단계의 기대 관측을 assert 한다. 모든 에이전트 실행은 §3.6 헤드리스 모드를 쓰고 종료 코드로 판정한다.
```
1  curl -fsSL https://raw.githubusercontent.com/snowpea-ai/snowpea-agent/main/installer/install.sh | sh
                                            → snowpea --version == 0.1.x
2  snowpea setup --quick --vendor <v> --key $KEY          → settings.json 기록
3  snowpea daemon status                                   → running, port>0, 종료 사유 출력
4  snowpea -c "edit README.md: add one line" --mode accept → exit 0, git diff 1 file
5  snowpea -c "/help" --json                               → 9개 내장 명령 문자열 매칭
6  snowpea tools list --json                                 → 미디어 툴 present(state 확인)
7  snowpea -c "/ralph 'make tests pass'" --mode auto        → exit 0, diff 비어있지 않음
8  snowpea -c "/team 2 'add docstrings'" --mode auto;
   snowpea team status                                      → merged 2건, worktree 정리
9  snowpea skill install ./tests/fixtures/plugins/sample-plugin → 훅 마커 + MCP 툴 실행
10 snowpea job schedule --in 60s --task "echo hi" --channel telegram:$CH → 60초 내 채널 수신
11 approval timeout 시나리오                                → denied_by_timeout 기록
12 backend docker / ssh 시나리오                            → hostname 상이
13 snowpea --mode plan -c "write foo.txt"                   → exit 4, 파일 미생성
14 python scripts/gen_protocol.py --check                   → diff 0
15 snowpea daemon stop                                      → PID 소멸, daemon.json 정리
```
7·8단계는 **`--mode auto`로 실행**한다. 두 명령은 다단계 셸·git 작업을 포함하므로 accept 모드로는 비대화형 스크립트에서 승인 대기에 걸린다. accept 모드의 승인 규칙(편집 무프롬프트, 셸 프롬프트, allowlist 예외)은 E2E가 아니라 AC-13·AC-14와 `tests/test_permission_matrix.py`가 담당한다. 픽스처 레포는 매 실행 시 새로 만들고 종료 시 삭제하므로 auto 모드의 부작용 범위가 `/tmp/snowpea-fixture`로 한정된다.

합격 기준: 15단계 전부 기대 종료 코드, 3개 OS 모두. CI(`.github/workflows/ci.yml`)가 ubuntu/macos/windows 러너에서 **1~9, 12~15를 자동 실행**한다. 이때 4·7·8단계의 LLM 호출은 `SNOWPEA_PROVIDER=fake:<script>`(§6 리스크 3의 스크립트 프로바이더)로 대체되어 실 API 키가 필요 없고, 12단계의 ssh는 픽스처 컨테이너를 쓴다. **10~11단계(메신저 전달, 승인 타임아웃)만** 실제 채널 자격증명이 있는 릴리즈 파이프라인에서 실행한다.

---

## 8. Open Questions (비즈니스 판단 필요)

1. **메신저 게이트웨이 v0.1 필수 1종 선택** — 수용 기준은 "최소 1개"다. 본 계획은 Telegram을 기준으로 M5를 배열했다. 사업상 Slack 우선이면 M5 내부 순서와 AC-08·AC-09·AC-20의 채널이 바뀐다.

> iteration 1의 Q1(설치 도메인), Q2(웹 로그인 벤더), Q4(미디어 툴 경계)는 각각 AC-01, §2.8, §3.1에서 확정되어 삭제되었다. 최종 후속 결정으로 웹검색 기본값도 확정되어(무료 제공자 기본, 유료는 선택) 삭제되었다.

---

## 9. ADR — snowpea-agent core architecture

Status: accepted (consensus iteration 3). Date: 2026-09-11. Supersedes: none.

### Decision

**A. 프로토콜은 WebSocket 위의 JSON-RPC 2.0, 부수적으로 읽기 전용 HTTP 3개.** 코어 데몬은 토큰 인증 핸드셰이크(`system.hello`) 후 단일 WS 연결에서 세션을 다중화하고, 상태를 바꾸는 모든 메서드는 이 채널에만 존재한다. 같은 포트의 `GET /health`, `GET /version`, `GET /protocol.json`은 설치 검증·디버깅·스키마 덤프 전용이며 쓰기 기능이 없다. 스키마와 `PROTOCOL_VERSION`은 `server/protocol.py` 한 곳에 정의되고 `scripts/gen_protocol.py`가 TS 타입과 문서를 생성한다.

**B. Hermes vendoring은 B2+B3 하이브리드, 무결성은 해시와 패치로 증명한다.** 실무 함정이 값비싼 영역(tools, gateway, scheduler, memory, setup)은 선별 복사하고, snowpea 고유 개념 영역(server, session, agent, providers, permissions, skills, commands)은 Hermes를 읽되 새로 작성한다. 복사한 파일은 수정해도 되며, 대신 원본 SHA256을 `docs/vendoring-map.md`에, 수정 diff를 `vendor/patches/<path>.patch`에 커밋한다. CI 잡 `vendor-integrity`가 (원본 + 패치) == 작업본을 검증하므로 upstream 재동기화가 "새 원본에 우리 패치 재적용"으로 환원된다.

**C. `snowpea`는 Python 콘솔 스크립트이고 번들된 Ink TUI를 spawn한다.** 진입점이 하나여서 데몬 수명 관리 주체가 명확하다. TUI는 esbuild 단일 번들로 wheel의 package data에 동봉되어 사용자 머신에서 npm install이 필요 없다. 동시에 `@snowpea/sdk`와 `@snowpea/tui`를 v0.1에 npm으로 발행해, 비공개 레포 3종(IDE·레지스트리·사이트)이 소스 참조나 서브모듈 없이 패키지로만 소비하게 한다. 개발 중에는 `SNOWPEA_TUI_ENTRY`로 번들 대신 소스를 띄운다.

**D. 데몬은 lazy-start, 종료는 바인딩을 보고 결정하며, 그 판단은 관측 가능하다.** 첫 클라이언트가 데몬을 띄우고 `~/.snowpea/daemon.json`에 port/pid/token을 기록한다. idle 종료 타이머는 활성 세션·스케줄 잡·게이트웨이 바인딩·영속 에이전트가 모두 0일 때만 작동한다. `snowpea daemon status`는 네 카운터와 함께 종료 예정 여부 및 그 이유를 문장으로 출력한다. 재부팅 영속이 필요한 사용자만 `snowpea service install`을 쓰고, 기본은 비활성이다.

**E. OMC 명령 포팅은 성격에 따라 둘로 나누고, 슬래시 디스패치는 코어가 소유한다.** 루프·병렬·완료 판정이 본질인 ralph·ultrawork·team·deepinit은 `commands/`의 Python 워크플로로 포팅하고, 프롬프트가 본질인 deep-interview·deep-research·ralplan은 `builtin_skills/`의 SKILL.md로 원문을 보존해 사용자 스킬과 동일한 로더로 읽는다. 그 위에서 슬래시 명령의 파싱·해석·디스패치는 전부 `commands/registry.py`에 모이고 `command.list`/`command.run`으로 노출된다. TUI·헤드리스 CLI·게이트웨이·향후 IDE가 각자 파서를 갖지 않고 이 한 곳을 통과한다.

### Drivers
1. v0.1 범위가 비정상적으로 넓다(툴셋·11벤더·메모리·스케줄러·게이트웨이·플러그인·팀모드가 모두 1차). 재사용 극대화와 조기 실행 가능성이 다른 기준을 이긴다.
2. 3 OS × 2 런타임(Python + Node) 설치 신뢰성이 첫 사용자 이탈 지점이다. 설치 경로를 줄이는 선택이 우대된다.
3. 표면 두 개(TUI, IDE)가 같은 코어를 공유하므로 프로토콜이 v0.2 착수 전에 얼어야 한다. 표현력·스트리밍·양방향성이 성능보다 중요하다.

### Alternatives considered
- **A**: HTTP+SSE(opencode 방식) 기각 — 서버발 승인 *요청*을 자연스럽게 표현하지 못해 폴링이나 별도 채널이 필요하다. WS와 HTTP 양쪽 전체 API 제공(A3) 기각 — 표면이 두 배가 되고 드리프트 위험만 늘어난다.
- **B**: 전체 복사 후 가지치기(B1) 기각 — Hermes 고유 개념이 영구 잔류하고 3모드·팀모드·영속 에이전트 요구와 충돌한다. 순수 참조 전용(B3 단독) 기각 — 메신저 API quirk, 스케줄 재기동, FTS 스키마 같은 해결된 함정을 다시 밟아 일정 위험이 최대가 된다.
- **C**: npm 설치 TUI가 Python 데몬을 spawn(C2) 기각 — uv 부트스트랩을 Node가 떠맡아 Windows 실패율이 오르고, 헤드리스·IDE 사용에도 Node 의존이 생긴다. 두 진입점 동시 제공(C3) 기각 — 수명 관리 규칙이 두 벌이 되어 좀비 데몬과 포트 충돌 비용을 낳는다.
- **D**: 항상 켜진 사용자 서비스(D1) 기각 — OS 3종 서비스 등록 비용과, 설치 즉시 상주하는 데 대한 반감·보안 리뷰 부담이 크다. 순수 lazy-start + idle 종료(D2) 기각 — 데몬 미기동 시 예약 작업과 메신저 수신이 누락되어 v0.1 수용 기준을 못 채운다.
- **E**: 전부 Python 포팅(E1 단독) 기각 — deep-interview 같은 프롬프트 본질 명령의 충실도가 떨어진다. 전부 SKILL.md 유지(E2 단독) 기각 — ralph의 반복과 team의 클레임이 비결정적이 된다. 슬래시 파싱을 TUI에 두는 안 기각 — IDE·게이트웨이·헤드리스가 각자 파서를 갖게 되어 동작이 갈라진다.
- **F(부수)**: 웹 토큰 로그인 대상에서 Nous Portal 제외 — 사양의 11개 벤더 목록 밖이다.

### Why chosen
세 드라이버가 같은 방향을 가리켰다. 양방향 프로토콜은 승인 큐라는 v0.1의 핵심 요구를 추가 채널 없이 표현하고, 단일 Python 진입점은 설치 실패 표면을 하나로 줄이며, 선별 vendoring은 남은 일정을 고유 기능에 쓰게 한다. 해시+패치 무결성은 "복사본을 고치지 말라"는 지키기 어려운 규칙 대신 "고치되 증명하라"는 검증 가능한 규칙을 준다. 바인딩 인식 keepalive는 상주 데몬의 기능성과 lazy-start의 설치 단순성을 모두 얻는 유일한 지점이었다. 명령 디스패치를 코어에 두는 결정은 v0.2 IDE가 TUI 로직을 복제하지 않게 만드는 가장 값싼 보험이다.

### Consequences
**Positive**
- 승인·서브에이전트·스케줄 이벤트가 한 스트림으로 흘러 TUI와 로그에서 동시에 관측된다.
- IDE는 npm 패키지 두 개만 소비하면 되고, 코어 수정 없이 붙는다.
- upstream Hermes 변경 흡수가 패치 재적용이라는 기계적 절차로 환원된다.
- 사용자 머신에 npm install이 없어 Windows 설치 실패 모드가 하나 줄어든다.
- 새 표면(게이트웨이, 향후 IDE)이 명령을 추가 구현 없이 즉시 얻는다.

**Negative**
- WS 재연결·재구독과 `seq` 기반 결손 복구를 직접 구현해야 한다(`session.resume(afterSeq)`).
- 패치 파일이라는 추가 유지 대상이 생기고, vendored 파일 수정마다 패치 갱신을 잊으면 CI가 막는다.
- TUI 변경이 번들 재생성을 요구해 개발 루프가 한 단계 길어진다(`SNOWPEA_TUI_ENTRY`로 완화).
- 종료 조건이 상태 의존이라 데몬 수명 테스트 케이스가 늘어난다.
- 명령이 코어에 있어 순수 프런트엔드 변경도 데몬 재시작을 부르는 경우가 생긴다.
- 11개 벤더의 tool-call 정규화가 `normalize.py` 한 파일에 집중되어 변경 영향 범위가 넓다.

### Follow-ups
1. **첫 게이트웨이 플랫폼 확정** — 수용 기준은 "최소 1개"이며 본 계획은 **Telegram을 기본 전제**로 M5를 배열했다. Slack 우선으로 뒤집으면 M5 내부 순서와 AC-08·AC-09·AC-20의 채널 표기가 바뀐다. (§8 Open Question 1)
2. **웹검색 기본값 = 무료 (사용자 확정, 2026-09-11)** — 기본 체인은 무료 제공자만으로 구성한다: DuckDuckGo → (실패/차단 시) Brave Search 무료 티어 또는 사용자 지정 SearXNG 인스턴스. API 키 없이 `snowpea setup` Quick 흐름만으로 웹검색 툴이 `state:"active"`가 되어야 한다. Tavily/Exa 등 유료 제공자는 `setup` Full 흐름의 선택 항목이며 매뉴얼에서도 "선택" 절에만 둔다. 제공자 카탈로그는 Hermes setup의 목록을 기준으로 한다(검색: Firecrawl self-hosted, Brave Free, DuckDuckGo ddgs, Exa Free/Paid, Firecrawl, Keenable Free/Paid, Parallel Free/Paid, SearXNG, Tavily keyless, xAI Grok; 브라우저: Local Chromium, Camoufox, Browser Use, Browserbase, Firecrawl cloud). Nous Subscription 항목은 제외. 반영 위치: `tools/web_search.py`, `config/schema.py`의 `search.providers` 순서 기본값, AC-06 웹검색 툴 활성 조건.
3. **v0.2 진입 게이트** — `snowpea-ide` 착수는 §4.9의 protocol v1.0 freeze(연속 3릴리즈 스키마 무변경 + AC-15a·15b 통과) 통과 이후로 한다. 불통과 시 3릴리즈 카운트를 재시작한다.

---

## 10. Changelog

### iteration 1 → iteration 2

| # | 요구 사항 | 반영 위치 |
|---|---|---|
| 1 | M1에 프로토콜 생성기·문서·SDK 계약 테스트 추가, AC-15를 M1으로 이동 | §4 M1 files(`scripts/gen_protocol.py`, `docs/protocol.md`, `sdk/test/contract.test.ts`) 및 satisfies, §5 AC-15, §7.2 |
| 2 | M1에 `config/project.py`와 `--mode` 런치 플래그 추가, M1이 AC-13 클레임 | §4 M1 files·satisfies(allowlist 절만 M4로 분리), §4 M4 satisfies |
| 3 | 헤드리스 `snowpea -c` 정의, `session.resume(afterSeq)` 수정 | §3.6 신설, §1.2 헤드리스 행, §3.1 `cli/main.py`, §3.5 메서드 목록, §7.2·§7.9 |
| 4 | 웹 토큰 로그인 벤더 확정, AC-02 재작성, Q2 삭제 | §2.8 신설, §1.2 벤더 행, §3.1 `auth_web.py`, §5 AC-02, §8 각주 |
| 5 | 미디어 툴 상시 등록·자격증명 시 활성, Q4 삭제 | §3.1 `tools/media.py`, §5 AC-06, §7.3, §8 각주 |
| 6 | M0에 비공개 레포 3종 스켈레톤 + npm SDK 소비 경로 | §4 M0 "Private repo bootstrap", §1.4, §7.1 |
| 7 | vendored 원본 파일별 SHA256 + `vendor-integrity` CI 잡 | §2.4 절차, §4 M0 files, §5 AC-19, §6 리스크 1 |
| 8 | 팀 worktree 병합 정책 명시 | §5 AC-16, §3.1 `agent/team.py`, §7.8 |
| 9 | 서브에이전트 동시성 설정화(`agents.max_concurrent`, 기본 3) | §1.2, §3.1 `agent/subagent.py`·`config/settings.py`, §3.5 `session.create` |
| 10 | 프로바이더 리플레이 픽스처 규약(1회 녹화·시크릿 스크럽·CI replay) | §6 리스크 3, §3.1 `providers/replay.py`, §4 M3 files, §7.4 |
| 11 | 추정 재산정(범위화, M2·M5·M7 각 2–3주) 및 M2↔M3 순서 교환 | §4 전체 재번호, 각 마일스톤 헤더, 총계 문단 |
| 12 | `tool.list`, `permission.allowlist.*` RPC 및 `/schedule` 슬래시 명령 추가 | §3.5, §3.2 `slash/registry.ts`, §4 M1·M4·M5 files |
| 13 | 프로토콜 `version` 필드와 freeze gate AC | §2.1 원칙 2, §3.5 핸드셰이크, §5 AC-21, §4 M8 satisfies |
| 14 | 설치 URL을 구체 소스로 고정, Q1 삭제 | §5 AC-01, §7.9 1단계, §8 각주 |
| 15 | AC-04에 동시 running 서브에이전트 2개 이상 단언 | §5 AC-04, §7.8 |
| 16 | 고정 픽스처 플러그인 + 훅 마커 + MCP 툴 관측 | §4 M6 files(`tests/fixtures/plugins/sample-plugin/`), §5 AC-10, §7.7 |
| 17 | SSH 호스트 픽스처(docker-compose openssh-server) | §7 공통 전제, §4 M2 files, §5 AC-18, §7.3 |
| 18 | PyInstaller **onedir** 명시(onefile은 Non-Goal) | §6 리스크 6 |
| 19 | 옵션 B3 "참조 전용" 추가, 권고를 B2+B3 하이브리드로 명시 | §2.4 |
| opt | `@snowpea/tui` v0.1 npm 발행 | §2.5, §4 M8 files |
| opt | `SNOWPEA_TUI_ENTRY` 개발 탈출구 | §2.5 |
| opt | `snowpea daemon status`가 종료 사유 출력 | §2.6, §6 리스크 8, §7.6, §7.9 3단계 |
| opt | 대화형 승인은 원 표면 전용, 무인 승인만 공유 큐 | §3.1 `approval_queue.py`, §5 AC-20, §6 리스크 4 |

### iteration 2 → iteration 3

| # | 요구 사항 | 반영 위치 |
|---|---|---|
| 1 | vendor 무결성을 "원본 SHA256 + 커밋된 패치" 기준으로 재정의(바이트 동일성 아님) | §2.1 원칙 4, §2.4 "수정 추적" 항목, §2.5 레이아웃(`vendor/patches/`), §4 M0 files, §5 AC-19, §6 리스크 1 |
| 2 | AC-21을 M8 satisfies에서 제거하고 v0.2 진입 게이트를 별도 절로 분리 | §4.9 신설, §4 M8 goal·satisfies, §2.1 원칙 2, §5 AC-21 제목, v0.2 전망 문단 |
| 3 | 슬래시 명령 파싱·디스패치를 코어로 이동, `command.list`/`command.run` RPC 노출, TUI는 얇은 클라이언트 | §3.1 `commands/registry.py`, §3.2 `slash/registry.ts`, §3.5 메서드, §3.6, §4 M1·M4·M5·M7 files |
| 4 | `snowpea tools list --json` 1급 서브커맨드로 교체 | §3.1 `cli/commands.py`, §3.6, §5 AC-06, §7.9 6단계 |
| 5 | Nous Portal 삭제, 벤더 11종 유지, 웹 로그인 2종(OpenAI device-code, OpenRouter PKCE) | §1.2 벤더 행, §2.8 표·본문, §3.1 `auth_web.py`, §4 M3 files, §5 AC-02 |
| 6 | 충돌 재큐잉 상한 `team.max_conflict_retries`(기본 2), 초과 시 `state:"failed"` | §3.1 `agent/team.py`·`config/settings.py`, §5 AC-16 |
| 7 | AC-15를 15a(M1: 세션·스트리밍·승인 왕복)와 15b(M7: `subagent.*`)로 분리 | §3.3 계약 테스트 주석, §4 M0·M1·M7 satisfies, §5 AC-15a/AC-15b, §7.2·§7.8 |
| 8 | 스크립트 가짜 프로바이더로 CI에서 ralph·team 실행(키 불요), 10~11만 자격증명 파이프라인 | §4 M3 files, §6 리스크 3, §7.9 CI 분리 문단 |

### iteration 3 → final

| # | 요구 사항 | 반영 위치 |
|---|---|---|
| A | 헤더를 `pending approval (consensus reached, iteration 3)`로 변경 | 문서 헤더, Revision note |
| B1 | §7.9 7·8단계를 `--mode auto`로 실행하고 그 이유·범위 제한을 명시. accept 규칙 검증은 AC-13·AC-14·권한 매트릭스가 담당 | §7.9 스크립트 7·8행 및 직후 문단 |
| B2 | M3 goal의 "3종 웹 로그인" → "2종 웹 로그인(OpenAI device-code, OpenRouter PKCE)" | §4 M3 goal |
| B3 | §2.8 제목 괄호를 "(iteration 3에서 확정)"으로 정정 | §2.8 제목·도입문 |
| B4 | M8 goal의 `<url>` 자리표시자를 AC-01·§7.9와 동일한 raw GitHub 설치 URL로 치환 | §4 M8 goal |
| B5 | 계약 테스트의 `--grep base` / `--grep subagent` 분리를 SDK 파일 설명에 명시 | §3.3 `sdk/test/contract.test.ts` |
| C | ADR 절 신설(Decision 5건 + commands-in-core, Drivers, Alternatives considered, Why chosen, Consequences, Follow-ups) | §9 ADR (changelog는 §10으로 이동) |
| D | 웹검색 기본값 확정: 무료 제공자 체인 기본, 유료는 선택. §8 Open Question 2 삭제 | §3.1 `web_search.py`, §4 M2 files, §9 Follow-ups 2, §8 |
| E | setup Full 흐름을 Hermes 식 단계 화면(벤더→검색→브라우저→툴 토글→게이트웨이)으로 구체화, 검색/브라우저 제공자 카탈로그와 무료 기본값 명시, AC-02b 추가 | §3.1 `tools/search_providers`·`browser_providers`·`setup/screens`, §4 M3, §5 AC-02b, §9 Follow-ups 2 |

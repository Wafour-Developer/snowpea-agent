# Deep Interview Spec: snowpea agent


## Context
사용자는 Claude Code / Hermes Agent / opencode 급의 자체 AI 에이전트 플랫폼 "snowpea agent"를 처음부터 만들려 한다. `snowpea/` 폴더는 비어 있다(greenfield). 요구가 매우 넓어(코어·CLI·IDE·확장 생태계·셋업·랜딩) 17라운드 딥 인터뷰로 정체성, 기반 전략, 아키텍처, 1차 릴리즈 범위, 완료 기준을 확정했다. 이 문서는 후속 계획(omc-plan consensus)과 실행의 단일 입력이다.

## Metadata
- Interview ID: di-snowpea-agent-20260911
- Rounds: 17 (+ Round 0 topology)
- Final Ambiguity Score: 12%
- Type: greenfield
- Generated: 2026-09-11
- Threshold: 0.2
- Threshold Source: default
- Initial Context Summarized: no
- Status: PASSED
- Challenge modes used: Contrarian (R4), Simplifier (R6), Ontologist (R8)

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|---|---|---|---|
| Goal Clarity | 0.86 | 0.40 | 0.34 |
| Constraint Clarity | 0.94 | 0.30 | 0.28 |
| Success Criteria | 0.84 | 0.30 | 0.25 |
| **Total Clarity** | | | **0.88** |
| **Ambiguity** | | | **0.12** |

## Topology
| Component | Status | Description | Coverage |
|---|---|---|---|
| agent-core | active | Python 에이전트 런타임. 로컬 WS JSON-RPC 서버(app-server 방식). 멀티벤더 LLM, Agent/SubAgent, Hermes급 툴셋, MCP, 모드, 메모리, 스케줄러, 메신저 게이트웨이, 웹검색, 미디어 생성 | R1, R5, R8, R9 |
| cli | active | 1차 표면. Node/Ink 풀 TUI (Claude Code·Hermes 계열). 내장 OMC급 명령 | R4, R5, R6 |
| ide | active | Electron+Vue 다크테마 에이전트 조종 앱 (Codex 앱 방식). mac/win/linux. 코어 내장. v0.2 | R3, R13 |
| extensions | active | Claude Code 플러그인/SKILL.md 호환, 내장 스킬셋, 스킬 마켓(집합 검색 + 자체 레지스트리), 에이전트/스킬 자동 생성 | R2, R6, R10, R12 |
| setup | active | `curl \| sh` 설치 + `snowpea setup` 마법사 (CLI·IDE 양쪽), 벤더 인증 | R7, R11 |
| site-docs | active | 랜딩 페이지("코딩 에이전트" + "나만의 AI 비서"), 홍보, 매뉴얼, 자체 스킬 레지스트리 웹 | R8, R10 |

## Goal
Hermes Agent의 코드를 복사·수정(git 포크 아님)한 **Python 코어**가 로컬 WebSocket JSON-RPC 서버로 동작하고, 그 위에 **Node/Ink 풀 TUI(1차 표면)**와 **Electron/Vue 데스크톱 앱(2차 표면)**이 같은 TS 클라이언트 SDK로 붙는 오픈소스 멀티벤더 AI 에이전트 플랫폼을 만든다. 본질은 코딩 에이전트이지만 메신저 게이트웨이·장기 메모리·스케줄러·웹검색·미디어 생성이 코어에 포함되어 "나만의 AI 비서"로도 쓰인다. OMC급 명령(ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview 등)은 코어에 포팅해 기본 내장하고, Claude Code 플러그인/SKILL.md 포맷을 그대로 설치할 수 있다. 기본 실행 모드는 accept.

## Constraints
- **언어/스택**: 코어·CLI 백엔드 Python (uv). TUI Node/Ink (React). IDE Electron + Vue, 다크 테마. 클라이언트 SDK TypeScript (TUI·IDE 공유).
- **기반**: hermes-agent 코드를 vendoring(복사 후 수정). OMC 스킬/에이전트 정의는 Claude Code 전용 의존(executor 등 에이전트, state/notepad MCP 툴)을 snowpea 코어 기능으로 포팅. 둘 다 MIT → 고지 유지.
- **아키텍처**: 코어 = 상주 데몬(스케줄러·게이트웨이 필요). CLI 실행 시 코어를 자동 기동/재사용, IDE는 내장 코어를 스폰. 프로토콜은 Codex app-server와 유사한 JSON-RPC 2.0 over WebSocket, 토큰 인증.
- **LLM 벤더 (v0.1)**: Anthropic(API 키만, OAuth 없음), OpenAI, OpenAI 호환 로컬(vLLM/Ollama/LM Studio, base_url), OpenRouter, Google Gemini, xAI, GLM(Zhipu), MiniMax, Kimi(Moonshot), DeepSeek, Qwen. 구조: Anthropic·Gemini 네이티브 어댑터 + OpenAI 호환 어댑터 + 벤더별 프리셋. Hermes식 웹 토큰 로그인은 지원 벤더에 한해 제공.
- **웹검색**: 기본값은 무료 제공자 체인(DuckDuckGo → Brave 무료 티어/SearXNG). API 키 없이 활성화되어야 하며, 유료 제공자(Tavily/Exa 등)는 선택 설정. (2026-09-11 사용자 확정)
- **모드**: plan / accept / auto 3개. 기본 accept. accept = Claude Code acceptEdits 의미(읽기·쓰기·편집 자동 승인, 셸·git push·스케줄 등록·메신저 발송·외부 API는 승인 프롬프트, allowlist로 명령별 예외). 모드별 런치 명령(`snowpea --mode <m>`, TUI `/plan` `/accept` `/auto`) 제공. 프로젝트 설정 파일에 기본 모드를 지정·기억.
- **무인 승인**: 스케줄·영속 에이전트가 승인 필요 작업을 만나면 요청을 해당 채널(메신저/IDE 알림)로 보내 대기, 타임아웃 시 거부. 승인 큐는 코어가 소유하며 TUI에서도 목록 확인·응답 가능. 스케줄 작업은 등록 시 모드 지정.
- **멀티 에이전트**: (1) 일회성 서브에이전트 위임(동시 N), (2) 팀 모드(공유 태스크 리스트, 에이전트 간 메시지, worktree 분리), (3) 영속 이름 에이전트(각자 세션·메모리·채널·스케줄을 가진 상시 인스턴스, 데몬이 관리).
- **실행 환경**: 로컬(기본), Docker 컨테이너 샌드박스, SSH 원격 호스트. 백엔드 선택식.
- **확장 포맷**: Claude Code 플러그인 포맷(plugin.json, skills/SKILL.md, agents/*.md, commands, hooks, .mcp.json) + agentskills.io 표준. opencode TS 플러그인 API는 비지원.
- **설치**: `curl | sh` 스크립트가 uv+Node 처리, `snowpea` 명령 등록. brew/npm 보조. IDE는 dmg/exe/AppImage.
- **레포/라이선스**: 코어+CLI(+TS SDK) = MIT 공개 레포. IDE, 레지스트리 서버, 홈페이지 = 각각 **비공개 별도 레포**. 복사한 Hermes/OMC 코드는 어느 레포든 MIT 고지.
- **릴리즈 단계**: v0.1 코어+CLI(코어 기능 전체 포함) → v0.2 IDE + 스킬 마켓 GUI → v0.3+ 랜딩/레지스트리 공개.
- **플랫폼**: mac / windows / linux 모두 CLI·IDE 지원.

## Non-Goals
- 풀 코드 편집기(Monaco 중심 IDE). IDE는 에이전트 조종 앱이다.
- opencode TS 코드 플러그인 실행.
- Anthropic 구독 OAuth 로그인.
- 단일 바이너리 번들 배포.
- 게이트웨이·메모리·스케줄러를 v0.2로 미루는 것 (모두 v0.1).
- 원격 다중 머신 코어 접속(한 IDE에서 여러 PC의 데몬 조종).
- 클라우드 샌드박스(Modal/Daytona/E2B).

## Acceptance Criteria
### v0.1 (core + cli)
- [ ] `curl -fsSL <url> | sh` 실행 후 3개 OS에서 `snowpea` 명령이 동작하고 uv Python 환경과 Node TUI가 설치된다.
- [ ] `snowpea setup`이 Quick/Full/Blank 흐름으로 위 벤더 중 최소 1개를 설정하고, 지원 벤더의 웹 토큰 로그인이 동작한다.
- [ ] `snowpea`로 TUI가 뜨고 코어 데몬이 자동 기동/재사용되며 `/help`에 내장 명령(ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview, plan/auto/accept 전환)이 나열된다.
- [ ] accept 모드에서 `/ralph <task>`가 실제 git 레포를 수정하고 완료 판정까지 자동 진행한다. 서브에이전트가 동시 실행되고 진행이 TUI에 표시된다.
- [ ] plan 모드에서는 파일 쓰기가 차단되고, auto 모드에서는 승인 프롬프트 없이 진행된다.
- [ ] Hermes급 툴셋(파일/셸/git/검색/브라우저/delegate_task/스케줄/메모리/웹검색/이미지·TTS 등)이 등록되어 있고 MCP 서버(.mcp.json)를 붙여 툴이 노출된다.
- [ ] 장기 메모리: 새 세션에서 이전 세션 내용을 검색·인용하고 사용자 프로필이 유지된다.
- [ ] 스케줄러: cron 또는 자연어로 예약한 작업이 데몬에 의해 실행되고 결과가 지정 채널로 전달된다.
- [ ] 메신저 게이트웨이: Telegram·Discord·Slack 중 최소 1개에서 같은 에이전트와 대화된다.
- [ ] Claude Code 플러그인(예: oh-my-claudecode 마켓플레이스)을 설치하면 스킬·에이전트·명령·hooks·MCP가 로드되어 실행된다.
- [ ] `/agent create "<설명>"` → agents/*.md 생성 후 즉시 서브에이전트로 호출 가능. `/skill learn` → 대화에서 SKILL.md 추출.
- [ ] `snowpea skill search <q>`가 Claude Code 마켓플레이스·agentskills.io·Hermes Hub를 통합 검색하고 설치한다.
- [ ] 코어 서버의 JSON-RPC 프로토콜이 문서화되고 TS SDK로 세션 생성·스트리밍·승인 요청·서브에이전트 이벤트를 수신할 수 있다.
- [ ] `snowpea --mode plan|accept|auto`와 TUI `/plan` `/accept` `/auto`로 모드 전환. 프로젝트 설정에 기본 모드를 저장하면 다음 실행에 적용된다.
- [ ] accept 모드에서 파일 편집은 프롬프트 없이, 셸 명령은 프롬프트 후 실행. allowlist에 등록한 명령은 프롬프트 없이 실행.
- [ ] 팀 모드: `/team N <task>`로 N개 에이전트가 공유 태스크 리스트를 클레임해 worktree에서 병렬 작업하고 결과가 병합된다.
- [ ] 영속 에이전트: 이름 있는 에이전트 2개를 각각 다른 Telegram 채널·스케줄에 바인딩하면 데몬 재시작 후에도 유지되고 서로 메모리가 분리된다.
- [ ] 무인 승인: 스케줄 작업이 셸 명령을 요구하면 Telegram으로 승인 요청이 오고, 같은 요청이 TUI 승인 큐에도 보이며 어느 쪽에서 답해도 진행된다. 타임아웃 시 거부.
- [ ] 실행 백엔드를 local/docker/ssh 중 선택하면 동일 툴셋이 해당 환경에서 동작한다.

### v0.2 (ide + market GUI)
- [ ] mac/win/linux 인스톨러로 설치하면 내장 코어가 자동 기동하고 setup 마법사가 IDE 안에서 동작한다.
- [ ] 쓰레드 채팅에서 내장 명령 실행, diff 패널에서 파일별 승인/거부, 병렬 서브에이전트 트리 표시, 통합 터미널, 파일트리 뷰어, worktree 병렬 세션.
- [ ] 같은 세션을 TUI에서 이어서 열 수 있다.
- [ ] 스킬 마켓 브라우저, 스케줄 관리, 메신저 게이트웨이 설정, 메모리 브라우저 화면이 동작한다.

### v0.3+ (site-docs + registry)
- [ ] snowpea.ai 랜딩이 "오픈소스 멀티벤더 코딩 에이전트"와 "나만의 AI 비서" 두 메시지를 담고, 설치 명령·매뉴얼·다운로드를 제공한다.
- [ ] 자체 레지스트리에 스킬 업로드·별점·큐레이션이 가능하고 TUI/IDE 검색에 포함된다.

## Assumptions Exposed & Resolved
| Assumption | Challenge | Resolution |
|---|---|---|
| 세 참고 프로젝트 중 하나를 포크 | R1: 기반/언어 | Hermes 코드를 복사·수정, Python |
| "플러그인 호환" = 3개 도구 전부 | R2 | Claude Code 플러그인 포맷 + SKILL.md만 |
| "Codex 수준 IDE" = 코드 편집기 | R3 | 에이전트 조종 앱, 편집기 아님 |
| CLI는 IDE의 보조 | R4 Contrarian | CLI가 1차 표면, 풀 TUI 필수 |
| TUI를 Python으로 | R5 | 참고 TUI 전부 Node 계열 → Ink 채택, 코어는 WS JSON-RPC 서버 |
| OMC를 "설치" | R6 Simplifier | 내장(포팅), 외부 플러그인은 별도 설치 |
| 벤더 2~3개면 충분 | R7 | 11개 벤더 + 웹 토큰 로그인 |
| 코딩 에이전트만 | R8 Ontologist | 코딩이 본질이나 비서 기능도 코어 |
| 비서 기능은 v0.2 | R9 | 전부 v0.1 |
| 스킬 마켓 = 외부 검색 | R10 | 집합 검색 + 자체 레지스트리 |
| 전체 오픈소스 | R11 + 중간 메모 | 코어+CLI만 MIT, IDE/레지스트리/홈페이지 비공개 별도 레포 |

## Technical Context (research)
- **Hermes Agent** (MIT): Python 코어, Ink TUI 인프로세스, Desktop = 웹 대시보드가 TUI 자식 프로세스를 WS로 연결, gateway = 메신저 20종 브릿지, `delegate_task` 서브에이전트(동시 3), cron/자연어 스케줄러, agentskills.io 스킬, SQLite FTS 메모리, `hermes setup` Quick/Full/Blank.
- **opencode** (MIT): TS 코어 `opencode serve` HTTP+SSE, TUI/데스크톱/웹이 모두 클라이언트, 데스크톱 Tauri→Electron 이전.
- **Codex 앱**: Electron, Rust app-server JSON-RPC 2.0 over WS + 토큰 인증, 쓰레드/diff/worktree/터미널.
- **Claude Code**: React/Ink TUI, 모드 default/acceptEdits/plan/bypass, 플러그인 = marketplace.json + skills/agents/commands/hooks/.mcp.json.
- **snowpea 자산**: 기존 snowpea-studio MCP(이미지/영상/음악/TTS)를 미디어 생성 툴로 바로 연결 가능.

## Recommended repo layout (for planning)
| Repo | Visibility | Contents |
|---|---|---|
| `snowpea-agent` | public, MIT | `core/` (Python: server, providers, tools, agents, memory, scheduler, gateway, skills loader, built-in commands), `tui/` (Node/Ink), `sdk/` (TS client), `installer/` (`install.sh`), `docs/` (manual source) |
| `snowpea-ide` | private | Electron+Vue app, embeds core build, uses `sdk/` |
| `snowpea-registry` | private | registry API + web market |
| `snowpea-site` | private | landing/marketing |

## Ontology (Key Entities)
| Entity | Type | Fields | Relationships |
|---|---|---|---|
| SnowpeaCore (AppServer) | core | protocol, port, token, config | hosts Sessions, Agents, Tools, Scheduler, Gateway, Memory |
| Session/Thread | core | id, mode, workdir, history | has Agents; shared by TUI & IDE |
| Agent / SubAgent | core | name, model, tools, prompt | Agent delegates SubAgents |
| AgentDefinition (agents/*.md) | supporting | frontmatter, prompt | generated by /agent create |
| LLMProvider / ProviderPreset / AuthMethod | core | vendor, base_url, key or token | Agent uses Provider |
| Tool / MCPServer | core | name, schema | Agent calls Tool |
| Skill (SKILL.md) / Plugin / Marketplace / SkillRegistry / RegistryListing | supporting | manifest, source, rating | Plugin bundles Skills, Agents, Commands, Hooks |
| Command / ExecutionMode | core | name (ralph…), mode (auto/plan/accept) | Command runs in Session |
| LongTermMemory / MemoryEntry | core | text, fts index, user profile | Session reads/writes |
| Scheduler / ScheduledJob | core | cron/NL, skill, channel | Job runs Agent, delivers via Gateway |
| MessagingGateway / Channel | core | platform, credentials | routes to Session |
| SearchProvider / MediaGeneration | supporting | provider, api | exposed as Tools |
| CLI(TUI) / IDE / ClientSDK | surface | — | clients of SnowpeaCore |
| Installer / SetupWizard | supporting | os, runtimes | produces config |
| Repository / ReleasePhase | meta | visibility, license, version | — |

## Ontology Convergence
| Round | Count | New | Changed | Stable | Stability |
|---|---|---|---|---|---|
| 1 | 13 | 13 | - | - | - |
| 2 | 17 | 4 | 0 | 13 | 76% |
| 3 | 20 | 3 | 0 | 17 | 85% |
| 4 | 20 | 0 | 0 | 20 | 100% |
| 5 | 22 | 2 | 0 | 20 | 91% |
| 6 | 23 | 1 | 0 | 22 | 96% |
| 7 | 25 | 2 | 0 | 23 | 92% |
| 8 | 28 | 3 | 0 | 25 | 89% |
| 9 | 29 | 1 | 0 | 28 | 97% |
| 10 | 31 | 2 | 0 | 29 | 94% |
| 11 | 32 | 1 | 0 | 31 | 97% |
| 12 | 33 | 1 | 0 | 32 | 97% |
| 13 | 33 | 0 | 0 | 33 | 100% |
| 14 | 35 | 2 | 0 | 33 | 94% |
| 15 | 36 | 1 | 0 | 35 | 97% |
| 16 | 37 | 1 | 0 | 36 | 97% |
| 17 | 38 | 1 | 0 | 37 | 97% |

추가 엔티티(R14–R17): Team/SharedTaskList, NamedAgentInstance, ProjectSettings, ApprovalQueue, ExecutionBackend(local/docker/ssh).

## Interview Transcript
<details><summary>Q&A (17 rounds)</summary>

- R0 토폴로지 6개 확인 → "맞습니다 (6개 모두 활성)".
- R1 코어 기반/언어 → 코드를 참조·복사해 새로 개발, Python, Hermes 포크 수준이되 카피해서 수정, 멀티 에이전트 관리 등 추가. (79%)
- R2 플러그인 호환 범위 → SKILL.md + Claude Code 플러그인 포맷. (73%)
- R3 IDE 정체 → 에이전트 조종 앱(Codex 앱 방식). (68%)
- R4 [Contrarian] CLI vs IDE → CLI 1차 표면, 풀 TUI 필수; Hermes/opencode 데스크톱 동작 확인 요청. (64%)
- R5 연결 구조 → (질문: opencode/Claude Code TUI 스택?) → Python 코어 = WS JSON-RPC 서버, TUI Node/Ink. (58%)
- R6 [Simplifier] v0.1 → (질문: OMC는 내장 아닌가?) → 코어+CLI, setup→TUI→/ralph 레포 수정 완료. (45%)
- R7 벤더 → Anthropic(API키만), OpenAI, 로컬 OpenAI 호환, OpenRouter, Gemini, xAI, GLM, MiniMax, Kimi, DeepSeek, Qwen, 웹 토큰 로그인. (40%)
- R8 [Ontologist] 정체성 → 코딩이 본질, 게이트웨이·메모리·스케줄·웹검색·미디어 생성도 코어, 랜딩은 AI 비서도. (38%)
- R9 v0.1 코어 범위 → 메모리·스케줄러·게이트웨이·웹검색(무료 제공자)·미디어 생성 전부 포함. (34%)
- R10 스킬 마켓 → 집합 검색 + snowpea.ai 자체 레지스트리. (30%)
- R11 설치/라이선스 → `curl | sh`(uv+Node) + brew/npm, MIT. (25%)
- R12 에이전트 생성 자동화 → NL→agents/*.md 즉시 사용 + 스킬 자동 추출. 중간 메모: IDE/레지스트리/홈페이지 비공개 별도 레포. (20%)
- R13 IDE 완료 기준 → 3 OS 인스톨러, 코어 내장, 쓰레드/diff/서브에이전트 + 마켓·스케줄·게이트웨이 화면. (17%)
- (사용자: 인터뷰 더 진행)
- R14 멀티 에이전트 관리 → 서브에이전트 위임 + 팀 모드 + 영속 이름 에이전트. 원격 다중 머신은 제외. (15%)
- R15 accept 모드 → Claude Code acceptEdits와 동일, 모드는 plan/accept/auto 3개. 메모: 모드별 런치 명령 + 프로젝트 단위 기본 모드 기억. (13%)
- R16 무인 승인 → 채널로 승인 요청·대기·타임아웃 거부, CLI에서도 승인 가능. (13%)
- R17 실행 환경 → 로컬, Docker, SSH. (12%)
</details>

## Verification (how the spec will be used)
1. 승인 후 이 문서를 `.omc/specs/deep-interview-snowpea-agent.md`로 복사.
2. 사용자가 선택한 브릿지(아래)로 넘긴다. 권장: `omc-plan --consensus --direct`로 Planner/Architect/Critic 합의 플랜(`.omc/plans/`)을 만들고 실행 승인은 별도.
3. 실행 단계의 검증은 Acceptance Criteria v0.1 체크리스트를 3개 OS에서 순서대로 통과시키는 것.


# 명령

명령 표면은 두 가지입니다. 슬래시 명령은 세션 안에서 돌고 코어가 소유하므로, 같은 `/ralph`가 TUI에서도, `snowpea -c`에서도, 예약 잡에서도, Telegram 메시지에서도 똑같이 동작합니다. CLI 서브커맨드는 세션을 열지 않고 데몬을 조회하고 설정합니다.

```bash
snowpea commands list
snowpea commands list --json
```

이 명령은 설치된 플러그인이 추가한 명령까지 포함해 현재 등록된 목록을 그대로 출력합니다. UI 안의 `/help`도 같은 내용을 보여줍니다.

## 슬래시 명령

### 세션과 모드

| 명령 | 하는 일 |
|---|---|
| `/help` | 사용 가능한 모든 명령을 나열 |
| `/tools` | 등록된 툴을 카테고리·권한·상태와 함께 나열 |
| `/compact [지시]` | 지금까지의 대화를 요약해 그 요약으로 이어서 진행 |
| `/plan`, `/accept`, `/auto` | 모드 전환 |
| `/mode [plan\|accept\|auto\|save\|show]` | 프로젝트 기본값을 보거나, 바꾸거나, 저장 |
| `/busy [steer\|queue]` | 바쁜 턴의 후속 프롬프트 처리 방식을 보거나 설정. 기본 `steer`는 대기열 프롬프트를 실행 중 턴의 다음 모델 호출 전에 합치고, `queue`는 예전처럼 별도 턴으로 나중에 실행 |
| `/memory [list\|search <q>\|forget <id>] [--project\|--global\|--all]` | 장기 기억을 보고, 찾고, 지웁니다. 범위 기본값은 `--all`이고, 에이전트가 실제로 회상하는 범위와 같습니다 |
| `/approvals` | 답을 기다리는 무인 승인 요청을 나열 |
| `/allow <regex> [--global]` | 반복되는 질문을 조용한 허용으로 승격 |
| `/allowlist [remove <id>]` | allowlist를 보거나 정리 |
| `/backend [local\|docker\|ssh] [json]` | 툴이 실행되는 곳을 보거나 바꿈 |
| `/delegate <에이전트> <task>` | 태스크 하나를 지정한 에이전트에게 위임 — 평범한 턴 안에서 `delegate_task`를 직접 호출하고, 기다린 뒤 결과를 답으로 알려줌 |
| `$<에이전트> <task>` | `/delegate <에이전트> <task>`의 축약형. 데몬이 이 접두사를 직접 해석하므로 터미널 UI뿐 아니라 어떤 클라이언트에서도 동일하게 동작 |

### 터미널 UI

아래 명령은 코어가 아니라 터미널 UI가 직접 처리합니다. 그래서 `snowpea commands list` 에는 나오지 않고, 사람이 앞에 앉아 있는 세션에서만 동작합니다. 화면에서 무슨 일이 일어나는지는 [터미널 UI](tui.md)에 있습니다.

| 명령 | 하는 일 |
|---|---|
| `/resume` | 이 디렉터리에서 마지막으로 쓰던 세션을 다시 열고 재생 |
| `/model`, `/models` | 설정된 모든 벤더가 지원하는 모델 전체를 나열하고 선택. `/model <vendor:model>` 은 이 세션에 고정(저장되어 재시작 후에도 유지), `/model inherit` 은 고정 해제, `/model default <id>` 는 `models.default` 설정 |
| `/effort` | 모델이 얼마나 오래 생각할지와 그것을 정한 규칙을 보여 줌. `/effort low\|medium\|high\|max` 는 이 세션에 고정(저장됨), `/effort auto` 는 고정 해제. [추론 강도](setup.md#추론-강도effort) 참고 |
| `/attach <경로>` | 다음 프롬프트에 파일을 첨부 |
| `/voice` | 음성 입력을 켬. 이후 `Ctrl+Space` 로 녹음 |
| `/rec` | 녹음 시작·중지, `Ctrl+Space` 와 같음 |
| `/tts on\|off` | 답변이 끝날 때마다 읽어 줌 |
| `/update` | 제안된 업그레이드를 받음, `U` 와 같음 |

### 작업

| 명령 | 하는 일 |
|---|---|
| `/ralph <task>` | PRD 루프: 수용 기준이 붙은 스토리를 쓰고, 구현하고, 검증하고, APPROVE가 나올 때까지 리뷰 |
| `/ultrawork <task>` | 독립적인 조각으로 쪼개 동시 서브에이전트에 돌리고 보고서를 합침 |
| `/review [what]` | 커밋되지 않은 변경을 읽기 전용 `reviewer` 에이전트로 리뷰하고 판정을 전달 |
| `/setup [providers\|search\|browser\|audio\|all]` | 설정 마법사 화면을 이 자리에서 다시 실행. 키는 가려진 채 입력 |
| `/login <vendor> [method]` | 벤더 로그인 — 브라우저 흐름, 디바이스 흐름, 또는 가려진 키 입력 |
| `/init [--force]` | 프로젝트 루트에 빠르고 거친 `AGENTS.md`를 한 턴에 작성; 기존 파일이 있으면 `--force` 없이는 병합 |
| `/deepinit [path]` | 탐색 후 계층적 `AGENTS.md` 작성 (디렉터리별 map→write, 이어서 루트) |
| `/team "<task>"` | 활성 팀 구성원이 역할별로 처리 — 계획·구현·테스트·리뷰 |
| `/team <name> "<task>"` | 같은 파이프라인을 지정한 프로젝트/전역 팀으로 이번 한 번만 |
| `/team <N> "<task>"` | `/workers <N> "<task>"` 호환 표기. 응답에서 현재 표기는 `/workers N`이라고 안내 |
| `/workers <N> "<task>"` | 동일한 작업자 N명에게 각각 git worktree를 주고 태스크가 끝나는 대로 브랜치를 병합 |
| `/team create <name> <agent...>` | 기존 에이전트로 프로젝트 팀을 만들고 즉시 활성화 |
| `/team use <name>` / `/team use none` | 프로젝트의 활성 팀 전환 또는 해제 |
| `/team list` | 모든 팀과 출처, 각 구성원이 맡는 단계 표시 |
| `/deep-interview <idea>` | 모호함을 점수화해 스펙이 확정될 때까지 넘기지 않는 소크라테스식 인터뷰 |
| `/deep-research <topic>` | 서브에이전트에 걸쳐 흩어진 다중 출처 웹 리서치, 출처와 함께 답변 |
| `/ralplan <task>` | 합의 기반 계획 — 코드를 쓰기 전에 planner, architect, critic이 논쟁 |

`/ralph`는 자신의 상태를 `<project>/.snowpea/ralph/`에 `prd.json`과 `progress.md`로 남기므로 지금 무엇을 하고 있다고 생각하는지 읽을 수 있고, 수렴하지 못하면 `ralph.max_iterations`(10)에서 멈춥니다. `/ultrawork`와 `/deepinit`은 `agents.max_concurrent`(기본 3) 안에서 흩어집니다. 마지막 세 개는 `core/snowpea_core/builtin_skills/` 아래의 `SKILL.md` 파일이고, 여러분의 스킬을 읽는 것과 같은 로더로 읽힙니다 — 읽고, 복사하고, 고치세요.

`/init`은 `/deepinit`의 빠른 버전입니다: 서브에이전트 없이 메인 에이전트 턴 하나, 도구 호출도 몇 번 정도로 — Claude Code 자체의 `/init`과 같은 정신입니다. 프로젝트에 설정 파일이 아직 없으면 `<project>/.snowpea/settings.json`을 `defaultMode: "accept"`로 만들지만, 이미 있으면 손대지 않습니다. plan 모드에서는 무엇을 쓸지 보고만 합니다.

### 생성기

| 명령 | 하는 일 |
|---|---|
| `/agent create "<description>"` | `<project>/.snowpea/agents/<name>.md`에 에이전트 정의를 작성 |
| `/agent list` | 에이전트 정의를 나열 |
| `/skill create <name> "<할 일>" [--global] [--force]` | 브리프로부터 SKILL.md를 생성해 `<project>/.snowpea/skills/<name>/`에 작성 (`--global`이면 `$SNOWPEA_HOME/skills/<name>/`); 이미 있으면 `--force` 없이는 덮어쓰지 않음 |
| `/skill learn [name]` | 방금 끝낸 세션을 `<project>/.snowpea/skills/<name>/SKILL.md`로 바꿈 |
| `/skill reload` | `SKILL.md`를 손으로 고친 뒤 스킬·에이전트·명령·플러그인 MCP 서버를 다시 읽음(설치나 `/skill create`는 스스로 재적재) |
| `/skill list` | 설치된 것을 종류·출처별로 표시 |

생성된 에이전트는 재적재 없이 곧바로 `delegate_task` 대상이 됩니다.

### MCP 서버

| 명령 | 하는 일 |
|---|---|
| `/mcp` 또는 `/mcp list` | 설정된 서버 표: 이름·스코프·전송·상태·툴 수 |
| `/mcp get <name>` | 서버 하나를 툴까지 전부; `env`와 `headers`는 키 이름만 보임 |
| `/mcp add <name> -- <command> [args…]` | stdio 서버를 `<project>/.mcp.json`에 추가 (`--global`이면 `$SNOWPEA_HOME/.mcp.json`) |
| `/mcp add <name> --url <https://…>` | 원격 서버를 추가; 자격 증명은 `--header K=V` |
| `/mcp add-json <name> '<json>'` | `.mcp.json` 항목을 그대로 추가 |
| `/mcp test <name>` | 띄워서 툴 목록을 받아보고, 실패하면 그 에러를 보고 |
| `/mcp configure <name> [--tools a,b] [tool…]` | 이 툴들만 등록; 이름이 없으면 전부 |
| `/mcp enable\|disable <name>` | 항목은 두되 더는 띄우지 않기, 그리고 되돌리기 |
| `/mcp remove <name>` | 항목을 지우고 서버를 멈춤 |
| `/mcp reload [name]` | 서버 하나를, 또는 모든 선언을 다시 읽어 재시작 |
| `/mcp catalog` | `--preset`이 받는 큐레이션 목록 |

`--env K=V`와 `--header K=V`는 여러 번 쓸 수 있습니다. 서버가 `tools/list`에 한 번 답하기 전에는 아무것도 쓰지 않고(`--no-test`로 건너뜀), 명령줄은 언제나 argv이지 셸 문자열이 아니며, 추가·삭제·수정은 데몬 재시작 없이 반영됩니다. 플러그인이나 설정의 `mcp.servers`가 선언한 서버는 목록에는 나오지만 읽기 전용입니다.

### 스케줄링

| 명령 | 하는 일 |
|---|---|
| `/schedule "<spec>" "<task>" [--channel X] [--mode M]` | 잡을 등록 |
| `/schedule` | 잡을 나열하거나, 하나를 취소 |

## CLI 서브커맨드

### 조회

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea agents --json
snowpea daemon status --json
```

`tools list`와 `commands list`는 각각 RPC 메서드 하나를 호출하고 끝납니다. 세션을 만들지도 모델을 부르지도 않으므로, 설치 직후나 CI에서 쓰기 좋은 스모크 테스트입니다. `tools list`는 뒷단 제공자가 있는 도구에는 그 제공자도 함께 출력하므로, `web_search`에서는 실제로 응답할 검색 제공자를 볼 수 있습니다.

### 프로젝트 초기화

```bash
snowpea init
snowpea init --force
```

현재 디렉터리(또는 실행한 위치)에 세션을 열고 `/init`을 — 위 [작업](#작업) 참고 — `snowpea -c`와 같은 경로로 한 번의 헤드리스 턴으로 돌립니다. 종료 코드는 [헤드리스](headless.md)를 참고하세요.

### 컨텍스트

```bash
snowpea session context --json
snowpea session compact s-abc123 "API 설계 결정은 남겨줘"
```

`session context` 는 살아 있는 세션마다 한 줄씩, 사용 중인 토큰과 모델의 컨텍스트 윈도우, 그 비율을 출력합니다. 알아낼 수 없는 윈도우는 추측하지 않고 `?` 로 표시합니다. 호스팅 벤더는 내장 표에서 찾고, 로컬 vLLM·Ollama 서버에는 한 번만 물어본 뒤 캐시하며, `settings.json` 의 `providers.<vendor>.context_window` 가 둘 다 덮어씁니다.

긴 세션을 그 윈도우 안에 유지하는 수단이 compaction 입니다. `/compact` 는 지금까지의 내용을 "Session summary" 시스템 메시지 하나로 요약하고, 마지막 몇 개 메시지는 그대로 남긴 뒤 이어서 진행합니다. `session compact` 는 셸에서 같은 일을 합니다. 한 턴이 윈도우의 `context.autoCompactPercent`(기본 85)를 넘길 것 같으면 자동으로도 실행되며, 툴 루프 중간이 아니라 항상 턴과 턴 사이에 일어납니다. `context.autoCompact` 를 `false` 로 두면 `/compact` 로만 하게 됩니다.

### 세션

```bash
snowpea session list
snowpea session list --include-closed --workdir ~/src/api --json
snowpea session delete s-abc123
snowpea session clear --all
```

`session list`는 살아 있는 세션을, `--include-closed`를 주면 저장된 세션까지 보여줍니다 — id, 모드, 생성 시각, 작업 디렉터리, 마지막 프롬프트. `session delete`와 `session clear`는 저장된 세션을 첨부·음성 파일까지 함께 지웁니다. 살아 있는 세션은 절대 지워지지 않으니 먼저 닫아 주세요. `session list`에서 고른 id는 `snowpea -c "…" --resume <id>`로 이어갈 수 있습니다.

### 체크포인트

Snowpea는 클라이언트가 "이 턴 이전으로 복원"을 제공할 수 있도록 턴별 체크포인트를 기록합니다. 세션 작업 디렉터리 안의 일반 파일에 대한 툴 쓰기는 한 턴에서 첫 번째 쓰기 전에 캡처되고, 셸 쪽 변경은 작업 디렉터리가 git 저장소일 때만 감지됩니다. 작업 디렉터리 밖의 파일, `.git/`, `node_modules/`, `.snowpea/`, `__pycache__/`, `dist/`, `out/`, 5 MB보다 큰 파일은 추적하지 않습니다. 저장된 세션을 삭제하면 그 체크포인트도 함께 삭제합니다.

### 모델 프로필

```bash
snowpea model profiles --json
snowpea model default fast
snowpea model assign executor deep
snowpea model assign executor daily --project
snowpea model assign executor
```

`model profiles`는 라우팅이 실제로 보는 그대로를 출력합니다 — 프로젝트 `models` 블록을 전역 위에 덮은 결과, 각 줄에 `[global]`/`[project]` 표시, 실제 적용되는 기본값에 `*` — 그리고 에이전트별 지정도 같이 나옵니다. `model assign`은 에이전트 하나를 프로필에 연결하고 `model default`는 기본값을 정합니다. 둘 다 `--project`를 주면 `<workdir>/.snowpea/settings.json`에 쓰고, 프로필 id를 빼면 해당 설정을 지웁니다. 없는 프로필 id는 데몬이 거부합니다.

이 설정들이 들어가는 5단계 우선순위는 [설정](setup.md)을 보세요.

### 검색

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
```

`search test`는 설정된 제공자로 실제 질의를 한 번 보내고, 어떤 제공자가 응답했는지와 건너뛴 제공자마다의 이유(API 키 없음, 인스턴스 URL 미설정, HTTP 오류)를 출력합니다. 데몬이 없어도 됩니다. `$SNOWPEA_HOME/settings.json`을 직접 읽습니다. 하나라도 응답하면 종료 코드 0, 아무도 응답하지 못하면 2입니다.

### 데몬

```bash
snowpea daemon status
snowpea daemon start
snowpea daemon stop
```

`status`는 포트, pid, 가동 시간, 네 가지 keepalive 카운터(세션, 잡, 게이트웨이 바인딩, 이름 있는 에이전트), 그리고 데몬이 종료하려는지와 종료하지 않는다면 그 이유를 출력합니다.

### 벤더

```bash
snowpea provider list
snowpea provider login openai
snowpea setup --vendor deepseek --key sk-...
```

OpenAI 호환 서버는 각각 이름을 달아 여러 대를 함께 등록할 수 있습니다.
`add-local`이 하나를 추가하고, `remove`는 그 블록과 그 서버를 가리키던 모델
프로필까지 지웁니다.

```bash
snowpea provider add-local hon2 --url http://hon2.example.com:8000/v1 --type vllm
snowpea provider models hon2
snowpea provider remove hon2
```

설정 파일 모양과 마법사 흐름은 [로컬 서버 여러 대](setup.md#로컬-서버-여러-대)를 보세요.

### 스킬과 플러그인

```bash
snowpea skill list
snowpea skill search "pdf"
snowpea skill install oh-my-claudecode
snowpea skill install ./my-plugin
snowpea skill remove my-plugin
```

### MCP 서버

```bash
snowpea mcp list
snowpea mcp add notes -- python -m my_notes_server
snowpea mcp add remote --url https://example.internal/mcp --header Authorization=Bearer-xxx
snowpea mcp add github --preset github --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
snowpea mcp get notes --json
snowpea mcp test notes
snowpea mcp configure notes --tools search,fetch
snowpea mcp disable notes
snowpea mcp remove notes
snowpea mcp reload
snowpea mcp catalog
```

`--scope global`(줄여서 `--global`)은 프로젝트 파일 대신 `$SNOWPEA_HOME/.mcp.json`에 씁니다. 바로 그 `--` 뒤는 자식 프로세스의 argv 그대로이고 파싱되지 않으므로, 플래그는 `--` 앞뒤 어디에 와도 됩니다. 모든 서브커맨드가 `--json`을 받습니다. 파일 형식은 [플러그인](plugins.md#mcp-서버)을 보세요.

### 잡

```bash
snowpea job schedule --at "0 9 * * *" --task "어제 커밋 요약" --channel telegram:123456
snowpea job schedule --in 10m --task "빌드 확인" --mode plan
snowpea job list --json
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`--at`, `--in`, `--every`, `--cron`, `--spec`은 같은 옵션의 다섯 가지 이름입니다. 쓰려는 스케줄에 가장 잘 읽히는 것을 고르세요.

### 게이트웨이

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

### 음성 엔진

```bash
snowpea audio install faster-whisper
snowpea audio install piper
snowpea audio install edge-tts
```

데몬이 도는 기계에 로컬 음성 엔진을 설치합니다. `uv tool install`, `pipx`, `pip install --user` 중 있는 것을 쓰고 로그를 그대로 보여줍니다. `piper`는 기본 음성 하나도 함께 받습니다. 시스템 패키지(`espeak-ng`, `say`, `powershell`)는 대신 설치하지 않습니다. 종료 코드 `2`와 함께 직접 실행할 명령을 알려줍니다. [설정](setup.md#음성-입력과-출력)을 보세요.

### 팀과 서비스

```bash
snowpea workers status
snowpea team status
snowpea team list
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery
snowpea service install
snowpea service status
snowpea service uninstall
```

`workers status`(예전 이름 `team status`)는 돌고 있는 팀의 태스크별 상태와 재시도 횟수를 보여줍니다. `team list`·`create`·`use`·`delete`는 재사용 가능한 에이전트 팀 쪽입니다. `/team create`가 쓰는 것과 같은 `<workdir>/.snowpea/settings.json`의 프로젝트 팀을 전역 팀과 함께 보여주고, 활성 팀에 표시를 붙입니다. `team delete`는 프로젝트 팀만 지웁니다. `service`는 데몬을 로그인 시 자동 시작하도록 등록합니다 — Linux에서는 systemd 사용자 유닛, macOS에서는 launchd 에이전트, Windows에서는 예약 작업입니다. 기본값은 꺼짐이고, 스케줄과 게이트웨이가 터미널 로그인 없이도 재부팅을 넘겨 살아남아야 할 때만 필요합니다.

### 전역 옵션

| 옵션 | 의미 |
|---|---|
| `--version` | 버전을 출력하고 종료 |
| `--home DIR` | 이번 실행에 한해 `SNOWPEA_HOME`을 덮어씀 |
| `--mode plan\|accept\|auto` | 시작하는 세션의 모드 |
| `-c`, `--prompt TEXT` | 헤드리스로 한 턴만 돌고 종료 |
| `--json` | 산문 대신 JSON Lines 출력 |
| `--cwd DIR` | 세션의 작업 디렉터리 |
| `--timeout SEC` | SEC초 뒤 턴을 중단 |
| `--provider VENDOR` | 이번 세션의 벤더 |
| `--resume SESSION_ID` | 새 세션 대신 저장된 세션을 이어서 진행 |
| `--approve-none` | 묻는 대신 모든 승인을 거부 |

## 슬래시 명령을 헤드리스로 돌리기

레지스트리가 코어 안에 있으므로 슬래시 명령은 그 자체로 유효한 헤드리스 프롬프트입니다.

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit" --json
snowpea -c "/init --force" --json
```

CLI는 이를 파싱하지 않습니다. 텍스트를 그대로 코어에 넘기고, 코어는 TUI가 하는 것과 똑같이 처리합니다.

## 다음

[플러그인](plugins.md) — 나만의 명령을 추가하기.

# snowpea 매뉴얼

snowpea는 내 컴퓨터에서 도는 오픈소스 멀티벤더 코딩 에이전트입니다. Python 코어가 로컬 데몬으로 상주하면서 세션·툴·권한·메모리·스케줄·메신저 바인딩 등 상태를 가진 것은 전부 소유합니다. 클라이언트는 문서화된 WebSocket JSON-RPC 프로토콜로 그 데몬에 붙습니다 — 지금은 Ink 터미널 UI, v0.2에는 Electron IDE, 그리고 TypeScript SDK 위에 직접 만드는 무엇이든.

다른 언어: [English](../en/index.md) · [日本語](../ja/index.md) · [简体中文](../zh-CN/index.md) · [Español](../es/index.md) · [전체 목록](../README.md)

## 시작하는 곳

snowpea를 막 설치했다면 [설치](install.md)를 읽고, 이어서 [설정](setup.md), [모드](modes.md) 순으로 읽으세요. 매일 쓰기에는 그걸로 충분합니다. 그 뒤는 필요할 때 찾아보는 선택 사항입니다.

| 문서 | 읽을 때 |
|---|---|
| [설치](install.md) | 설치할 때, 업그레이드할 때, 설치 단계가 실패했을 때 |
| [설정](setup.md) | 벤더를 고를 때, API 키를 넣을 때, 브라우저로 로그인할 때, 검색·브라우저 제공자를 고를 때 |
| [모드](modes.md) | 에이전트가 너무 많이 묻거나, 너무 안 물을 때 |
| [터미널 UI](tui.md) | 키, 패널, 첨부, 음성 — 화면이 무슨 말을 하는지 |
| [명령](commands.md) | 슬래시 명령과 CLI 서브커맨드 전체 목록이 필요할 때 |
| [첨부와 음성](voice.md) | 프롬프트에 이미지·파일을 보낼 때, 말로 시키고 소리로 들을 때 |
| [플러그인](plugins.md) | 스킬·에이전트·명령·훅·MCP 서버를 설치하거나 만들 때 |
| [에이전트와 위임](agents.md) | 작업 위임, 내장 `explore`·`reviewer` 에이전트, 리뷰 |
| [언어 서버](lsp.md) | 편집 직후의 진단, 그리고 lsp_* 툴 |
| [스케줄러](scheduler.md) | 자리를 비운 사이 작업이 돌아가야 할 때 |
| [게이트웨이](gateway.md) | Telegram, Discord, Slack에서 에이전트와 대화하고 싶을 때 |
| [백엔드](backends.md) | 코드가 컨테이너 안이나 다른 호스트에 있을 때 |
| [헤드리스](headless.md) | snowpea를 스크립트로 돌리거나 CI에 엮을 때 |
| [프로토콜](protocol.md) | 데몬을 상대로 클라이언트를 만들 때 |

## 생김새

```text
snowpea              → 필요하면 데몬을 띄우고 터미널 UI를 붙입니다
snowpea -c "..."     → 헤드리스로 한 턴만, UI 없이, 결정적인 종료 코드
snowpea <subcommand> → 세션을 만들지 않고 조회하거나 설정합니다
```

데몬은 게으릅니다. 첫 클라이언트가 붙을 때 시작되고, 유휴 시간이 지나면 스스로 꺼집니다 — 단, 살려 둘 이유가 하나도 없을 때만입니다: 열린 세션도, 활성 잡도, 게이트웨이 바인딩도, 이름 있는 에이전트도 없어야 합니다. 무엇이 데몬을 붙잡고 있는지는 `snowpea daemon status`가 알려줍니다.

```bash
snowpea daemon status
```

## 있는 곳

| 경로 | 내용 |
|---|---|
| `$SNOWPEA_HOME` (기본값 `~/.snowpea`) | 전역에 걸친 모든 것 |
| `$SNOWPEA_HOME/settings.json` | 제공자, 검색·브라우저 선택, 툴 카테고리, 타임아웃 |
| `$SNOWPEA_HOME/credentials.json` | 봇 토큰과 비밀값, 권한 `0600` |
| `$SNOWPEA_HOME/daemon.json` | 포트, pid, 토큰, 시작 시각, 프로토콜 버전 |
| `$SNOWPEA_HOME/state.db` | 세션, 이벤트, 메모리, 잡, 팀 태스크, 이름 있는 에이전트 |
| `$SNOWPEA_HOME/logs/` | `daemon.log`, `approvals.jsonl` |
| `$SNOWPEA_HOME/plugins/`, `skills/`, `agents/`, `commands/` | 설치했거나 직접 만든 확장 |
| `<project>/.snowpea/settings.json` | 이 저장소의 기본 모드, allowlist, 백엔드 |
| `<project>/.snowpea/{skills,agents,commands}/` | 프로젝트 로컬 확장 |
| `<project>/.claude/{skills,agents,commands}/` | Claude Code 호환을 위해 읽는 위치 |

`SNOWPEA_HOME`은 어디서나 존중되므로, 격리된 두 번째 설치는 환경 변수 하나로 끝납니다.

```bash
SNOWPEA_HOME=/tmp/snowpea-scratch snowpea daemon status
```

## 도움받기

UI 안의 `/help`는 코어가 지금 가진 모든 명령을 나열합니다. 플러그인이 추가한 것도 포함됩니다. 셸에서는 `snowpea commands list`가 같은 레지스트리를 출력하고, `snowpea tools list`는 각 툴의 권한 태그와 활성 여부를 출력합니다.

```bash
snowpea commands list
snowpea tools list --json
```

버그와 질문은 [GitHub issues](https://github.com/Wafour-Developer/snowpea-agent/issues)에 남겨 주세요. 코드를 고치고 싶다면 먼저 [Contributing](../../CONTRIBUTING.ko.md)과 [Architecture](../../ARCHITECTURE.md) 두 문서를 읽어 보세요.

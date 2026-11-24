# CLI 훅 연동 (베타)

Claude Code, Codex, Cursor agent, Antigravity(agy)가 snowpea 터미널 안에서 돌 때, snowpea는 그 CLI가 무엇을 하고 있는지 알아야 합니다. 어떤 세션인지, 일하는 중인지, 끝났는지, 답을 기다리는지. 이 CLI들은 그런 일이 생길 때 훅(hook)을 호출할 수 있습니다. 연동은 snowpea의 작은 훅 스크립트 하나를 각 CLI 자신의 훅 설정에 등록해서, CLI가 snowpea에게 알리도록 합니다.

snowpea 터미널 밖에서는 훅이 아무것도 하지 않습니다. 연동을 설치해도 사용자가 직접 CLI를 실행할 때의 동작은 바뀌지 않습니다.

이 기능은 베타이며, [터미널 세션](terminals.md)도 마찬가지입니다.

다른 언어: [English](../en/integrations.md) · [전체 목록](../README.md)

관련: [터미널 세션 (베타)](terminals.md) · [외부 세션 (베타)](external-sessions.md)

## 명령

```bash
snowpea integration status                  # 네 CLI 모두
snowpea integration install claude          # CLI 하나
snowpea integration install all             # 설정 폴더가 있는 CLI 전부
snowpea integration uninstall codex
snowpea integration status agy --json
```

`install`, `uninstall`, `status`는 `claude`, `codex`, `cursor`, `agy`, `all`(기본값) 중 하나를 받습니다. 파일을 직접 다루므로 데몬이 떠 있지 않아도 됩니다.

## 각 CLI가 건드리는 파일

| CLI | 설정 폴더 (환경 변수 재정의, 기본값) | 파일 | 이벤트와 snowpea가 알게 되는 것 |
|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR`, 없으면 `~/.claude` | `settings.json` | `SessionStart`(세션 id; 시작, 재개, clear, compact, fork일 때) · `UserPromptSubmit` → 작업 중 · `Stop` → 완료 · `Notification`이 `permission_prompt` 또는 `elicitation_dialog`일 때 → 막힘 |
| Codex | `$CODEX_HOME`, 없으면 `~/.codex` | `hooks.json`, `config.toml` | `SessionStart`(세션 id) · `UserPromptSubmit` → 작업 중 · `Stop` → 완료. `config.toml`의 `[features]` `hooks` 플래그가 꺼져 있으면 켭니다 |
| Cursor agent | `$CURSOR_CONFIG_DIR`, 없으면 `~/.cursor` | `hooks.json` | `sessionStart`(세션 id) · `stop` → 완료 |
| Antigravity (agy) | `$ANTIGRAVITY_CLI_CONFIG_DIR`, 없으면 `~/.gemini/config` | `hooks.json`의 `snowpea` 블록 | `PreInvocation`(세션 id) |

설치할 때마다 snowpea 파일 두 개도 건드립니다.

- `$SNOWPEA_HOME/hooks/snowpea-agent-state.sh`(Windows에서는 `.ps1`): 네 CLI가 함께 쓰는 훅 스크립트입니다. `SNOWPEA_TERMINAL_ID`가 설정되어 있을 때만 동작하며, 그때 CLI의 이벤트를 snowpea 코어로 넘깁니다.
- `$SNOWPEA_HOME/integrations/state.json`: snowpea가 각 CLI에 정확히 무엇을 넣었는지 기록합니다. 제거할 때 이 기록을 보고 되돌립니다.

snowpea를 업데이트한 뒤 다시 설치하면 훅 스크립트는 덮어써집니다. 직접 만든 훅은 이 파일에 넣지 말고 다른 파일에 두세요.

훅으로 "막힘" 상태를 보고하는 CLI는 Claude Code뿐입니다. Codex, Cursor agent, agy는 권한 요청이나 질문을 화면에서 읽습니다. 이때는 각 CLI의 화면 규칙을 씁니다.

snowpea는 훅을 자신이 쓴 명령 문자열이 정확히 일치하는 것으로만 찾습니다. 사용자의 다른 훅은 그대로 남습니다.

## 설치가 하는 일

- 위의 파일에 snowpea 항목만 추가하고 다른 것은 건드리지 않습니다. 서식, 주석, 사용자 항목의 순서는 그대로 유지됩니다.
- 파일은 원자적으로 씁니다(임시 파일을 만든 뒤 이름 바꾸기). 권한은 그대로 두고, 쓴 결과를 다시 읽어 검사합니다.
- 하드 링크로 된 파일은 거절합니다. 쓰면 의도하지 않은 두 번째 파일이 바뀔 수 있기 때문입니다.
- 연동이 이미 최신이면 아무 바이트도 바꾸지 않습니다. 설치를 두 번 해도 안전합니다.
- 기존 파일을 처음 바꾸기 전에 백업을 하나 남깁니다: 같은 폴더의 `<파일>.snowpea-backup-<날짜>`. 제거해도 백업은 지우지 않습니다.
- 설정 폴더가 없으면 `install <cli>`는 먼저 그 CLI를 한 번 실행하라고 알려 줍니다. `install all`은 폴더가 없는 CLI를 건너뛰고 그렇다고 알려 줍니다.

## 제거가 하는 일

제거는 설치가 추가한 것만 정확히 되돌리고, 그 밖은 건드리지 않습니다.

- snowpea 항목과, 그 항목만 남기고 비게 된 그룹만 지웁니다. 사용자의 훅은 남습니다.
- 설치가 만든 파일은, 남은 내용이 없으면 지웁니다. 원래 있던 빈 파일은 원래 상태(빈 내용)로 되돌립니다.
- Codex의 `[features]` `hooks` 플래그는 설치가 켰을 때만 다시 끕니다. 이미 켜져 있었다면 그대로 둡니다.
- 연동이 남은 CLI가 하나도 없으면 훅 스크립트를 지웁니다.

## 상태 값

`status`는 CLI마다 한 줄씩 상태, 설정 폴더, (있다면) 이유를 출력합니다. `[CLI not found on PATH]` 표시는 CLI 자체가 설치되어 있지 않다는 뜻이므로, 연동이 있어도 아직 아무것도 하지 않습니다.

| 상태 | 뜻 | 할 일 |
|---|---|---|
| `not-installed` | CLI 설정에 snowpea 항목이 없습니다 | `snowpea integration install <cli>` |
| `current` | 모두 제자리에 있고 최신입니다 | 없음 |
| `outdated` | 예전 snowpea가 설치했거나, 등록된 이벤트가 이 버전과 다릅니다 | `snowpea integration install <cli>` |
| `modified` | snowpea 항목 중 하나를 손으로 고쳤거나 지웠거나, Codex 플래그를 껐습니다 | 제거 후 다시 설치하거나, 파일을 원래대로 고치세요 |

## 데스크톱 앱과 웹

데스크톱 앱과 snowpea web의 설정에 있는 연동(Integrations) 페이지는 같은 행들을 보여 주고, 각 CLI가 건드리는 파일을 함께 표시하며, 같은 설치와 제거 동작을 실행합니다. 각 행에는 **설치됨**, **업데이트 있음**, **직접 수정됨**, **설치 안 됨** 중 하나가 나오고, CLI가 없으면 **CLI 없음**도 나옵니다. 설치, 업데이트, 제거는 먼저 묻고 바뀌는 파일을 보여 줍니다. 이 페이지와 동작은 이 컴퓨터에 앉은 사용자 본인만 쓸 수 있습니다. 원격으로 로그인한 연결은 보거나 바꿀 수 없습니다.

## 환경 변수

| 변수 | 쓰임 |
|---|---|
| `SNOWPEA_HOME` | 훅 스크립트와 `integrations/state.json`이 있는 곳(기본값 `~/.snowpea`) |
| `CLAUDE_CONFIG_DIR` | Claude Code의 설정 폴더 |
| `CODEX_HOME` | Codex의 설정 폴더 |
| `CURSOR_CONFIG_DIR` | Cursor agent의 설정 폴더 |
| `ANTIGRAVITY_CLI_CONFIG_DIR` | agy의 설정 폴더 |

CLI를 이 변수 중 하나를 설정한 채로 실행한다면, 연동도 같은 변수를 설정한 채로 설치하세요. 그래야 CLI가 읽는 폴더에 씁니다.

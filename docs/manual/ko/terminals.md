# 터미널 세션 (베타)

터미널 세션은 snowpea 안에서 실제 터미널로 도는 프로그램입니다. 셸, snowpea CLI, 또는 외부 CLI(Claude Code, Codex, agy, Cursor agent) 중 하나입니다. 화면을 보고 거기에 입력할 수 있으며, snowpea는 각 터미널이 일하는 중인지, 답을 기다리는지, 끝났는지 추적합니다.

이 기능은 베타입니다. 각 CLI 화면을 읽는 규칙은 CLI가 출력하는 내용을 따르므로, 새 CLI 버전이 나오면 snowpea가 보는 내용이 달라질 수 있습니다. 상태가 이상해 보이면 터미널 화면 자체가 기준입니다.

다른 언어: [English](../en/terminals.md) · [전체 목록](../README.md)

관련: [외부 세션 (베타)](external-sessions.md) · [자비스 모드 (베타)](jarvis.md) · [CLI 훅 연동](integrations.md) · [snowpea web](web.md) · [터미널 UI](tui.md)

## 터미널 세션이란

- 데몬은 터미널마다 작은 홀더(holder) 프로세스를 하나씩 띄웁니다. 홀더는 프로그램의 의사 터미널(pseudo-terminal)을 소유하고, 마지막 1 MiB의 출력을 보관하며, 종료 코드를 기록합니다.
- 터미널은 데스크톱 앱, snowpea web, 또는 자비스가 엽니다. `snowpea term` 명령은 이미 있는 터미널을 나열하고, 읽고, 입력하고, 붙는 데 씁니다.
- 동시에 실행할 수 있는 터미널은 `terminals.max`만큼입니다(기본 16, 1에서 64 사이).
- 프로필: `shell`, `snowpea`, `claude`, `codex`, `agy`, `cursor`.

## 외부 CLI 세션을 터미널로 열기

`jarvis.external.view`가 `terminal`이면(기본값), 외부 세션은 터미널로 열립니다. CLI가 채팅 화면 대신 패널에서 실행됩니다. 이전처럼 구조화된 채팅 화면을 쓰려면 `chat`으로 바꾸세요. 보기 방식을 요청하지 않는 클라이언트는 채팅 화면을 그대로 씁니다.

- 새 세션은 고른 프로젝트 폴더에서 CLI를 시작합니다.
- 기존 세션은 CLI 자체의 재개 명령으로 이어집니다(`claude --resume <id>`, `codex resume <id>`, agy와 Cursor도 각자의 명령). 세션 id는 훅 연동에서 오므로, 터미널에서 재개가 되게 하려면 먼저 [`snowpea integration install`](integrations.md)을 실행하세요.
- 다른 터미널에서 이미 살아 있는 세션은 예전처럼 보기만 할 수 있습니다.

CLI는 사용자의 대화형 셸 안에서 실행되므로 PATH나 nvm 같은 설정이 그대로 적용됩니다. CLI가 끝나면 `terminals.keepShell`이 꺼져 있지 않은 한 패널은 셸로 돌아갑니다.

## 터미널에서 본 snowpea 세션

snowpea 세션은 두 가지 방식으로 볼 수 있습니다. 채팅으로 보거나, 같은 세션에 붙은 터미널에서 snowpea CLI를 실행해서 보거나. 두 보기는 같은 세션의 클라이언트이므로 동시에 열어 둘 수 있습니다. 전환 버튼은 세션 헤더에 있습니다.

- 기본 보기는 `ui.sessionView`로 정합니다. `chat`(기본값) 또는 `terminal`입니다. 이 설정은 사용자 본인의 화면에 대한 설정이며, 다른 사람의 보기는 바꾸지 않습니다.
- 셸에서 로컬 데몬에 이미 있는 세션에 붙으려면 `snowpea --session <id>`를 실행하세요. TUI는 새 세션을 만들지 않고 그 세션을 재개합니다.

## 설정

| 키 | 기본값 | 역할 |
|---|---|---|
| `ui.sessionView` | `chat` | snowpea 세션을 처음 열 때의 기본 보기 |
| `jarvis.external.view` | `terminal` | 외부 CLI 세션을 여는 방식: `terminal` 또는 `chat` |
| `terminals.max` | `16` | 동시에 실행할 수 있는 터미널 수(1~64) |
| `terminals.keepShell` | `true` | CLI가 끝나면 패널을 닫지 않고 셸로 돌아감 |
| `terminals.headlessCols` / `terminals.headlessRows` | `160` / `48` | 붙은 화면이 없을 때 터미널이 쓰는 크기 |
| `terminals.sounds.blocked` | `true` | 터미널이 사용자의 답을 기다릴 때 소리와 알림 |
| `terminals.sounds.done` | `true` | 터미널이 끝났는데 사용자가 보고 있지 않을 때 소리와 알림 |

`terminals.*`와 `ui.*` 설정은 이 컴퓨터에 앉은 사용자 본인만 바꿀 수 있습니다. 원격이나 터널로 붙은 연결은 바꿀 수 없습니다.

## 상태: 점이 뜻하는 것

각 터미널은 다섯 상태 중 하나이며, 앱은 이를 점으로 보여 줍니다.

| 점 | 상태 | 뜻 |
|---|---|---|
| 확인 필요 | `blocked` 또는 `error` | 프로그램이 답(권한 요청이나 질문)을 기다리거나 실패했습니다 |
| 완료 (안 본 것) | `done` | 한 턴이 끝났고, 아직 아무도 그 터미널을 보지 않았습니다(`seen`이 false) |
| 작업 중 | `working` | 턴이 돌고 있습니다 |
| 대기 | `idle` | 아무것도 돌지 않습니다 |

막힌 상태에는 종류도 있습니다: `permission`(권한), `question`(질문), `auth`(인증). 사이드바는 프로젝트의 점을 확인 필요, 완료, 작업 중, 대기 순서로 모아서 보여 줍니다.

상태는 아래 순서로 우선하는 출처에서 가져옵니다.

1. 프로그램 자체(snowpea TUI는 자기 상태를 직접 보고합니다);
2. 화면에 보이는 권한 요청이나 질문(각 CLI의 화면 규칙이 읽음);
3. CLI의 훅 보고([훅 연동](integrations.md));
4. 화면 규칙만으로 얻은 상태.

## 권한 옵션

CLI를 터미널에서 시작할 때 권한 모드를 고를 수 있습니다. 옵션은 각 CLI의 자체 모드에서 나옵니다.

| CLI | 제공되는 옵션 | 위험한 옵션(빨간색, 먼저 확인) |
|---|---|---|
| Claude Code | Accept edits, Plan, Auto, Don't ask | Bypass permissions (`--dangerously-skip-permissions`) |
| Codex | Read only, Default | Full access (`--dangerously-bypass-approvals-and-sandbox`) |
| agy | Accept edits, Auto, Plan | Skip permissions (`--dangerously-skip-permissions`) |
| Cursor agent | Ask, Plan | Run everything (`--yolo`) |

위험한 옵션은 다음과 같이 동작합니다.

- 나타나는 곳마다 빨간 배지로 표시되며, 앱은 시작 전에 위험을 설명하는 확인 창을 띄웁니다.
- 자비스나 다른 도구가 요청하면 설정과 관계없이 거절합니다.
- `web.remote.maxMode`가 `auto`보다 낮으면 거절합니다.
- 웹 터널로 붙은 연결에서는 터미널 잠금 해제([snowpea web](web.md#터널로-공개-주소-얻기))를 하지 않으면 거절합니다.

자비스는 위험한 옵션으로 시작된 터미널의 권한 요청에 답하지 않습니다. Accept edits나 Don't ask 같은 위험하지 않은 모드도 로컬 앱에서 사용자가 직접 승인해야 하며, 자비스는 이를 대신 고르지 않습니다. 자비스가 `terminal_create`로 시작하는 CLI 터미널은 항상 CLI의 기본 권한으로 시작합니다.

## 데몬을 재시작해도 터미널은 남는다

홀더는 독립적으로 돌기 때문에, 데몬이 재시작하거나 업그레이드되어도 터미널은 계속 실행됩니다. 데몬이 다시 뜨면 살아 있는 홀더마다 다시 연결하고, 홀더가 가진 최근 출력으로 화면을 다시 그립니다. 홀더가 죽은 터미널(예: 재부팅 뒤)은 "잃어버린 세션"으로 표시되며, CLI 세션을 재개할 수 있습니다.

```bash
snowpea daemon stop                    # 데몬만 멈춤; 터미널은 계속 실행
snowpea daemon stop --and-terminals    # 데몬과 모든 터미널 세션을 함께 종료
```

## `snowpea term` 명령

```bash
snowpea term ls                        # 살아 있는 터미널 목록
snowpea term ls --all                  # 끝난 것도 포함
snowpea term read <id> --lines 100     # 화면을 평문으로, 기록 100줄 포함
snowpea term send <id> "테스트 돌려 줘"   # 텍스트를 입력하고 Enter
snowpea term send <id> --no-submit "초안"  # Enter 없이 입력만
snowpea term attach <id>               # 이 창에서 해당 터미널을 직접 조작
snowpea term attach <id> --observe     # 보기만: 입력도, 크기 변경도 없음
```

- `attach`는 현재 터미널을 raw 모드로 바꾸고 세션을 보여 줍니다. **Ctrl+\\** 를 누르면 분리(detach)되며, 세션은 계속 실행됩니다. `attach`는 대화형 터미널에서만 되고, Windows에서는 아직 지원하지 않습니다. Windows에서는 `read`와 `send`를 쓰거나, 앱에서 터미널을 여세요.
- `send`는 사용자가 그 터미널에 입력을 멈춘 뒤에 텍스트를 넣습니다. 권한 요청 등으로 막힌 터미널에는 보내지 않으며, `--force`를 붙여야 보냅니다. `--force`는 화면을 읽고 무엇을 묻는지 확실히 안 뒤에만 쓰세요.
- `read`는 화면을 보여 줄 뿐 아무것도 바꾸지 않습니다.

## 자비스와 터미널 (베타)

자비스 모드를 켜면, 자비스는 볼 수 있는 프로젝트의 터미널을 보고 다음 도구를 씁니다.

| 도구 | 하는 일 |
|---|---|
| `terminal_list` | 터미널 목록, 상태, 누가 시작했는지 |
| `terminal_read` | 한 터미널의 화면 텍스트, 상태, 제목 |
| `terminal_send` | CLI 터미널에 메시지를 입력하고 Enter |
| `terminal_keys` | 이름 있는 키 누르기(enter, escape, up, y, 1 …) |
| `terminal_interrupt` | CLI가 하고 있는 일을 중단 |
| `terminal_wait` | 터미널이 작업을 멈추거나, 막히거나, 끝나거나, 찾는 텍스트를 출력할 때까지 기다림 |
| `terminal_create` | 프로젝트에서 Claude Code, Codex, agy, Cursor 중 하나를 기본 권한으로, 선택적으로 첫 프롬프트와 함께 시작 |

규칙은 다음과 같습니다.

- 자비스는 CLI 터미널에만 입력합니다. 일반 셸에는 입력하지 않고, `force`를 쓰지 않으며, 위험한 옵션으로 시작된 터미널에도 입력하지 않습니다.
- 자비스는 사용자의 요청이나 사용자가 위임한 작업을 위해서만 입력합니다. 모든 읽기와 지시는 접근 기록(`jarvis.access`)에, 모든 지시는 결정 원장(ledger)에 남습니다.
- `terminal_create`는 위험한 옵션을 절대 쓰지 않으며, 만든 터미널에 자비스가 시작했다는 표시를 붙입니다.

자비스를 깨우는 경우:

- 위임된 터미널이 `blocked`나 `error`가 되거나 `done`에 이르면, 자비스는 바로 깨어납니다(평소의 묶음 처리와 예산 규칙을 따릅니다).
- 그 밖의 터미널은 30초 요약(다이제스트)에 들어갑니다.
- 자비스가 시작한 터미널이 막히거나, 오류가 나거나, 끝나면 자비스는 그 터미널의 마지막 20줄이 담긴 메모를 받습니다. 메모는 자비스에게 전달되는 이벤트이며, 터미널에 입력되지 않습니다.

터미널 안의 권한 요청에 대한 답: 자비스는 다음이 모두 참일 때만 권한 요청에 답합니다. 터미널이 자비스에게 완전히 위임되어 있고 사용자가 잡고 있지 않을 것, 위험한 터미널이 아닐 것, 화면 규칙이 요청이 묻는 명령을 읽었을 것, 위임 엔진이 그 명령을 `safe`(안전) 또는 `risky`(위험) 로 분류했을 것. `guarded`나 `never`로 분류된 명령, 그 밖의 모든 경우에는 사용자가 답합니다. 자비스의 모든 답과 거절은 결정 원장에 기록됩니다. 위임 수준은 [자비스 모드](jarvis.md#위임)에서 설명합니다.

# 터미널 UI

인자 없이 `snowpea`를 실행하면 터미널 UI가 열립니다. 이 UI는 얇은 클라이언트입니다 — 어떤 툴을 실행할지, 명령이 무슨 뜻인지, 언제 압축할지 같은 판단은 전부 데몬이 합니다. 이 문서는 그 판단이 드러나는 화면에 관한 이야기입니다.

다른 언어: [English](../en/tui.md) · [日本語](../ja/tui.md) · [简体中文](../zh-CN/tui.md) · [Español](../es/tui.md) · [전체 목록](../README.md)

## 시작 화면

세션이 가장 먼저 그리는 것은 터미널 너비에 맞춘 워드마크, 그리고 지금 어디에 있는지 알려주는 세 줄입니다.

```text
██████████  ██      ██  ██████████  ██      ██  ██████████  ██████████  ██████████
██          ████    ██  ██      ██  ██      ██  ██      ██  ██          ██      ██
██████████  ██  ██  ██  ██      ██  ██      ██  ██████████  ██████████  ██████████
        ██  ██    ████  ██      ██  ██  ██  ██  ██          ██          ██      ██
        ██  ██      ██  ██      ██  ████  ████  ██          ██          ██      ██
██████████  ██      ██  ██████████  ██      ██  ██          ██████████  ██      ██
🌱 snowpea v0.1.2

          Open-source multi-vendor coding agent and personal AI assistant
        v0.1.2 · anthropic/claude-sonnet-4-5 · /home/you/project · ACCEPT

Last session: 26m ago · "add the worktree parallel session story to the IDE plan"
Press R or type /resume to continue it
```

마지막 세션 안내는 이 디렉터리의 세션을 데몬이 아직 들고 있을 때만 나옵니다 — 다른 터미널에서 연 세션, 헤드리스 실행, 크래시가 남기고 간 세션. 빈 입력에서 `R`을 누르거나 `/resume`을 치면 그 세션을 이 창으로 불러옵니다. 데몬에 그런 세션이 없으면 안내 자체가 나오지 않습니다. 받을 수 없는 제안은 제안이 없는 것만 못하니까요.

배너는 한 번만 출력됩니다. 다른 출력처럼 위로 밀려 올라가고 다시 그려지지 않습니다.

## 화면 구성

UI는 `git log`처럼 인라인으로 그립니다. 전체 화면 앱이 아닙니다. 끝난 출력은 터미널에 넘기므로 스크롤백도, 마우스도, `Ctrl+Shift+F`도 평소 쓰던 그대로입니다. 살아 움직이는 것은 화면 아래쪽뿐입니다.

```text
 › 재시도 로직 설명해줘                     ← 스크롤백: 내 것, 다시 그리지 않음
 ⏺ Read 3 files (128 lines)
 ◆ 재시도는 `client.ts`에 있습니다…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← 여기부터가 살아 있는 영역
 > 다음에 칠 것
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

아래쪽 패널은 위에서부터 상태 줄, (있을 때만) 컨텍스트 경고, 요약 줄, 에이전트 행 순서입니다. 입력줄은 그 위에 있고, 작업 표시줄은 다시 그 위에 있습니다. 바뀌지 않은 줄은 다시 그리지 않습니다.

## 작업 중일 때

턴이 도는 동안 입력줄 위에 한 줄이 뜨고, 그 줄은 지금 실제로 무슨 일이 일어나는지 말합니다.

| 표시 | 뜻 |
|---|---|
| `✢ Pondering… (12s · ↓ 3.7k tokens)` | 모델이 생각 중. 동사는 몇 초마다 바뀝니다 |
| `✳ Running shell: npm test… (4s · …)` | 툴이 실행 중, 이름과 인자까지 |
| `✶ 3 agents working… (1m 2s · …)` | 턴이 하위 에이전트에 위임했습니다 |
| `✳ /ralph… (3m 10s · …)` | 명령 워크플로가 턴을 잡고 있습니다 |
| `⏸ Waiting for approval` | 사람 답을 기다리는 중 |

시계와 토큰 수는 세션 전체가 아니라 이번 턴의 것입니다. 세션 합계는 상태 줄에 있습니다. `Esc`로 중단합니다.

연달아 실행된 툴 호출은 끝나면 카드 여러 장이 아니라 스크롤백의 한 줄로 접힙니다.

```text
⏺ Ran 2 shell commands (34 lines)
⏺ Read 3 files (128 lines)
```

실패한 호출만은 출력과 함께 자기 카드를 유지합니다. 정작 읽어야 하는 것이 그것이기 때문입니다. `Ctrl+O`는 아직 살아 있는 영역에 있는 가장 최근 툴 호출이나 diff를 펼칩니다.

## 모드

`Shift+Tab`이 accept → auto → plan → accept 순으로 모드를 돌립니다. 현재 모드는 상태 줄과 요약 줄에 있고, 각 모드가 무엇을 허용하는지는 [모드](modes.md)에 있습니다. `Ctrl+P`는 순회 없이 plan 모드만 켜고 끕니다.

## 승인

데몬이 물을 때는 메뉴로 묻습니다. `↑`/`↓`로 옮기고, `Enter`로 선택한 줄을 고르고, `Esc`는 거절입니다.

```text
╭──────────────────────────────────────────────────────╮
│ Approval required                                    │
│ shell risk=high timeout=300s                         │
│   command: rm -rf build                              │
│                                                      │
│ ❯  Yes   (y)                                         │
│    Yes, and don't ask again this session   (a)       │
│    Yes for this project   (p)                        │
│      adds an allowlist rule the daemon keeps         │
│    No   (n)                                          │
│ ↑↓ move · Enter confirm · Esc cancel                 │
╰──────────────────────────────────────────────────────╯
```

커서는 `Yes`에서 시작하므로 `Enter`는 곧 예입니다. `y`, `a`, `p`, `n`도 그대로 동작합니다. 프롬프트가 떠 있는 동안에는 키보드를 프롬프트가 가집니다 — 입력한 글자가 뒤의 입력줄로 새지 않고, `Shift+Tab`도 모드를 바꾸지 않습니다.

보는 사람 없이 도는 턴이 올린 승인 — 예약 작업, Telegram 메시지 — 은 대기열에 쌓입니다. `Ctrl+R`이 그 대기열에 키보드를 넘기고, `/approvals`가 목록을 보여줍니다.

## Diff

파일 변경은 그 일이 일어난 자리, 대화 안에 그려집니다.

```text
✎ Edited README.md  (+4 −2)
--- a/README.md
+++ b/README.md
@@ -1,5 +1,7 @@
 # snowpea
-an agent
+an open-source multi-vendor coding agent
… 3 more lines (Ctrl+O)
```

새 파일은 `✚ Created notes.md (7 lines)`로 나옵니다. 긴 패치는 열두 줄에서 잘리고, 아직 살아 있는 영역에 있다면 `Ctrl+O`로 펼칩니다.

## 컨텍스트

상태 줄은 모델의 컨텍스트 윈도가 얼마나 찼는지 들고 다닙니다.

```text
ctx 34% (68k/200k)
```

70%까지는 흐리게, 거기서부터 노랑, 85%부터 빨강이고, 80%를 넘으면 요약 줄 위에 무엇을 해야 하는지가 한 줄 뜹니다.

```text
[!!] context 85% — /compact to free space
```

`/compact [지시]`는 지금까지의 대화를 요약하고 그 요약에서 이어갑니다. 뒤에 붙이는 지시는 무엇을 남길지 말해 줍니다. 데몬도 턴이 `context.autoCompactPercent`를 넘길 것 같으면 스스로 압축합니다. 어느 쪽이든 어디서 일어났는지는 기록에 남습니다.

```text
───────────── compacted (68.0k → 12.1k tokens) ─────────────
```

퍼센트 없이 `ctx 12.3k used`로 보인다면 데몬이 그 모델의 윈도 크기를 모른다는 뜻입니다. `snowpea session context`와 `providers.<vendor>.context_window` 설정은 [명령](commands.md)에 있습니다.

## 입력

`↑`는 이전에 보낸 프롬프트를 거슬러 올라가고, `↓`는 쓰던 것으로 돌아옵니다. 기록은 세션이 아니라 머신 단위입니다 — `$SNOWPEA_HOME/tui-history.jsonl`에 최근 500개까지 남고, 같은 프롬프트가 연달아 두 번 기록되지는 않습니다.

가장 최근 항목에서 한 번 더 누른 `↓`는 다른 일을 합니다. 커서가 입력줄을 빠져나와 아래 행으로 내려갑니다. 먼저 요약 줄이고, 거기서 `Enter`를 누르면 지금 돌고 있는 것이 펼쳐집니다.

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to list them
    ◦ Ran shell: npm -w tui test · 12s
```

그다음은 에이전트 행이 한 줄씩입니다. `Esc`나 `↑`로 입력줄로 돌아옵니다.

### 에이전트 안을 들여다보기

에이전트 행에서 `Enter`를 누르면 화면의 기록이 그 에이전트의 대화로 바뀝니다 — 받은 지시, 실행한 툴, 내놓은 답까지.

```text
╭────────────────────────────────────────────────────────────────────╮
│ ◯ executor · Implement story IDE-004      running · 28s · ↓ 159.1k │
│ › Implement story IDE-004 (Worktree parallel sessions)             │
│ ✓ read_file path=docs/stories/IDE-004.md (1 lines)                 │
│ ◆ Added the worktree manager and its tests; 3 files changed.       │
│ ↑↓ PgUp/PgDn scroll · Esc back to the main transcript              │
╰────────────────────────────────────────────────────────────────────╯
```

위임된 에이전트는 저마다 자기 세션에서 돕니다. 이 화면이 바로 그 세션이고, 이미 지나간 부분을 다시 재생한 뒤 이어서 따라갑니다. `Esc`, 또는 `● main`에서 `Enter`를 누르면 돌아옵니다. `Ctrl+A`는 접기 규칙을 풀어 패널을 전부 펼치므로, 쉬고 있는 에이전트와 `↓ N more` 뒤에 숨은 행까지 보입니다.

## 업데이트

새 버전이 있으면 상태 줄이 알려주고, 빈 입력에서 `U`를 누르거나 `/update`를 치면 확인 메뉴가 열립니다. 업그레이드가 돌고 데몬이 재시작한 뒤 UI가 새 버전으로 돌아옵니다.

```text
snowpea v0.1.2 → v0.1.3 (U to update)
```

## 첨부

파일 경로를 입력줄에 붙여넣거나 끌어다 놓으면 글자가 아니라 칩이 됩니다.

```text
[📎 screenshot.png 1.2MB] (backspace removes the last · Ctrl+X clears)
> 이 레이아웃 뭐가 문제야?
```

터미널이 실제로 건네주는 형태를 그대로 알아듣습니다 — 경로 하나, 여러 개, 공백을 이스케이프한 경로, 이스케이프하지 않은 경로, `file://` URL, 파일 관리자가 붙인 따옴표. `Ctrl+V`는 시스템 클립보드의 이미지를 바로 가져와 `$SNOWPEA_HOME/tmp/` 아래에 저장합니다 — `wl-paste`, `xclip`, AppleScript, PowerShell 중 이 머신에 있는 것을 씁니다. `/attach <경로>`는 손으로 붙이는 방법입니다.

빈 입력에서 `Backspace`는 가장 최근 칩을 떼고, `Ctrl+X`는 전부 지웁니다. 프롬프트를 보내면 파일도 함께 가고, 기록에는 무엇이 갔는지 남습니다.

```text
› 이 레이아웃 뭐가 문제야?
  📎 screenshot.png
```

파일은 경로로 보내므로 복사본이 생기지 않습니다. 그 파일을 모델이 어떻게 다루는지 — 20MB 제한, 축소, 이미지를 못 보는 모델에서 벌어지는 일 — 은 [첨부와 음성](voice.md)에 있습니다.

## 음성

음성은 백엔드가 있어야 동작하고, 백엔드를 가진 쪽은 데몬입니다. `snowpea setup audio`로 설정하고, 무엇이 필요한지는 [첨부와 음성](voice.md)에 있습니다.

| 키·명령 | 하는 일 |
|---|---|
| `/voice` | 음성 입력을 켭니다 |
| `Ctrl+Space` 또는 `/rec` | 녹음 시작, 다시 누르면 중지 |
| `/tts on`, `/tts off` | 답변이 끝날 때마다 읽어 줍니다 |
| `Esc` | 읽는 중인 답변을 멈춥니다 |

녹음 중에는 작업 표시줄 자리가 시간을 셉니다.

```text
● REC 00:07
```

멈추면 받아쓴 다음 곧바로 보내지 않고 입력줄에 넣습니다. 음성 인식은 틀릴 때가 충분히 잦아서 한 번은 읽어야 합니다. 녹음은 데몬에 마이크가 있으면 데몬에서, 없으면 이 머신에서 합니다.

상태 줄의 `🔊`는 답변을 읽어 주는 중이라는 뜻입니다. 없는 기능을 요청하면 아무 일도 안 일어나는 대신 데몬이 자기 말로 이유를 돌려줍니다.

```text
voice input needs speech-to-text: no transcription backend: install the whisper CLI, set an OpenAI API key, or …
```

## 전체 화면

`--fullscreen`은 대체 화면 버퍼 레이아웃으로 바꿉니다. 기록이 UI가 직접 굴리는 창이 되어 `PgUp`/`PgDn`과 `Ctrl+U`/`Ctrl+D`로 스크롤하고, 나갈 때 스크롤백에 아무것도 남기지 않습니다.

```bash
snowpea --fullscreen
```

바뀐 줄만 다시 그리므로 느린 연결에서 대역폭을 덜 씁니다. 대신 그 세션 동안 터미널의 스크롤백을 빼앗아 갑니다. 기본값이 아닌 이유가 그것입니다.

## 키

| 키 | 하는 일 |
|---|---|
| `Enter` | 보내기, 또는 선택한 항목 확정 |
| `Shift+Tab` | 모드 순회 |
| `Ctrl+P` | plan 모드 토글 |
| `↑` / `↓` | 이전 프롬프트. 가장 최근에서 한 번 더 `↓`면 패널로 |
| `Esc` | 턴 중단, 읽기 중지, 에이전트 화면 나가기 |
| `Ctrl+O` | 가장 최근 툴 호출이나 diff 펼치기 |
| `Ctrl+A` | 에이전트 패널 전부 펼치기 |
| `Ctrl+R` | 대기 중인 승인 대기열로 |
| `Ctrl+V` | 클립보드 이미지 첨부 |
| `Ctrl+X` | 첨부 전부 비우기 |
| `Ctrl+Space` | 녹음 시작·중지 |
| `U` | 제안된 업데이트 받기 |
| `R` | 시작 화면이 제안한 세션 이어가기 |
| `F1` | 도움말 |
| `Ctrl+C` | 종료 |

`/help`는 플러그인이 추가한 것까지 데몬이 가진 명령 전부를 보여주고, 이 표도 함께 출력합니다.

## Markdown 표

응답의 Markdown 표는 열 너비를 맞춘 테두리 표로 표시합니다. 한글·CJK·이모지의 터미널 표시 폭을 계산하며, 긴 경로나 문장은 셀 안에서 줄바꿈합니다. 열이 너무 많아 화면에 들어가지 않으면 열 이름과 값을 세로로 표시합니다. 일반 화면과 전체 화면에 동일하게 적용되며 코드 블록 안의 표 원문은 그대로 유지합니다.

## 기본 에이전트와 커스텀 에이전트

`/agent list`는 기본 역할(`architect`, `critic`, `executor`, `explorer`, `test-engineer`, `verifier`)과 커스텀 정의를 함께 보여줍니다. 기본 역할은 별도 파일 생성 없이 사용할 수 있으며 출처는 `builtin`으로 표시합니다. 같은 이름의 커스텀 정의가 있으면 해당 정의가 우선합니다. 전역 정의보다 프로젝트 정의가 우선하며, `delegate_task`의 `agent` 인자에도 같은 이름을 사용할 수 있습니다.

## 시작 시 업데이트 알림

실행할 때 백그라운드에서 최신 업데이트를 확인합니다. `/update`는 이전의 “업데이트 없음” 캐시를 사용하지 않고 항상 다시 확인하며, 새 버전이 없으면 설치 실패 대신 이미 최신 상태라고 안내합니다. 업데이트가 있으면 배너가 표시되며 빈 입력줄에서 `U` 또는 `/update`로 확인 메뉴를 열 수 있습니다. `y`를 선택하면 설치 후 재시작하고, `n`이나 Esc로 미룹니다. 입력 중인 소문자 `u`는 단축키로 처리하지 않습니다.

Git의 `main`/`master` 설치본은 설치된 커밋과 비교하므로 버전 번호가 같아도 새 커밋을 감지합니다. 확인 실패, 동일 버전/커밋, 이전 커밋으로의 다운그레이드는 설치하지 않습니다. PyPI/릴리스 설치본은 기존 버전 확인을 유지합니다.

구버전 자동 업데이트 후 `Cannot read properties of undefined (reading 'rawCall')`로 실행이 막힌 경우, 현재 `main`으로 재설치할 수 있습니다:

```sh
uv tool install --force --reinstall 'snowpea-agent[images] @ git+https://github.com/Wafour-Developer/snowpea-agent@main'
```

재설치 후 진행 중인 작업이 없을 때 `snowpea daemon stop`으로 기존 데몬을 종료하고 `snowpea`를 실행하면 새 코드가 적용됩니다.

도움말은 화면 높이에 맞춰 표시됩니다. `↑`/`↓`, `PgUp`/`PgDn`으로 스크롤하고 **Esc, F1, q 또는 Enter**로 닫습니다. 도움말에서 Esc를 눌러도 진행 중인 작업은 중단하지 않습니다.

하단의 입력, 연결/모델 상태, 모드 요약, 에이전트 목록은 터미널 내용 폭 전체의 가로선으로 구분되어 현재 키 입력이 어느 영역에 적용되는지 쉽게 확인할 수 있습니다.

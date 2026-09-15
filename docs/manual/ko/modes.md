# 모드, 권한, allowlist

모든 툴은 권한 태그를 하나씩 가집니다. 모드는 각 태그를 어떻게 처리할지 정합니다.

| 태그 | 툴 |
|---|---|
| `read` | `read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `process_list`, `memory_search`, `transcribe_audio`, `skill_search`, `skill_list`, `ask_user`, `set_mode` |
| `write` | `write_file`, `edit_file`, `git_commit`, `memory_write` |
| `exec` | `shell`, `process_kill`, `skill_install`, `skill_remove` |
| `delegate` | `delegate_task` — 자식은 세션의 모드를 물려받으므로 위임 자체는 부모보다 더 할 수 없고, 승인은 자식의 개별 호출에서 묻습니다 |
| `network` | `web_search`, `web_extract`, `browser_*`, 미디어 툴, `text_to_speech`, 기본적으로 MCP 서버 |
| `send` | `schedule_create`, `schedule_list`, `schedule_cancel` |

## 매트릭스

| 모드 | 읽기 | 쓰기 | 실행 | 네트워크 | 발송 | 위임 |
|---|---|---|---|---|---|---|
| **plan** | 허용 | 거부 † | 질문 ‡ | 허용 | 거부 | 허용 |
| **accept** (기본) | 허용 | 허용 | 질문 | 질문 | 질문 | 허용 |
| **auto** | 허용 | 허용 | 허용 | 허용 | 허용 | 허용 |

† plan 모드는 마크다운·계획 파일만 씁니다. ‡ plan 모드는 읽기 전용 명령을 묻지 않고 실행하고, 나머지는 그대로 물어봅니다.

**plan**은 생각하는 용도입니다. 에이전트는 저장소를 읽고 웹을 검색하며, 코드는 바꾸지 못합니다. 다만 일을 하려면 꼭 필요한 두 가지는 됩니다.

- **계획 자체 쓰기.** `.md`·`.markdown`·`.txt` 파일, `docs/`나 `.snowpea/plans/` 아래, 그리고 `$SNOWPEA_HOME/plans/`. 그 밖의 경로는 거부되고, 거부 메시지에 `plan mode: only markdown/plan files may be written`가 붙어 같은 쓰기를 반복하지 않고 다른 곳에 쓰게 합니다. 설정 파일은 이름이 무엇이든 plan 모드에서 절대 쓸 수 없습니다.
- **읽기 전용 명령 실행.** `ls`, `cat`, `grep`, `find`, `git status`·`diff`·`log`·`show`·`blame`, `npm test`, `pytest` 같은 것은 묻지 않고 실행됩니다. 무언가를 바꿀 수 있는 것 — `rm`, `mv`, `git commit`, `python -c`, 리다이렉션, 명령 치환, 한 조각이라도 안전하지 않은 체인 — 은 그대로 물어보고, 모르는 프로그램은 항상 물어봅니다.

거부된 호출은 `mode_denied` 코드를 담은 `error` 이벤트를 내며 그 턴을 끝내고, 헤드리스 실행이라면 종료 코드 `4`로 끝납니다.

무엇을 쓸 수 있는지는 `modes.plan.writableGlobs`로 바꿉니다. 작업 디렉터리 기준 glob 목록입니다.

```json
{ "modes": { "plan": { "writableGlobs": ["**/*.md", ".snowpea/plans/**", "docs/**"] } } }
```

**accept**는 일하기 위한 기본값이며, Claude Code의 acceptEdits와 대응합니다: 파일 읽기와 편집은 묻지 않고 흐르고, 셸 명령·네트워크 호출·무언가를 발송하는 동작은 먼저 묻습니다.

**auto**는 아무것도 묻지 않습니다. 지켜보고 있을 때, 버려도 되는 컨테이너 안에 있을 때, 혹은 파급 범위를 이미 따져 본 예약 잡에서 쓰세요.

## plan 모드 나가기

계획이 끝나면 에이전트는 모드를 직접 바꾸라고 부탁하지 않습니다. `set_mode("accept")`를 호출해 선택 UI를 띄웁니다 — accept 모드로 전환하고 구현 시작, auto 모드로 전환하고 구현 시작, plan 모드 유지 중에서 고르면 됩니다. 추천하는 항목이 맨 위에 오고 `(추천)` 표시가 붙습니다.

고른 것은 그 자리에서, 질문을 띄운 그 턴 안에서 적용됩니다. accept를 고르면 에이전트가 같은 턴에서 곧바로 파일을 고치기 시작합니다 — 다시 프롬프트를 쓸 필요도, 계획을 다시 받을 필요도 없습니다. plan 유지를 고르거나 Esc를 누르거나 질문이 타임아웃되면 아무것도 바뀌지 않습니다.

헤드리스 실행(`snowpea -c`)은 물어볼 상대가 없으므로 질문이 스스로 거부되고 모드는 그대로입니다 — plan 모드는 CI에서 여전히 읽기 전용 게이트입니다. 연결된 채팅방에서는 질문이 버튼으로 도착합니다. [게이트웨이](gateway.md)를 보세요.

`/plan`, `/accept`, `/auto`, `/mode`는 그대로 있고, 사용자가 먼저 모드를 바꾸고 싶을 때 쓰는 수단입니다.

## 전환

```
/plan
/accept
/auto
/mode
/mode show
/mode save
```

```bash
snowpea --mode plan
snowpea -c "draft a migration plan" --mode plan
```

`/mode save`는 `<project>/.snowpea/settings.json`에 `"defaultMode"`를 써서, 이 저장소의 다음 세션이 거기서 시작하게 만듭니다. 현재 모드는 상태 줄에 항상 표시됩니다.

## 승인

정책이 *질문*이라고 답하면 코어는 승인 요청을 만들고 그 툴 호출을 막습니다. 대화형 요청은 그 턴이 시작된 화면에만 갑니다 — 입력하고 있던 TUI 창이거나, `snowpea -c`를 돌리는 터미널입니다. 다른 누구의 큐에도 뜨지 않습니다.

답에는 범위가 붙습니다.

| 범위 | 의미 |
|---|---|
| `once` | 이번 호출에만 |
| `session` | 이번 세션이 끝날 때까지 일치하는 모든 것 |
| `project` | `<project>/.snowpea/settings.json`에 저장 |
| `always` | `$SNOWPEA_HOME/settings.json`에 저장 |

아무도 답하지 않는 것도 하나의 답입니다. `approvals.timeoutSec`(기본 300초)이 지나면 요청은 거부되고 턴이 끝납니다. 타임아웃을 포함한 모든 결정은 `$SNOWPEA_HOME/logs/approvals.jsonl`에 남습니다.

예약 잡이나 들어오는 채팅 메시지가 올린 요청은 *무인* 요청이며 동작이 다릅니다 — [게이트웨이](gateway.md)를 보세요.

## allowlist

allowlist 항목은 일치하는 명령에 대해 *질문*을 *허용*으로 올립니다. *거부*는 절대 올릴 수 없으므로, 여기에 무엇을 추가하든 plan 모드를 약화시키지 못합니다.

```
/allow ^ls( .*)?$
/allow ^git (status|diff|log)\b
/allow ^npm (run )?test$ --global
/allow tool:web_search
/allowlist
/allowlist remove 3
```

패턴은 셸 명령에 매치되는 정규식입니다. `tool:<name>` 형태는 툴 하나를 통째로 allowlist에 올립니다. `--global` 없이 추가하면 프로젝트 설정에 들어가고, 붙이면 `$SNOWPEA_HOME/settings.json`에 들어갑니다.

패턴은 앵커를 걸고 좁게 쓰세요. `^git `는 `git push --force`도 허용해 버립니다. `^git (status|diff|log)\b`는 그렇지 않습니다.

## 헤드리스와 무인 실행

`snowpea -c`는 TTY가 있으면 stdin으로 묻습니다. TTY가 없으면 — CI 잡, 파이프 — 물어볼 상대가 없으므로 승인 요청은 즉시 거부되고 프로세스는 `4`로 종료됩니다. 그걸 의도했다면 명시적으로 밝히세요.

```bash
snowpea -c "run the linter" --approve-none
```

CI에서 정직한 조합은 읽기만 하는 작업에는 plan 모드, 잃어도 되는 컨테이너 안에서는 auto 모드입니다. 개발자 머신에서 프롬프트를 조용히 시키려고 auto를 쓰지는 마세요. 그럴 때 쓰라고 있는 게 allowlist입니다.

## 다음

[명령](commands.md) — 명령 전체 목록입니다.

# 모드, 권한, allowlist

모든 툴은 권한 태그를 하나씩 가집니다. 모드는 각 태그를 어떻게 처리할지 정합니다.

| 태그 | 툴 |
|---|---|
| `read` | `read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `process_list`, `memory_search` |
| `write` | `write_file`, `edit_file`, `git_commit`, `memory_write` |
| `exec` | `shell`, `process_kill`, `delegate_task` |
| `network` | `web_search`, `web_extract`, `browser_*`, 미디어 툴, 기본적으로 MCP 서버 |
| `send` | `schedule_create`, `schedule_list`, `schedule_cancel` |

## 매트릭스

| 모드 | 읽기 | 쓰기 | 실행 | 네트워크 | 발송 |
|---|---|---|---|---|---|
| **plan** | 허용 | 거부 | 거부 | 허용 | 거부 |
| **accept** (기본) | 허용 | 허용 | 질문 | 질문 | 질문 |
| **auto** | 허용 | 허용 | 허용 | 허용 | 허용 |

**plan**은 생각하는 용도입니다. 에이전트는 저장소를 읽고 웹을 검색할 수 있지만, 아무것도 바꿀 수 없습니다. 거부된 호출은 `mode_denied` 코드를 담은 `error` 이벤트를 내며 그 턴을 끝내고, 헤드리스 실행이라면 종료 코드 `4`로 끝납니다.

**accept**는 일하기 위한 기본값이며, Claude Code의 acceptEdits와 대응합니다: 파일 읽기와 편집은 묻지 않고 흐르고, 셸 명령·네트워크 호출·무언가를 발송하는 동작은 먼저 묻습니다.

**auto**는 아무것도 묻지 않습니다. 지켜보고 있을 때, 버려도 되는 컨테이너 안에 있을 때, 혹은 파급 범위를 이미 따져 본 예약 잡에서 쓰세요.

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

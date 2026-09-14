# 헤드리스 실행

`snowpea -c`는 UI 없이 한 턴만 돌고 나서 분기할 수 있는 종료 코드와 함께 끝납니다. snowpea를 스크립트, git 훅, CI 잡에 넣는 방법이 바로 이것입니다.

```bash
snowpea -c "what does this repository do?"
snowpea -c "add a regression test for the parser" --mode auto
snowpea -c "summarize today's diff" --json --cwd ~/src/api --timeout 300
```

## 옵션

| 옵션 | 의미 |
|---|---|
| `-c`, `--prompt TEXT` | 프롬프트. 이게 있다는 것 자체가 헤드리스 실행이라는 뜻입니다 |
| `--mode plan\|accept\|auto` | 권한 모드, 기본값은 프로젝트 기본값 |
| `--json` | 텍스트 대신 JSON Lines를 출력 |
| `--cwd DIR` | 세션의 작업 디렉터리 |
| `--timeout SEC` | SEC초가 지나면 세션을 인터럽트하고 닫음 |
| `--provider VENDOR` | 이번 실행에 쓸 벤더 |
| `--resume SESSION_ID` | 새 세션을 열지 않고 저장된 세션을 이어서 진행 |
| `--approve-none` | 묻는 대신 모든 승인을 거부 |
| `--home DIR` | `SNOWPEA_HOME` 오버라이드 |

## 동작 방식

데몬이 떠 있는지 확인하고, `--cwd`에 세션을 만들고, 프롬프트를 보낸 뒤, 도착하는 `session.event` 알림을 그대로 렌더링하고, 턴이 끝나면 세션을 닫습니다. TUI를 띄우지 않으므로 Node도 필요 없습니다.

슬래시 명령도 유효한 프롬프트입니다. 명령 레지스트리가 UI가 아니라 코어에 속해 있기 때문입니다:

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit"
```

`snowpea init [--force]`는 바로 이것의 지름길입니다: `--cwd`(또는 현재 디렉터리)에 세션을 열고 `/init`을 한 번의 헤드리스 턴으로 돌립니다. 즉 `snowpea init`과 `snowpea -c "/init"`은 같은 일을 합니다.

```bash
snowpea init
snowpea init --force
```

## 저장된 세션 이어가기

`-c`는 보통 새 세션을 엽니다. `--resume`은 이미 있는 세션을 이어갑니다. TUI의 `/resume`에 해당하는 헤드리스 쪽 기능으로, 저장된 대화 기록을 다시 불러온 뒤 그 뒤에 프롬프트를 덧붙입니다.

```bash
snowpea session list --include-closed
snowpea -c "and now write the tests" --resume s-4f2c9a1b7e30
```

`--resume`과 함께 준 `--mode`·`--provider`는 무시됩니다. 저장된 세션이 자기 값을 그대로 유지합니다. `--resume`만 단독으로 주면 아무 일도 하지 않습니다. TUI 안에서는 `/resume`으로 세션을 골라 주세요.

## 저장된 세션 관리

세션은 닫힌 뒤에도 `$SNOWPEA_HOME/state.db`에 남고, 첨부와 음성은 각각 `$SNOWPEA_HOME/attachments/<id>/`와 `$SNOWPEA_HOME/audio/<id>/`에 남습니다. 저장된 세션을 지우면 이 파일들도 함께 사라집니다.

```bash
snowpea session list                                  # 살아 있는 세션
snowpea session list --include-closed --json          # 저장된 세션까지
snowpea session list --include-closed --workdir ~/src/api
snowpea session delete s-4f2c9a1b7e30                 # 저장된 세션 하나
snowpea session clear --workdir ~/src/api             # 한 프로젝트의 저장 세션 전부
snowpea session clear --all                           # 모든 디렉터리의 저장 세션 전부
```

살아 있는 세션은 절대 지워지지 않습니다. 먼저 닫아 주세요. 각 줄에는 세션 id, 모드, 생성 시각, 작업 디렉터리, 마지막 프롬프트가 나옵니다.

## 팀과 모델 프로필

프로젝트 팀과 에이전트별 모델 라우팅은 설정값이므로 UI 없이도 다룰 수 있습니다.

```bash
snowpea team list                                     # 전역 + 프로젝트 팀, `*`가 활성 팀
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery

snowpea model profiles --json                         # 프로필, 기본값, 에이전트별 지정
snowpea model default fast                            # models.default
snowpea model assign executor deep                    # agents.models.executor
```

`snowpea team create`는 `/team create`가 쓰는 것과 같은 `<workdir>/.snowpea/settings.json`에 기록하고, 찾을 수 없는 에이전트 이름은 거부합니다. `snowpea team delete`는 프로젝트 팀만 지웁니다. 전역 팀은 `$SNOWPEA_HOME/settings.json`에서 고쳐 주세요. `snowpea model assign`은 없는 프로필 id를 주면 데몬이 거부합니다.

## 종료 코드

| 코드 | 의미 |
|---|---|
| `0` | 턴이 완료됨 |
| `1` | 에이전트가 실패로 끝남 |
| `2` | 사용법 또는 설정 오류 |
| `3` | 데몬에 연결하지 못함 |
| `4` | 승인이 거부됐거나 모드가 그 동작을 막음 |
| `5` | `--timeout`이 지나서 세션이 인터럽트되고 닫힘 |

```bash
if snowpea -c "does this repo have a failing test?" --mode plan; then
  echo "clean"
else
  echo "exit $?"
fi
```

`4`가 눈여겨볼 코드입니다. plan 모드에서 쓰기를 시도하면 `mode_denied` 오류와 함께 `4`로 끝나므로, plan 모드는 CI에서 쓸 만한 읽기 전용 게이트가 됩니다.

## JSON 출력

`--json`을 주면 받은 `session.event` 하나하나가 한 줄에 하나씩 JSON 객체로 쓰이고, 마지막 줄은 결과 레코드입니다:

```json
{"kind":"message.delta","sessionId":"…","seq":12,"payload":{"text":"Looking at "}}
{"kind":"tool.call","sessionId":"…","seq":13,"payload":{"callId":"c1","name":"grep","args":{"pattern":"def main"}}}
{"kind":"tool.result","sessionId":"…","seq":14,"payload":{"callId":"c1","name":"grep","ok":true,"output":"…"}}
{"kind":"turn.done","sessionId":"…","seq":20,"payload":{"turnId":"t1","reason":"complete"}}
{"kind":"result","exitCode":0,"sessionId":"…","usage":{"inputTokens":4120,"outputTokens":380}}
```

이벤트 종류는 `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `usage`, `error`, `turn.done`입니다. `turn.done`의 reason은 `complete`, `interrupted`, `error`, `denied`, `timeout` 중 하나입니다. 전체 payload 스키마는 [docs/protocol.md](../../protocol.md)에 있습니다.

```bash
snowpea -c "list the modules" --json | jq -r 'select(.kind=="message.delta") | .payload.text' | tr -d '\n'
```

## 사람 없이 하는 승인

TTY가 있으면 승인 요청이 stdin에서 y/n 프롬프트가 됩니다. TTY가 없으면 — 파이프, CI 러너 — 물어볼 사람이 없으므로 요청은 즉시 거부되고 프로세스는 `4`로 종료됩니다. 그걸 원한다면 명시적으로 밝혀 두세요:

```bash
snowpea -c "run the linter" --approve-none
```

자동화에 맞는 정직한 구성은 두 가지입니다 — 읽기만 필요한 작업에는 plan 모드, 버려도 되는 컨테이너 안에서라면 auto 모드. 컨테이너 쪽은 [백엔드](backends.md)를 보세요.

## 세션 없는 서브커맨드

일부 서브커맨드는 RPC 메서드 하나만 호출하고 세션을 만들거나 모델을 호출하지 않은 채 종료합니다. 빠르고, 결정적이고, 비용이 없으므로 설치 직후 스모크 테스트로 쓰기 좋습니다:

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea daemon status --json
```

## CI에서

```yaml
- run: curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
- run: snowpea --version
- run: snowpea tools list --json
- run: snowpea -c "/deep-research whether this dependency has a known CVE" --mode plan --timeout 600 --json
```

`SNOWPEA_HOME`을 잡 전용 디렉터리로 지정해서 실행끼리 상태를 공유하지 않게 하세요. 그리고 스텝이 끝나도 데몬은 계속 돈다는 점을 기억하세요 — 러너가 오래 사는 환경이라면 잡 끝에 `snowpea daemon stop`을 넣으세요.

## 다음

[프로토콜](protocol.md) — CLI가 실제로 무엇을 주고받는지.

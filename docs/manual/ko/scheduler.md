# 스케줄러

스케줄러는 데몬 안에 삽니다. 잡은 데몬 프로세스 안에서, 등록할 때 지정한 모드로 돌고, 답을 지정한 채널로 보냅니다. 별도의 cron 데몬에서 도는 것은 아무것도 없고, 터미널이 열려 있을 필요도 없습니다.

## 잡 등록

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check whether the build is green" --mode plan
snowpea job schedule --every 30m --task "watch the error log" --channel log --workdir ~/src/api
```

세션 안에서는 이렇게 씁니다.

```
/schedule "every day at 9am" "summarize yesterday's commits" --channel telegram:123456
/schedule
```

`--at`, `--in`, `--every`, `--cron`, `--spec`은 같은 옵션의 다섯 가지 이름입니다. 줄이 가장 자연스럽게 읽히는 것을 고르세요.

## 스펙

| 형태 | 예 |
|---|---|
| cron, 5개 필드 | `0 9 * * *` |
| 일회성 지연 | `in 60s`, `in 10m`, `in 2h` |
| 간격 | `every 30m`, `every 6h` |
| 자연어, 영어 | `every day at 9am`, `in 10 minutes` |
| 자연어, 한국어 | `매일 09:00`, `10분 뒤` |

잡은 어떻게 파싱되었는지에 따라 `cron`, `once`, `interval` 중 하나가 됩니다. `job list`는 파싱된 종류와 다음 실행 시각을 보여주므로, 자연어 스펙이 생각한 대로 해석됐는지 확인하는 가장 빠른 방법입니다.

```bash
snowpea job list
snowpea job list --json
```

## 잡 관리

```bash
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`job run`은 타이머가 그랬을 것과 똑같이 데몬 안에서 잡을 즉시 발동시킵니다. 스케줄에 맡기기 전에 잡을 테스트하는 올바른 방법입니다.

각 잡은 `last_run`과 `last_status`를 기록하며, `last_status`는 `ok`, `error`, 또는 승인이 답 없이 만료됐을 때의 `denied_by_timeout`입니다.

## 잡이 실행되는 방식

스케줄러는 15초마다 틱을 돕니다. 잡이 실행 시각이 되면 그 잡의 작업 디렉터리와 모드로 세션을 만들고 무인(unattended)으로 표시한 뒤, 잡의 태스크로 프롬프트하고 마지막 어시스턴트 메시지를 채널로 전달합니다. `started`, `finished`, `failed`, `denied` 종류의 `job.event` 알림이 붙어 있는 모든 클라이언트로 가므로, 돌고 있는 TUI는 잡의 진행 상황을 보여줍니다.

두 가지가 알아둘 만합니다. 첫째, 잡 id와 예정 시각을 합친 발생 키(occurrence key)가 고유하므로, 데몬이 틱 도중에 재시작해도 같은 슬롯에 대해 잡이 두 번 발동하지 않습니다. 둘째, 스케줄러는 시작할 때 데몬이 꺼져 있던 동안 놓친 일회성 잡을, 한 시간 넘게 늦지 않았다면 따라잡습니다. 그보다 오래된 것은 실행 대신 놓침(missed)으로 표시됩니다.

## 채널

| 채널 | 가는 곳 |
|---|---|
| `telegram:<chat_id>` | Telegram 대화방 |
| `discord:<channel_id>` | Discord 채널 |
| `slack:<channel>` | Slack 채널 |
| `log` | `$SNOWPEA_HOME/logs/daemon.log`로만 |

플랫폼은 먼저 바인딩되어 있어야 합니다 — [게이트웨이](gateway.md)를 보세요. `log`는 아무 설정도 필요 없어서, 잡이 무슨 말을 해야 할지 아직 가늠하는 중일 때 쓰기 좋습니다.

## 모드와 승인

잡은 등록한 세션과 무관하게 자기 자신의 모드를 가집니다. 잡을 등록하는 것 자체에 `send` 권한이 필요하므로, accept 모드에서는 등록 자체를 승인해 달라는 요청을 받게 됩니다. 이는 의도된 것으로, 예약된 잡은 그 모드가 허용하는 모든 것에 대한 상시 승인이기 때문입니다.

잡의 턴이 승인이 필요한 지점에 부딪히면 그 요청은 무인 처리됩니다. TUI 승인 큐와 바인딩된 대화방에 허용·거부 버튼과 함께 동시에 나타납니다. 먼저 답하는 쪽이 이기고, 나머지에게는 결과가 통보되며, `approvals.timeoutSec`(300초) 안에 아무도 답하지 않으면 요청은 거부되고 잡의 `last_status`는 `denied_by_timeout`이 됩니다.

plan 모드의 잡은 읽고 검색할 수는 있지만 절대 쓰지 못합니다. auto 모드의 잡은 아무것도 묻지 않습니다. 무인 실행이라면 plan이 안전한 기본값이고, auto는 컨테이너가 있어야 어울립니다.

## 데몬을 계속 살려두기

활성화된 잡은 유휴 종료를 막는 네 가지 카운터 중 하나이므로, 스케줄이 있는 데몬은 스스로 계속 떠 있습니다.

```bash
snowpea daemon status
```

이 명령은 `will not exit`과 그 이유를 출력합니다. 재부팅을 넘겨 살아남는 것은 별개의 문제입니다 — 그러려면 서비스를 등록하세요.

```bash
snowpea service install
snowpea service status
```

## 다음

[게이트웨이](gateway.md) — 답이 어디로 가는지.

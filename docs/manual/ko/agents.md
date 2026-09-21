# 에이전트와 위임

다른 언어: [English](../en/agents.md)

**서브에이전트**는 자기 세션에서 도는 위임된 작업 하나입니다. 작업 디렉터리, 모드, 프로바이더, 백엔드는 부모에게서 물려받지만 그 외에는 아무것도 공유하지 않습니다. 지금 이 대화를 볼 수 없고, 질문할 수 없으며, 돌아오는 것은 마지막 메시지 하나뿐입니다. 그 고립이 핵심입니다 — 파일 발췌 백 개로 컨텍스트를 채울 탐색이 문단 하나로 끝납니다.

에이전트는 `delegate_task` 도구로 위임합니다. 직접 시키려면 `/delegate <agent> <task>`, 또는 프롬프트 맨 앞의 `$<agent> <task>` 축약형을 씁니다.

```text
$explore 응답 언어는 데몬 어디서 정해지나?
/delegate reviewer tools/fs.py 변경분을 봐줘
```

## 한 작업에 한 에이전트

위임은 늘 같은 방식으로 망가집니다. 한 작업에 주인이 둘 생기는 것입니다. 같은 파일을 고치는 자식 둘은 각각 절반씩 끝난 변경을 남기고, 위임해 놓고 자기도 그 일을 하는 부모는 같은 값을 두 번 치릅니다. 그래서 규칙은 한 작업에 한 에이전트이고, 말로만 두지 않도록 두 개의 가드가 코드에 있습니다.

**중복은 거절됩니다.** 자식이 일하는 동안 같은 에이전트에 같은 브리프로 `delegate_task`를 다시 부르면, 이미 그 일을 맡은 자식의 id를 담은 오류가 돌아옵니다. 공백이나 대소문자는 브리프를 다르게 만들지 않습니다. 정말로 한 작업을 두 번 시도하고 싶다면(best-of-N) `force: true`를 넘기세요.

**돌고 있는 형제는 자기가 쓴 파일의 주인입니다.** 두 자식이 나란히 일하다 한쪽이 쓴 파일을 다른 쪽이 고치려 하면 `file_owned_by_sibling`으로 거절되고, 고치는 대신 충돌을 보고하라는 말을 듣습니다. 그러면 부모가 순서를 세웁니다. 이 가드는 `tools.readBeforeWrite` 설정을 따르고, 형제가 아직 돌고 있는 동안에만 적용됩니다. 형제가 보고를 마친 뒤에는 평범한 재읽기면 충분합니다.

그러므로 worktree 없는 병렬 위임은 파일이 겹치지 않을 때만 안전합니다. `/team`은 작업자마다 git worktree를 주므로 이 제약이 없습니다.

## 내장 에이전트

| 이름 | 쓰임 |
|---|---|
| `explore` | 읽기 전용 탐색: 무엇이 어디 있는지 찾아 `path:line` 근거로 보고 |
| `reviewer` | 읽기 전용 리뷰: 판정 한 줄과, 근거가 붙은 지적들 |
| `executor`, `architect`, `verifier`, `test-engineer` | 역할 프롬프트, 도구 제한 없음 |
| `explorer`, `critic` | 기본은 읽기 전용 역할(`read_file`, `glob`, `grep`), 정의에서 명시적으로 확장한 경우만 추가 도구 사용 |

`explore`와 `reviewer`는 도구 허용 목록을 들고 다녀서 지시가 아니라 사실로 읽기 전용입니다. `write_file`도 `edit_file`도 `shell`도 없습니다. `<project>/.snowpea/agents/`에 같은 이름의 정의를 두면 내장 정의를 통째로 덮어씁니다.

### `explore`와 `explorer`, `reviewer`와 `critic`

이름만 보면 겹쳐 보이는 두 쌍이 있지만 쓰임이 다릅니다. `explore`와 `reviewer`는 **위임해서 쓰는 내장 에이전트**입니다. 읽기 전용이고 도구 허용 목록이 붙어 있으며, 질문 하나나 diff 하나를 맡깁니다. `explorer`와 `critic`은 **팀 역할**로 파이프라인의 explore·review 단계를 채우지만, 기본 도구는 `read_file`/`glob`/`grep` 읽기 전용이고 정의에서 명시적으로 추가한 경우에만 더 넓어집니다. 지금 당장 뭔가를 물어볼 때는 내장 에이전트를, 로스터를 적을 때는 역할 이름을 씁니다.

| 위임용 | 팀 역할 | 차이 |
|---|---|---|
| `explore` | `explorer` | `explore`는 내장 읽기 전용 탐색 에이전트, `explorer`는 파이프라인 explore 단계 담당 |
| `reviewer` | `critic` | `reviewer`는 요청 시 동작하는 내장 리뷰 에이전트, `critic`은 파이프라인 review 단계 담당 |

`snowpea agents` 목록의 설명에도 같은 문장이 들어가 있어, 목록만 봐도 어느 쪽인지 알 수 있습니다.

### 정의를 어디서 읽어왔는지

각 행에는 `project` 한 단어가 아니라 실제로 읽어온 위치가 실립니다.

| `source` | 읽어온 곳 |
|---|---|
| `builtin` | snowpea에 함께 배포된 정의 |
| `global` | `~/.snowpea/agents/` |
| `project` | `<project>/.snowpea/agents/` |
| `claude-global` | `~/.claude/agents/` |
| `claude-project` | `<project>/.claude/agents/` |

Claude Code의 에이전트 디렉터리도 그대로 읽되 출처를 그대로 표시하므로, snowpea용으로 쓰지 않은 정의를 한눈에 구분할 수 있습니다.

`explore`는 브리프에 적힌 철저함(quick / medium / very thorough)에 맞춰 움직이고, 파일 덤프가 아니라 절대 경로가 붙은 텍스트로 보고합니다. `reviewer`는 판단할 파일을 반드시 열어 보고, 세 판정 중 하나로 답합니다.

```text
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
VERDICT: NEEDS_MORE_EVIDENCE
```

근거 없는 지적은 의견일 뿐이므로, 지적마다 위치와 무엇이 잘못되는지와 그것이 터지는 조건이 함께 붙습니다.

## 리뷰는 요청할 때만 돕니다

스스로 리뷰를 시작하는 것은 없습니다. `/review`는 작업 트리에 커밋되지 않은 변경을 대상으로 한 번 돌고 판정을 전달합니다.

```text
/review
/review 새 파서의 오류 처리
```

`/ralph`도 루프 끝에서 리뷰를 받습니다. 프로젝트에 `architect` 정의가 있으면 그것을, 없으면 내장 `reviewer`를 씁니다.

팀 모드는 태스크를 받아들이기 전에 리뷰를 끼울 수 있고, 기본값은 꺼짐입니다.

```json
{ "team": { "review": true } }
```

켜 두면 태스크가 병합될 때마다 `reviewer` 자식이 그 병합분을 읽습니다. `REQUEST_CHANGES` 판정은 그 태스크를 지적과 함께 같은 작업자에게 한 번 되돌리고, 그 외의 판정은 병합을 그대로 둡니다.

## 팀과 작업자는 서로 다른 명령입니다

```text
/workers 3 "파서 모듈 세 개에 docstring 추가"   # 동일한 작업자 3명
/team "파서 모듈 세 개에 docstring 추가"        # 내 팀이 역할별로
```

예전에는 첫 단어가 숫자인지로 구분하는 한 명령이었는데, 그건 문법이라기보다 퀴즈였습니다. **팀**은 내가 꾸린 사람들이 각자 역할대로 일하는 것이고, **작업자**는 익명 에이전트 N개가 같은 작업 목록을 나눠 달리는 것입니다. `/team 3 "…"`은 지금도 작업자 모드로 실행되며, 현재 표기는 `/workers N`이라는 안내를 한 줄 덧붙입니다.

**`/workers <N> "<task>"`**(별칭 `/worker`)가 옮겨간 그 모드입니다. 작업자 N명에게 각각 git worktree를 주고, 태스크가 끝나는 대로 리드가 브랜치를 병합합니다.

**`/team "<task>"`**는 활성 프로젝트 팀의 구성원을 이름이 뜻하는 역할대로, 현재 체크아웃에서 단계별로 실행합니다.

```text
explore? -> plan -> implement -> test? -> verify? -> review? -> fix? -> review?
```

각 단계는 평범한 서브에이전트라서 에이전트 트리에 그대로 보입니다. 어느 단계를 누가 맡는지는 모델이 아니라 로스터가 정합니다.

| 단계 | 담당 | 해당자가 없으면 |
|---|---|---|
| explore | `explore`, 없으면 `explorer` | 건너뜀 |
| plan | `architect`, 없으면 `planner` | 리드가 직접 계획 |
| implement | `executor` | 명령이 중단되고 이유를 알려줌 |
| test | `test-engineer` | 건너뜀 |
| verify | `verifier` | 건너뛰고 보고서에 사유를 남김 |
| review | `critic`, 없으면 `reviewer` | 건너뜀 |

### 어느 팀이 실행할지 고르기

`/team "<task>"`는 프로젝트의 활성 팀을 씁니다. 이번 한 번만 다른 팀으로 돌리려면 이름을 앞에 적으세요.

```text
/team external "파서 모듈 세 개에 docstring 추가"
```

이름은 프로젝트 팀이든 `settings.json`의 전역 팀이든 됩니다. 실행해도 프로젝트의 활성 팀은 그대로입니다. 모르는 이름을 적으면 실제로 있는 팀 목록으로 답합니다.

`/team list`는 모든 팀과 출처, 각 구성원이 맡는 단계를 보여줍니다. 어느 단계에도 맞지 않는 구성원도 조용히 버리지 않고 이름을 알려줍니다. `/team use <name>`은 활성 팀을 바꾸고, `/team use none`은 활성 팀을 해제해 위임 제한을 없앱니다.

같은 목록은 `agent.list`로도 옵니다. 팀마다 `kind: "team"` 행 하나에 `active`, `source`(`global`/`project`), `agents`, `stages`, 가이드가 있으면 `hasGuide`가 실립니다. implement 담당이 없는 팀은 `stages`가 빈 채로 나오므로, 선택 UI가 이유와 함께 보여줄 수 있습니다.

### 팀 가이드: 페르소나와 라우팅

**팀 가이드**는 이 팀이 어떻게 일하는지, 어떤 일을 어떤 에이전트에게 맡길지 적는 마크다운 파일입니다. 위치는 다음과 같고, 프로젝트 파일이 전역보다 우선합니다.

```text
<workdir>/.snowpea/teams/<team>.md
~/.snowpea/teams/<team>.md
```

`default`는 활성 팀이 없을 때(`/team use none`)만 쓰입니다. 활성 팀에 가이드가 없으면 `default`로 **대체하지 않습니다**.

예시:

```markdown
---
description: 플랫폼 전달 팀
---
# Persona
플랫폼 팀으로 일합니다. 작은 diff를 선호하고, 보고 전에 프로젝트 검사를 실행합니다.

## Routing
- DB 마이그레이션, SQL, 스키마 변경 -> sql-reviewer
- UI, 스타일, 접근성 -> designer
- 그 외 -> executor
```

명령:

```bash
/team guide
/team guide <team>
/team guide set <team|default> [--global] <persona...>
/team guide route <team|default> [--global] <agent> <when...>
/team guide unroute <team|default> [--global] <agent|index>
/team guide delete <team|default> [--global]

snowpea team guide show [team]
snowpea team guide set <team> <persona...> [--global]
snowpea team guide route <team|default> <agent> <when...> [--global]
snowpea team guide unroute <team|default> <agent|index> [--global]
snowpea team guide delete <team> [--global]
```

어디에 붙는지:

- **리드**(메인 세션, pipeline의 explore/plan/review/verify)에는 페르소나와 **Who does what** 라우팅 표가 들어갑니다.
- **워커**(위임된 자식, pipeline의 implement/test/fix)에는 페르소나와 팀 이름 한 줄만 들어가고 라우팅 표는 없습니다.

`/team create` 후에는 `/team guide set <name> …`로 페르소나를 추가하세요. RPC는 `team.guide.get/set/delete/list`, 변경 시 `teams.changed` 이벤트를 씁니다.

여기에는 worktree가 없으므로 작업을 파일 단위로 갈라놓아야 합니다. plan 단계가 각 태스크가 건드릴 파일을 적고, 같은 파일을 주장하는 태스크는 시작 전에 하나로 합쳐지며, 파일이 겹치지 않는 태스크만 `agents.max_concurrent`까지 함께 돕니다. 파일을 하나도 적지 않은 태스크는 혼자 실행됩니다.

test·verify·review 단계는 각각 명시적인 한 줄로 답합니다 — `TESTS: PASS`/`TESTS: FAIL`, `VERIFY: PASS`/`VERIFY: FAIL`, `VERDICT: APPROVE`/`VERDICT: REQUEST_CHANGES`. 그 줄이 없거나, 응답이 비었거나, 승인이 거부됐거나, 도구 호출 근거 없이 승인만 적힌 보고는 `NEEDS_MORE_EVIDENCE`로 처리되며 통과가 아닙니다. 최종 보고서는 테스트가 실제로 PASS이고 리뷰가 실제로 APPROVE일 때만 `Nothing was left unfinished.`라고 적고, 그렇지 않으면 무엇이 남았는지 나열하며 헤드리스 실행은 실패로 끝납니다.

`REQUEST_CHANGES` 판정은 해당 코드를 쓴 에이전트의 수정 1회와 재리뷰 1회를 부릅니다. 그래도 변경을 요구하면 실행이 끝나고 무엇이 남았는지 보고합니다 — 3회차는 없습니다.

설정은 셋입니다.

```json
{ "team": { "pipeline": { "maxTasks": 8, "review": true, "test": false } } }
```

`maxTasks`는 계획의 태스크 수 상한입니다. `review`와 `test`는 로스터에 담당자가 있으면 켜집니다. 담당자가 있어도 끄려면 `false`로 두세요.

### 단계별 핸드오프와 감사 추적

파이프라인의 각 단계는 10~20줄 분량의 구조화된 핸드오프 블록(````handoff ... ````)을 남깁니다:
- `Decided`: 해당 단계에서 결정한 사항
- `Files touched`: 수정하거나 검사한 파일
- `Findings`: `path:line` 근거가 포함된 구체적 발견 사항
- `Remaining`: 남은 작업 또는 "nothing"
- `Risks`: 잠재적 위험이나 주의사항

펜스가 없으면 앞 20줄을 유지합니다. 핸드오프는 디스크에 저장되어 감사 추적으로 남습니다:

```text
<workdir>/.snowpea/handoffs/<team-run-id>/<stage>.md
```

다음 단계의 브리프는 이전 단계들의 모든 핸드오프를 원문 그대로 전달받으며, 최대 20,000자로 제한된 `git diff --stat` 및 `git diff`를 함께 받습니다(초과 시 캐시 파일로 스필되어 포인터로 안내). 실행이 끝나면 요약 보고서에 모든 핸드오프 파일 경로와 단계별 라운드/예산/토큰 사용량 표가 출력됩니다.

## 툴 라운드 예산과 Grace Call

토큰 과다 소모를 방지하기 위해 서브에이전트와 파이프라인 단계는 역할별 툴 라운드 예산을 기준으로 동작합니다.

| 역할 / 에이전트 | 기본 툴 라운드 |
|---|---|
| `explore`, `explorer` | 8 |
| `reviewer`, `critic` | 16 |
| `test-engineer` | 15 |
| `verifier` | 14 |
| `architect` | 10 |
| `executor` 및 기타 서브에이전트 | 80 (하한; `agent.max_tool_rounds`가 더 크면 그대로) |

### 미완료 재발행

자식이 `reason: budget`, `timeout`, 또는 빈 `error`로 끝나면 `SubagentManager.run`이 `agents.incompleteRetries`회(기본 **1**)까지 이전 보고서·마지막 툴 호출을 담은 continuation 브리프로 **새 자식 턴**을 다시 띄웁니다. 같은 라운드 카운터를 늘리는 것이 아니라 Hermes/OMC처럼 미완료 작업을 재발행하는 방식입니다. `0`이면 끕니다. `interrupted`·권한 거부는 재발행하지 않습니다.

### 역할 배정

`agent`를 생략하면 런타임이 역할을 고릅니다:

1. `prefer=(...)` 전문 역할 (팀 로스터 우선, 그다음 빌트인)
2. `agents.generalAgent` (기본 `executor`)
3. `agents.missingRole`: `general` (2에서 끝), `anonymous` (이름 없는 자식), `parent` (`reason: parent` — 메인 에이전트가 직접 수행, 재위임 금지)

`/deepinit`은 2단계입니다. `explorer`/`explore`가 디렉터리를 읽고(쓰기 없음) 노트를 남기면, `writer`/`executor`가 그 노트로 `AGENTS.md`를 씁니다. 루트는 `architect`(없으면 explorer)가 개요를 잡은 뒤 같은 쓰기 역할이 파일을 만듭니다. 쓰기 단계에서는 읽기 전용 generalist로 떨어지지 않습니다. `/ultrawork`·`/ralph`는 `executor`를 선호합니다.

### 라운드 예산 설정 우선순위

예산은 다음 우선순위에 따라 결정됩니다:
1. `agents.maxToolRoundsBy.<role>` (예: `{ "agents": { "maxToolRoundsBy": { "explore": 10 } } }`)
2. `settings.json`의 `agents.maxToolRounds` 전역 스칼라 설정
3. 에이전트 정의 프론트매터의 `max_tool_rounds`
4. 위의 내장 역할 기본값
5. 폴백: `agent.max_tool_rounds`, 위임된 자식은 80 하한

### 예산 소진 시 무도구 Grace Call

Hermes Agent 모델에 따라, 서브에이전트가 툴 라운드를 모두 소진(`rounds_left <= 0`)하더라도 갑자기 종료되지 않습니다. 에이전트 루프는 툴이 제공되지 않는 1회의 마지막 "grace" 모델 호출(`_budget_report`)을 수행하여, 모델이 작업 내용, 발견 사항, 남은 작업을 요약하도록 유도합니다. 이 결과는 `budget` 사유와 `done` 상태로 안전하게 반환됩니다.


## 보고서는 무엇이고 무엇이 아닌가

자식의 보고는 자기 보고입니다. 자기가 했다고 믿는 것이지, 실제로 일어난 일은 아닙니다. 세션 바깥에 효과가 남는 일 — 파일 쓰기, 업로드, 외부 호출 — 은 브리프에서 핸들(경로, URL, id)을 받아 오게 하고, 됐다고 말하기 전에 직접 확인하세요.

에이전트가 읽는 결과는 세 줄로 시작합니다.

```text
status: done
reason: budget
roundsUsed: 40
```

`reason`은 자식이 왜 멈췄는지입니다. `complete`는 끝난 것, `budget`과 `timeout`은 라운드나 시간이 떨어져 보고가 일부라는 뜻이고, `error`, `interrupted`, `denied`는 말 그대로입니다. 일부만 담긴 보고도 쓸모가 있습니다. 끝난 것을 취하고 남은 것만 다시 위임하는 것이 맞고, 같은 작업을 그대로 다시 보내는 것은 틀립니다. 긴 보고는 머리와 꼬리만 남기고 잘리며, 전문이 저장된 경로가 함께 붙어 `read_file`로 다시 읽을 수 있습니다.

## 동시 실행

`agents.max_concurrent`(기본 3)가 한 부모가 동시에 굴리는 자식 수를 정합니다. 프로젝트의 `.snowpea/settings.json`이 이를 낮추거나 올리고, `session.create(maxConcurrent)`가 한 세션에 한해 둘 다 덮어씁니다. 한도를 넘는 자식은 실패하지 않고 줄을 섭니다.

`/ultrawork <task>`는 작업을 병렬 서브태스크로 쪼갭니다. 분할기는 서브태스크마다 겹치지 않는 파일 목록을 붙이도록 요구받고, 그 결과는 검사를 거칩니다. 같은 파일을 주장하는 서브태스크는 실행 전에 하나의 브리프로 합쳐지고, 합쳐졌다는 사실이 출력에 남습니다.

```bash
snowpea agents --json
```

는 지금 대기 중이거나 돌고 있는 자식을 보여 줍니다.

## 자식 컨텍스트 다이어트

위임된 자식(`delegate_task`)은 빈 대화 기록과 작업 설명이 적힌 브리프로 시작합니다. 자식에게 부모의 전체 프롬프트 — 기억 회상 블록, 전체 스킬 색인, 트리의 모든 중첩 `AGENTS.md` — 를 넘겨주면 라운드마다 수십만 토큰의 입력 비용이 낭비됩니다.

`agents.childContext` 설정이 컨텍스트 다이어트를 제어합니다.

| 설정 | 기본값 | 선택값 | 하는 일 |
|---|---|---|---|
| `agents.childContext` | `"lean"` | `"lean"`, `"full"` | `"lean"`은 위임된 자식에서 기억 회상 블록, 스킬 색인, 중첩 지시 파일을 제거하고 읽기 전용 자식 툴을 축소합니다. `"full"`은 부모와 동일한 프롬프트를 복원합니다. |

`"lean"` 모드에서 동작 방식:
- **기억 블록 및 가이드 생략**: 자식은 기억 회상 블록과 가이드 라인을 받지 않습니다(기억 관련 툴 자체는 허용된 경우 정상 호출 가능).
- **스킬 색인 생략**: 설치된 스킬 목록이 프롬프트에 나열되지 않으나, `skill_view`를 통해 이름으로 직접 스킬을 읽는 것은 언제든 가능합니다.
- **루트 지시 파일만 제공**: 루트의 `AGENTS.md` 또는 `CLAUDE.md`만 사전에 로드되며, 중첩 지시 파일은 툴이 해당 디렉터리를 건드릴 때 온디맨드로 첨부됩니다.
- **더 작은 eager 툴 세트**: 읽기 전용 자식(`explore`, `reviewer`)은 `read_file`, `grep`, `glob`, `tool_search`로 시작합니다. 그 외 툴은 지연(deferred)되어 필요 시 온디맨드로 로드됩니다.
- **보존되는 요소**: 자식의 정의 프롬프트, 역할(role), 툴 라운드 예산 안내(`BUDGET_LINE`)는 항상 그대로 유지됩니다.

# 툴과 컨텍스트 예산

매 모델 라운드마다 세션의 프롬프트 — 시스템 지침, 툴 정의, 프로젝트 컨텍스트 파일, 대화 기록 — 가 다시 전송됩니다. 긴 턴이나 멀티 에이전트 워크플로에서는 쓰지도 않는 수십 개의 툴 스키마를 매번 다시 보내거나, 동일한 파일을 반복해서 읽거나, 수백 줄의 이전 툴 출력을 계속 싣고 다니면서 이미 얻은 답에 수십만 개의 입력 토큰을 낭비하기 쉽습니다.

snowpea는 이를 해결하기 위해 다섯 가지 협력 메커니즘을 제공합니다: 루프와 중복 읽기를 차단하는 **반복 방지(repeat guard)**, 나가는 요청에서 오래된 툴 결과를 줄이는 **출력 정리(pruning)**, `tool_search`를 통해 필요할 때 스키마를 로드하는 **지연 로딩 툴(deferred tools)**, 위임된 서브에이전트를 위한 **자식 컨텍스트 다이어트**, 그리고 **계층적 컨텍스트 파일 한도**.

## 이미지

세션 모델이 비전을 지원할 때 이미지는 세 가지 경로로 전달됩니다: 붙여넣기·`/attach`, 프롬프트의 `@경로` 참조, `view_image`(및 이미지 블록을 돌려주는 MCP 툴). 모두 같은 첨부 저장소·20MB 한도·축소 규칙을 씁니다. `view_image`나 MCP 이미지가 성공하면 에이전트 루프가 사진을 실은 사용자 메시지를 추가하고, 툴 줄은 짧은 텍스트 요약(`image attached: …` 또는 `N image(s) attached`)만 남기며 base64는 넣지 않습니다. `.png`·`.jpg`에 `read_file`을 쓰면 `view_image` 안내 한 줄만 돌아옵니다. 이미지를 볼 수 없는 모델에서는 `view_image`가 거부되고, `@`·MCP 이미지는 이미지 블록 대신 나가는 요청에 텍스트 표시로 대체됩니다.

## 툴 확인하기

```bash
snowpea tools list
snowpea tools list --json
```

`snowpea tools list`는 등록된 모든 툴의 이름, 권한 태그, 설명을 출력합니다. JSON 출력(`--json`)에서는 각 항목에 `"deferred": true|false` 필드가 포함되어 매 라운드마다 전체 스키마가 전송되는지, 아니면 필요 시 로드되는지 확인할 수 있습니다.

## 반복 방지 (repeat guard)

몇 라운드 전에 읽은 파일을 잊어버린 모델이 같은 파일을 다시 읽거나, 막혔을 때 동일한 검색 명령을 계속 실행하는 경우가 있습니다. 반복 방지기(`tools/repeat_guard.py`)는 이러한 불필요한 호출을 실행 전에 가로채 간결한 스텁(stub)을 돌려주고, 집요한 루프를 차단합니다.

```
unchanged since your earlier read_file of core/app.py (412 lines, sha256 9f2c1ab4); the content in that result is still current
```

가드는 세 단계로 동작합니다:

1. **재읽기 스텁 및 차단 (`read_file`).** 세션별 `(해결된 경로, offset, limit)`을 키로 기억합니다. 이전 읽기 이후 디스크의 파일 내용 해시가 바뀌지 않았다면, snowpea는 이전 내용이 여전히 최신임을 알리는 한 줄 스텁을 반환합니다. 이러한 스텁이 두 번 나간 뒤 세 번째 반복은 `ok=False` 및 오류 코드 `repeat_blocked`와 함께 거부됩니다. 해당 경로에 쓰기(`write_file`)나 편집(`edit_file`)이 일어나거나 디스크에서 내용이 수정되면 기록된 키가 지워져 다음 읽기는 정상 실행됩니다.
2. **연속 호출 및 동일 결과 반복.** 어떤 툴이든 정확히 같은 인자로 연달아 세 번 호출되면 결과 뒤에 경고 줄이 붙습니다. 네 번째 연속 호출은 `repeat_blocked`로 거부됩니다. 사이에 다른 툴 호출이 끼어들면 연속 횟수는 초기화됩니다. 스캔 툴(`shell`, `grep`, `glob`, `list_dir` 및 MCP 툴)의 경우 *출력 텍스트*도 비교합니다: 직전 호출과 완전히 같은 텍스트를 내놓으면 두 번째 호출부터 스텁(`same result as your earlier grep call (N lines)`)으로 대체되며, 네 번째 호출은 거부됩니다.
3. **루프 의심과 `loop.suspected` 이벤트.** 최근 20번의 `(툴 이름, 인자)` 호출을 롤링 윈도우로 감시합니다. 연속 여부와 무관하게 같은 호출이 이 윈도우 안에 5번 나타나면 결과에 경고 줄이 붙습니다:
   ```
   loop suspected: shell with the same arguments has run 5 times this turn; change approach or finish with what you have
   ```
   동시에 세션은 `{tool, count}` 정보를 담은 `loop.suspected` 이벤트를 발생시킵니다. 이 이벤트는 모델이 안내에 묻히지 않도록 턴당 한 번만 발생합니다.

반복 방지는 기본적으로 켜져 있습니다. 완전히 끄려면 다음과 같이 설정합니다:

```json
{
  "tools": {
    "repeatGuard": false
  }
}
```

## 오래된 툴 출력 정리 (pruning)

긴 세션 동안 테스트 실행, 빌드 로그, 대규모 `grep` 검색 같은 방대한 명령 결과가 대화 기록에 누적됩니다. 2라운드에서 나온 5,000자의 컴파일러 출력을 40라운드까지 매번 그대로 실어 보내면 매 턴마다 토큰 비용이 발생합니다.

`session/compaction.py:prune_old_tool_outputs`는 `agent/agent.py:build_messages` 안에서 모델 제공자에게 요청을 보내기 직전에 실행됩니다:

- **`agent.keepToolRounds`(기본값 `6`)보다 오래된 결과:** 기준보다 오래된 툴 라운드의 결과는 나가는 요청 안에서 한 줄 스텁으로 교체됩니다:
  ```
  [earlier shell output pruned — 8123 chars; re-run the tool if you need it again]
  ```
- **최근 라운드(최근 6라운드 이내):** 그대로 보냅니다. 모델이 아직 참고 중인 결과를 자르면 파일이 잘린 것으로 오해합니다.
- **`skill_view` 본문은 제외:** 스킬은 자체 압축 수명 주기와 재로드 포인터(`[SKILL_PRUNED: …]`)를 가지며, 이는 `skills.protectRecentViews`가 관리합니다.

> [!IMPORTANT]
> **저장된 대화 기록은 절대 변경되지 않습니다.** 잘라내기는 오직 모델 제공자에게 건네는 요청 사본에서만 일어납니다. SQLite 데이터베이스(`state.db`), 세션 재개, 내보내기, 대화 기록 로그에는 툴이 생성한 모든 바이트가 온전히 보존됩니다.

출력 정리 관련 설정:

| 설정 | 기본값 | 하는 일 |
|---|---|---|
| `agent.pruneToolOutputs` | `true` | 나가는 요청에서 오래된 툴 출력을 스텁으로 바꿉니다. `false`로 두면 모든 결과를 있는 그대로 보냅니다. |
| `agent.keepToolRounds` | `6` | 출력을 온전히 유지할 최근 툴 라운드 수. |

## 지연 로딩 툴과 `tool_search`

여러 MCP 서버, 브라우저 툴, 미디어 생성 툴, 시스템 유틸리티가 활성화되면 JSON 매개변수 스키마만으로도 수천 토큰을 차지할 수 있습니다. 매 라운드 모든 스키마를 보내면 현재 턴에서 필요하지 않은 수십 개의 정의까지 모델이 매번 읽어야 합니다.

snowpea는 툴을 매 라운드 전송하는 **즉시 전송(eager)** 세트와 필요할 때 불러오는 **지연 로딩(deferred)** 세트로 나눕니다:

### 즉시 전송 (eager) 세트

코딩 에이전트의 핵심 작업에 필수적인 툴들로 구성됩니다:

- 핵심 툴: `read_file`, `view_image`, `write_file`, `edit_file`, `shell`, `grep`, `glob`, `ask_user`, `delegate_task`, `skill_view`, `set_mode`, 그리고 `tool_search` 자체.
- 읽기 전용 자식 세션(쓰기 권한이 없는 `explore`나 `reviewer` 정의): `read_file`, `grep`, `glob`, `shell`, `tool_search`로 좁혀집니다.
- 세션 모드나 역할이 명시적으로 요구하는 툴(`allowed_tools`) 또는 `tools.eager`에 지정된 툴.

### 모델이 보는 것

그 외의 모든 툴(브라우저 조작, 미디어 생성, git 관련 부가 툴, 그리고 모든 `mcp__*` 툴)은 기본적으로 지연 로딩됩니다. 시스템 프롬프트는 전체 스키마 대신 한 줄의 그룹 요약으로 이들을 표시합니다:

```
Deferred (load with tool_search): browser (4), git (4), media (5), mcp:github (12)
```

요청하기 전까지는 매개변수 스키마나 설명이 프롬프트에 실리지 않습니다.

### `tool_search`로 툴 로드하기

에이전트가 지연 로딩된 툴을 쓰고자 할 때 `tool_search`를 부릅니다:

- **정확한 이름 지정:** `tool_search(query="select:git_diff,git_commit")`
- **키워드 검색:** `tool_search(query="+browser click")` (`+` 접두사는 필수 검색어 지정; 최대 5개 반환)

검색된 툴은 `session.loaded_tools`에 추가되며, 해당 세션의 이후 모든 라운드에서 전체 스키마가 프롬프트에 포함됩니다.

**자동 로딩 지원:** 모델이 `tool_search`를 먼저 부르지 않고 지연 로딩된 툴을 이름으로 직접 호출하더라도, snowpea는 호출을 정상 실행하고 `session.loaded_tools`에 즉시 등록한 뒤 결과에 `loaded <tool> for this session` 안내를 덧붙입니다.

### 지연 로딩 관련 설정

| 설정 | 기본값 | 하는 일 |
|---|---|---|
| `tools.deferred` | `true` | 핵심 툴만 즉시 보내고 나머지는 지연 로딩합니다. `false`로 두면 매 라운드 모든 툴 스키마를 보냅니다. |
| `tools.eager` | `[]` | 기본값과 무관하게 항상 즉시 전송할 툴 이름 목록 (자주 쓰는 특정 MCP 툴 등). |

또한 렌더링된 툴 프래그먼트는 활성 툴 세트, 모드, 역할을 키로 세션별로 캐시됩니다. 내용이 바뀌지 않은 연속 라운드는 바이트 단위로 동일한 문자열을 생성하므로, 모델 제공자의 접두사 캐싱(KV 캐시)이 깨지지 않습니다.

## 자식(서브에이전트) 컨텍스트 다이어트

위임된 서브에이전트(`session.is_subagent=True`)는 명시적인 작업 지시문과 빈 대화 기록으로 시작합니다. 자식 에이전트에게 부모 세션의 장기 기억 요약, 전체 스킬 목록, 깊숙한 하위 지침 파일까지 모두 넘겨주는 것은 위임된 작업과 무관한 컨텍스트 낭비입니다.

`agents.childContext` 설정이 이 동작을 제어합니다:

- **`"lean"` (기본값):**
  1. **메모리 블록 제외:** 부모의 기억 회상 요약과 메모리 지침을 건너뜁니다 (허용된 경우 메모리 툴 자체는 사용 가능).
  2. **스킬 색인 제외:** 프롬프트에서 전체 스킬 목록 색인을 뺍니다. 자식은 필요할 경우 여전히 `skill_view`로 특정 스킬의 본문을 읽을 수 있습니다.
  3. **루트 지침 파일만 제공:** 초기 프로젝트 지침을 루트의 `AGENTS.md` 또는 `CLAUDE.md`로 한정합니다. 하위 패키지의 지침 파일은 처음에 제외되며, 툴이 해당 디렉터리를 실제로 건드릴 때 동적으로 첨부됩니다.
  4. **즉시 전송 툴 축소:** 읽기 전용 서브에이전트는 최소화된 툴 세트(`read_file`, `grep`, `glob`, `shell`, `tool_search`)로 시작합니다.
  5. **유지되는 요소:** 자식의 정의/페르소나 프롬프트, 역할 지침, 그리고 남은 도구 라운드를 알려주는 `BUDGET_LINE`("You have N tool rounds for this task...")은 온전히 유지됩니다.
- **`"full"`:** 부모 세션과 동일한 전체 프롬프트 컨텍스트(메모리 요약, 전체 스킬 색인, 모든 하위 컨텍스트 파일)를 자식에게도 전달합니다.

```json
{
  "agents": {
    "childContext": "lean"
  }
}
```

## 컨텍스트 파일과 한도

프로젝트 지침 파일(`AGENTS.md`, `CLAUDE.md`, `.snowpea/instructions.md`, `.cursorrules`)은 에이전트에게 저장소 규칙을 안내합니다. 하위 디렉터리마다 지침 파일이 있는 모노레포에서는 이 파일들을 무제한으로 불러올 경우 모델의 컨텍스트 창 상당 부분을 차지할 수 있습니다.

snowpea는 개별 파일과 전체 블록에 예산 상한을 적용합니다:

- **파일별 한도 (`agent.contextFileMaxChars`):** 기본값 `None`이며, 모델의 컨텍스트 창 크기에 따라 동적으로 계산됩니다(20,000자 ~ 500,000자 범위). 32k 토큰 이하의 작은 컨텍스트 창에서는 하한선이 20,000자에서 8,000자로 낮아집니다.
- **전체 블록 한도 (`agent.contextFilesMaxChars`):** 기본값 `None`이며, 컨텍스트 창 크기에 따라 `# Project Context` 블록 전체의 글자 수 상한을 동적으로 계산합니다(12,000자 ~ 120,000자 범위).
- **계층적 잘라내기:** 지침 파일들의 총합이 전체 예산을 초과하면, 루트 지침을 온전히 보존하기 위해 깊은 디렉터리의 파일부터 먼저 잘라냅니다. 잘려 나간 파일은 앞부분(70%)과 뒷부분(20%)을 남기고 가운데에 안내 표시를 넣습니다:
  ```
  …[truncated: 4120 more chars; read src/client/AGENTS.md for the rest]
  ```
- **컨텍스트 파일 무시:** `agent.ignoreContextFiles: true`로 설정하면 프로젝트 지침 파일을 전혀 읽지 않습니다. 기본 상태에서 문제를 재현할 때 유용합니다.

```json
{
  "agent": {
    "contextFileMaxChars": 20000,
    "contextFilesMaxChars": 60000,
    "ignoreContextFiles": false
  }
}
```

## 언제 설정을 조정해야 하는가

이 최적화들은 토큰을 절약하고 무한 루프를 방지하기 위해 기본으로 켜져 있지만, 작업 방식에 따라 조정하거나 꺼야 할 때가 있습니다:

- **`tools.repeatGuard` 끄기 (`tools.repeatGuard: false`):** `shell`로 백그라운드 프로세스 상태를 주기적으로 폴링하는 스크립트를 실행하거나, 파일 수정 시각이나 출력이 바뀌지 않는 상태에서 동일한 툴을 반복 호출해야 하는 테스트 작업을 수행할 때.
- **`tools.deferred` 끄기 (`tools.deferred: false`) 또는 `tools.eager` 추가:** `tool_search`를 스스로 호출해 도구를 찾아내는 능력이 부족한 소형 모델을 쓰거나, 특정 MCP 서버의 툴을 작업 내내 빈번하게 호출해야 할 때.
- **`agent.pruneToolOutputs` 끄기 (`agent.pruneToolOutputs: false`) 또는 `agent.keepToolRounds` 늘리기:** 세션 초반에 실행한 방대한 컴파일러 경고나 테스트 추적 로그의 특정 줄을 여러 턴이 지난 뒤에도 명령을 재실행하지 않고 원문 그대로 참조해야 하는 복잡한 디버깅 작업일 때.
- **`agents.childContext: "full"` 설정:** 자식 에이전트가 부모의 전역 장기 기억을 모두 참조해야 하거나, 설치된 모든 스킬을 처음부터 즉시 파악해야 하는 자율 연구/설계 작업을 맡았을 때.
- **`agent.contextFilesMaxChars` 조정:** 하위 패키지의 지침 파일에 필수적인 빌드 및 린트 규칙이 들어 있어 일부라도 잘려서는 안 되는 대규모 모노레포일 때.

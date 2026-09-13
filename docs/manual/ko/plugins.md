# 플러그인과 스킬

snowpea는 Claude Code 플러그인 배치를 그대로 읽습니다. Claude Code용으로 쓰인 플러그인은 수정 없이 여기서 설치되고 돌아가며, 이미 `.claude/` 디렉터리가 있는 저장소는 아무것도 바꾸지 않아도 그대로 동작합니다.

## 배치

```
my-plugin/
  plugin.json            # {"name": "...", "version": "...", "description": "..."}
  skills/<name>/SKILL.md # 스킬 하나, /<name> 명령이 됨
  agents/*.md            # 에이전트 정의, delegate_task 대상으로 쓸 수 있음
  commands/*.md          # 순수 마크다운 명령
  hooks/hooks.json       # PreToolUse / PostToolUse / Stop
  .mcp.json              # 이 플러그인이 가져오는 MCP 서버
```

`plugin.json` 대신 `.claude-plugin/plugin.json`도 받아들입니다. 마켓플레이스 저장소는 루트에 `plugins: [{name, source}]`를 나열하는 `marketplace.json`을 둡니다.

## 설치

```bash
snowpea skill install ./my-plugin
snowpea skill install https://github.com/someone/their-plugin.git
snowpea skill install oh-my-claudecode
snowpea skill list --json
snowpea skill remove my-plugin
```

설치는 플러그인을 `$SNOWPEA_HOME/plugins/<name>`에 복사하고 레지스트리를 재적재합니다. 재적재는 `commands.changed` 알림을 내보내므로, TUI는 재시작 없이 팔레트를 새로 고칩니다.

TUI 안에서 에이전트에게 부탁해도 됩니다. "pdf 관련 스킬 찾아줘"라고 하면 `skill_search`를 호출해 후보와 각각의 install spec을 보여주고, "두 번째 거 설치해줘"라고 하면 그 spec으로 `skill_install`을 호출합니다. `skill_list`는 이미 설치된 것을, `skill_remove`는 플러그인 삭제를 담당합니다. 검색과 목록은 `read` 툴이라 묻지 않고 바로 실행되고, 설치와 삭제는 `exec`이라 accept 모드에서는 먼저 물어보고 plan 모드에서는 거부됩니다 — 승인하지 않은 설치는 일어나지 않습니다. 설치하면 그 자리에서 재적재되고, 에이전트가 새로 생긴 `/commands`·에이전트·MCP 서버를 알려줍니다.

## 검색

```bash
snowpea skill search "pdf"
snowpea skill search "code review" --json
```

두 출처가 함께 조회됩니다: `claude-marketplace`(등록된 모든 마켓플레이스 저장소의 `marketplace.json`)와 `registry.snowpea.ai`에 호스팅되는 레지스트리 — 이 레지스트리 자체가 다른 스킬 허브들을 **연합(federate)**하므로, 한 번의 호출로 여러 곳의 결과가 함께 돌아올 수 있습니다. 각 결과는 실제로 나온 `source`를 달고 나옵니다: `local`(레지스트리에 직접 배포된 스킬), `clawhub`(ClawHub), `claude-marketplaces`(레지스트리가 미러링하는 GitHub 기반 Claude Code 마켓플레이스) — `snowpea skill search` 출력에는 사람이 읽을 라벨(예: `ClawHub`)로 보입니다. 한 허브, 또는 레지스트리 전체가 응답하지 않아도 검색 전체가 실패하지 않고 그만큼만 빠집니다 — 어떤 허브가 살아 있는지는 `snowpea skill sources`로 확인하세요.

```bash
snowpea skill search "planning" --source registry
snowpea skill sources
```

`--source registry`(또는 같은 뜻인 `--source snowpea`)는 레지스트리가 연합하는 모든 허브의 결과만 남깁니다. `snowpea skill sources`는 각 허브의 id, 라벨, 활성/비활성 상태(비활성이면 이유), 스킬 수, 마지막 동기화 상태를 보여줍니다.

등록된 마켓플레이스는 `$SNOWPEA_HOME/marketplaces.json`에 있고, oh-my-claudecode 마켓플레이스가 기본으로 들어 있습니다.

> 이전 버전에 있던 agentskills.io/hermes-hub 어댑터는 제거되었습니다:
> agentskills.io는 스킬 목록 API가 없는 Agent Skills **명세** 사이트였고,
> hermes-hub.ai는 아예 도메인이 풀리지 않았습니다. 둘 다 여기서 추측해서
> 만드는 대신, 레지스트리의 연합 쪽에서 이유와 함께 비활성 상태로 처리됩니다.

## 레지스트리에 배포하기

```bash
snowpea skill install registry:ralplan          # id로 내려받아 설치
snowpea skill install clawhub:@cua/driver       # 연합된 다른 허브의 스펙도 그대로 동작
snowpea setup tools                              # 배포자 토큰을 한 번 저장(마스킹 입력)
snowpea skill publish ./my-skill                 # 압축 + 검증 + 업로드
snowpea skill rate ralplan 5 --comment "좋아요"   # 1-5점, 호출자당 하나
```

`skill install`은 검색 결과가 내놓는 어떤 설치 스펙이든 받아들입니다 — 로컬에 배포된 스킬이면 `registry:<id>`, 연합된 허브의 스펙이면 `clawhub:<id>`나 `github:<owner>/<repo>[@plugin]` 같은 형태이며, 모두 레지스트리 자체의 다운로드 프록시로 풀립니다. `github:` 스펙은 그 허브에 내려받을 아카이브가 없어 레지스트리가 처리하지 못할 때(501) 순수 `git clone`으로 대체됩니다. 다른 연합 스펙은 이런 대체 수단이 없어 레지스트리가 알려준 이유가 그대로 나타납니다.

`publish`는 `<dir>/SKILL.md`를 읽어 프런트매터를 로컬에서 먼저 검사합니다
(`name`은 `^[a-z0-9][a-z0-9._-]{1,63}$`를 만족해야 하고, `description`은
8-500자여야 합니다 — 레지스트리가 서버 쪽에서 강제하는 것과 같은 규칙입니다).
그다음 디렉터리를 압축하고(`.git`, `__pycache__`, `node_modules` 등 빌드
찌꺼기는 제외) `Authorization: Bearer <token>`으로 업로드합니다. 토큰은
`--token`, `SNOWPEA_REGISTRY_TOKEN` 환경변수, `settings.skills.registry.token`
(`snowpea setup tools`의 마스킹 입력으로 한 번 저장) 순서로 찾습니다. 다시
배포하기 전에는 프런트매터의 `version`을 올리세요 — 레지스트리는 이미 본
버전을 거부합니다. `/skill publish <dir>`는 실행 중인 세션 안에서 같은
일을 하며, 상대 경로는 세션의 작업 디렉터리 기준으로 풀립니다.

레지스트리 기본 주소는 `https://registry.snowpea.ai/v1`이고, 호출마다
`--registry <url>`로, 셸 전체에는 `SNOWPEA_REGISTRY_URL`로, 영구적으로는
`settings.skills.registry.url`로 바꿀 수 있습니다.

## SKILL.md

front matter는 [agentskills.io](https://agentskills.io) 표준을 따릅니다.

```markdown
---
name: changelog
description: Write a release changelog from the commits since the last tag.
argument-hint: "<tag>"
allowed-tools: [git_log, git_diff, read_file, write_file]
---

Collect the commits since $ARGUMENTS. Group them by type, drop noise,
and write CHANGELOG.md with the newest release on top.
```

본문은 `/changelog` 명령이 됩니다. `$ARGUMENTS`는 명령 뒤에 붙은 텍스트로 치환되고, 본문이 지시문으로 주입되며, 턴이 시작됩니다. `allowed-tools`는 그 턴 동안 강제되어, 읽기 툴만 나열한 스킬은 모드가 무엇을 허용하든 쓸 수 없습니다. 생략하면 스킬은 세션의 평소 툴셋을 그대로 받습니다. `user-invocable: false`는 스킬을 명령 목록에서 빼면서도 에이전트가 쓸 수 있게는 남겨 둡니다.

## 에이전트 정의

```markdown
---
name: reviewer
description: Reviews a diff for correctness and missing tests.
model: inherit
tools: [read_file, grep, git_diff]
permission: plan
max_turns: 12
---

You review changes. Be specific, cite file and line, and say APPROVE or
list what must change. Never edit files yourself.
```

`<project>/.snowpea/agents/reviewer.md`에 이렇게 넣거나, `/agent create "reviews diffs for missing tests"`가 대신 써 주게 하세요. 어느 쪽이든 바로 `delegate_task` 대상이 되고, `snowpea agents --json`에 나열됩니다.

## 훅

`hooks/hooks.json`은 Claude Code와 같은 모양을 씁니다.

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "shell|write_file", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/guard.sh"}]}
    ]
  }
}
```

이 명령은 stdin으로 `{event, tool_name, tool_input, session_id, cwd}`를 JSON으로 받고, 환경변수로 `SNOWPEA_HOME`과 `SNOWPEA_TOOL_NAME`을 받습니다. `PreToolUse`는 권한 판정 이후, 툴 실행 직전에 돕니다. 종료 상태 2는 호출을 막고 그 stderr가 모델이 보는 에러가 되므로, 턴은 죽지 않고 거부로 이어집니다. `PostToolUse`는 툴이 값을 돌려준 직후에, `Stop`은 턴이 더 이상 툴 호출 없이 끝날 때 돕니다. 다른 훅 이벤트는 파싱만 되고 무시됩니다.

`${CLAUDE_PLUGIN_ROOT}`, `${SNOWPEA_PLUGIN_ROOT}`, `${SNOWPEA_PYTHON}`은 중괄호가 있든 없든 훅과 MCP 명령 안에서 확장됩니다.

## MCP 서버

`.mcp.json`은 Claude Code 형식을 쓰고 `$SNOWPEA_HOME`, 프로젝트 디렉터리, 그리고 설치된 플러그인 각각에서 읽힙니다.

```json
{
  "mcpServers": {
    "notes": {"command": "${SNOWPEA_PYTHON}", "args": ["-m", "my_notes_server"]},
    "remote": {"url": "https://example.internal/mcp"}
  }
}
```

서버는 지연 시작되고 데몬이 사는 동안 캐시되며, 그 툴은 `mcp__<server>__<tool>`로 나타납니다.

```bash
snowpea tools list --json
```

권한은 서버마다 기본값이 `network`이고, 설정의 `mcp.permissions` 아래에서 덮어쓸 수 있습니다.

## 어디서 찾고, 누가 이기는가

루트는 다음 순서로 훑고, 이름이 겹치면 나중 것이 이깁니다.

1. 내장 — `core/snowpea_core/builtin_skills/`
2. 전역 — `$SNOWPEA_HOME/{skills,agents,commands}/`
3. 플러그인 — `$SNOWPEA_HOME/plugins/*/`
4. 프로젝트 — `<project>/.claude/{skills,agents,commands}/`, 그다음 `<project>/.snowpea/{skills,agents,commands}/`

그래서 프로젝트 스킬은 같은 이름의 플러그인 스킬을 덮어쓰고, 플러그인은 내장을 덮어씁니다. 로더는 각각이 어디서 왔는지 기록하고, `skill list`가 그것을 보여줍니다.

## 다음

[스케줄러](scheduler.md) — 자리를 비운 동안 작업을 돌리기.

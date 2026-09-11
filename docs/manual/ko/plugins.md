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

## 검색

```bash
snowpea skill search "pdf"
snowpea skill search "code review" --json
```

세 출처가 함께 조회되고, 각 결과는 자신이 나온 `source`를 달고 나옵니다: `claude-marketplace`(등록된 모든 마켓플레이스 저장소의 `marketplace.json`), `agentskills.io`, `hermes-hub`. 한 출처가 실패해도 검색 전체가 실패하지 않고 그 출처만 아무것도 기여하지 않습니다. 결과의 설치 스펙을 그대로 `skill install`에 넣으면 됩니다.

등록된 마켓플레이스는 `$SNOWPEA_HOME/marketplaces.json`에 있고, oh-my-claudecode 마켓플레이스가 기본으로 들어 있습니다.

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

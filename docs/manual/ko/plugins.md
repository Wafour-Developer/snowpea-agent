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

## 스킬 만들기

```bash
/skill create <name> "<할 일>"
/skill create pdf-merge "pdftk로 PDF를 합치되, 합치기 전에 각 파일이 유효한지 먼저 확인" --global
/skill create notes "세션을 릴리즈 노트로 요약" --force
```

한 번의 provider 턴이 완전한 `SKILL.md`를 씁니다 — frontmatter(`name`, `description`, `version`)에 "When to use this" / "Procedure" / "Inputs" / "Checks" 본문까지, `/skill learn`이나 직접 설치한 플러그인이 쓰는 것과 같은 agentskills.io / Claude Code 형식입니다 — 그리고 아무것도 쓰기 전에 데몬이 frontmatter를 스스로 검증합니다(`/skill publish`가 돌리는 것과 같은 검사). 결과는 `<project>/.snowpea/skills/<name>/SKILL.md`에 놓이고, `--global`을 주면 대신 `$SNOWPEA_HOME/skills/<name>/SKILL.md`에 써서 모든 프로젝트에서 쓸 수 있게 합니다. 대상에 이미 `SKILL.md`가 있으면 `--force` 없이는 그대로 두고 손대지 않습니다. plan 모드는 무엇을 만들거나 덮어쓸지만 보고하고 아무것도 쓰지 않습니다. 파일이 놓이는 즉시 레지스트리가 재적재되므로 `/<name>`이 바로 동작합니다 — 설치와 마찬가지로 재시작이 필요 없습니다.

`/skill learn [name]`은 또 다른 생성기입니다: 브리프 대신 방금 끝낸 세션을 같은 레이아웃의 `SKILL.md`로 요약합니다.

데스크톱 앱의 편집기는 `skill.create`(직접 쓴 문서를 저장하려면 `content`를, 같은 생성 턴을 돌리려면 `description`을 넘김 — 기존 `sessionId`를 함께 주면 같은 `workdir`에 뿌리내린 그 세션에서 돌리고, 주지 않으면 데몬이 headless 세션을 새로 엽니다; 어느 쪽이든 `{turnId, sessionId}`를 돌려줍니다), `skill.read`, `skill.write`를 씁니다 — 폼 기반 스킬 편집기를 위한 같은 경로의 RPC 버전입니다.

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

`skill install`은 검색 결과가 내놓는 어떤 설치 스펙이든 받아들입니다. `registry:<id>`(로컬에 배포된 스킬)와 `clawhub:<id>`는 레지스트리 자체의 다운로드 프록시로 풀립니다. `github:<owner>/<repo>[@plugin]`(레지스트리가 미러링하는 Claude Code 마켓플레이스 항목에 대해 돌려주는 형태)은 대신 곧바로 `git clone`됩니다 — 이 스펙은 레지스트리 id와 같은 문자열이 아니라서, 레지스트리에 다운로드를 요청하면 항상 404가 나기 때문입니다. `@plugin`이 없으면 저장소 전체를 클론하고, 있으면 그 플러그인의 디렉터리만 설치합니다 — 같은 저장소를 가리키는 로컬에 등록된 마켓플레이스에서, 그마저 없으면 GitHub의 저장소 자체 `marketplace.json`에서 그 위치를 찾습니다. 결과의 `id`(`skill search --json`에 표시됨)를 그대로 쓸 수도 있습니다 — 로컬에 배포된 사본을 id로 바로 설치하려면 `skill install registry:<id>`.

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

### JSON을 직접 고치지 않고 추가하기

세션 안에서는 `/mcp`, 셸에서는 `snowpea mcp`가 같은 일을 합니다. 둘 다 같은 `.mcp.json`에 쓰므로, 손으로 고친 파일과 snowpea가 쓴 파일은 같은 파일입니다.

```bash
snowpea mcp list
snowpea mcp add notes -- python -m my_notes_server
snowpea mcp add remote --url https://example.internal/mcp --header Authorization=Bearer-xxx
snowpea mcp add github --preset github --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
snowpea mcp test notes
snowpea mcp get notes
snowpea mcp disable notes
snowpea mcp remove notes
snowpea mcp catalog
```

`--` 뒤는 전부 명령과 그 인자이고 argv 그대로 넘어갑니다. snowpea는 그것으로 셸 문자열을 만들지 않으며, 그렇게 하려는 항목(`bash -c …` 같은 서버)은 `--force` 없이는 거부합니다. 같은 검사가 진짜 MCP 서버라면 가질 리 없는 모양도 막습니다 — 코드를 받아서 실행하는 셸 스크립트, `~/.ssh/authorized_keys`·PAM·sudoers·cron·셸 rc 파일에 쓰는 것, 그리고 명령·인자·환경변수 어디에든 들어 있는 알려진 침해 지표(IOC)입니다. `--scope global`은 프로젝트 파일 대신 `$SNOWPEA_HOME/.mcp.json`에 쓰고, `--preset`은 큐레이션된 카탈로그 항목(`snowpea mcp catalog`)에서 시작하며, `--no-test`는 먼저 찔러보지 않고 저장합니다. 기본값은 서버가 `tools/list`에 한 번 답한 뒤에야 파일에 쓰는 것이라, 명령 오타는 파일에 닿기 전에 걸립니다.

세션 안에서는 같은 동사가 `/mcp`, `/mcp add <name> -- <command> [args…]`, `/mcp test <name>`, `/mcp enable|disable <name>`, `/mcp configure <name> [tool…]`, `/mcp reload [name]`, `/mcp catalog`입니다. 터미널 UI에서 인자 없이 `/mcp add`만 치면 폼이 대신 뜹니다. 데스크톱 앱에는 같은 화면이 **설정 → MCP 서버**에 있고, 줄마다 상태 점이 실시간으로 갱신됩니다.

비밀값은 되돌려주지 않습니다. `env`와 `headers`는 사용자가 고른 파일 안에 남고, 목록에는 키 이름만 나옵니다(`TOKEN=•••`). 플러그인이나 설정의 `mcp.servers`가 선언한 서버도 목록에는 나오지만 여기서는 읽기 전용입니다 — 플러그인을 지우거나 설정 파일을 고치세요.

추가·삭제·수정은 즉시 반영됩니다. 옛 프로세스는 멈추고, 그 툴은 레지스트리에서 빠지며, 새 항목은 데몬 재시작 없이 잡힙니다.

## 어디서 찾고, 누가 이기는가

루트는 다음 순서로 훑고, 이름이 겹치면 나중 것이 이깁니다.

1. 내장 — `core/snowpea_core/builtin_skills/`
2. 전역 — `$SNOWPEA_HOME/{skills,agents,commands}/`
3. 플러그인 — `$SNOWPEA_HOME/plugins/*/`
4. 프로젝트 — `<project>/.claude/{skills,agents,commands}/`, 그다음 `<project>/.snowpea/{skills,agents,commands}/`

그래서 프로젝트 스킬은 같은 이름의 플러그인 스킬을 덮어쓰고, 플러그인은 내장을 덮어씁니다. 로더는 각각이 어디서 왔는지 기록하고, `skill list`가 그것을 보여줍니다.

## 다음

[스케줄러](scheduler.md) — 자리를 비운 동안 작업을 돌리기.

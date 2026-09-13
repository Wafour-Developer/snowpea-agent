# 언어 서버

snowpea는 에디터가 쓰는 바로 그 언어 서버와 대화합니다. 파일을 저장하면 에이전트가 타입 에러를 곧바로 보고, 이름을 바꾸기 전에는 `grep`으로 짐작하는 대신 컴파일러에게 "이거 누가 쓰냐"고 물어봅니다.

다른 언어: [English](../en/lsp.md) · [전체 목록](../README.md)

## 무엇이 달라지나

두 가지입니다. 둘 다 설정 없이 바로 동작합니다.

**모든 `write_file`·`edit_file` 결과에 `Diagnostics` 블록이 붙습니다** — 그 파일을 맡는 서버가 떠 있을 때:

```text
replaced 1 occurrence(s) in src/app.py

<diagnostics file="src/app.py">
ERROR [42:5] "userId" is not defined (reportUndefinedVariable)
WARN [3:1] Import "os" is not accessed (reportUnusedImport)
</diagnostics>
```

에이전트는 다음 작업으로 넘어가기 전에 이걸 읽습니다. 오타가 테스트를 돌리는 세 번째 툴 호출에서가 아니라, 만들어진 바로 그 턴에서 잡힙니다.

**툴 일곱 개**가 에이전트에게 열립니다:

| 툴 | 무엇을 답하나 |
|---|---|
| `lsp_diagnostics` | 한 파일의, 또는 지금까지 본 모든 파일의 문제 |
| `lsp_definition` | 이 위치의 심볼이 정의된 곳 |
| `lsp_references` | 이 위치의 심볼을 쓰는 모든 곳, 프로젝트 전체 |
| `lsp_symbols` | 파일 하나의 개요 |
| `lsp_workspace_symbols` | 이름으로 클래스·함수 찾기, 위치 무관 |
| `lsp_hover` | 이 위치 심볼의 타입과 문서 |
| `lsp_rename` | 심볼을 전부 바꾸고 파일에 쓰기 |

`lsp_rename`만 `write`이고 나머지는 전부 `read`입니다. 따라서 plan 모드에서는 다른 쓰기와 똑같이 `lsp_rename`이 거부됩니다.

```bash
snowpea tools list --json
```

## 어떤 서버들

snowpea가 아는 서버는 아래와 같습니다. 그 서버가 맡는 파일이 처음 건드려질 때 해당 파일의 프로젝트 루트에서 하나 띄우고, 10분간 쓰이지 않으면 내립니다.

typescript, deno, vue, eslint, biome, oxlint, gopls, ruby-lsp, pyright, ruff, ty, elixir-ls, zls, csharp, fsharp, sourcekit-lsp, rust, clangd, svelte, astro, yaml-ls, lua-ls, bash, dockerfile, terraform, dart, ocaml-lsp, gleam, clojure-lsp, nixd, prisma, haskell-language-server, julials.

바이너리가 이미 `PATH`에 있을 때만 씁니다. 요청하지 않는 한 아무것도 내려받지 않으며, 서버가 없는 것은 에러가 아닙니다 — 그 언어에 대해 진단이 안 나올 뿐입니다.

`ruff`와 `ty`는 기본으로 꺼져 있습니다. `.py`는 이미 `pyright`가 맡고 있고, 편집할 때마다 파이썬 서버를 둘씩 돌리면 같은 답을 얻는 데 두 배로 기다리게 되기 때문입니다. 쓰려면 `lsp.servers`에 그 id를 적으세요.

## 무엇이 떠 있는지 보기

데몬은 `lsp.status`에 서버마다 한 줄씩 답합니다 — id, 프로젝트 루트, 상태(`starting`·`ready`·`broken`·`stopped`), 떠 있는 동안의 pid. [프로토콜](protocol.md)을 쓰는 클라이언트라면 무엇이든 물어볼 수 있고, 터미널 UI의 `/lsp` 화면과 IDE의 LSP 카드가 이걸 읽습니다.

두 번 죽은 서버는 `broken`으로 보고되고 그 데몬이 사는 동안 다시 시작되지 않습니다. 설치를 고친 뒤 데몬을 다시 띄우세요.

`lsp.status`는 어떤 루트가 실제로 띄운 서버만 압니다. 파일을 먼저 건드리지 않고도 snowpea가 *돌릴 수 있는* 서버 전부를 보려면 `lsp.catalog`를 물어보세요 — 등록된 id마다 한 줄씩, `languageIds`, `extensions`, `installable`(`autoInstall`이 npm·pip·go로 구할 수 있는지) 여부와 손으로 설치할 명령을 담은 `installHint`, 지금 `disabled`인지(기본으로 꺼져 있거나, `lsp.disabled`에 이름이 있거나, `lsp.servers` 오버라이드로 꺼진 경우)를 답합니다. 설정 화면의 LSP 카드가 아무것도 뜨기 전에 보여주는 목록이 바로 이것입니다.

```bash
snowpea daemon stop
snowpea daemon start
```

## 설정

전부 `$SNOWPEA_HOME/settings.json`의 `lsp` 아래에 있고, 재시작 없이 반영됩니다.

```json
{
  "lsp": {
    "enabled": true,
    "autoInstall": false,
    "disabled": ["eslint"],
    "idleTimeoutSec": 600,
    "servers": {
      "clangd": { "command": ["clangd", "--header-insertion=never"] },
      "zig": { "command": ["zls"], "extensions": [".zig"] }
    }
  }
}
```

| 키 | 기본값 | 하는 일 |
|---|---|---|
| `enabled` | `true` | `false`면 툴 일곱 개가 에이전트 목록에서 빠지고 `Diagnostics` 블록도 붙지 않습니다 |
| `autoInstall` | `false` | `true`면 없는 서버를 npm·pip·go로 `$SNOWPEA_HOME/lsp`에 설치합니다 |
| `disabled` | `[]` | 건드리지 않을 서버 id |
| `servers` | `{}` | 새 서버를 정의하거나, 내장 서버의 실행 명령을 갈아끼웁니다 |
| `idleTimeoutSec` | `600` | 쓰이지 않은 서버를 내리기까지의 초; `0`이면 안 내립니다 |

`servers` 항목은 `command`(필수, argv 배열), `extensions`, `rootMarkers`, `env`, `initialization`을 받습니다. 이미 있는 id를 적으면 그 서버의 실행 명령만 바뀌고 나머지 정의는 그대로입니다 — `pyright`를 래퍼 스크립트로 돌리고 싶을 때 쓰는 방법입니다.

전부 끄려면 그 파일에서 `"enabled": false`로 두세요.

## 어떤 언어가 조용할 때

확인할 순서:

1. **바이너리가 `PATH`에 없습니다.** `which pyright-langserver`, `which gopls`. `autoInstall`이 꺼져 있으면 snowpea는 다른 곳을 뒤지지 않습니다.
2. **프로젝트 루트 표식이 없습니다.** 서버마다 편집한 파일에서 위로 올라가며 자기 표식을 찾습니다 — gopls는 `go.mod`, rust-analyzer는 `Cargo.toml`, pyright는 `pyproject.toml`, 자바스크립트 서버들은 락파일. 표식이 없으면 엄격한 서버(rust, deno, biome, oxlint)는 그 파일을 아예 맡지 않습니다.
3. **TypeScript는 워크스페이스 자신의 TypeScript가 필요합니다.** `typescript-language-server`와 `astro-ls`는 `node_modules/typescript/lib/tsserver.js`가 없으면 기동 중에 종료되므로, snowpea는 그게 생기기 전에는 띄우지 않습니다. 설치를 먼저 돌리세요.
4. **아직 뜨는 중입니다.** 편집은 서버가 올라오기를 최대 3초까지만 기다립니다. 차가운 pyright나 rust-analyzer는 그보다 오래 걸리므로, 새 세션의 첫 편집은 블록 없이 돌아오고 그다음 편집부터 붙습니다.
5. **죽었습니다.** `lsp.status`가 `broken`이라고 말합니다.

## 보안

모든 서버는 셸 명령줄이 아니라 인자 배열로 실행됩니다. 파일명이나 설정에 들어 있는 무엇도 명령으로 해석될 수 없습니다. `autoInstall`을 기본값 `false`로 두면 LSP 계층은 어떤 네트워크 요청도 하지 않습니다.

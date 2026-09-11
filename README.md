# snowpea-agent

snowpea는 내 컴퓨터에서 도는 코딩 에이전트입니다. Python 코어가 데몬으로 세션·툴·권한을
관리하고, Ink로 만든 터미널 UI가 그 데몬에 붙어 대화와 승인 화면을 보여주며, TypeScript
SDK가 같은 프로토콜을 외부 앱에 열어 줍니다. 11종 LLM 벤더, 로컬·Docker·SSH 실행 백엔드,
스케줄러와 메신저 게이트웨이를 하나의 진입점 `snowpea` 뒤에 둡니다.

snowpea is a local-first coding agent. A Python core runs as a daemon that owns
sessions, tools, and permission policy; an Ink terminal UI attaches to it for chat
and approval prompts; and a TypeScript SDK exposes the same protocol to other
applications. Eleven LLM vendors, local/Docker/SSH execution backends, a scheduler,
and messenger gateways all sit behind a single `snowpea` entry point.

## Layout

```
snowpea-agent/
  pyproject.toml  uv.lock  README.md  LICENSE  NOTICE
  core/snowpea_core/      Python package: server, session, agent, providers,
                          tools, exec, permissions, memory, scheduler, gateway,
                          skills, commands, config, setup, cli, vendor
  tui/                    Node + Ink terminal UI (esbuild single-file bundle)
  sdk/                    TypeScript client SDK (@snowpea/sdk)
  installer/              install.sh, install.ps1, brew/, npm/
  scripts/                gen_protocol.py, verify_vendor_integrity.py
  docs/                   protocol.md, vendoring-map.md, omc-porting-map.md
  tests/                  pytest suites and fixtures
  .github/workflows/      ci.yml, release.yml
```

## Dev quickstart

Requires Python 3.11+ (via [uv](https://docs.astral.sh/uv/)) and Node 20+.

```bash
uv sync                 # create .venv and resolve uv.lock
npm ci                  # install the sdk + tui workspaces
uv run pytest           # Python test suite
npm run build           # build the SDK and bundle the TUI
uv run snowpea --version
```

`uv run ruff check .` lints the Python core; `uv run mypy core` type-checks it.

## Status

M0 skeleton. The subpackages under `core/snowpea_core/` are placeholders that
later milestones fill in; see `.omc/plans/snowpea-agent-consensus-plan.md` §4.

## License

MIT. See `LICENSE`, and `NOTICE` for upstream attribution (hermes-agent,
oh-my-claudecode).

# M8 Contract — Installers, Wheel Bundling, npm Publish, Service, Docs, E2E (binding for US-022)

Plan: §2.5, §3.4, §4 M8, §7.9, AC-01, AC-19, §6 risk 2/8. Public repo: https://github.com/Wafour-Developer/snowpea-agent.

## 1. Wheel bundling of the TUI
- `tui/esbuild.config.mjs` output `tui/dist/snowpea-tui.js`; hatch build hook (`hatch_build.py`, `[tool.hatch.build.hooks.custom]`) copies it to `core/snowpea_core/tui/dist/snowpea-tui.js` at `uv build` time (fails the build if missing, unless `SNOWPEA_SKIP_TUI=1`). `cli/main.py` resolution order: `SNOWPEA_TUI_ENTRY` → packaged `snowpea_core/tui/dist/snowpea-tui.js` → repo `tui/dist/snowpea-tui.js`.
- `snowpea --version` prints `snowpea 0.1.x`; version is single-sourced from `core/snowpea_core/__init__.py` (hatch `[tool.hatch.version] path`).

## 2. Installers
- `installer/install.sh` (bash, mac/linux): detect OS/arch; ensure `uv` (official installer script) ; ensure Node ≥ 20 (`node --version`; if missing: mac → brew if present else print official instructions; linux → try `fnm`/`nvm`-free approach: download official tarball into `~/.snowpea/node` and symlink) ; `uv tool install snowpea-agent` (from PyPI when published; `SNOWPEA_WHEEL_URL`/`SNOWPEA_INSTALL_SOURCE=git+https://github.com/Wafour-Developer/snowpea-agent` override for pre-PyPI) ; ensure `~/.local/bin` on PATH (append to shell rc with a marker) ; print `snowpea --version` and next steps (`snowpea setup`). Idempotent; `--dry-run`; exit non-zero with a precise manual command on any failure.
- `installer/install.ps1` (Windows): `winget install astral-sh.uv` and `OpenJS.NodeJS.LTS` if missing; `uv tool install …`; `%LOCALAPPDATA%\snowpea` for data (paths.py already honours SNOWPEA_HOME; default Windows home = `%LOCALAPPDATA%\snowpea`).
- `installer/brew/snowpea.rb` (formula stub depending on uv + node, installs via `uv tool install`), `installer/npm/package.json` (`npx snowpea` → runs install.sh/ps1 then execs `snowpea`).
- Install URL (v0.1): `https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh`.

## 3. Service registration — `cli/commands.py` `snowpea service install|uninstall|status`
systemd user unit (`~/.config/systemd/user/snowpea.service`, `ExecStart=<snowpea-core path> --port 0`), launchd plist (`~/Library/LaunchAgents/ai.snowpea.daemon.plist`), Windows Scheduled Task at logon (`schtasks`). Off by default; `status` reports installed/running.

## 4. Release workflow — `.github/workflows/release.yml`
On tag `v*`: build TUI bundle → `uv build` → upload wheel/sdist as release assets (+ PyPI publish when `PYPI_TOKEN` secret exists) → `npm publish` `@snowpea/sdk` and `@snowpea/tui` (when `NPM_TOKEN` exists) → attach `installer/install.sh`. Bump `PROTOCOL_VERSION` to `1.0.0` in the v0.1.0 release commit (freeze baseline, plan §4.9).

## 5. Docs — `docs/manual/`
`index.md`, `install.md`, `setup.md` (Quick/Full/Blank, vendors, web login, search/browser providers, tool toggles), `modes.md` (plan/accept/auto, allowlist, project settings), `commands.md` (all built-ins), `plugins.md` (Claude Code format, marketplace, skill learn/agent create), `scheduler.md`, `gateway.md` (Telegram setup, approvals via chat), `backends.md` (docker/ssh), `headless.md` (`-c`, exit codes, JSON lines), `protocol.md` (link to generated). Korean + English sections per page (ko first).

## 6. E2E — `tests/e2e/v01_smoke.sh`, `tests/e2e/v01_smoke.ps1`
Implements plan §7.9 steps 1–9, 12–15 with `SNOWPEA_PROVIDER=fake:<script>` and a fresh `/tmp/snowpea-fixture` git repo; steps 10–11 behind `SNOWPEA_E2E_CREDENTIALED=1`; docker/ssh step skips (not fails) when docker is unavailable; step 1 uses `installer/install.sh --from-checkout` (installs the local checkout) when run from CI. Each step asserts the expected exit code/observable and prints `PASS n` / `FAIL n <why>`; exit 0 only if all non-skipped steps pass. CI `e2e-<os>` jobs (ubuntu/macos/windows) run it.

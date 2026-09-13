# M8 Contract — Installers, Wheel Bundling, npm Publish, Service, Docs, E2E (binding for US-022)

Plan: §2.5, §3.4, §4 M8, §7.9, AC-01, AC-19, §6 risk 2/8. Public repo: https://github.com/Wafour-Developer/snowpea-agent.

## 1. Wheel bundling of the TUI
- `tui/esbuild.config.mjs` output `tui/dist/snowpea-tui.js`; hatch build hook (`hatch_build.py`, `[tool.hatch.build.hooks.custom]`) copies it to `core/snowpea_core/tui/dist/snowpea-tui.js` at `uv build` time (fails the build if missing, unless `SNOWPEA_SKIP_TUI=1`). `cli/main.py` resolution order: `SNOWPEA_TUI_ENTRY` → packaged `snowpea_core/tui/dist/snowpea-tui.js` → repo `tui/dist/snowpea-tui.js`.
- `snowpea --version` prints `snowpea 0.1.x` (`__version__ = "0.1.7"` at the time of writing); version is single-sourced from `core/snowpea_core/__init__.py` (hatch `[tool.hatch.version] path`). `update.installed_cli_version()` shells exactly this command after an upgrade to report what actually landed, rather than what was offered (§7.3).

## 2. Installers
- `installer/install.sh` (bash, mac/linux): detect OS/arch; ensure `uv` (official installer script) ; ensure Node ≥ 20 (`node --version`; if missing: mac → brew if present else print official instructions; linux → try `fnm`/`nvm`-free approach: download official tarball into `~/.snowpea/node` and symlink) ; `uv tool install snowpea-agent` (from PyPI when published; `SNOWPEA_WHEEL_URL`/`SNOWPEA_INSTALL_SOURCE=git+https://github.com/Wafour-Developer/snowpea-agent` override for pre-PyPI) ; ensure `~/.local/bin` on PATH (append to shell rc with a marker) ; print `snowpea --version` and next steps (`snowpea setup`). Idempotent; `--dry-run`; exit non-zero with a precise manual command on any failure. It also records the resolved source in `$SNOWPEA_HOME/install.json` (`record_install`; `Record-Install` in `install.ps1`), requesting the `[images]` extra in whichever of the two spellings the source takes (`snowpea-agent[images] @ git+https://…` for a URL, `/path[images]` for a path), so `snowpea update` repeats it with no second flag.
- `installer/install.ps1` (Windows): `winget install astral-sh.uv` and `OpenJS.NodeJS.LTS` if missing; `uv tool install …`; `%LOCALAPPDATA%\snowpea` for data (paths.py already honours SNOWPEA_HOME; default Windows home = `%LOCALAPPDATA%\snowpea`).
- `installer/brew/snowpea.rb` (formula stub depending on uv + node, installs via `uv tool install`), `installer/npm/package.json` (`npx snowpea` → runs install.sh/ps1 then execs `snowpea`).
- Install URL (v0.1): `https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh`.

## 3. Service registration — `cli/commands.py` `snowpea service install|uninstall|status`
systemd user unit (`~/.config/systemd/user/snowpea.service`, `ExecStart=<snowpea-core path> --port 0`), launchd plist (`~/Library/LaunchAgents/ai.snowpea.daemon.plist`), Windows Scheduled Task at logon (`schtasks`). Off by default; `status` reports installed/running.

## 4. Release workflow — `.github/workflows/release.yml`
On tag `v*`: build TUI bundle → `uv build` → upload wheel/sdist as release assets (+ PyPI publish when `PYPI_TOKEN` secret exists) → `npm publish` `@snowpea/sdk` and `@snowpea/tui` (when `NPM_TOKEN` exists) → attach `installer/install.sh`. ~~Bump `PROTOCOL_VERSION` to `1.0.0` in the v0.1.0 release commit (freeze baseline, plan §4.9).~~ **Withdrawn.** `PROTOCOL_VERSION` moved with the feature work instead and reads `1.4.0` at v0.1.7 (`core/snowpea_core/server/protocol.py`); the v1.0 freeze gate is defined by three consecutive releases with no change to the generated schema, and applies before the v0.2 IDE — not by a release-commit bump.

## 5. Docs — `docs/manual/`
`index.md`, `install.md`, `setup.md` (Quick/Full/Blank, vendors, web login, search/browser providers, tool toggles), `modes.md` (plan/accept/auto, allowlist, project settings), `commands.md` (all built-ins), `plugins.md` (Claude Code format, marketplace, skill learn/agent create), `scheduler.md`, `gateway.md` (Telegram setup, approvals via chat), `backends.md` (docker/ssh), `headless.md` (`-c`, exit codes, JSON lines), `protocol.md` (link to generated). Korean + English sections per page (ko first).

## 6. E2E — `tests/e2e/v01_smoke.sh`, `tests/e2e/v01_smoke.ps1`
Implements plan §7.9 steps 1–9, 12–15 with `SNOWPEA_PROVIDER=fake:<script>` and a fresh `/tmp/snowpea-fixture` git repo; steps 10–11 behind `SNOWPEA_E2E_CREDENTIALED=1`; docker/ssh step skips (not fails) when docker is unavailable; step 1 uses `installer/install.sh --from-checkout` (installs the local checkout) when run from CI. Each step asserts the expected exit code/observable and prints `PASS n` / `FAIL n <why>`; exit 0 only if all non-skipped steps pass. CI `e2e-<os>` jobs (ubuntu/macos/windows) run it.

## 7. Update channels, provenance and the downgrade guard (`update.py`, `server/update_handlers.py`)

Added in v0.1.x; undocumented in any earlier contract.

### 7.1 Install provenance

An installation records how it was installed in `$SNOWPEA_HOME/install.json` (`{method, source, time}`, written by `record_install` in `install.sh`, `Record-Install` in `install.ps1`, and `update.write_install_json`). For a git install that is cross-checked against **PEP 610** `direct_url.json` in the installed dist-info, and `git_install_provenance(paths) -> GitInstall{branch, installed_revision} | None` returns a branch install only when the resolved branch is in `_TRACKED_BRANCHES` (`{"main", "master"}`). The branch is resolved from `direct_url.json` when its `url` normalises to this repository's URL and its `vcs_info.vcs == "git"` — a bare git install carrying a `commit_id` and no `requested_revision` is read as `main` — otherwise from the recorded source.

A branch name alone is never enough to *offer* an update: without an installed commit there is nothing to compare against, and `_check_git_branch` answers with `error: "cannot determine the installed git revision; reinstall from main"` rather than offering one.

### 7.2 The downgrade guard

Two channels, two different guards, and **neither may ever offer a move backwards**:

- **PyPI / tag installs** — strict semver greater-than only. `is_newer(latest, current)` parses both with `_VERSION_RE` and returns `left > right`; an unparseable version on either side is `False`, never `True`.
- **Git branch installs** — the installed commit and the branch head are compared through `GET https://api.github.com/repos/<repo>/compare/<installed>...<head>`, and `available` is set **only** when `status == "ahead"`:

  | compare `status` | outcome |
  |---|---|
  | `identical` (or `_same_revision`, which skips the call) | `available: false`, no error |
  | `ahead` | `available: true` |
  | `behind` | `available: false` — the local build is in front of the branch; replacing it would be a downgrade |
  | anything else (`diverged`, missing) | hard error `"branch history diverged; refusing an automatic replacement"` |
  | compare call non-200 | hard error `"could not verify update ancestry"` |
  | any network failure | `_answer(error=str(exc))` — never an exception out of startup |

  A successful answer is written to the cache under `installKey = f"git:{branch}:{revision}:{__version__}"`, so the cache is invalidated by an upgrade without a new network call. **Only a negative answer is ever reused**: the read rejects a cached entry that carries an `error` or `available: true`, because a force-push inside the 24h window would otherwise have it offering a commit whose ancestry was never re-checked. A failed check writes no cache at all — a briefly offline machine must not report "no update" for 24 hours.

### 7.3 Displayed version for a branch upgrade

A branch install tracks commits, but the prompt must still name a release. `_git_version_at(client, revision)` reads `core/snowpea_core/__init__.py` at the target commit over `raw.githubusercontent.com` and, when that version `is_newer` than the running one, the offer is labelled with it. Result fields are `latest = f"{displayVersion}+{sha[:8]}"` and `current = f"{__version__}+{installedSha[:8]}"`, so `v0.1.2+oldsha → v0.1.3+newsha` rather than the misleading `v0.1.2+newsha`. **Tag discovery is presentation-only**: any failure is swallowed at `debug` and MUST NOT invalidate the ancestry check. After the upgrade, `installed_cli_version()` shells `snowpea --version` to report what actually landed.

### 7.4 RPC contract

- `system.checkUpdate{force?}` — 24h cache unless `force`. The handler strips the internal keys `installKey`, `trackingSource` and `configured` from the answer before validating it into `CheckUpdateResult` (`server/update_handlers.py`); they are provenance bookkeeping, not part of the wire schema.
- `system.update` — **forces a fresh check first** (`check_update(paths, settings, force=True)`). An explicit upgrade must not act on a negative 24-hour cache, because a tracked branch may have advanced since startup checked it. It then refuses when the fresh answer carries an `error` or is not `available`, returning `UpdateResult{started: false, command: "", log: <path>, error: <reason or "No newer update is available; refusing to reinstall or downgrade.">}`. Refusing is a successful RPC, not an error.
- `system.restart` — shuts the daemon down via `core.request_shutdown("restart")`; the next `snowpea` launch spawns a fresh one through the existing `ensure_daemon`. The TUI half of the handshake is exit code `75` (EX_TEMPFAIL) turned into `os.execv` by `cli/main.launch_tui`.
- `update_command` prefers `uv tool install --force --reinstall` whenever `uv` is on PATH, falls back to the `method` recorded in `install.json` (`pipx`, `pip`), and when neither is recognisable returns `None` so the handler reports `started: false` with `manual_command(source)` in `command` — it never guesses. `update.with_images` threads the `[images]` extra through every upgrade path so an upgrade cannot silently drop image downscaling.
- `SNOWPEA_UPDATE_CHECK=0` overrides `updates.check` and disables the daily background check (the test suite sets it globally).

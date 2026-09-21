# Install

snowpea needs two runtimes: Python 3.11+ (managed by [uv](https://docs.astral.sh/uv/)) and Node 20+. The installer puts both in place if they are missing. The terminal UI ships pre-bundled inside the Python wheel, so there is no `npm install` on your machine.

## One-liner

**macOS, Linux, WSL2**

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
```

**Windows (PowerShell)**

```powershell
iwr https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.ps1 | iex
```

Then check it:

```bash
snowpea --version
```

The script is idempotent — running it again upgrades in place. It installs uv through the official installer, makes sure `node --version` reports 20 or higher, installs the `snowpea` command as a uv tool, and appends `~/.local/bin` to your shell profile if it is not already on `PATH`. On Windows the data directory is `%LOCALAPPDATA%\snowpea` instead of `~/.snowpea`.

To see what it would do without touching anything, download it first and pass `--dry-run`:

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh -o install.sh
sh install.sh --dry-run
```

## Manual install

If you would rather not pipe a script into a shell, or the one-liner failed at a step you want to do yourself:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, if missing
uv tool install snowpea-agent
snowpea --version
```

Node 20+ must be on `PATH` for the terminal UI. Headless runs (`snowpea -c`) and every `snowpea <subcommand>` work without Node; only the TUI needs it.

## Install source overrides

The installer takes a source override, which is what CI and offline installs use:

```bash
sh install.sh --from-checkout
SNOWPEA_WHEEL_URL=./snowpea_agent-0.1.0-py3-none-any.whl sh install.sh
SNOWPEA_INSTALL_SOURCE=git+https://github.com/Wafour-Developer/snowpea-agent sh install.sh
```

`SNOWPEA_BIN_DIR` moves where the `snowpea` executable lands, and `--from-checkout` installs the checkout the script itself lives in.

## Running from a checkout

```bash
git clone https://github.com/Wafour-Developer/snowpea-agent.git
cd snowpea-agent
uv sync
npm ci
npm run build
uv run snowpea --version
```

`npm run build` produces `tui/dist/snowpea-tui.js`, which `snowpea` looks for after the packaged bundle. While working on the UI, point `SNOWPEA_TUI_ENTRY` at your own entry file to skip the bundle entirely.

## Updating

snowpea checks for a newer release once a day, in the background, and caches the answer in `$SNOWPEA_HOME/update-check.json`. Nothing is ever installed without you saying so.

**In the terminal UI.** When an update is waiting, a banner appears at the top of the screen:

```text
⬆ Update available v0.1.2 (current v0.1.1) — press U or type /update
```

Press `U` (or type `/update`) and answer `y`. The upgrade runs in the background, the banner reports progress, and when it finishes snowpea restarts itself on the new version. A running turn is never interrupted.

**From the command line.**

```bash
snowpea update --check
snowpea update
```

`--check` reports the current and latest versions and installs nothing. Without it, `snowpea update` runs the upgrade, stops the daemon, and tells you to start `snowpea` again. `snowpea --version` also mentions a pending update, using only the cached answer, so it never waits on the network.

The upgrade writes its output to `$SNOWPEA_HOME/logs/update.log`. It runs `uv tool install --force --reinstall`; if `uv` is not on `PATH`, the method the installer recorded in `$SNOWPEA_HOME/install.json` is used instead, and if nothing is known snowpea prints the command to run by hand rather than guessing.

Two settings control this in `$SNOWPEA_HOME/settings.json`:

```json
{ "updates": { "check": true, "channel": "auto" } }
```

`check: false` turns the daily background check off, leaving `/update` and `snowpea update` working on demand. `channel` is `auto` (PyPI when the package is published there, the repository's git tags otherwise), `pypi`, or `git`.

To upgrade by hand instead:

```bash
uv tool upgrade snowpea-agent
snowpea daemon stop
snowpea --version
```

Stop the daemon after upgrading by hand. A running daemon keeps the old code in memory, and the next client to attach would negotiate against a protocol version that no longer matches the installed one.

## Desktop app

Install the desktop app for this operating system and CPU, then launch it:

```bash
snowpea desktop
```

If it is already installed, the command only launches it. Use `snowpea desktop --check` to compare the installed and newest available versions, `snowpea desktop --reinstall` to upgrade or reinstall, and `snowpea desktop --install-only` to install without launching. In scripts, add `--yes` to approve the download without an interactive prompt.

## Uninstalling

```bash
snowpea daemon stop
uv tool uninstall snowpea-agent
```

That leaves your data. To remove it as well, delete `$SNOWPEA_HOME` (`~/.snowpea`, or `%LOCALAPPDATA%\snowpea` on Windows). If you registered the daemon as a service, unregister it first:

```bash
snowpea service uninstall
```

## When something goes wrong

**`snowpea: command not found` right after installing.** `~/.local/bin` is not on your `PATH` in this shell. Open a new terminal, or source your shell profile. The installer appends the line but cannot change the shell you are standing in.

**Node is missing or too old.** The TUI will not start. Install Node 20+ from your package manager or [nodejs.org](https://nodejs.org), then run `snowpea` again. Everything else keeps working in the meantime:

```bash
snowpea -c "hello" --json
```

**The daemon will not start.** Look at `$SNOWPEA_HOME/logs/daemon.log`, and check whether a stale process is holding the recorded port:

```bash
snowpea daemon status
snowpea daemon stop
snowpea daemon start
```

`daemon status` reads `$SNOWPEA_HOME/daemon.json` and verifies the pid is alive. A stale file with a dead pid is safe to delete.

**Corporate proxy or offline machine.** `uv tool install` needs PyPI. Set `HTTPS_PROXY` before running the installer, or install from a wheel you carry in yourself with `uv tool install ./snowpea_agent-0.1.0-py3-none-any.whl`.

## Next

[Setup](setup.md) — pick a vendor and get a key in place.

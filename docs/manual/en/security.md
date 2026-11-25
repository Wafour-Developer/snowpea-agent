# Security lists

**What this page promises, and what it does not.** In `auto` mode the agent runs programs as you. snowpea reads each command line before it runs — and the code given directly on that line or in a script the line names — and refuses or asks you about what the security lists and the rule about snowpea's own files cover. It does **not** read what those programs load or start in turn: your tests, your build, git hooks, a program found on `PATH`, a library. Code run that way can do whatever you can do on this computer, including running snowpea's own command and editing snowpea's files. While snowpea runs, a tamper guard keeps its own copy of the trusted-projects record and of Jarvis's delegation rules, decides from that copy, and puts the files back when something else changes them. Keeping the programs snowpea starts from acting as you towards snowpea needs the operating system; snowpea does not do that yet (it is the next planned step). **For a repository you do not trust, use accept mode**, where every command and every write is yours to approve.

**Off by default (protocol 1.66.0).** The checks on this page apply only while you have turned them on: Settings → Security, or `"security": {"enabled": true}` in `$SNOWPEA_HOME/settings.json`. Until then the mode rules decide alone (plan does not write; accept asks for commands; auto runs them), and the agent works under the safety guidelines in its instructions: no destructive or irreversible command unless you asked for exactly that, never one as a test, a look at the exact target before deleting or moving, and no reading of tokens or login files unless you ask for that file. See [`security.enabled`](#securityenabled-turning-the-checks-on-and-off).

With the checks on: two lists you set once and that hold for every session — with or without Jarvis mode, in every mode, whoever runs the call:

- **Blocked commands** are stopped wherever snowpea sees them: on the line, in code it reads, in a script it reads. Jarvis, a scheduled job, a remembered answer, an "always allow" rule or auto mode cannot let one through; only you can, one call at a time (below). A program the line starts that then runs the command itself (a test, a build step, a git hook) is not read: see [What snowpea does not read](#what-snowpea-does-not-read).
- **Protected folders** are stopped as the target of a change snowpea sees. Each entry has a level: **no changes** (`write`, the default: reading is fine) or **no access** (`all`: not read, not listed, not changed).

Both lists start from **presets** you tick on or off, plus entries you type yourself.

## When a call is blocked, you are asked — once

A blocked call does not run on its own, but it is never out of your reach (0.2.94). snowpea stops the call and asks you on a card that says why: "on your block list", "touches a protected folder" or "touches snowpea's own controls" (below), with the rule that matched. Allow it and the work goes on; deny it and the agent reads that you declined.

- The answer covers **this one call**. It is never remembered: not for the session, not as a rule, not for the next identical line.
- Only you answer it: on the IDE, the TUI, the CLI, the web page, or the Snowpea browser's own card — version 0.4.0 or later, on this computer (not through a remote link), for a conversation it opened or hosts, and only after you confirm in the browser's own window. Jarvis, auto mode, an "always allow" rule, a delegation, a connector and a remote rule cannot answer it. A messenger chat and a remote web login (the web page opened through `snowpea remote`) may decline it but not allow it: the chat shows "allow it on the machine" instead of an allow button.
- A run with nobody there to ask — a scheduled job, a Jarvis task — is refused as before, and the agent reads `blocked by the security list: …`.
- Plan mode refuses writes by the tools that write files (`write_file`, `patch`, …) as it always did: that is the mode, not the block. A blocked shell line in plan mode is asked like any other command.
- Every answer to a blocked call is written to `logs/security.jsonl` with what blocked it and who answered (the kind of connection, its client id, whether it was confirmed in a window, whether it was a remote login); `logs/approvals.jsonl` records the block too, also when nobody could be asked.

**Two things you are never asked about, and that never run while the checks are on:** an agent **reading** the daemon's token or address (`token`, `daemon.json`), or **reading** saved logins (`credentials.json`, `mcp-oauth.json`, and the other agent CLIs' logins listed below). An agent that read them could act with your authority. Changing or deleting those files is asked like any other block. Moving, hard-linking or archiving them away (`mv`, `ln`, `tar --remove-files`), or moving the folder that holds them (snowpea's home or a folder above it), keeps what they hold under another name: that counts as reading them.

## Where to set them

- **snowpea IDE and snowpea web**: Settings → Security.
- **Terminal UI**: the security screen (`/security`).
- **The Snowpea browser's Settings page** (version 0.3.0 or later, on this computer): since protocol 1.58.0 it can change the master switch (`security.enabled`), `unknownCode` and both lists, and only from that page. It also shows what each project adds, read-only.
- You change them on a local connection (IDE, TUI, CLI, the local web page) or on that browser page. A remote web login (the page opened through [snowpea web](web.md) from another machine) and the remote connector can read the lists but not change them.

Behind the screens these are two settings in `$SNOWPEA_HOME/settings.json`:

```json
{
  "security": {
    "blockedCommands": { "presets": ["essential"], "custom": ["git push --force"] },
    "protectedPaths": {
      "presets": ["essential"],
      "custom": [{ "path": "~/finance", "level": "all" }]
    }
  }
}
```

A fresh install, and an install from before this feature, starts with the `essential` preset on in both lists. If you empty a list on purpose it stays empty.

A project can **add** entries of its own in `<project>/.snowpea/settings.json` (same shape under `security`). It can never remove anything from your lists.

## Presets

Blocked commands:

| Preset | What it blocks |
|---|---|
| `essential` (on by default) | deleting `/` or your home (or a folder that holds your home or a project), formatting or overwriting a disk (`mkfs`, `dd of=/dev/…`, `wipefs`), `chmod -R`/`chown -R` on `/` or your home, a fork bomb, `kill -9 -1` |
| `mass-delete` | `rm -r`/`rm -rf` anywhere, `find -delete`, `git clean -fd`/`-fx` |
| `disk` | `dd`, `mkfs`, `fdisk`, `parted`, `wipefs`, mounting and unmounting |
| `permissions` | `chmod -R`, `chown -R`, `chgrp -R`, `setfacl -R` |
| `power` | `shutdown`, `reboot`, `halt`, `poweroff`, `systemctl poweroff`… |
| `git-destructive` | force pushes (`--force`, `-f`, `+branch`), remote deletes, `git reset --hard`, `filter-branch`, `git branch -D`, `git clean -f`, `git stash clear` |
| `remote-script` | a download run straight away: `curl … \| sh`, `bash <(curl …)`, `sh -c "$(curl …)"` |
| `package-global` | `sudo`, `doas`, system package managers changing packages (`apt install`, `brew install`, …), `npm -g`, `pip install` outside a virtual environment |
| `process-kill` | `pkill`, `killall`, `kill -9 -1` |
| `containers` | `docker system prune` (and every `docker … prune`), `docker rm -f`, `docker volume rm`, `compose down -v`, `kubectl delete`, `helm uninstall` |
| `database` | `DROP DATABASE/TABLE`, `TRUNCATE` through a database client, `dropdb`, redis `FLUSHALL` |
| `cloud` | `terraform destroy`, `aws s3 rm --recursive`, `aws … delete-*`, `gcloud … delete`, `az … delete` |

Protected folders:

| Preset | What it protects | Level |
|---|---|---|
| `essential` (on by default) | `/etc`, `/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/var/lib`, `/System` are never changed by a call snowpea reads; `/` and your home folder themselves (and every folder above your home) are never deleted, moved or changed recursively by one; what runs when you log in or open a shell is never changed by one: `~/.ssh`, the shell start-up files (`~/.bashrc`, `~/.bash_profile`, `~/.bash_login`, `~/.bash_logout`, `~/.profile`, `~/.zshrc`, `~/.zprofile`, `~/.zshenv`, `~/.zlogin`, `~/.config/fish/config.fish`), `~/.config/autostart`, `~/.config/systemd/user`, `~/Library/LaunchAgents`, `/Library/LaunchAgents`, `/Library/LaunchDaemons` | no changes |
| `credentials` | `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, `~/.config/gh`, `~/.config/gcloud`, `~/.azure`, `~/.netrc`, `~/.git-credentials`, `~/.npmrc`, `~/.pypirc`, password stores, keyrings | no access |
| `snowpea-home` | `$SNOWPEA_HOME` (snowpea's plan files stay writable) | no changes |
| `other-projects` | every project snowpea knows, except the one the session works in | no changes |
| `mounts` | mounted data and network disks (`/mnt/…`, `/media/…`, `/Volumes/…`) outside the session's project; a disk that holds your home or snowpea's folder is left out | no changes |

## Your own blocked commands

Type a pattern per line. It is matched against every command a line would run — through `sudo`, `env`, `nice`, `timeout`, `xargs`, `find -exec`, inside `sh -c '…'`/`bash -c '…'`, on both sides of pipes and `&&`/`;`, inside `$( … )` and here-strings.

| Pattern | Blocks | Does not block |
|---|---|---|
| `rm -rf` | `rm -rf x`, `rm -r -f x`, `rm -fR x`, `sudo rm --recursive --force x` | `rm -r x` (no `-f`) |
| `git push --force` | `git push --force`, `git push -f`, `git push --forc`, `git -C repo push origin --force` | `git push --force-with-lease` (a different option; add it too if you want) |
| `git push origin` | `git push origin main` | `git push upstream main` |
| `docker * prune` | `docker system prune`, `docker image prune -a` | `docker prune` |
| `mkfs*` | `mkfs.ext4 /dev/sdb1` | |
| `rm -r ~/keep` | `rm -rf ~/keep`, `cd ~ && rm -r keep` | `rm -r keep` run in another folder |
| `curl --upload-file=*` | `curl --upload-file=notes.txt https://…` | |
| `re:^ssh .*prod` | `ssh deploy@prod.example` | `ssh dev.example` |

The rules:

- The **first word is the program**, by its name (`/usr/bin/rm` is `rm`; `python3.12` is also `python`). It may use wildcards (`mkfs*`) but cannot be only a wildcard.
- **Options** (words starting with `-`) must all be present, anywhere, in any order. `-rf` means `-r` and `-f`, however they are written (`-rf`, `-fr`, `-r -f`, `-rfv`). `--force` also matches the abbreviations programs accept (`--forc`) and `--force=…`. For `rm`, `-r` = `-R` = `--recursive` and `-f` = `--force`; for `chmod`/`chown`/`chgrp`, `-R` = `--recursive`; for `git push`, `-f` = `--force`; `git branch -D` = `--delete --force`. For `find`, an option is the whole word (`find -delete`).
- **Other words** must appear in the same order among the command's other words, not necessarily next to each other. `*` is any one word. A word starting with `/` or `~` is a path and is compared after resolving it (`~`, `$HOME`, relative to the folder the command runs in).
- `re:` followed by a regular expression matches against each command's text (its words joined by spaces). A regular expression that matches an empty command is refused.

A pattern that cannot work is refused when you save it, with the reason ("the program cannot be only a wildcard", "the regular expression does not compile: …").

## Protected folders you add

Type an absolute path (`/srv/backups`) or one under your home (`~/finance`), and choose the level. Protecting a folder protects everything below it. A change counts as touching it when it would:

- write, delete, truncate, `chmod`/`chown` it or anything below it, redirect output into it, extract an archive into it, or move/rename something from or onto it;
- delete, move or recursively change (`chmod -R`, `git clean`, extracting an archive) a folder **above** it;
- for **no access** also: read or list it, or search a folder above it recursively (`grep -r`, `find … -exec`). With **no access**, a command that merely names a path inside it is refused too.

Symlinks are followed both ways (a link to a protected folder is protected; a protected path given as a link protects what it points to). A wildcard (`rm -rf /srv/*`) or a path with a part only the shell knows (`rm -rf /srv/$NAME`) is judged by the widest thing it could reach. A protected folder inside the project is protected like any other.

## Try it

The screens have a "try it" box: type a command or a path and see whether it would be blocked and by what, against your saved lists or the edits you have not saved yet. Nothing runs.

## What snowpea reads

snowpea judges a call by reading it before it runs: the command line, every command in it (through `sudo`, `env`, `xargs`, `sh -c`, pipes, `$( … )`), the code given on the line (`python -c`, `node -e`, a here-document), a script the line names (`./x.sh`, `python tool.py`, `awk -f prog.awk`), a git alias and the `npm`/`deno` task a line names. A file tool's path is read the same way.

It lets a line run unasked when what it read is allowed by your lists and by the rule about snowpea's own files, as described on this page. What it could not read is put to you (below). That is a check of what a call says, not a wall around what the call then does.

### snowpea's own files

Apart from your lists there is one rule you cannot switch off. It covers the files that decide what snowpea trusts and who may talk to it, in every mode, with any presets, also with both lists empty — for every call snowpea reads:

- **Never read; a change asks you, once**: `daemon.json`, `token`, `credentials.json` and `mcp-oauth.json` in `$SNOWPEA_HOME`, and every file next to them whose name starts the same way (`credentials.json.tmp`, a half-written copy). With these a program could act as you. The logins of other agent CLIs are treated the same, in snowpea's folder and in your home: `~/.codex/auth.json`, `~/.claude/.credentials.json`, `~/.claude.json`, the Gemini and Cursor logins, `~/.config/anthropic`.
- **A change asks you, once** (reading is fine): `settings.json`, the MCP servers started for every project (`.mcp.json` in `$SNOWPEA_HOME`), the trusted-projects record (`trust/`), Jarvis's rules and ledger (`jarvis/`), the session store, the update files, the approval and security logs — and **any other name directly in `$SNOWPEA_HOME`** that is not one of the working areas below. snowpea's folder is closed by default and open where the agent works: plans, session workspaces (`sessions/`), attachments, tool content, audio, caches, checkpoints, ordinary logs, teams, `memory.md`, extension data.
- **Asked**: a shell line or code that writes into your own hooks, plugins, extensions, skills, commands or agent definitions in `$SNOWPEA_HOME`; a link made anywhere in `$SNOWPEA_HOME` (`ln`, `ln -s`, `cp -s`, `os.symlink`) — a link is a second name for something there, or a way out of it; a write into snowpea's own program files (its package folder, the Python it runs in, the `snowpea` launcher), unless that folder is inside a project you work on.
- **Asked**: a line that runs snowpea's own command (`snowpea …`, `python -m snowpea_core …`, through `uv run`, a script of snowpea's, from inside code in a language snowpea reads). That command acts as you, so only you can let it run. The card says "this runs snowpea's own CLI".
- **Asked**: one of the "a change asks you, once" files handed to a program that may change it. `cat`, `grep`, `jq`, `ls`, `stat`, `cp` (as the source) and the like only read, and run as before. Anything else asks: a database client opened on the session store (`sqlite3 ~/.snowpea/state.db …`, even to look), a link to one of the files (`ln -s`), a variable set to its path, code that names it and does more than open it to read.

A refused read reads `blocked by the security list: security — $SNOWPEA_HOME/token holds snowpea's own secrets …`. A change of these files is put to you on a card that says it "touches snowpea's own controls" ([once](#when-a-call-is-blocked-you-are-asked--once)). An "asked" one is yours alone to answer: Jarvis does not decide it, and the answer is not remembered.

snowpea also writes its own files carefully: each one is written to a new file with a random name next to it and then renamed into place, and a file that is a symbolic link (or not yours) is not written through and not loaded as `credentials.json`, `.mcp.json`, `hooks.json` or `daemon.json`; snowpea logs why instead, and what that file holds is not used: no stored credential, no MCP server or hook from it, no daemon found through it. For `credentials.json` the log and the Security screen name the file and the fix: replace the link with the file itself.

`settings.json` is the exception, because dotfile managers (stow, chezmoi, home-manager) link it: a link that points at a regular file of yours is followed. snowpea reads that file, writes its changes into it (the link stays a link, the file keeps its mode), and protects that file exactly like `settings.json` itself, wherever it lies — a shell line, code, `fs.write` or a file tool that writes it is refused or asks as for `settings.json`. Creating such a link from a session asks you (a link made in `$SNOWPEA_HOME` always does). A link to anything else — a folder, a file that is not yours, nothing, a loop — is not used: snowpea starts as for an unreadable file (below).

## What snowpea does not read

These run code nobody showed snowpea. snowpea lets them run, because asking before every test run or commit would make auto mode useless — so know what they mean:

- **Test runners and build tools.** `pytest`, `npm test`, `make`, `cargo build`, `tox`, `pre-commit`, `mkdocs build` … load the project's own files: test files, `conftest.py`, the `Makefile`, `package.json` scripts, plugins. snowpea reads the line, not what those files do. Code the agent wrote into a test or a build file runs when the tests or the build run.
- **git.** git runs hooks (`.git/hooks/*`) and programs its configuration names (`core.pager`, `core.hooksPath`, `core.fsmonitor`, `core.sshCommand`, `diff.external`, `*.textconv`, `filter.*`, `credential.helper`, `gpg.program`, …). What snowpea does here: a write into `.git/hooks`, `.git/config` or `.git/info/attributes` asks; `git config`, `git -c` and `--config-env` setting one of those keys ask (a value that runs nothing, like `core.pager=cat`, does not); and a git command in a repository whose hooks the agent wrote or changed, or whose configuration the agent wrote with such a key, asks. A hook or a configuration that was there before the session runs as it does without snowpea.
- **Programs found on `PATH`.** For a bare name (`mytool`) snowpea looks up what the command will find, with the same `PATH` the command gets. When that file was written by the agent (in any session snowpea remembers) or lies in a temporary folder, it is read like a script the agent wrote: what it does is judged, and a file that cannot be read (a binary) asks. A write into a folder on snowpea's own `PATH` outside the project (`~/.local/bin`, `/usr/local/bin`) asks. Everything else on `PATH` is a program of your system and runs as named.
- **What a program loads or starts.** A library, a plugin, a module found by the interpreter, a subprocess built while it runs, a compiled program.

Code run in any of these ways can do whatever you can do on this computer: read snowpea's token, run `snowpea …`, change snowpea's files. The tamper guard (next) keeps the trust and delegation records while snowpea runs; the rest is the operating system's job (see [Future work](#future-work)).

## What happens while snowpea runs

### The tamper guard

For the two records that matter most there is a second layer that reads no command: while snowpea runs, it keeps its own copy of the trusted-projects record (`trust/`) and of Jarvis's delegation rules (`jarvis/delegation.db`) in memory. Every decision is made from that copy, not from a second read of the file, and the copy changes only when snowpea itself writes. Before using a record snowpea looks at the file; if something else changed, replaced or removed it, or left a journal next to the rules database, snowpea does not use the change, puts its copy back, writes a line to `logs/security.jsonl` and tells you ("something outside snowpea changed the trusted-projects record …; snowpea did not use the change and kept its own copy"; on the Security screen too). For Jarvis's rules you get a Jarvis notice. A file changed again and again is told once at once and then at most once a minute, with a count. If putting the file back fails, the notice says so.

The guard holds while snowpea runs. If snowpea is not running, nobody is watching: at the next start it takes the files as they are, except that a journal or WAL next to the rules database is removed unread. A program running as you can also stop snowpea.

### Settings that wait for you

You can edit `settings.json` by hand, and most of it is picked up as before. What can run programs or widen access does not take effect from an edit of the file while snowpea runs: `security` (your lists, `unknownCode`), `jarvis`, `web.remote`, the allow rules (`allowlist`), MCP permissions, and every setting that names a program, an address snowpea calls or snowpea's own code — MCP servers' commands and addresses, language servers, `media.mcp`, the audio player, recorder and speech commands, providers' base URLs, every address-shaped field (a name ending in `url`, `uri`, `endpoint`, `host` or `proxy`, like `base_url`, `baseUrl` or `SEARXNG_URL`) of the search and browser credentials, of a provider block and of a messenger block, extensions, the skills registry, the update channel. snowpea keeps the values it has, tells you "settings.json changed on disk: settings that can run programs or widen access (…) are not applied and wait for you", and applies them only when you say so on a local connection (IDE, TUI, CLI, the local web page). You can also discard the edit: snowpea then writes its own values back to the file. Changing the same settings on the Settings and Security screens works as always. Other settings, and API keys, are picked up at once as before. `jarvis` includes **Allow delegation from a remote web login** (`jarvis.delegation.remoteGrant`); the agent's own settings tool cannot change that one at all ([snowpea web](web.md#delegation-from-a-remote-sign-in)).

At start snowpea takes `settings.json` as it is — unless it is not a regular file of yours (a link to your own file is followed, see above) or not valid. Then the affected sections use their defaults (all of them when the file cannot be read at all): for `security` that means the default security lists with unknown code asking, for `web` remote access stays off. The Security screen and `snowpea daemon status` say so, and the file as it was is kept as `settings.json.invalid`. While the whole file cannot be read, snowpea does not write over it: a change of a setting is refused with "… could not be read, so snowpea runs on defaults and does not write over it" until you fix the file and restart snowpea or reload its settings. When only some sections are invalid, the rest is used and saved as usual; setting an invalid section replaces it.

## Lines snowpea cannot check

When snowpea cannot tell before a line runs what it will touch (a program named by a variable, a script it cannot read, a write whose target is computed), it puts the line to you, marked "snowpea cannot tell what this will touch before it runs, so it asks you", with each part it could not check listed (the command, and the script file and line when the cause is inside a script); Jarvis does not decide it. A script run with literal arguments, or one that gives a variable a single literal value (`OUT=${1:-/tmp/shot.png}`, `ADB=/opt/sdk/adb`), is read with those values and checked like any other line. It is not refused — you decide. With the default `security.unknownCode: "auto"` (below) such a line runs without a question in a session in auto mode. A line that acts on snowpea's own controls always asks.

Running snowpea's own program only to read is an ordinary read: `snowpea --help`, `snowpea --version`, `snowpea session list`, `snowpea daemon status`, `snowpea agents` (also after `export PATH="$HOME/.local/bin:$PATH"`). Every other `snowpea …` (`project trust`, `--home`, `daemon stop`, …) asks you in every mode.

What cannot be read: the program comes from a variable or a substitution (`$CMD …`, `` `echo rm` … ``), `eval` of a variable, a script piped into a shell (`… | sh`), a `sh -c` built from variables, inline code for a program snowpea has no reader for (`mongosh --eval`, `gdb -ex`, `Rscript -e`, `elixir -e`, …), a command sent to another machine (`ssh host 'command'`; on this machine the command is read) or handed over to run later (`crontab file`, `at`, `batch`, `launchctl load`), a function that takes the name of a shell builtin (`cd() { … }`), a path only the shell knows and nothing in front of it tells where it lands. These first ones are asked **even when both lists are empty**, because an act on snowpea's own files could hide there.

The same goes for a line that changes what the next program is or does:

- a variable that carries code or redirects configuration — `LD_PRELOAD`, `BASH_ENV`, `NODE_OPTIONS=--require …`, `GIT_SSH_COMMAND`, `EDITOR`/`PAGER` set to a program, `PATH` with a folder in the project, a temp folder or a relative one, `PIP_INDEX_URL`, `NPM_CONFIG_SCRIPT_SHELL`, `JAVA_TOOL_OPTIONS`/`JDK_JAVA_OPTIONS`, `LUA_INIT`, `DOTNET_STARTUP_HOOKS`, `PHPRC`, `VIMINIT`, `ERL_AFLAGS`, `RSYNC_RSH`, `KUBECONFIG`, `AWS_CONFIG_FILE`, `DOCKER_CONFIG`, `CURL_HOME`, `WGETRC`, `GCONV_PATH`, `LESSOPEN`;
- **any other UPPER_CASE variable** snowpea does not know, unless its value is a plain word or number (`FOO=bar`, `LEVEL=3`) or it is a common build, test or app setting: `NODE_ENV`, `CI`, `DEBUG`, `LANG`, `LC_*`, `TZ`, `PORT`, `HOST`, `FORCE_COLOR`, `RUST_LOG`, `PYTEST_*`, `DJANGO_*`, `FLASK_*`, `VITE_*`, names ending in `_URL`, `_KEY`, `_TOKEN`, `_SECRET`, `_ENV`, `_PORT`, `_HOST`, and `*_DIR` pointing inside the project. A lowercase name (`my_var=…`) is the shell's own and runs as before;
- `HOME`, `PWD`, `TMPDIR`, `SNOWPEA_HOME`, `CDPATH` or an `XDG_*` variable given another value: snowpea reads `~` and `$HOME` with your real home, so after such an assignment it cannot say where a path lands;
- `git -c` with a setting that runs a program, an editor started with commands (`vim -c '!…'`), an `awk` program that calls `system`.

What stays unasked, so you know the line is not hurt by this: `PATH=/usr/local/bin:/usr/bin:/bin make`, `export PATH="$PATH:/usr/local/bin"` and any folder already on snowpea's own `PATH`; `NODE_ENV=production npm test`, `CI=1 …`, `FOO=bar ./script.sh`, `DATABASE_URL=postgres://… pytest`, `GOOS=linux GOARCH=amd64 go build`; `PYTHONPATH=src pytest`; `CC=gcc make`, `MAKEFLAGS=-j8`; `GIT_PAGER=cat`, `EDITOR=true`, `git -c core.pager=cat log`; `PIP_NO_CACHE_DIR=1`, `NPM_CONFIG_LOGLEVEL=error`, `CARGO_TERM_COLOR=always`; `TMPDIR=/tmp` when that is the temp folder anyway.

## Code snowpea can't tell about

snowpea reads the code a line runs — `python -c`, `node -e`, `bun -e`, `deno eval`, a here-document fed to an interpreter, a script file, `execute_code` — without running it:

- Code that deletes, overwrites or moves something **it names** is judged like the command it stands for. `shutil.rmtree('/home/me')` is `rm -rf /home/me`, however it is spelled, imported or aliased.
- Code that only reads, prints or computes, or that writes to a path it names, runs as before.
- Code where snowpea **can't tell** asks you, in every mode, with "can't tell what this code changes". That is: a delete, write, copy, move or `chmod` whose target is computed while the code runs (`os.remove(path)`); a command built from variables; code that does not parse; calls reached through `getattr`, `eval`, `exec` or a computed import; an interpreter fed code snowpea cannot read (`curl … | python3`, a bare `python3 -`); `python -m` of a module that is neither a known tool nor in your project.

Code a connected host runs in the host — the Snowpea browser's `repl` scripts run in its own pages — is judged by that host, not by this check; a host tool marked `exec` is still read, and every host tool's file paths are still judged. A code tool given `language: "javascript"` or `"typescript"` is read with the JavaScript reader (top-level `await` included).

Scripts that were in your project before the session are your project's own: what they plainly do is judged (a blocked command in them is blocked), what they compute is not held against them. A script the agent wrote is read the strict way: what cannot be read of it, and what it computes, asks. snowpea remembers which files the agent wrote for each session, also after a restart; what a session wrote since snowpea started is kept, and a session that writes more files than can be listed (20 000) is from then on taken to have written every file that changes.

Also asked: code that runs a string as code by another door (`timeit.timeit("…")`, `cProfile.run`, `pdb.run`, node `new Function(…)`, a worker started from a string) when the string is not written out; loading pickled data (`pickle.load`), which is code once loaded; code that changes its own names or surroundings behind snowpea's back (`os.chdir`, setting `HOME` in `os.environ`, assigning into `__main__`).

Common harmless things that ask, so you know why you see the card: a script the agent just wrote that writes to a path it received as an argument (`open(args.out, "w")`); `subprocess.run(cmd)` with a built command; `python -m http.server`; `make` on a Makefile the agent just wrote that uses make variables; `python -c "print('$NAME')"` (the shell fills in text before Python reads it); `pickle.load(open('model.pkl', 'rb'))`; `HOME=/tmp/x npm test`; `ssh build-host make`; `node dist/index.js` when `dist/index.js` is built by the same line and is not there yet; `PYTHONPATH=$(pwd) pytest` (write `PYTHONPATH=. pytest`); `TAG=$(git describe) make`, `BUILD_DIR=/tmp/build make`, `CONFIG_FILE=config/dev.yaml npm start`, `JAVA_OPTS=-Xmx2g ./gradlew build`, `EXTRA_ARGS='--verbose' make` (a variable snowpea does not know, with more than a plain word); `git config core.editor vim`; writing a file of your own directly into `$SNOWPEA_HOME` (refused: put it under `sessions/` or `plans/`).

### `security.unknownCode`

"Commands and scripts snowpea cannot read ahead" on the Security screen, or in `$SNOWPEA_HOME/settings.json` (a hand edit of this setting waits for your confirmation, see above):

```json
{ "security": { "unknownCode": "auto" } }
```

- `"ask"`: asked in every mode.
- `"auto"` (the default since 0.2.90): not asked in a session in auto mode; asked in plan and accept.
- `"allow"`: not asked in any mode, a line snowpea cannot parse included. Only blocked commands, protected folders, snowpea's own files and the always-asked patterns below (a downloaded script run at once, for one) are asked. For a line it could not read, the raw text is checked at its worst: a blocked command anywhere in it is refused, and a path in it that, deleted with everything below, would reach a protected folder or snowpea's own files makes it ask.

What it gives up: with `"auto"` (in auto mode) or `"allow"`, a script or command whose target is computed while it runs can write to a protected folder without a question. What snowpea can read is still judged: blocked commands and protected paths it sees are refused. These always ask, whatever the setting: anything that acts on snowpea's own files or command (or names snowpea next to something unreadable); a program only the shell knows given a protected path (`$X -rf ~`); a change to `HOME`, `PWD`, `PATH` or a similar variable; git told to run a program, a write where git or `PATH` finds programs; a script the same line writes and then runs.

Settings written by snowpea 0.2.88 and 0.2.89 hold `"ask"` (the old default). The first start of 0.2.90 changes a stored `"ask"` to `"auto"` once, says so on the Security screen and records it in `$SNOWPEA_HOME/security/unknown-code-migrated.json`; after that it never changes it again, so if you set `"ask"` back it stays.

You change it on a local connection or on the Snowpea browser's Settings page; a project cannot.

### `security.enabled`: turning the checks on and off

"Security checks" on the Security screen, or in `$SNOWPEA_HOME/settings.json` (a hand edit waits for your confirmation, see above):

```json
{ "security": { "enabled": true } }
```

**Off is the default** (protocol 1.66.0, capability `securityDefaultOff`): the checks apply only when `settings.json` says `true`. Every `settings.json` saved since protocol 1.58.0 held `"enabled": true` because the old default was written out, so the first start of 1.66.0 changes a stored `true` to `false` once, unless `$SNOWPEA_HOME/security/switch.json` records that you turned the checks on yourself. That run is recorded in `$SNOWPEA_HOME/security/default-off-migrated.json`; after that the value is never changed again, so if you turn the checks on they stay on.

Off, snowpea applies neither the blocked commands nor the protected paths (presets, your own entries and a project's additions), code it cannot read ahead runs as under `unknownCode: "allow"`, and reads of the daemon's token and address, saved logins and other CLIs' logins are not blocked: they follow the mode rules like any other read (a file tool's read of a credential file is asked in plan and accept and runs in auto; a shell command is asked like any other command). Your lists and your `unknownCode` stay stored as they are: turning the checks on restores all of it, the block on reading tokens and logins included. Still in force while off: the rest of the rule about snowpea's own files (writing, deleting or moving them is asked), the mode rules, Jarvis's delegation rules and the questions only you may answer.

`security.get` reports the state: `enabled`; `byDefault`, true while off only because nobody turned the checks off; and while someone did, who (`disabledBy`: desktop, TUI, browser, …) and when (`disabledAt`). For apps of protocol 1.65.0 and older, which do not read these fields, the first line of `problems` (problem code `security-disabled`) says neutrally that the checks are off and how to turn them on. Every change is one line in `$SNOWPEA_HOME/logs/security.jsonl` (`"kind": "security-switch"`), the who/when record is `$SNOWPEA_HOME/security/switch.json` (a control file no tool changes), and every connected app, TUI and browser is told at once.

Who may change it: you on a local connection, and the Snowpea browser's Settings page (protocol 1.58.0). The browser may change exactly `security.enabled`, `security.unknownCode`, `security.blockedCommands` and `security.protectedPaths`, and only from its Settings page; a remote web login, a connector, Jarvis and the agent's own tools cannot change any of them, and a project cannot turn the checks off.

### Remembering a script you allowed

When a call asks only because of what a script in your project does — say `bash bin/shot.sh`, where the script writes to a file name it computes — the card offers to remember your answer for **that exact command and that script as it is now** ("bash bin/shot.sh (while bin/shot.sh is unchanged)"). "For this session" is the default; "for this project" keeps it, in your own settings (never in the project's `.snowpea/settings.json`), listed with the other allow rules where you can remove it. The moment the script's content changes, snowpea asks again and says the script changed.

It is never offered when the script itself is not the only reason: something on the line itself snowpea cannot read, a script the same line writes first, a script outside the project, anything on snowpea's own files or command, or a script that runs code it takes from somewhere else (`eval` of a file's content, a pipe into a shell, a program named by a variable, a variable read as code). Only your own answer in the app, the TUI or the browser keeps it — not a remote web login, a messenger or Jarvis.

### Lines with `;`, `&&`, `||` or `|`

A chain of commands each of which would run without a question on its own (each has an allow rule, or, while planning, each only inspects) runs without one. For a chain that does ask, "for this session" remembers **that exact line**, run in the same folder, for this session only (spaces do not matter, every other character does); nothing is stored, a restart forgets it, and "for this project" on such a line counts for the session. A line snowpea cannot check, a remote web login, a messenger and Jarvis never remember one. A single command's answer is remembered exactly as before.

## Project trust pins what hooks run

When you trust a project (`snowpea project trust`, or the trust card), snowpea records the project's hooks, MCP servers, commands, agents, prompts and settings exactly as they are — and also **the project's own files they run**: the script a hook calls (`bash scripts/guard.sh`), a server's program inside the project, the files in a skill's folder. If one of them changes later, the hook or server is switched off until you look and trust again. A control file that is a symbolic link is not read for this.

The card lists them under "runs". Under **not pinned** it lists commands whose code snowpea cannot record, with the reason:

| Reason | What it means |
|---|---|
| `computed` | the command holds a variable or a substitution; what runs is decided when it runs |
| `project-script` | `npm run …`, `make …`: the project's own script table decides, and may change |
| `package-runner` | `npx …`, `uvx …`: a package looked up or downloaded when it runs |
| `module` | `python -m …`: found by the interpreter when it runs |
| `missing` | it names a project file that is not there |
| `outside-project` | it runs a file outside the project (`/home/me/tools/x.sh`): not the project's, so not recorded |
| `symlink` | it runs a link inside the project that leads out of it |
| `too-large`, `too-many`, `unreadable` | more than can be recorded, or a file that cannot be read |

"Not pinned" does not stop you from trusting the project. It means: for this command, trust covers the line you see, and what that line runs can change without snowpea asking you again.

**Only the files listed are pinned.** What one of them loads or runs in turn — a script it sources, a module it imports, a program it starts — is not recorded and can change without a question. The card says so.

Every agent definition a project brings (`.snowpea/agents/*.md`, `.claude/agents/*.md`) is part of this: it is not used until you trust the project for agents, and again after it changes. An agent you create by typing `/agent create` yourself (or from your own IDE or terminal) is recorded as snowpea wrote it and works at once; one the agent or a tool asked for (a queued command, Jarvis, a scheduled job) waits for your trust like a project's own.

In a project inside another project (a package folder with its own `.snowpea`), the inner project's commands, skills and agents win over the outer one's of the same name, and the outer project's are there only while you trust the outer project.

What you trust is what you were shown: if a file changes between the list and your click, nothing is trusted and snowpea shows the list again. `snowpea project trust` prints the list, asks, and trusts exactly that (`--yes` skips the question).

At startup the TUI asks "Do you trust this folder?" for a folder that is not trusted yet (see [the TUI](tui.md)): Yes trusts it as shown and starts at once, No exits.

The snowpea browser 0.4.0 or later can trust a folder too, after you confirm in its own window (see [Trusting a folder from the browser](web.md#trusting-a-folder-from-the-browser)): only on this computer, only the folder of a conversation that browser opened or hosts, and only the default scopes (hooks, MCP servers, commands, agents, prompts, settings). Letting the folder set the default mode or start external CLIs stays in the snowpea app or the terminal; a remote sign-in, the connector, Jarvis and messengers still cannot trust a project. Each grant is recorded with the browser that made it, in the trust record and in `logs/security.jsonl`.

If the record of trusted projects is lost or damaged — also when it and its marker both disappear while snowpea is not running — no project is trusted until you trust it again; snowpea says so with a session's first message and on the Security screen.

## Future work

What is not stopped today, plainly:

- **Code that runs without being read** (the section above) can do anything you can: read snowpea's token, talk to snowpea as you (trust a project, change Jarvis's rules — such a change comes through snowpea itself, so the tamper guard does not undo it), edit snowpea's files.
- **A program that finds snowpea's login at run time.** The token in `$SNOWPEA_HOME` is readable by every program that runs as you. snowpea refuses calls that name it and asks about calls it cannot read, but a program that builds the path while it runs is not seen.
- **Anything while snowpea is not running.** The tamper guard and the wait for your confirmation of settings exist only in the running program.

The next step is a boundary that does not depend on reading anything: snowpea will tell apart a connection that comes from a program it started (directly or through others) from one that comes from you — on Linux by keeping every started program in its own process tree and checking where a connection comes from, on Windows with a job object, on macOS with a sandbox profile — so nothing started by the agent can act as you towards snowpea. Until then, rely on the operating system for a hard guarantee.

## Practical advice

- **For a repository you do not trust, use accept mode**, where every command and every write is yours to approve, or a [sandbox session](sandbox.md).
- Remember that `auto` mode lets the project's own tests, builds and hooks run: trust a repository's code before you let the agent run it unattended.
- Do not point remote access at the open internet; keep it on a private network or behind a tunnel you control.
- For a hard guarantee use operating-system protection as well: a separate user, read-only mounts, file permissions, or a [sandbox session](sandbox.md).

The lists cover what goes through snowpea: its own tools, approvals, Jarvis, scheduled jobs, and external CLI sessions snowpea controls (their permission prompts are bridged). They cannot see:

- what a program works out while it runs: a path or a command built from a download, from a file's content, from arithmetic on text. snowpea asks when it can see that it can't tell; it cannot always see that;
- libraries that write where they are told (`numpy.save`, `DataFrame.to_csv`), compiled programs, and scripts in languages snowpea does not read (it asks about those only when the agent wrote them);
- external CLI sessions in unrestricted mode, ones whose prompts are answered in their own terminal, or ones running in another terminal. The session list marks them; snowpea warns when such a session opens while lists are set.

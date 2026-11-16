# Security lists

**What this page promises, and what it does not.** In `auto` mode the agent runs programs as you. The security lists and the rule about snowpea's own files stop the forms snowpea can recognise, and ask you about what it cannot read. A tamper guard undoes changes to the trusted-projects record and to Jarvis's delegation rules that something makes behind snowpea's back while it runs. That is all of it. A program that works out where snowpea's login token is while it runs, and reads it, can still act as you: none of this stops that. Keeping a tool's process away from those files is the operating system's job, and snowpea does not do that yet (it is planned, separate work). **For a repository you do not trust, use accept mode**, where every command and every write is yours to approve.

Two lists you set once and that hold for every session — with or without Jarvis mode, in every mode, whoever runs the call:

- **Blocked commands** never run. They are not asked, not approvable, and nobody (Jarvis, a scheduled job, a remembered answer, an "always allow" rule, auto mode) can let them through. The agent reads a refusal — `blocked by the security list: commands:essential — …` — and your screen shows "blocked by your security list".
- **Protected folders** are never changed. Each entry has a level: **no changes** (`write`, the default: reading is fine) or **no access** (`all`: not read, not listed, not changed).

Both lists start from **presets** you tick on or off, plus entries you type yourself.

## Where to set them

- **snowpea IDE and snowpea web**: Settings → Security.
- **Terminal UI**: the security screen (`/security`).
- A browser host and the remote web page can read the lists but not change them. Only you, on a local connection (IDE, TUI, CLI, the local web page), can change them.

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
| `essential` (on by default) | `/etc`, `/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/var/lib`, `/System` are never changed; `/` and your home folder themselves (and every folder above your home) are never deleted, moved or changed recursively; what runs when you log in or open a shell is never changed: `~/.ssh`, the shell start-up files (`~/.bashrc`, `~/.bash_profile`, `~/.bash_login`, `~/.bash_logout`, `~/.profile`, `~/.zshrc`, `~/.zprofile`, `~/.zshenv`, `~/.zlogin`, `~/.config/fish/config.fish`), `~/.config/autostart`, `~/.config/systemd/user`, `~/Library/LaunchAgents`, `/Library/LaunchAgents`, `/Library/LaunchDaemons` | no changes |
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

## snowpea's own files are always protected

Apart from your lists there is one rule you cannot switch off. The files that decide what snowpea trusts, and who may talk to it, are out of every tool's reach — in every mode, with any presets, even with both lists empty:

- **Never read, never changed**: `daemon.json`, `token`, `credentials.json` and `mcp-oauth.json` in `$SNOWPEA_HOME`. With these a program could act as you. The logins of other agent CLIs are treated the same, in snowpea's folder and in your home: `~/.codex/auth.json`, `~/.claude/.credentials.json`, `~/.claude.json`, the Gemini and Cursor logins, `~/.config/anthropic`.
- **Never changed** (reading is fine): `settings.json`, the MCP servers started for every project (`.mcp.json` in `$SNOWPEA_HOME`), the trusted-projects record (`trust/`), Jarvis's rules and ledger (`jarvis/`), the session store, the update files, the approval and security logs.
- **Always asked**: a shell line or code that writes into your own hooks, plugins, extensions, skills, commands or agent definitions in `$SNOWPEA_HOME`.
- **Always asked**: a line that runs snowpea's own command (`snowpea …`, `python -m snowpea_core …`, through `uv run`, a script of snowpea's, from inside code in any language). That command acts as you, so only you can let it run. The card says "this runs snowpea's own CLI".
- **Always asked**: one of the "never changed" files handed to a program that may change it. `cat`, `grep`, `jq`, `ls`, `stat`, `cp` (as the source) and the like only read, and run as before. Anything else asks: a database client opened on the session store (`sqlite3 ~/.snowpea/state.db …`, even to look), a link to one of the files (`ln -s`), a variable set to its path, code that names it and does more than open it to read.

The agent's own working places stay as they were: plan files, session workspaces, attachments, caches.

A blocked call reads `blocked by the security list: security — $SNOWPEA_HOME/token holds snowpea's own secrets …`. An "always asked" one is yours alone to answer: Jarvis never decides it, and the answer is never remembered.

### The tamper guard

The rule above reads what a call says before it runs. A program that builds the path while it runs is not read. For the two records that matter most there is a second layer that reads nothing: while snowpea runs, it is the only one that writes the trusted-projects record (`trust/`) and Jarvis's delegation rules (`jarvis/delegation.db`). Every time snowpea is about to use one of them it first looks whether the file is still what it last wrote. If something else changed, replaced or removed it, the change is **not used**: snowpea keeps its own copy, puts the file back, writes a line to `logs/security.jsonl` and tells you ("something outside snowpea changed the trusted-projects record … the change was not applied"; on the Security screen too). For Jarvis's rules you get a Jarvis notice.

The guard holds while snowpea runs. If snowpea is not running, nobody is watching: at the next start it takes the files as they are.

### Security settings edited in the file

You can edit `settings.json` by hand, and most of it is picked up as before. Three parts are different while snowpea runs — `security` (your lists, `unknownCode`), `jarvis` and `web.remote`: an edit of the file does **not** take effect by itself. snowpea keeps the values it has, tells you "settings.json changed on disk: security settings wait for you", and applies them only when you say so on a local connection (IDE, TUI, CLI, the local web page). You can also discard the edit: snowpea then writes its own values back to the file. Until you answer, the Security screen keeps showing that settings wait. Changing the same settings on the Security screen works as always.

## Lines snowpea cannot check

snowpea lets a line run unasked only for what it understood. Whatever it could not read is put to you, in every mode, marked "cannot be checked against your block list"; Jarvis never decides it. It is not refused — you decide.

What cannot be read: the program comes from a variable or a substitution (`$CMD …`, `` `echo rm` … ``), `eval` of a variable, a script piped into a shell (`… | sh`), a `sh -c` built from variables, inline code for a program snowpea has no reader for (`mongosh --eval`, `gdb -ex`, `Rscript -e`, `elixir -e`, …), a command sent to another machine (`ssh host 'command'`; on this machine the command is read) or handed over to run later (`crontab file`, `at`, `batch`, `launchctl load`), a function that takes the name of a shell builtin (`cd() { … }`), a path only the shell knows and nothing in front of it tells where it lands. These first ones are asked **even when both lists are empty**, because an act on snowpea's own files could hide there.

The same goes for a line that changes what the next program is or does:

- a variable that carries code or redirects configuration — `LD_PRELOAD`, `BASH_ENV`, `NODE_OPTIONS=--require …`, `GIT_SSH_COMMAND`, `EDITOR`/`PAGER` set to a program, `PATH` with a folder in the project, a temp folder or a relative one, `PIP_INDEX_URL`, `NPM_CONFIG_SCRIPT_SHELL`;
- `HOME`, `PWD`, `TMPDIR`, `SNOWPEA_HOME`, `CDPATH` or an `XDG_*` variable given another value: snowpea reads `~` and `$HOME` with your real home, so after such an assignment it cannot say where a path lands;
- `git -c` with a setting that runs a program, an editor started with commands (`vim -c '!…'`), an `awk` program that calls `system`.

What stays unasked, so you know the line is not hurt by this: `PATH=/usr/local/bin:/usr/bin:/bin make`, `export PATH="$PATH:/usr/local/bin"` and any folder already on snowpea's own `PATH`; `NODE_ENV=production npm test`, `CI=1 …`, `FOO=bar ./script.sh`; `PYTHONPATH=src pytest`; `CC=gcc make`, `MAKEFLAGS=-j8`; `GIT_PAGER=cat`, `EDITOR=true`; `PIP_NO_CACHE_DIR=1`, `NPM_CONFIG_LOGLEVEL=error`, `CARGO_TERM_COLOR=always`; `TMPDIR=/tmp` when that is the temp folder anyway.

## Code snowpea can't tell about

snowpea reads the code a line runs — `python -c`, `node -e`, `bun -e`, `deno eval`, a here-document fed to an interpreter, a script file, `execute_code` — without running it:

- Code that deletes, overwrites or moves something **it names** is judged like the command it stands for. `shutil.rmtree('/home/me')` is `rm -rf /home/me`, however it is spelled, imported or aliased.
- Code that only reads, prints or computes, or that writes to a path it names, runs as before.
- Code where snowpea **can't tell** asks you, in every mode, with "can't tell what this code changes". That is: a delete, write, copy, move or `chmod` whose target is computed while the code runs (`os.remove(path)`); a command built from variables; code that does not parse; calls reached through `getattr`, `eval`, `exec` or a computed import; an interpreter fed code snowpea cannot read (`curl … | python3`, a bare `python3 -`); `python -m` of a module that is neither a known tool nor in your project.

Scripts that were in your project before the session are your project's own: what they plainly do is judged (a blocked command in them is blocked), what they compute is not held against them. A script the agent wrote in this session is judged strictly, for the whole session and after a restart.

Also asked: code that runs a string as code by another door (`timeit.timeit("…")`, `cProfile.run`, `pdb.run`, node `new Function(…)`, a worker started from a string) when the string is not written out; loading pickled data (`pickle.load`), which is code once loaded; code that changes its own names or surroundings behind snowpea's back (`os.chdir`, setting `HOME` in `os.environ`, assigning into `__main__`).

Common harmless things that now ask, so you know why you see the card: a script the agent just wrote that writes to a path it received as an argument (`open(args.out, "w")`); `subprocess.run(cmd)` with a built command; `python -m http.server`; `make` on a Makefile the agent just wrote that uses make variables; `python -c "print('$NAME')"` (the shell fills in text before Python reads it); `pickle.load(open('model.pkl', 'rb'))`; `HOME=/tmp/x npm test`; `ssh build-host make`; `node dist/index.js` when `dist/index.js` is built by the same line and is not there yet; `PYTHONPATH=$(pwd) pytest` (write `PYTHONPATH=. pytest`).

### `security.unknownCode`

If those questions are more than you want, you can switch some of them off on the Security screen, or in `$SNOWPEA_HOME/settings.json` (a hand edit of this setting waits for your confirmation, see above):

```json
{ "security": { "unknownCode": "allow" } }
```

`"ask"` is the default. What `"allow"` means, exactly: code runs without a question when the only thing snowpea could not tell is **which path** an ordinary write, copy, move or delete lands on (`os.remove(path)`, `open(out, "w")`), when the code is too large or too odd to parse, or when it is `python -m` of a module snowpea does not know.

It does **not** switch off the rest. These still ask with `"allow"`: a command built from variables (`subprocess.run(cmd)`); a call reached through `getattr`, `globals()`, an alias or a computed import; `exec`/`eval` of a computed string and the other ways to run a string as code; native code (`ctypes`); code fed to an interpreter that snowpea never saw (`curl … | python3`); code in a language snowpea has no reader for; anything that touches snowpea's own files or command. Blocked commands and protected folders are still blocked.

Only you, on a local connection, can change it; a project cannot.

## Project trust pins what hooks run

When you trust a project (`snowpea project trust`, or the trust card), snowpea records the project's hooks, MCP servers, commands, agents, prompts and settings exactly as they are — and also **the project's own files they run**: the script a hook calls (`bash scripts/guard.sh`), a server's program inside the project, the files in a skill's folder. If one of them changes later, the hook or server is switched off until you look and trust again.

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

Every agent definition a project brings (`.snowpea/agents/*.md`, `.claude/agents/*.md`) is part of this: it is not used until you trust the project for agents, and again after it changes. An agent you create yourself with `/agent create` is yours and works at once.

In a project inside another project (a package folder with its own `.snowpea`), the inner project's commands and skills win over the outer one's of the same name, and the outer project's are there only while you trust the outer project.

What you trust is what you were shown: if a file changes between the list and your click, nothing is trusted and snowpea shows the list again. `snowpea project trust` prints the list, asks, and trusts exactly that (`--yes` skips the question).

If the record of trusted projects is lost or damaged — also when it and its marker both disappear while snowpea is not running — no project is trusted until you trust it again; snowpea says so with a session's first message and on the Security screen.

## Limits

What is **not** stopped, plainly:

- **A program that finds snowpea's login at run time.** The token in `$SNOWPEA_HOME` is readable by every program that runs as you. snowpea refuses calls that name it and asks about calls it cannot read, but a program that builds the path while it runs (from pieces, from a listing, from another file) is not seen. With the token it can talk to snowpea as you: trust a project, change Jarvis's rules. The tamper guard does not help there, because such a change comes through snowpea itself.
- **Anything while snowpea is not running.** The tamper guard and the wait for your confirmation of security settings exist only in the running program.
- **The real fix is not here yet.** Keeping a tool's process from reading or writing those files at all needs the operating system (a sandbox for tool processes). That is planned as separate work. Until then, for a repository you do not trust: **accept mode**, or a [sandbox session](sandbox.md).

The lists cover everything that goes through snowpea: its own tools, approvals, Jarvis, scheduled jobs, and external CLI sessions snowpea controls (their permission prompts are bridged). They cannot see:

- what a program works out while it runs: a path or a command built from a download, from a file's content, from arithmetic on text. snowpea asks when it can see that it can't tell; it cannot always see that;
- libraries that write where they are told (`numpy.save`, `DataFrame.to_csv`), compiled programs, and scripts in languages snowpea does not read (it asks about those only when the agent wrote them);
- external CLI sessions in unrestricted mode, ones whose prompts are answered in their own terminal (tmux), or ones running in another terminal. The session list marks them; snowpea warns when such a session opens while lists are set.

All of this is a check of what a call says, before it runs. It is not a wall around the process. For a hard guarantee, use operating-system protection as well: a separate user, read-only mounts, file permissions, or a [sandbox session](sandbox.md).

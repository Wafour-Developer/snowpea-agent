# Security lists

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
| `essential` (on by default) | `/etc`, `/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/var/lib`, `/System` are never changed; `/` and your home folder themselves (and every folder above your home) are never deleted, moved or changed recursively | no changes |
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

- **Never read, never changed**: `daemon.json`, `token`, `credentials.json` and `mcp-oauth.json` in `$SNOWPEA_HOME`. With these a program could act as you.
- **Never changed** (reading is fine): `settings.json`, the trusted-projects record (`trust/`), Jarvis's rules and ledger (`jarvis/`), the session store, the update files, the approval and security logs.
- **Always asked**: a shell line or code that writes into your own hooks, plugins, extensions, skills, commands or agent definitions in `$SNOWPEA_HOME`.
- **Always asked**: a line that runs snowpea's own command (`snowpea …`, `python -m snowpea_core …`, through `uv run`, a script of snowpea's). That command acts as you, so only you can let it run. The card says "this runs snowpea's own CLI".

The agent's own working places stay as they were: plan files, session workspaces, attachments, caches.

A blocked call reads `blocked by the security list: security — $SNOWPEA_HOME/token holds snowpea's own secrets …`. An "always asked" one is yours alone to answer: Jarvis never decides it, and the answer is never remembered.

## Lines snowpea cannot check

Some lines cannot be checked: the program comes from a variable (`$CMD …`), `eval` of a variable, a script piped into a shell (`… | sh`), a `sh -c` built from variables, or a path only the shell knows and nothing in front of it tells where it lands. While a list is set, such a line is never run on its own and never decided by Jarvis: it is put to you, in every mode, marked "cannot be checked against your block list". It is not refused — you decide.

The same goes for a line that hides what it runs: a variable that carries code for the next program (`NODE_OPTIONS=--require …`, `BASH_ENV=…`, `LD_PRELOAD=…`), `git -c` with a setting that runs a program, an editor started with commands (`vim -c '!…'`), an `awk` program that calls `system`.

## Code snowpea can't tell about

snowpea reads the code a line runs — `python -c`, `node -e`, a here-document fed to an interpreter, a script file, `execute_code` — without running it:

- Code that deletes, overwrites or moves something **it names** is judged like the command it stands for. `shutil.rmtree('/home/me')` is `rm -rf /home/me`, however it is spelled, imported or aliased.
- Code that only reads, prints or computes, or that writes to a path it names, runs as before.
- Code where snowpea **can't tell** asks you, in every mode, with "can't tell what this code changes". That is: a delete, write, copy, move or `chmod` whose target is computed while the code runs (`os.remove(path)`); a command built from variables; code that does not parse; calls reached through `getattr`, `eval`, `exec` or a computed import; an interpreter fed code snowpea cannot read (`curl … | python3`, a bare `python3 -`); `python -m` of a module that is neither a known tool nor in your project.

Scripts that were in your project before the session are your project's own: what they plainly do is judged (a blocked command in them is blocked), what they compute is not held against them. A script the agent wrote in this session is judged strictly, for the whole session and after a restart.

Common harmless things that now ask, so you know why you see the card: a script the agent just wrote that writes to a path it received as an argument (`open(args.out, "w")`); `subprocess.run(cmd, shell=True)` with a built command; `python -m http.server`; `make` on a Makefile the agent just wrote that uses make variables.

### `security.unknownCode`

If those questions are more than you want, switch them off in `$SNOWPEA_HOME/settings.json` (the Security screen shows the same switch in app versions that have it):

```json
{ "security": { "unknownCode": "allow" } }
```

`"ask"` is the default. With `"allow"`, code snowpea can't tell about runs like any other line in the session's mode. Everything else stays as it is: blocked commands and protected folders are still blocked, snowpea's own files are still protected, and snowpea's own command still asks. Only you, on a local connection, can change it; a project cannot.

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
| `too-large`, `too-many`, `unreadable` | more than can be recorded, or a file that cannot be read |

"Not pinned" does not stop you from trusting the project. It means: for this command, trust covers the line you see, and what that line runs can change without snowpea asking you again.

What you trust is what you were shown: if a file changes between the list and your click, nothing is trusted and snowpea shows the list again. `snowpea project trust` prints the list, asks, and trusts exactly that (`--yes` skips the question).

If the record of trusted projects is lost or damaged, no project is trusted until you trust it again; snowpea says so when a session opens and on the Security screen.

## Limits

The lists cover everything that goes through snowpea: its own tools, approvals, Jarvis, scheduled jobs, and external CLI sessions snowpea controls (their permission prompts are bridged). They cannot see:

- what a program works out while it runs: a path or a command built from a download, from a file's content, from arithmetic on text. snowpea asks when it can see that it can't tell; it cannot always see that;
- libraries that write where they are told (`numpy.save`, `DataFrame.to_csv`), compiled programs, and scripts in languages snowpea does not read (it asks about those only when the agent wrote them);
- external CLI sessions in unrestricted mode, ones whose prompts are answered in their own terminal (tmux), or ones running in another terminal. The session list marks them; snowpea warns when such a session opens while lists are set.

All of this is a check of what a call says, before it runs. It is not a wall around the process. For a hard guarantee, use operating-system protection as well: a separate user, read-only mounts, file permissions, or a [sandbox session](sandbox.md).

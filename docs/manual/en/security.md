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

## Lines snowpea cannot check

Some lines cannot be checked: the program comes from a variable (`$CMD …`), `eval` of a variable, a script piped into a shell (`… | sh`), a `sh -c` built from variables, or a path only the shell knows and nothing in front of it tells where it lands. While a list is set, such a line is never run on its own and never decided by Jarvis: it is put to you, in every mode, marked "cannot be checked against your block list". It is not refused — you decide.

## Limits

The lists cover everything that goes through snowpea: its own tools, approvals, Jarvis, scheduled jobs, and external CLI sessions snowpea controls (their permission prompts are bridged). They cannot see:

- inside a script a command runs (`./deploy.sh`, `python tool.py`, a Makefile target) — snowpea checks the line, not the file;
- inside inline code beyond its string literals (`python -c`, `execute_code`: a literal like `'rm -rf /'` is caught, code that builds the command is not);
- external CLI sessions in unrestricted mode, ones whose prompts are answered in their own terminal (tmux), or ones running in another terminal. The session list marks them; snowpea warns when such a session opens while lists are set.

For a hard guarantee, use operating-system protection as well: a separate user, read-only mounts, file permissions, or a [sandbox session](sandbox.md).

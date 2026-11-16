# M20 — Security lists, remote web access, security review 4 (protocol 1.54.0)

Owner requests of 2026-11-16 ("보안 관련 실행되면 안 되는 명령어 등 및 보호 목록 설정", remote access
decision 2b) and the fourth security review. Everything here is additive to 1.53.0
(`docs/design/m19-jarvis-mode.md`). User-facing text: `docs/manual/{en,ko}/security.md`,
`docs/manual/{en,ko}/web.md`.

## 1. Security lists

### What it is

A general feature, with or without Jarvis mode:

- **Blocked commands** never run: not asked, not approvable, not delegable, not allowed by a mode, an
  allowlist rule or a cached answer. The model reads a refusal result `blocked by the security list:
  <by> — <why>`; the surfaces get `security.blocked`; `$SNOWPEA_HOME/logs/security.jsonl` gets a line;
  while Jarvis mode is on the ledger gets a record (`kind: escalate`, `decided_by: auto`,
  `reason: security:<by>`).
- **Protected paths** are never changed (level `write`) or never touched at all (level `all`).

### Settings

```
security.blockedCommands: {presets: string[], custom: string[]}            # custom = patterns
security.protectedPaths:  {presets: string[], custom: [{path, level: "write"|"all"}]}
```

- Global (`settings.json`) — `SecuritySettings` in `config/settings.py`. An absent key means the
  defaults (`essential` on for both lists): a fresh install and an install from before 1.54.0 are
  protected the same; an explicit empty list stays empty. Values are validated by `settings.set`
  (`invalid_params`, `details.problems`), not by the model, so a hand-edited file never stops the
  daemon: unknown presets and broken patterns are skipped and listed by `security.get.problems`; an
  unknown level fails closed to `all`.
- Project (`<project>/.snowpea/settings.json`, key `security`, same shape, empty by default): **adds
  only**. The effective lists are the union; a project-scope `settings.set` that would remove a
  project entry is `invalid_params` with `details.reason: "project_add_only"`.
- Writes: `settings.set {scope: "global", patch: {security: {...}}}` — nested patch, arrays replace.
  Only a local person (`settings_gate.PERSON_LOCAL_ONLY` has `security`); `settings.changed` fires with
  the key `security`.
- Any attempt to change the lists through a tool is `blocked` (`by: "security"`), even with empty
  lists: `settings_set` with a `security` key; `write_file`/`patch`/edit tools whose result (the edit
  is applied to the file's text and the `security` subtree compared; an unparseable result counts as
  a change) differs; any delete/move of a settings file; a shell write to a settings file or its own
  folder.

### Presets

Commands (`security/catalog.py`, en + ko, each with `examples` it blocks and `near_misses` it does
not — the test suite holds every preset to both): `essential` (default), `mass-delete`, `disk`,
`permissions`, `power`, `git-destructive`, `remote-script`, `package-global`, `process-kill`,
`containers`, `database`, `cloud`. Paths: `essential` (default; `/` and the home are `self`
entries — never deleted, moved or recursively changed, nor through a folder above them; `/etc`,
`/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/var/lib`, `/System` are trees), `credentials` (level
`all`), `snowpea-home` (`plans/` exempt), `other-projects` (every known project but the session's own,
resolved at check time), `mounts` (`/proc/mounts` data and network disks, `/Volumes/*`, the session's
project exempt, a disk holding the home or `$SNOWPEA_HOME` left out).

### Matching (deterministic, fail closed)

`security/shell.py` drives the classifier's parser (`jarvis/classify.py` `_Judge`, subclassed as
`_Probe`) and collects every simple command and every path effect instead of a verdict. It follows the
wrappers the classifier already knows (`sudo`, `env`, `nice`, `timeout`, `xargs`, `find -exec`,
package runners, …), and additionally reads the bodies of `sh -c`/`bash -c`, `eval` of literal text,
`su -c`, `watch`, `flock -c`, `env -S`, here-strings and literal here-documents fed to a shell, and
every `$(…)`/backtick/`<(…)` body (also inside double quotes). The scanner now keeps a here-string or
here-document body (`_Cmd.stdin_text`, `stdin_dynamic`); nothing else in the classifier changed.

Custom patterns (`security/patterns.py`, documented in the manual): program (name or family, globs
allowed, never only a wildcard), options that must all be present (short clusters, long options with
every unique-prefix abbreviation and `=value`, per-program equivalents such as `rm -r`=`-R`=
`--recursive`, `git branch -D` = `--delete --force`, whole words for `find`), positional words in
order with `*`, path words resolved, and `re:<regex>` searched in each command's text. Validation on
save says what is wrong.

Paths: every effect is resolved against the call's folder (`cwd`/`workdir` argument, else the
project; `cd` is followed), `~`, `$HOME`, `$PWD`, `$SNOWPEA_HOME`, `$TMPDIR` expanded, symlinks
followed on both sides (each protected path is checked as written and resolved). A glob, or a path
whose tail only the shell knows, is judged by every protected path it could reach (component-wise
match, `**` reaching everything). Writes hit a tree entry at or below it; deletes, moves and recursive
changes also hit through any ancestor; level `all` also hits reads, listings, recursive searches of an
ancestor and any command naming a path inside. Programs whose write semantics the classifier does not
spell out (`rm`, `mv`, `cp`, `ln`, `install`, `rsync`, `chmod`/`chown`/`chgrp`/`chattr`/`setfacl`,
`truncate`, `touch`, `mkdir`, `tee`, `rename`, `shred`, `trash`, `gio`) are judged in `shell.py`
(destination landing paths included); tree-rewriting git subcommands (`checkout`, `switch`, `reset
--hard`, `clean`, `restore`, `stash`, `merge`, `rebase`, `pull`, …) write their work tree recursively.
A protected path inside the project is never exempt.

Opaque: when a list is non-empty and part of a call cannot be seen — a dynamic program, `eval` of a
variable, a `-c` body or here-document built from variables, a shell reading a pipe or file on stdin
(covers encoded payloads), PowerShell/cmd bodies, `parallel`, a line the parser cannot split, a path
with nothing known before its unknown part (writes; reads only when an `all` entry exists) — the call
is never run without the person and never decided by Jarvis: `ApprovalRequest.reason: "cannot be
checked against your block list"`, `personOnly`, `route: "person"`, uncacheable, in every mode. It is
not refused.

Inline code (`python -c`, `node -e`, `execute_code`): its string literals that read as command lines
are checked as commands (best effort, never opaque). Scripts a line runs are not read.

### Enforcement points (all before anything else)

| Where | What happens |
|---|---|
| `agent/loop.py` `_run_one_call` (snowpea's own tools, every mode, Jarvis session, workers, scheduled jobs) | blocked: refusal result, `security.blocked`, audit line, the turn continues; opaque: forced `ask`, uncacheable |
| `permissions/approval_queue.py` `request` (every ask, before the session cache, the router and the allowlist promotion) | blocked: `Decision("deny", by="security", code="security_blocked", reason=refusal)`, never posted; opaque: person-only, route `person` |
| `external/manager.py` `_ask_approval` (bridged CLI prompts, before the per-session cache) | blocked: `ApprovalAnswer(allow=False, reason=refusal, by="security")` — the CLI's model reads why |
| Jarvis's answers | a blocked request never exists; an opaque one is `personOnly` (Jarvis's `approval_decide` gets `jarvis_person_only`) |

`ExternalSessionRow.securityListsApply` / `JarvisOverviewRow.securityListsApply`: null with empty
lists; false for a session snowpea cannot see into (unrestricted/uncontrolled, a tmux pane answering
its own prompts, another terminal).

### Contract (capability `securityLists`)

- `security.presets.list {locale}` → `{commands: [SecurityPreset], paths: [SecurityPreset + level,
  paths], locale}`.
- `security.get {workdir?, locale?}` → stored global lists, `project` additions, `effective`
  (`blockedCommands`, `protectedPaths`, `commands: [{pattern, by}]`, `paths: [{path, level, scope,
  by, exempt}]`), `writable` (local person only), `problems`.
- `security.check {command?, path?, access?, workdir?, draft?, locale?}` → `{blocked, by, why, opaque,
  reason?, match?, level?}`; `draft` judges unsaved lists (invalid → `invalid_params`).
- Event `security.blocked {sessionId, tool, summary, by, why, surface}`.
- `by`: `commands:<presetId>`, `paths:<presetId>`, `custom`, or `security` (an edit of the lists).
- Error code `security_blocked` (approval decision code).
- People only: `security.*` in `person_gate.PERSON_METHODS` (a remote web login reads,
  `writable: false`); never from a browser connection (`BROWSER_NEVER_PREFIXES`).
- `ApprovalRequest.neverDelegable` and `reason`; `QuestionRequest.neverDelegable`.

### Deviations from the client-side draft

- `security.get.effective` carries both the draft's `commands`/`paths` arrays (what the TUI renders)
  and the stored shape (`blockedCommands`, `protectedPaths`).
- `security.presets.list` path presets carry `paths` (resolved on this machine now) and the result a
  `locale`, as the TUI's local types expected.
- `by: "security"` is new: an attempt to edit the lists through a tool.

### Limits (stated in the manual)

Works for everything that goes through snowpea. Cannot see inside scripts a line runs, inside inline
code beyond literals, or inside external CLIs that are unrestricted, answer prompts in their own pane,
or run in another terminal. For hard guarantees: OS-level protection.

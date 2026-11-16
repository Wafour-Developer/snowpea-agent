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

## 2. Remote web access at runtime (owner decision 2b)

- `snowpea web --host H --port P [--user U] [--password PW | --password-stdin | $SNOWPEA_WEB_PASSWORD]
  [--save]` opens the external listener now (`web.remote.listen`), no daemon restart;
  `--save` writes `web.remote.{enabled,host,port,username}`. Without flags: unchanged (loopback).
- **Access link** (no password): a web access token of its own (`credentials.json`
  `web.remote.accessToken`, 32 random bytes, never the daemon token). The first `GET /web/…?t=…`
  compares it in constant time, sets the session cookie (`HttpOnly`, `SameSite=Strict`, `Secure`
  over HTTPS) and redirects to the same address without `t`. Wrong tokens count in the same
  per-address/global limiter as wrong passwords. `snowpea web token [--rotate]`,
  `web.remote.rotateToken`: rotation (and any password change) changes the session fingerprint,
  so every open session ends.
- **Password mode** (username + password set) is stricter: `?t=` is dropped (redirect, no cookie)
  and only the login page opens a session. `--password` on the command line warns (shell history,
  process list).
- Every connection through the external listener is `remote_login = True` (link or password).
  Remote login = the person for running work (sessions, approvals of its own sessions, raising a
  session's mode, MCP servers, providers, skills, memory, LSP/audio installs, messengers —
  `settings_gate.REMOTE_LOGIN_METHODS` / `REMOTE_LOGIN_KEYS`), never for security configuration
  (jarvis.*, security.*, web.*, daemon, extensions, allowlist rules, mcp.permissions, CLI path
  confirmation, unrestricted mode, guarded-class grants) — refused `jarvis_person_only`.
- RPC (local person only, `person_gate.PERSON_METHODS`): `web.remote.status` → `{enabled,
  listening, host, port, username, passwordSet, mode, listenUrls, accessUrl, sessions,
  needsRestart, plainHttp, warning, error, turnsRunning}`; `web.remote.listen {host, port,
  username?, save?}`; `web.remote.stop {save?}`; `web.remote.setPassword {password, username?}`;
  `web.remote.clearPassword`; `web.remote.rotateToken` → `{accessUrl}`. Capability
  `webRemoteControl`. `config_problem` now only refuses a password without a username.
- `trustProxy`, `allowedOrigins` and the WebSocket Origin check are unchanged. The page's
  `/web/session` still hands the page the daemon token for `system.hello` (unchanged).

## 3. Security review 4 (decisions as implemented)

| Finding | How it is closed | Tests (`tests/test_security_review4.py` unless named) |
|---|---|---|
| C1 mode questions | `QuestionRequest.personOnly` + `kind: "mode"` + `neverDelegable` on the set_mode and plan-review pickers; `route_question` returns person first; `QuestionQueue.respond` refuses any non-person (`jarvis_person_only`, `details.reason: "mode_raise"`) and the card stays; the hosting browser may answer with `actor: "person"`; Jarvis's `question_answer` refuses; the tool raises the mode only with a person-verified answer | `test_a_mode_question_is_the_persons_alone`, `test_the_hosting_browser_may_answer_with_the_marker`, `test_jarvis_question_answer_refuses_a_person_only_question`, `test_set_mode_tool_asks_person_only_and_uses_the_verified_answer` |
| C1/H2 raising a mode | one guard (`session/mode_guard.py`) in `SessionManager.set_mode` and `create`, with the caller's identity; `plan`+autoAfterPlan counts as `auto`; every refusal `details.reason: "mode_raise"` | `test_set_mode_raise_needs_a_person`, `test_plan_then_auto_counts_as_auto`, `test_a_browser_raises_only_its_own_session`, `test_create_wider_than_the_default`, `test_job_schedule_and_update`, `test_mode_commands_are_refused_before_the_turn` |
| C2 `test -v 'a[$(…)]'` | `inspect_table._test` allows only unary file tests / `-n`/`-z` / `=`,`!=`,`==` on literal operands; the classifier makes `-v`/`-R` or a subscript with `$`/backtick guarded-all (`exec:opaque:test-subscript`) | `test_review4_pairs` |
| H1 allowlist.add | local person only (`PERSON_METHODS`, `/allow` too), never a browser (`BROWSER_NEVER_METHODS`); catch-all rules refused at add time (`allowlist.catch_all_problem`, probe commands of 16 programs) | `test_catch_all_rules_are_refused`, `test_allowlist_add_is_a_local_persons_only` |
| H3 session cache | the approval queue classifies first; a guarded/never request is never served from the first-word cache (a folder-keyed outside-project answer keeps working) | `test_session_cache_never_serves_a_guarded_call` |
| H4 connectors / remote logins | `settings_gate.kind_method_refusal` in the dispatcher and `guard_call`: a connector calls no config method (`CONNECTOR_METHODS` is empty: its own connection only says `system.hello`); a remote login only `REMOTE_LOGIN_METHODS` | `test_a_connector_calls_no_config_method`, `test_a_remote_login_operates_but_never_configures_security` |
| H5 cd chains, hard links | every `cd`/`pushd` target judged as a read; outside the project every bare operand is a path; `ln` (no -s) / `link` sources are reads; a hard-linked or non-regular file is never `safe` | `test_review4_pairs`, `test_hard_link_out_of_a_secret_folder`, `test_hard_linked_and_odd_files_are_never_safe` |
| H6 control files | a destination folder's `dest/<basename>` judged (cp/mv/ln/install, `-t`); writing `.claude`/`.codex`/`.cursor`/`.gemini`/`.agy` itself is control; extraction and `patch` with unknown members guarded-all; tree-materialising git (`switch`, `checkout`, `merge`, `rebase`, `cherry-pick`, `am`, `pull`, `revert`, `restore` but `--staged`, `stash apply/pop`, `read-tree -u`, `checkout-index`) guarded-all — "check out a branch" therefore always asks the person unless all three classes were granted | `test_control_files_through_directory_destinations`, `test_review4_pairs` |
| M1 whitespace | operands are never stripped; one with leading/trailing whitespace is never `safe` | `test_hard_linked_and_odd_files_are_never_safe` |
| M2 external cwd | an external call without `cwd` never makes a relative read read-like; the external cache key includes `cwd` | `test_external_relative_reads_without_a_cwd_are_not_read_like` |
| M3 PATH | the shell tool drops relative and in-project PATH entries at spawn (`exec.local.sanitized_path`); a `safe` verdict requires each program to resolve outside every project and world-writable folder | `test_path_sanitising_drops_relative_and_in_project_entries`, `test_a_planted_program_is_not_safe` |
| M4 secret names | case-insensitive, plus `*.env`, `.env*`, `.envrc`, `*.tfvars`, `.dev.vars`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_*`, `*.keystore`, `secrets.*`, `credentials*` | `test_in_project_secret_names` |
| M5 legacy browser | person-only and Jarvis-routed checks come before the legacy short-circuit. The browser host is trusted code; the marker guards against accidents, not a hostile host | existing review-3 tests |
| M7 git | `remote -v` not read-like; `--format`/`--pretty` with `%G` not read-like; writes to `objects/info/alternates`, `info/grafts`, `refs/replace/` guarded-all. Limit: a user-global `include.path` can still point git at another config | `test_plain_test_and_git_reads_stay_unchanged`, `test_git_alternates_and_grafts_are_control` |
| Low | `respond` returns whether it resolved and Jarvis ledgers truthfully; `*.resolved` sends `by: "messenger"` to browsers/connectors; `hostname -i/-f/-d/-A` and `date -d` left the table; FIFOs/devices never `safe` | — |

### Mode raise: every writer of a session's mode

Order `plan < accept < auto` (`permissions.policy.MODE_STRICTNESS`); `plan` with `autoAfterPlan`
counts as `auto`. Default for a new session or job: the project's `defaultMode`, else `accept`
(`session.manager.DEFAULT_MODE`).

| Path | Caller passed | Verdict |
|---|---|---|
| `session.setMode` | the connection + `actor` | raise needs a person |
| `session.create {mode}` | the connection + `actor` | wider than the default needs a person |
| `command.run` `/auto`, `/accept`, `/mode <m>`, `/mode plan auto`, `/mode save` (project default) | the connection + `actor`, checked before the turn and again in the command | raise needs a person |
| `set_mode` tool pickers | `PERSON_ANSWER` only for a person-verified answer, else the agent | the model / Jarvis cannot raise |
| `job.schedule {mode}` | the connection + `actor` | wider than the default needs a person; a re-created job is new |
| `job.update` | no mode field | never raises (enable/disable free) |
| `schedule_create` tool | approval with `personOnly` | only a person's allow |
| scheduler runs | `internal("scheduled-job")` | the job's stored mode only |
| subagents / team workers | `internal("subagent-inherits-parent")` | never wider than the parent (`stricter_mode`) |
| fork | `internal("fork-keeps-mode")` | the source's mode |
| messenger new session | `internal("messenger-binding")` | the binding's configured mode (set with `gateway.bind`) |
| named agents | `internal("named-agent-definition")` | the definition's `permission` for an agent a person created |
| Jarvis task workers | `internal("jarvis-task-worker")` | fixed `accept` |
| restore / resume | no `set_mode` call | the stored mode, not a raise |
| `session.setAgent` | — | does not change the mode (checked: `host_handlers.session_set_agent_handler`) |
| Jarvis's own session | pinned (`ModePinned`) | — |

### The `actor` marker, everywhere

The dispatcher accepts `actor` on EVERY method from every connection kind: only `"person"` (else
`invalid_params`), recorded for the call (`rpc.current_actor()`), and removed before validation
unless the method's model declares it. The replies to `approval.request` / `question.request` carry
it the same way. `ApprovalResolvedNotification.scope` and `approval.respond`'s result `scope` report
the scope actually applied (a browser's unmarked widening reply → `once`).

### Browser gate (2026-11-16)

- An unmarked `skill.install` from a browser (0.3.0+) is accepted only for the browser's bundled
  plugin: real path `…/snowpea-browser-host/snowpea-plugin`, `.claude-plugin/plugin.json` a regular
  file (not a symlink) and every manifest named exactly `snowpea-browser` (so the install creates or
  replaces only that plugin; installed from the real path), or the daemon's
  `SNOWPEA_BROWSER_DEV_PLUGIN` folder. The old `$SNOWPEA_HOME/plugins|extensions` rule is gone.
  Refusal: `jarvis_person_only`, `data.details.reason: "not_bundled_plugin"`, `data.details.why`.
- `permission.allowlist.add` is never allowed from a browser connection, any version, marked or not.
- Known limit: with `actor`, the browser proves that a person clicked or pressed Enter on that page
  within about 10 s as their latest input — not which control they meant; a compromised
  Settings/agent page can still get one wide write (plus a narrow follow-up) per real click; the
  browser limits rate and shape.

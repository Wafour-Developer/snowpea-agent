# M20 — Project trust, and security review 5 (protocol 1.55.0)

Security review 5 found that files inside a PROJECT folder were trusted as they
are: a cloned repository, or a worker that wrote into the project, could add a
hook that runs shell for every tool call of every session, start MCP servers,
pre-approve tools, raise a named agent's mode, or start every session in `auto`.
This document is the contract of the fix (capability `projectTrust`) and lists
the other review-5 decisions as implemented.

## 1. Project trust

### What a project brings

Per project root (its real path) snowpea looks for its *control files*; each
belongs to one **scope**:

| Scope | Files | What it makes snowpea do |
|---|---|---|
| `hooks` | `.snowpea/hooks.json`, `.snowpea/hooks/hooks.json`, `.claude/hooks.json`, `.claude/hooks/hooks.json` | run shell commands around tool calls |
| `mcp` | `.mcp.json` | start MCP servers |
| `commands` | `.snowpea|.claude/commands/*.md`, `.snowpea|.claude|.agents/skills/*/SKILL.md` **with `allowed-tools`** | pre-approve tools for the command's turn |
| `agents` | `.snowpea|.claude/agents/*.md` **with a `permission`** | a mode for an agent |
| `prompts` | `.snowpea/prompts/**/*.md` | replace snowpea's own prompts |
| `settings` | `.snowpea/settings.json`, its widening parts: `allowlist`, `models`, `providers`, `mcp`, `hooks` | pre-approve calls, redirect prompts |
| `mode` | `.snowpea/settings.json` `defaultMode: auto` | start sessions in auto |
| `externalCli` | (no file) | let external CLIs start there and load the project's own configuration |

Commands, skills and agents without those keys are plain text and load as before.
What only narrows always applies, trusted or not: a stricter `defaultMode`,
`jarvis.expose: false`, security-list additions.

### The record

One record under `$SNOWPEA_HOME/trust/projects.json` (0600, its folder 0700),
which extends the external-CLI trust record of review 2 (M4,
`$SNOWPEA_HOME/jarvis/external-trust.json`, still read and folded in as the
`externalCli` scope):

```json
{"version": 1, "migrated": "<utc>", "migratedProjects": ["…"],
 "projects": {"/real/root": {"scopes": ["hooks", "mcp", "commands", "agents", "prompts", "settings"],
                             "files": {".snowpea/hooks.json": "sha256:…"},
                             "by": "ide", "at": "<utc>", "trusted": false}}}
```

It lies inside `$SNOWPEA_HOME`, so a tool's write to it is `config` (asks) and a
shell line that writes it is `never` delegable. `trusted` mirrors `externalCli`.

### Trust pins content

`project.trust.set` records the digest of every control file — as the `digests`
the status showed say it is (security review 6: `digests` is required, and a
project that differs from them is `trust_changed`, nothing trusted). A
file is **active** only while its scope is trusted AND its digest is the pinned
one. A file changed since, or a new one, waits for the person again — so a
worker's later write cannot become active silently, even in a trusted project.
Since review 6 the project files a control file runs (hook scripts, an MCP
server's program, a skill's folder) are pinned with it, and a rule stored by an
approval answer re-pins only when a LOCAL person answered; see
`m19-jarvis-mode.md`, "Review 6 (part A)".

One exception keeps the person's own edits working: when a LOCAL person changes
`.snowpea/settings.json` or `.mcp.json` through snowpea itself (a rule from an
approval answer or `permission.allowlist.*`, `settings.set {scope: "project"}`,
`mcp.add|update|remove {scope: "project"}`), the new content stays active if what
was there before was trusted, or held nothing widening
(`TrustStore.person_wrote`). An agent's own `settings_set` tool never re-pins.

### Who asks the record

Every reader asks the one store (`snowpea_core/trust/projects.py`):

- the skills loader: project hooks load only when active, and are tagged with
  their project — `Hook.project`; `HookRegistry.for_tool(…, workdir)` runs a
  project's hooks only for sessions whose workdir is that project or below it
  (before, the registry was global: one project's hooks ran for every session
  of every project);
- `skills/loader.py` `_may_pre_approve`: a project command's `allowed-tools`;
- `tools/mcp_client.discover`: a project's `.mcp.json` servers;
- `config/project.py` `ProjectSettings.load_effective` (allowlist, model routing)
  and `SessionManager.default_mode` (`defaultMode: auto` needs the `mode` scope);
- `prompts/loader.py`: a project prompt override;
- `external/trust.py` (`externalCli`), unchanged for its callers.

### Contract (capability `projectTrust`, protocol 1.55.0)

| Method | Params | Result | Who |
|---|---|---|---|
| `project.trust.status` | `{workdir}` | `ProjectTrustStatus` | people (a remote login too) |
| `project.trust.set` | `{workdir, scopes?, digests}` | `ProjectTrustStatus` (error `trust_changed`) | a LOCAL person only |
| `project.trust.revoke` | `{workdir, scopes?}` | `ProjectTrustStatus` | a LOCAL person only |

`ProjectTrustStatus`: `workdir` (the root), `trusted`, `scopes`, `by`, `at`,
`files: [{path, scope, state: active|untrusted|new|changed, summary}]`,
`inactive: [path]`, `changed: [path]`, `defaultModeAuto`, `modeActive`. No file
contents beyond names and a short summary (hook commands' first 200 characters,
MCP server commands, `allowed-tools`, a `permission`, the widening keys).

`scopes` omitted on `set`: `hooks, mcp, commands, agents, prompts, settings` —
never `mode` or `externalCli`, which the person names explicitly. On `revoke`:
all. After a change the skills are reloaded (hooks on/off) and the project's MCP
servers synced.

Event `project.trust.changed {workdir, scopes, by, reason: set|revoke|migration,
projects}` — to people's connections only (`session/manager.PERSON_ONLY_EVENTS`).

Per-session notice: when a session opens or resumes in a project with inactive
content, its event stream gets one `error` event with code `project_untrusted`:
"This project brings hooks, mcp … that are not active until you trust it (snowpea
project trust <root>, or the project's trust card); changed since you trusted
it: <files>."

CLI: `snowpea project status|trust|untrust [path] [--scope S …] [--json]`
(path defaults to the current folder).

### Migration (decision d)

On the first daemon start with this version (`wire_core` → `wire_trust`, before
the first skills scan): every project that already has sessions in the store and
brings control files is trusted for `hooks, mcp, commands, agents, prompts,
settings`, pinned as it is now (`by: "migration"`) — its hooks already ran
before. `mode` and `externalCli` are not granted by the migration. New projects
start untrusted. Runs once (`migrated` in the record); a warning line in the log
lists the projects, and the first session's `project_untrusted` notice says it
once.

### Control files are configuration (decision e)

`tools/config_guard.is_config_path` treats every control path as configuration
(`trust.projects.control_path`): `<proj>/.snowpea/{hooks.json, hooks/**,
agents/**, commands/**, skills/**, prompts/**, plugins/**, settings.json,
credentials.json, mcp*.json}`, `<proj>/.claude/{hooks.json, hooks/**,
settings*.json, agents/**, commands/**, skills/**}`, `.mcp.json`, `.codex/**`,
`.cursor/**`, and the `.snowpea`/`.claude`/`.codex`/`.cursor` folders themselves.
A tool's write there is tagged `config` (asks in accept and auto; never
promotable); the classifier's `_is_config` uses the same function, so a shell
write there is `never` delegable. The security lists still block only the
settings files (as before).

### Hook commands meet the security lists (decision f)

Before each run, a hook's command (placeholders expanded) is judged like a shell
line (`skills/hooks.security_refusal`). A blocked command does not start and
counts as a failed hook: a fail-closed guard then blocks its call.

## 2. Named agents (H1)

`named_agents.approved_mode` (new column) stores the mode a person approved at
`agent.create`. On restore the definition's `permission` may only narrow it
(capped by `approved_mode`, or the project's default when that is empty).
`agent.create {named: true}` needs a person (the mode-raise rule's callers),
and so does any create whose definition is wider than the project's default.
A generated definition never carries `auto` (`definition_from_payload` keeps
`inherit|plan|accept`). Rows from before this version are migrated once from
the definition as it is then.

## 3. Commands (M2)

`PermissionPolicy.promote`: a skill's `allowed-tools` never cover a call the
classifier rates `guarded` or `never` (the security lists' blocked/opaque are
judged before, as always). `commands/persists.persists`: a PROJECT's own command
(`project`, `claude-project`, `codex-project` sources) is `person`-level for
every non-person caller (a browser needs the marker; a connector or an unknown
in-process caller may not start it). Installed commands (global, plugins) stay
`session`.

## 4. Remote web access (H3, M3, M4, L1)

- `/web/session` answers this login's own **capability** in the `token` field
  (the field the web client already reads — no client change), never the daemon
  token. `secrets.token_urlsafe(32)`, made per login, stable for the life of the
  login cookie, gone with it (logout, expiry, password change, token rotation,
  the listener stopping). `system.hello` on a remote-listener socket accepts only
  the capability of that socket's own login; the loopback listener never accepts
  one. No route of the remote listener answers anything derived from the daemon
  token (test: every route scanned).
- On the remote listener a connection is always `remote_login`, whatever hello
  declares: `connection_kind` checks `remote_login` first, `clientKind` is forced
  to `web`, a `clientId` is namespaced `remote:<id>` (it cannot re-bind a browser
  host's sessions); `tool.register` is refused there.
- No access log on either listener (`AppRunner(access_log=None)`): the access
  link's `?t=` and the local page's `?token=` never reach a log.
- A stale or wrong access link opened in a browser (`GET` accepting `text/html`)
  answers a small self-contained page, status 401, in English or Korean by
  `Accept-Language`, saying the link no longer works and to ask for a new one —
  no token echoed, no reason given. Other clients keep
  `401 {"ok": false, "error": "invalid_token"}`. It counts toward the limits
  either way. (In password mode the link is dropped and the sign-in page shows.)
- `web.remote.listen` binds the new address first and stops the old one only
  once it is bound; a failed move leaves the listener and its sessions where
  they were. The address it already listens on (`port` 0 included) updates the
  options in place. Failures carry stable `error.data.details`:
  `{field: "port", reason: "in_use"}` and `{field: "port", reason: "permission"}`
  with code `unavailable`; `{field: "host", reason: "bad_address"}`,
  `{field: "port", reason: "out_of_range"}` (outside 0–65535) and
  `{field: "username", reason: "required"}` (a password without a username)
  with code `invalid_params`. `web.remote.setPassword` without a username (none
  given, none configured, or an empty one) answers `invalid_params` with
  `{field: "username", reason: "required"}`.
- Rate limits: the per-address bucket refuses before checking; the global bucket
  is judged after the credential — a correct link or password is never refused
  by failures from elsewhere.

## 5. Deny by default (M1)

`settings_gate.kind_method_refusal` is table-driven and refuses every method a
kind's table does not list: `CONNECTOR_ALLOWED` (handshake, liveness, shutdown,
reads and streams, the authority-reducing `jarvis.takeover` /
`jarvis.delegation.revoke`), `REMOTE_LOGIN_ALLOWED` (operating sessions,
answers, files, Jarvis's day-to-day, the config of `REMOTE_LOGIN_METHODS`),
browser tables (`BROWSER_OPEN_METHODS` plus the marker/host/answer tables).
`extension.*`, `system.update`, `system.restart`, `project.trust.set|revoke`
are a local person's (`PERSON_METHODS`); `system.shutdown` stays open to every
authenticated local connection, not a remote login.
`tests/test_security_review5_methods.py` holds a verdict for every registered
method × {local person, remote login, connector, legacy browser, browser 0.3.0
unmarked, marked}; an unlisted method fails it.

## 6. Interpreter code and scripts (H4, M5, M6)

`security/code.py` analyses inline code (`python -c`, `node -e`, `perl -e`,
`ruby -e`, `php -r`) and `execute_code` bodies with a table per language: a
destructive primitive with a literal target becomes the command it stands for
(`shutil.rmtree('/')` → `rm -rf /`, `os.system("…")` → that line) and is judged
by every preset and protected path; a destructive or process-spawning primitive
with a non-literal argument makes the call opaque (asks in every mode); code
with neither stays allowed. String contents are masked before matching, so a
word inside a string is never a call. A script FILE a line runs (`bash x.sh`,
`sh x`, `python x.py`, `node x.js`, `./x`, `source x`) is read (256 KiB cap): a
shell script through the full shell matcher, other languages through the
analysis. What cannot be read (missing, too large, binary) is opaque only when
the file is recent (changed in the last 10 minutes by its ctime, or written by a
tool call this daemon judged); a pre-existing script's own opaque parts do not
count, its plain destructive lines do. Every script read is recorded with its
digest on the verdict, and the approval queue refuses an allowed answer when a
script changed while the question was open.

Also: `toybox <applet>`, `tar --remove-files` and `zip -m` (sources deleted),
hard links (`ln` without `-s` is a write to its source; a write to an existing
regular file with `st_nlink > 1` is opaque), and case-insensitive comparison of
protected paths on macOS/Windows (with Unicode NFC).

**The limit, stated honestly.** Static checks cannot see what a program computes
at run time (a path built from input, code fetched later, a compiled binary).
The security lists stop the plain and the plainly disguised cases and ask about
the rest; OS-level confinement of tool processes (Landlock/bubblewrap on Linux,
a sandbox profile on macOS) is the only real guarantee and is planned separately.

## 7. Smaller items

- L2: a hand-edited `security.*.presets` that is not a list (`"essential"`,
  `null`) means the defaults, never "no protection", and `security.get` reports
  it.
- L3: `taskkill /T` stays the last resort after the job (documented limit:
  stale parent pids).

## What a user notices

- A project's own hooks, MCP servers, `allowed-tools`, agent permissions, prompt
  overrides, allowlist rules and model routing do nothing until the person
  trusts the project (`snowpea project trust`, or the client's trust card); a
  session there says so once. Projects the person already worked in keep working
  (migration). A changed hooks file stops until trusted again.
- `defaultMode: auto` in a project file starts sessions in `accept` until the
  person trusts the project for `mode` explicitly.
- A remote web login cannot use the daemon token elsewhere, cannot be a browser
  host, cannot manage extensions, update, restart or stop the daemon, or trust a
  project.
- A connector (and any unknown client kind) can only read and watch.
- `python -c`/`node -e` code that deletes or spawns with a computed argument,
  and a just-written script that cannot be fully read, ask the person in every
  mode.

# `remote up`, file RPC and machine identity (protocol 1.40.0)

Contract for an app that drives a remote or container daemon with its own UI (the snowpea
browser, `snowpea-browser/docs/design/remote-sessions.md`). User-facing text: `docs/manual/en/remote.md`
("For a program"). Wire reference: `docs/protocol.md` (`fs.*`, `system.hello`, `session.list`).

Capabilities added to `system.hello.capabilities`: `fs`, `machineIdentity`, `holdSec`, `llmSampleHost`,
`approvalEvaluate`.
Protocol: `1.39.0` → `1.40.0`.

## 1. `remote up` (R1)

```
snowpea[-core] remote up <[user@]host[:port]> [--cwd DIR] [--hold-sec N] --json
snowpea[-core] remote up --docker [IMAGE] [--docker-volume NAME] [--docker-keep] --cwd DIR [--hold-sec N] --json
```

The bundled `snowpea-core` routes `remote up` in `snowpea_core/__main__.py` (before the daemon's
own argument parser); the `snowpea` console script routes it in `cli/main.py`. Both call
`snowpea_core.cli.remote_up.main`. Exit codes: 0 clean close, 1 after an `error` event, 2 usage.

### stdout (one JSON object per line, flushed, nothing else)

```json
{"event":"ready","port":53105,"token":"…","version":"0.2.82","protocol":"1.40.0",
 "machine":{"id":"3ab6…","hostname":"build-box","os":"linux","container":false},"cwd":"/home/me"}
{"event":"reconnecting"}
{"event":"ready", …}
{"event":"ask","id":"7f3a…","kind":"install","version":"0.2.82","target":"me@build-box"}
{"event":"ask","id":"c01b…","kind":"hostKey","fingerprint":"SHA256:…","keyType":"ssh-ed25519","host":"me@build-box"}
{"event":"error","code":"auth","message":"…","reason":"…"}
{"event":"closed"}
```

- `ready.port` is a local loopback port (the ssh `-L` end, or Docker's published port). `ready.machine`
  is the daemon's own `system.hello.machine` (`null` from a daemon older than 1.40.0). `ready.cwd` is
  `--cwd`, else the remote login directory; for `--docker` the mounted project path.
- `ready` is repeated after every reconnect. The local port is kept when still free. `port`/`token` may
  change (the remote daemon restarted; the token was replaced). A plain remote restart keeps the token
  (core persists `<home>/token`) but changes the daemon's own port, which the tunnel hides.
- `error` is always final: `closed` follows, then exit 1. `reason` (optional) refines `code`.
- The token is in `ready` only. It is scrubbed from every other event and from stderr, and is in no argv
  (the ssh `-L` argument names ports; the container gets it on stdin).
- stdout is the real pipe; `sys.stdout` inside the process is redirected to stderr so stray prints cannot
  corrupt it. Container build output goes to stderr.

### stdin

`{"id":"<ask id>","answer":true|false}` per line. Also read: `accept` for `answer`, and extra keys such as
`"t":"answer"`. Nothing is answered by default; an ask waits. **End of stdin** (or SIGTERM/SIGINT,
Ctrl+Break on Windows) tears down and exits 0 after `closed`: the ssh tunnel and control master are
closed; a `--docker` container is stopped and removed (kept with `--docker-keep`). End of stdin while an
ask is open is a clean close, not an error.

`hostKey` is asked **before the first connect** to a host that is not in `known_hosts`: `ssh -G` finds the
files and the real host name/port, `ssh-keygen -F` checks them, `ssh-keyscan` reads the key, the SHA-256
fingerprint is computed locally. On `true` the line is appended to the user's `known_hosts` and ssh runs
with `StrictHostKeyChecking=yes` (and `HostKeyAlgorithms=<that key type>`). A changed key is `error hostKey`
with no ask. Through `ProxyJump`/`ProxyCommand` nothing can be scanned: an unknown key is `error hostKey`.

### Error codes (exact strings)

| code | reason (when present) | when |
|---|---|---|
| `auth` | | ssh `Permission denied` etc. (`BatchMode=yes`: no password or passphrase prompts) |
| `hostKey` | `rejected` | ask answered `false`; changed key; unknown key behind a proxy |
| `unreachable` | | DNS/refused/timeout/no route/host key not readable/tunnel did not open/reconnect failed for 10 min |
| `notInstalled` | | installer failed, or snowpea still not found after installing |
| `installDeclined` | | `install` ask answered `false` |
| `versionMajor` | | remote protocol major ≠ this client's (`ready` is never sent) |
| `dockerMissing` | `notInstalled` `notRunning` `denied` `unsupportedPlatform` | Docker unusable (Windows: `--docker` is not supported yet) |
| `dockerBuild` | | the sandbox image failed to build; bad `SNOWPEA_SANDBOX_SOURCE` |
| `internal` | `usage` `sshMissing` `daemonFailed` `containerFailed` `containerExited` | everything else |

Mapping to the browser design §4.4: `ssh_missing`→`internal/sshMissing`, `auth_failed`→`auth`,
`host_key_unknown`/`host_key_changed`→`hostKey` (an unknown key is an `ask`; `rejected` after a no),
`unreachable`→`unreachable`, `not_installed`→`installDeclined`/`notInstalled`, `daemon_failed`→
`internal/daemonFailed`, `protocol_major`→`versionMajor`, `docker_missing`/`docker_denied`→`dockerMissing`
(`reason` `notInstalled`/`notRunning`/`denied`). `machine_changed` is the client's check (compare
`ready.machine.id` with the pinned id); `core_too_old`/`core_missing` are the client's.

### Reconnect

Every 0.5 s the tunnel process is checked; every 5 s `GET /health` through it. A dead tunnel (or 3 failed
probes) → `reconnecting`; then the host is probed again (`snowpea daemon start` reuses or restarts the
daemon, so the new token/port are read), the tunnel reopened, `system.hello` repeated, `ready` printed.
Retried with backoff (1, 2, 4 … 15 s) for 10 minutes, then `error unreachable`. Fatal codes
(`auth`, `hostKey`, `versionMajor`, `notInstalled`, `installDeclined`, `docker*`) stop at once. A container
that exits is `error internal/containerExited` (never silently replaced).

ssh details: `BatchMode=yes`, `ConnectTimeout=15`, `ServerAliveInterval=15`/`CountMax=3`. On POSIX one
ControlMaster serves the probe/install calls (`ControlPersist=120`, closed with `ssh -O exit` on teardown);
the `-N -L` tunnel is its own connection (a mux client would exit at once and say nothing about the tunnel).
Windows: no ControlMaster; `ssh`, `ssh-keygen`, `ssh-keyscan` are found on `PATH` or in
`%SystemRoot%\System32\OpenSSH`.

## 2. Idle hold (C6)

`system.hello` takes `holdSec` (30–3600). `remote up` sends it on every (re)connect (`--hold-sec`, default
300; `0` sends nothing). Effect on the daemon (`server/lifecycle.py`):

- idle timeout = `holdSec` instead of `daemon.idleTimeoutSec` (default stays 1800 for everyone else);
- while held, sessions that are merely loaded no longer keep the daemon up (a browser leaves them open);
  running turns, jobs, gateway bindings, named agents and `keepAlive` clients still do;
- a hello without `holdSec` from `clientKind` `tui`, `desktop` or `ide` lifts the hold. `cli` and others
  do not. A browser may send `holdSec` itself with `keepAlive:true` (the keepAlive client keeps it up while
  connected; the hold applies after it goes).

A daemon older than 1.40.0 rejects the unknown field; `remote up` repeats the hello without it.

## 3. Machine identity (R3, C7)

`system.hello.result.machine = {id, hostname, os, container}`; `session.list` rows carry the same `machine`.
`id` is a random UUID in `<home>/machine-id` (mode 0600): per machine **and per state home** (a container
without `--docker-volume` is a new machine every time). `os` is `linux|darwin|windows`. `container` is true
when `SNOWPEA_SANDBOX` is set (the sandbox container sets it), or `/.dockerenv`, `/run/.containerenv`, or
`/proc/1/cgroup` names docker/containerd/kubepods/libpod/lxc.

## 4. File RPC (R2)

All six methods take `sessionId` (the roots are per session; the session must be loaded: `session.create`
or `session.resume` first) and `path` (absolute, or relative to the session workdir).

| method | params (besides `sessionId`) | result |
|---|---|---|
| `fs.stat` | `path` | `{path, type: "file"\|"dir"\|"other", size, mtimeMs, nlink, isLink}` |
| `fs.list` | `path`, `depth=1` (1–32), `maxEntries=2000` (≤20000) | `{path, entries:[{path (relative, "/"), type, size, mtimeMs, nlink, isLink}], truncated}` |
| `fs.glob` | `pattern`, `path="."`, `maxEntries=2000` | same as `fs.list` |
| `fs.read` | `path`, `offset=0`, `length?`, `maxBytes=8 MiB` (capped at 8 MiB) | `{dataBase64, size, offset, length, mtimeMs, eof}` |
| `fs.write` | `path`, `dataBase64`, `mode: "create"\|"overwrite"` | `{path, size, mtimeMs, created}` |
| `fs.hash` | `path` | `{sha256, size, mtimeMs}` |

Choices: `fs.glob` exists next to a recursive `fs.list` (pattern: `*`, `?`, `[..]`, a `**` segment for any depth;
no `..`, not absolute; `**` does not enter `.git` or `node_modules`). Listing/globbing never follows
symbolic links; entries come sorted by path. A link's `type` is its target's type when the target is inside a
root, else `other`; `size`/`mtimeMs` are the link's own.

Roots: the session `workdir`, its `workspaceDir` (`sessions/<day>_<id>`), and the root conversation's
attachments `files/` directory (`attachments/<root id>/files`; a delegated child gets its root's). Every path
is resolved with `realpath` and must fall inside a root: `outside_root` for `..`, absolute paths and symbolic
links that leave (also for `fs.stat` — it lstats the last component after resolving the directories).

Errors (`error.data.code`): `outside_root`, `protected`, `too_large`, `not_found`, `exists`, `is_dir`,
`invalid_params` (also: not a directory for `fs.list`, not a regular file, bad base64, bad pattern),
`unavailable` (permission denied), `internal`.

- `protected` (read, write, hash — checked **before** the roots, so a request for
  `<home>/settings.json` says `protected`): `$SNOWPEA_HOME` and everything in it except the session's own
  workspace and attachments; `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `.ssh/`, `.gnupg/`,
  `.aws/`, browser profiles/cookie stores, keyrings (`agent/prompt_refs.is_secret_pattern`, the same rule the
  tools use for the `secret` tag); a project's `.snowpea/settings.json` and `credentials.json`
  (`tools/config_guard`). `fs.stat`/`fs.list`/`fs.glob` still show these names. **Consequence for the
  browser:** a `.key` file cannot be peeked (the design's tier-2 "first 64 bytes of `.key`"); `protected` is
  the answer. Treat it like tier 1.
- `protected` also judges the case-folded, NFC-normalised spelling (macOS and Windows reach `.env` as `.ENV`), and
  a hard-linked name for one of the files directly in `$SNOWPEA_HOME` (read/hash refuse when `nlink > 1` and the inode matches).
- `fs.glob` looks at no more than `10 × maxEntries + 1000` directory entries; past that it answers `truncated: true`.
- `fs.read`: `length` over the cap (or no `length` and the rest of the file over the cap) is `too_large`;
  read in ranges (`offset`,`length` ≤ 8 MiB; `eof` says when to stop). An `offset` past the end gives an empty
  read with `eof:true`. A FIFO or device is `invalid_params`.
- `fs.write`: payload > 8 MiB decoded is `too_large` (checked before decoding). Written to a temp file in the
  target directory, fsynced, then `rename` (overwrite) or `link`+unlink (create — atomic no-clobber, so
  `exists` cannot race). Parents are created when missing (inside a root). An existing file keeps its mode.
  `overwrite` of a missing path creates it (`created:true`). Refused with `outside_root`: a symbolic link as
  the last component (even one that points inside), a directory symlink that leaves the root, an existing file
  with `nlink > 1` (it may be a name for a file outside; the check cannot see the other names).
- Frame size: an 8 MiB read is ~11.2 MB of base64 in one WebSocket message. The daemon accepts 160 MB;
  clients must raise their receive limit (Python aiohttp defaults to 4 MiB; `ws` for Node defaults to 100 MiB).

## 5. Host-level calls (C3, C4)

- `llm.sample` without `sessionId` (capability `llmSampleHost`): allowed for a connection that has
  registered host tools (`tool.register`), refused `unauthorized` otherwise. The model is the daemon's own
  default (`models.default`); `modelCategory` `fast`/`standard`/`deep` pick `browser.fastModel` /
  `models.categories.*` and fall back to that default. No session is billed and no `usage` event is written.
  With a `sessionId` nothing changes (that session's host only).
- `approval.evaluate {tool, permission, site?, mode="accept", args?, risk?}` → `{decision: "allow"|"ask"|"deny",
  by: "mode"|"allowlist"|"none"}` (capability `approvalEvaluate`). Host connections only. The mode matrix
  first (`deny`/`allow` outright), then the allowlist entries of the caller's own profile (`clientId` from its
  hello, as for a session's `hostToolsFrom`), with the `site`/origin rule; `risk:"payment"` or
  `args.onceOnly` never consult the allowlist. Nothing is asked, stored or logged. The reason codes
  and `scopeHint` of a real `approval.ask` are not produced.

## 6. Deviations from the browser doc's assumptions (§9.5) and why

| browser doc | here | reason |
|---|---|---|
| stdin `{"t":"answer","id","accept"}` | `{"id","answer"}` (the old shape is read too) | spec of this request |
| `fs.list` → `{entries:[{name,type,size?}], cursor?}` | `{entries:[{path,type,size,mtimeMs,nlink,isLink}], truncated}` (`depth`, `maxEntries`) | recursive list with a cap, stat fields in one call |
| `fs.read` → `{base64,size,eof}` | `{dataBase64,size,offset,length,mtimeMs,eof}` | spec |
| `fs.write {sessionId,path,base64}` → `{bytes}` | `{…, dataBase64, mode}` → `{path,size,mtimeMs,created}` | create vs overwrite |
| error codes `auth_failed` … | `auth`, `hostKey`, `unreachable`, `notInstalled`, `installDeclined`, `versionMajor`, `dockerMissing`, `dockerBuild`, `internal` + `reason` | spec; table above maps them |
| `.key` peek | `protected` | the tools' secret rule applies to every fs read |

# Remote sessions

`snowpea --remote` runs the session on another machine. The agent, its tools, the project files, memory and the approval queue all live on that host; this machine only holds the terminal. Nothing is sent "to be executed elsewhere" from a local session — the session itself is remote.

```
snowpea --remote me@build-box
snowpea --remote me@build-box:2222 --cwd /srv/app
```

`--cwd` is a path **on the remote host**; without it the session opens in the remote home directory. `--mode` and `--inline` work as usual.

## What happens

1. `snowpea` calls `ssh` once. A password or key passphrase is asked for here, once; later steps reuse that login.
2. If the host has no `snowpea`, it asks before installing: `Install snowpea 0.2.83 there?` Answer `y` and the project's installer runs on the host, pinned to the same version as this machine, without Node (the terminal UI stays here) and without editing shell rc files. With no answer, or a non-interactive terminal, nothing is installed.
3. It starts the host's daemon (or reuses the one already running) and reads where it listens and its token.
4. It opens an `ssh -L` tunnel to that daemon and starts the terminal UI against the tunnel. If the connection drops, the tunnel is opened again and the UI reconnects to the same daemon.

The daemon listens only on the host's own loopback address; no port is opened on the host. Everything crosses the ssh connection.

While you are connected to a remote daemon the status line shows `⇄ user@host` (`⇄ docker:<name>` for `--docker`); a local session shows nothing.

## Requirements

- `ssh` on this machine, and a login on the host that your ssh setup already accepts (keys, agent, `~/.ssh/config` aliases all work).
- On the host: Linux or macOS with `sh`; `curl` if snowpea has to be installed.
- The same major protocol on both sides. A different minor version works with a warning (features newer than the older side are unavailable); a different major version is refused with a hint to update.

## What stays where

| | |
|---|---|
| Files the agent reads and writes, shell commands | the host |
| Sessions, memory, settings, credentials, approvals | the host's `~/.snowpea` |
| The terminal, keyboard, clipboard | this machine |

When you leave, the host's daemon keeps running until its idle timeout (`daemon.idleTimeoutSec`, 30 minutes by default) so a running turn can finish; a later `snowpea --remote` to the same host finds your sessions there. (A daemon reached with [`remote up`](#for-a-program-snowpea-remote-up) uses a shorter hold.)

## For a program: `snowpea remote up`

An app that has its own UI (the snowpea browser) does not want a terminal UI. `remote up` does the same setup as `--remote` and `--docker` — host key, daemon, tunnel, readiness, reconnect — and reports it as JSON lines. It is also in the bundled `snowpea-core` binary, which has no node and no `snowpea` script:

```
snowpea remote up me@build-box:2222 --cwd /srv/app --json
snowpea remote up --docker --docker-volume work --docker-keep --cwd ~/src/app --json
snowpea-core remote up me@build-box --json
```

`--json` is required. `--hold-sec N` (default 300, `0` for none) is described under "Idle timeout". Nothing is asked on the terminal: ssh runs with `BatchMode=yes` (keys and `ssh-agent` only; a password or passphrase prompt is an `auth` error), and the installer runs without a pty.

**stdout** carries one JSON object per line, flushed, and nothing else. Diagnostics go to stderr.

| Event | Fields | Meaning |
|---|---|---|
| `ready` | `port`, `token`, `version`, `protocol`, `machine`, `cwd` | The daemon is reachable at `127.0.0.1:<port>` with this token. `machine` is the daemon's `{id, hostname, os, container}` (`null` from a daemon older than protocol 1.40.0); `cwd` is `--cwd`, else the remote login directory. |
| `reconnecting` | | The tunnel or the daemon went away; reconnecting. |
| `ready` | as above | Again after a reconnect. `port` and `token` may be new (the remote daemon restarted or its token was replaced). The local port is kept when it is still free. |
| `ask` | `id`, `kind`, … | A question for the person; see below. |
| `error` | `code`, `message`, optional `reason` | A failure that ends the run. `closed` follows and the exit code is 1. |
| `closed` | | Everything is torn down; the last line. |

The token appears **only** in `ready` on stdout: not on stderr, not in a log, not in a command line, not in an `error` message.

**stdin** carries the answers, one JSON object per line. When stdin ends, or on SIGTERM or SIGINT (Ctrl+Break on Windows), the run tears down — the ssh tunnel and its control master are closed, a `--docker` container is stopped and removed (kept with `--docker-keep`) — prints `closed` and exits 0.

### Questions

```
{"event":"ask","id":"7f3a…","kind":"install","version":"0.2.82","target":"me@build-box"}
{"event":"ask","id":"c01b…","kind":"hostKey","fingerprint":"SHA256:…","keyType":"ssh-ed25519","host":"me@build-box"}
```

Answer with `{"id":"7f3a…","answer":true}` or `false`. Nothing is assumed: an ask stays open until it is answered or stdin ends (which is a clean close).

- `install`: snowpea is missing on the host; `true` runs the installer there, pinned to this machine's version. `false` is the error `installDeclined`.
- `hostKey`: the host is not in `known_hosts` (`ssh -G` says which files). snowpea reads its key with `ssh-keyscan` and asks **before the first connect**. On `true` the key is added to the user's `known_hosts` and ssh then runs with `StrictHostKeyChecking=yes` and that key type, so it cannot be fooled by a different one. `false` is the error `hostKey`. A host whose key **changed** is the error `hostKey` with no question. A host behind `ProxyJump` or `ProxyCommand` cannot be scanned: ssh refuses an unknown key itself (`hostKey`).

### Error codes

| `code` | When | Usual `reason` |
|---|---|---|
| `auth` | ssh was refused (no key accepted; a passphrase-protected key must be in `ssh-agent`) | |
| `hostKey` | the host key was refused, is unknown to a proxied connection, or has changed | `rejected` |
| `unreachable` | the host did not answer (DNS, refused, timeout, no route), or the tunnel would not open, or a reconnect failed for ten minutes | |
| `notInstalled` | the installer failed, or snowpea is still not found on the host afterwards | |
| `installDeclined` | the `install` ask was answered `false` | |
| `versionMajor` | the host's protocol major differs from this client's | |
| `dockerMissing` | Docker is not installed, its daemon is not running, this user may not use it, or the platform has no `--docker` (Windows) | `notInstalled`, `notRunning`, `denied`, `unsupportedPlatform` |
| `dockerBuild` | the sandbox image could not be built (or a bad `SNOWPEA_SANDBOX_SOURCE`) | |
| `internal` | anything else, including a bad command line | `usage`, `sshMissing`, `daemonFailed`, `containerFailed`, `containerExited` |

A bad command line is an `error` with `reason: "usage"` and exit code 2. `sshMissing` means `ssh` is not on `PATH` (on Windows `System32\OpenSSH\ssh.exe` is found without it).

### Reconnect and give-up

The tunnel process is watched, and the daemon is probed through the tunnel every few seconds. A dead tunnel, or three failed probes, prints `reconnecting`. snowpea then asks the host again (its daemon may have restarted with a new token), opens the tunnel again and prints `ready`. A failure that a retry cannot fix (`auth`, `hostKey`, `versionMajor`, `notInstalled`, `installDeclined`, `docker…`) ends the run with `error`; `unreachable` is retried with backoff for ten minutes, then it is an `error`. A `--docker` container that stops is never replaced silently: the run ends with `error`/`internal`/`containerExited` (with `--docker-volume` the sessions are still in the volume).

### Idle timeout

By default a daemon with nothing left to serve exits after `daemon.idleTimeoutSec` (30 minutes). A daemon that `remote up` reaches should outlive a short network drop but not a client that is gone, so `remote up` sends the daemon `system.hello` with `holdSec` (`--hold-sec`, default 300): from then on the daemon exits that many seconds after the last connection goes, **even when sessions are still loaded** — the sessions stay on disk and `session.list` with `includeClosed` still finds them. A running turn, a scheduled job, a gateway binding, a named agent or a connected `keepAlive` client still keep it up. The next connection from a `tui`, `desktop` or `ide` client lifts the hold and the ordinary timeout applies again. A daemon older than protocol 1.40.0 ignores all this and keeps its 30 minutes.

### Files on the remote machine

With `ready.machine` the app knows which machine it is talking to. A daemon at protocol 1.40.0 or newer (capability `fs`) also answers `fs.stat`, `fs.list`, `fs.glob`, `fs.read`, `fs.write` and `fs.hash` for a session, limited to the session's working directory, its workspace and its conversation's attachments, and never for snowpea's own settings or credentials; see [the protocol reference](../../protocol.md#fsstat) and [the design note](../../design/remote-up-and-fs.md).

## Limits

- Interactive terminal UI only: `--remote` does not combine with `-c` or a subcommand yet.
- A path you attach or mention is read on the host, not on this machine. Files that exist only here cannot be attached by path.
- Microphone and speakers belong to the daemon's machine, so voice input and spoken replies are not available.
- An MCP server's browser login (OAuth) listens on the host's loopback address, which this machine's browser cannot open.
- A container on this machine is [a sandbox session](sandbox.md), not a remote one.

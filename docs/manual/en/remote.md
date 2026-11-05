# Remote sessions

`snowpea --remote` runs the session on another machine. The agent, its tools, the project files, memory and the approval queue all live on that host; this machine only holds the terminal. Nothing is sent "to be executed elsewhere" from a local session — the session itself is remote.

```
snowpea --remote me@build-box
snowpea --remote me@build-box:2222 --cwd /srv/app
```

`--cwd` is a path **on the remote host**; without it the session opens in the remote home directory. `--mode` and `--inline` work as usual.

## What happens

1. `snowpea` calls `ssh` once. A password or key passphrase is asked for here, once; later steps reuse that login.
2. If the host has no `snowpea`, it asks before installing: `Install snowpea 0.2.81 there?` Answer `y` and the project's installer runs on the host, pinned to the same version as this machine, without Node (the terminal UI stays here) and without editing shell rc files. With no answer, or a non-interactive terminal, nothing is installed.
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

When you leave, the host's daemon keeps running until its idle timeout (`daemon.idleTimeoutSec`, 30 minutes by default) so a running turn can finish; a later `snowpea --remote` to the same host finds your sessions there.

## Limits

- Interactive terminal UI only: `--remote` does not combine with `-c` or a subcommand yet.
- A path you attach or mention is read on the host, not on this machine. Files that exist only here cannot be attached by path.
- Microphone and speakers belong to the daemon's machine, so voice input and spoken replies are not available.
- An MCP server's browser login (OAuth) listens on the host's loopback address, which this machine's browser cannot open.
- A container on this machine is [a sandbox session](sandbox.md), not a remote one.

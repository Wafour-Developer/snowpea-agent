# Remote machines (beta)

Machines let this computer work with snowpea on other computers over SSH. Each added machine shows its snowpea sessions and terminals beside this computer's, in the left sidebar under **Machines**. The work runs on the remote computer; your local daemon connects to it and relays what you see and type.

This replaced the old remote modes, `snowpea --remote` and `snowpea remote up` (removed in 1.78.0). See [Remote sessions](remote.md) for what changed.

Other languages: [한국어](../ko/machines.md) · [all pages](../README.md)

Related: [Remote sessions](remote.md) · [Jarvis mode (beta)](jarvis.md#remote-machines) · [Terminal sessions (beta)](terminals.md) · [snowpea web](web.md) · [Security lists](security.md)

## The sidebar

**Machines** is a section of the left sidebar, below the projects of this computer:

- **This computer** comes first and is always there.
- Each machine you added follows, with its own collapsible group. Its projects are listed under it, and its sessions and terminals under those.
- A machine that is not connected shows its status. Connect it to see its projects.

## Add a machine

In the sidebar, choose **Machines → Add machine**. Give:

- the target, `user@host` or `user@host:port`;
- an optional label, which the sidebar shows instead of the target;
- a way in: the path to a private key file, or a pasted private key.

A pasted key is stored under `$SNOWPEA_HOME/machines/<id>/id_key` with mode 0600, and it does not leave this computer. A key file is used where it is; the path must be an absolute path to a regular file.

From the command line, the same:

```bash
snowpea machine add user@host:2222 --label build --key ~/.ssh/build_ed25519
```

The first connection checks the host key. snowpea shows its fingerprint and asks you to confirm it; nothing is trusted before you do. Later, a host key that has changed is refused. If snowpea is not installed on the machine, it asks you to confirm the install first, the same as before: the installer runs there, pinned to this machine's version.

## Connect and disconnect

A machine is connected while snowpea holds an SSH connection to it. Connect from the sidebar, or with:

```bash
snowpea machine list                 # the machines, with their status
snowpea machine connect <id>         # open SSH and the tunnel
snowpea machine disconnect <id>      # close the tunnel
snowpea machine remove <id>          # forget the machine and its stored key
```

`snowpea machine add` only registers. It does not connect. The ids are shown by `machine list`.

## Use a machine

- **Sessions.** Open a machine's project, then a session. A session on a remote machine is a normal snowpea session that runs there: its files, tools, memory and approvals are on that computer. See [how a session opens](#how-a-session-opens) below.
- **Terminals.** A machine's terminals are served by its own daemon and shown in the same terminal view as local ones. From the command line, list them with `snowpea term ls --machine <id>`, and read, type into or attach to one with `--machine <id>`, or with the `<machine>:<terminal>` form: `snowpea term attach <id>:<terminal>`. See [Terminal sessions](terminals.md#the-snowpea-term-command).
- **External sessions** (Claude Code, Codex, agy, Cursor agent) open in a terminal on the machine, like local ones. See [External sessions](external-sessions.md).
- **Jarvis** can supervise a remote machine's sessions and terminals, under the same switches and rules as local ones. See [Jarvis mode, remote machines](jarvis.md#remote-machines).

## How a session opens

On this computer, a snowpea session opens as a **chat** or as a **terminal**, according to `ui.sessionView` in **Settings → Terminal** (default `chat`).

On a remote machine:

- The default comes from `ui.remoteSessionView` (default `chat`), also in **Settings → Terminal**.
- A machine can override it with **sessionView** on its row, under **Machines → Edit**.
- The first time you open a snowpea session on a remote machine and no setting covers it, snowpea asks whether to open it as chat or as a terminal, and remembers your answer for that machine.

Remote chat works for snowpea sessions only. It shows the machine's name as a badge on the conversation, and each event from that session carries the machine's id. External sessions always open in a terminal, on this computer and on machines.

## Security

- **Only the local person decides the strong choices.** Trusting a project, running a CLI unrestricted and choosing a dangerous permission option are done by the person at this computer, on a machine as well. They are recorded in this computer's ledger.
- **Remote web logins are kept out by default.** A remote sign-in in [snowpea web](web.md) can use connected machines only when `web.remote.machines` is on. It is off by default and can be changed only on this computer.
- **The browser and connectors cannot use machines.** Neither a browser session nor a connector can add, connect or use a machine.
- **Keys stay here.** A pasted key is stored with mode 0600 on this computer and is never sent to a web page or to a machine.
- **Host keys are confirmed once.** A changed host key is refused until you remove the machine and add it again.
- **Jarvis** manages a remote session or terminal only where that item's own Jarvis switch is on. It never uses trust, unrestricted mode or a dangerous option on a remote machine. A permission prompt on a remote machine is not answered by Jarvis; it goes to you.

The ssh connection is yours: snowpea uses the `ssh` on this computer, with your keys, agent and `~/.ssh/config`. A password or key passphrase is asked for here, once, as before.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `web.remote.machines` | `false` | A remote web sign-in may use machines that are connected from this computer. Set on this computer only. |
| `ui.sessionView` | `chat` | How a snowpea session on this computer opens: `chat` or `terminal`. |
| `ui.remoteSessionView` | `chat` | How a snowpea session on a remote machine opens by default: `chat` or `terminal`. |

## Limits

- Beta. The remote computer needs snowpea of a compatible protocol version. A different major version is refused with a hint to update.
- Remote chat is for snowpea sessions. External sessions run in terminals.

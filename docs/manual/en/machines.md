# Remote machines

snowpea can keep several SSH connections to other computers that also run snowpea. From the IDE, web or CLI you open that computer's sessions and terminals through your local daemon; the work still runs on the remote PC.

## Register a machine

In the IDE sidebar, **Machines → Add machine**: give `user@host[:port]`, an optional label, and either a path to a private key or a pasted key. Pasted keys are stored under `$SNOWPEA_HOME/machines/<id>/id_key` (mode 0600) and never leave your computer.

The daemon asks to trust the host key (trust on first use) and, when needed, to install snowpea on the remote host — the same steps as `snowpea remote up`.

## Use a machine

When a machine is connected, its project folders appear under it in the sidebar. Opening a CLI session there uses the same session list and permission choices as on this computer. Terminal panes are served by the remote daemon and proxied locally.

RPC calls carry `machine: "<id>"` on the allowlisted methods (`workspace.list`, `session.*`, `external.*`, `terminal.*`). Older clients that never send `machine` see only this computer.

## Settings

- `web.remote.machines` — when on, a remote web sign-in may use machines that are already connected from this computer (default off).
- Each machine has a **Jarvis allowed** switch (default off) for creating or typing into remote terminals through Jarvis.

## CLI

```bash
snowpea machine list
snowpea machine add user@host
snowpea machine connect <id>
snowpea machine disconnect <id>
snowpea machine remove <id>
```

See also [remote](remote.md) for the single-window remote mode (`snowpea --remote`), which is unchanged.

## Snowpea sessions on a remote machine (1.77.0)

On this computer, snowpea sessions open as **chat** or **terminal** according to `ui.sessionView` in **Settings → Terminal**. On a connected remote machine the default comes from `ui.remoteSessionView` (also in **Settings → Terminal**). A machine may override that with **sessionView** on its row in **Machines → Edit**.

Chat uses the normal conversation view with a machine badge; events from the remote session carry `machine` on each `session.event`. Terminal opens (or reuses) a `snowpea` profile terminal on that machine.

Remote chat uses the same proxied methods as local sessions (`session.resume`, `session.prompt`, `session.interrupt`, and the rest of the allowlist). Only a local person or an allowed remote web login may send them; Jarvis uses a separate in-process path when [Jarvis remote](jarvis.md#remote-machines) is on.
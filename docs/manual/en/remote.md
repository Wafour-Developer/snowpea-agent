# Remote sessions

The old remote mode is gone. It ran a whole session on another computer from a terminal (`snowpea --remote`), and it ran a program's remote daemon through `snowpea remote up`. Both were replaced in 1.78.0 by **Machines**: you register a computer once, and its snowpea sessions and terminals appear next to this computer's, in one sidebar.

Read [Remote machines (beta)](machines.md) for how to add, connect and use a machine, what stays on which computer, and the security rules.

What changed:

- `snowpea --remote` no longer starts a session. It prints a message that points to `snowpea machine add` and exits with code 2.
- `snowpea remote up` and `snowpea-core remote up` no longer start a daemon. They print one JSON error line with `"code":"removed"` and exit with code 2.
- The desktop app's remote-window mode is removed. Add the computer as a machine instead.
- A local sandbox is not a remote machine. [`snowpea --docker`](sandbox.md) is unchanged.

Other languages: [한국어](../ko/remote.md) · [all pages](../README.md)

Related: [Remote machines (beta)](machines.md) · [Sandbox sessions](sandbox.md) · [Jarvis mode (beta)](jarvis.md#remote-machines) · [snowpea web](web.md)

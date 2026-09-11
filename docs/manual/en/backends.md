# Execution backends

Every tool that touches a filesystem or runs a command goes through an execution backend. The backend decides *where* that happens. The tool suite does not change: `read_file`, `shell`, `grep`, `git_commit` and the rest behave identically whether they are running on your laptop, inside a container, or on a host across the network.

## Switching

```
/backend
/backend local
/backend docker {"image": "python:3.11-slim"}
/backend ssh {"host": "10.0.0.5", "user": "build", "key": "~/.ssh/id_ed25519"}
```

`/backend` with no argument prints the current one. The change applies to the session immediately, including work already in flight on the next tool call. A project can set its default in `<project>/.snowpea/settings.json` under `backend`.

## local

The default. Commands run as your user, in the session's working directory, with your environment. Nothing is isolated — this is the backend you want while working on your own code, and the one you should not point an auto-mode job at.

## docker

```
/backend docker {"image": "python:3.11-slim"}
```

| Key | Default | Meaning |
|---|---|---|
| `image` | `python:3.11-slim` | image to create the container from |
| `workdir` | the session workdir | bind-mounted at the same absolute path inside |

The container is named after the session, created lazily on the first tool call, and removed when the session closes. The working directory is bind-mounted at the same absolute path, so paths the model reasons about are the same inside and outside. Everything else — installed packages, network, environment — is the container's.

This is the right backend for auto mode. The agent can install whatever it likes and break whatever it likes, and you throw the container away.

Requires a running Docker daemon. Without one, the backend reports the failure rather than silently falling back to local.

## ssh

```
/backend ssh {"host": "10.0.0.5", "port": 22, "user": "build", "key": "~/.ssh/id_ed25519"}
```

| Key | Meaning |
|---|---|
| `host` | hostname or address |
| `port` | defaults to 22 |
| `user` | remote user |
| `key` | private key path |
| `password` | alternative to `key` |
| `cwd` | remote working directory, defaults to the remote home |

Commands run over SSH; file reads and writes go over SFTP. Files the agent writes exist on the remote host only — nothing is mirrored locally, and a `write_file` under an ssh backend leaves your own disk untouched. Key authentication is strongly preferred; a password in a session config is a password in a config file.

## Checking where you are

The honest test is to ask:

```bash
snowpea -c "run hostname and tell me what it printed"
```

Under `local` it is your machine, under `docker` the container id, under `ssh` the remote host. Since the agent's whole view of the filesystem comes through the backend, this is not a detail it can be wrong about.

## What does not move

The backend covers tool execution, not the daemon. Sessions, memory, the approval queue, the scheduler and the gateway all stay in the daemon on your machine, whichever backend a session uses. Network tools — `web_search`, `web_extract`, the browser tools, media tools and MCP servers — run from the daemon, not from the backend host.

## Next

[Headless](headless.md) — using all of this from a script.

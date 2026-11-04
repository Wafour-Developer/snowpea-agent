# Sandbox sessions

`snowpea --docker` runs the whole session in a throwaway Docker container. The container runs the snowpea daemon, so the agent, its tools, the shell, installs and memory all live inside; this machine only holds the terminal. The point is auto mode: the agent may install anything and break anything, and when you leave, the container is thrown away.

```
snowpea --docker
snowpea --docker --mode auto --cwd ~/src/app
snowpea --docker --docker-volume work
snowpea --docker my/snowpea-dev:1
```

`--cwd` (default: the current directory), `--mode` and `--inline` work as usual. The project directory is bind-mounted into the container at **the same absolute path**, so paths in prompts, errors and diffs mean the same thing on both sides, and files the agent writes there are yours on the host.

| Flag | |
|---|---|
| `--docker [IMAGE]` | run the session in a container; without `IMAGE` the image snowpea builds is used |
| `--docker-volume NAME` | keep the sandbox's `~/.snowpea` in the Docker volume `NAME`, so sessions and memory survive the container |
| `--docker-keep` | stop the container when you leave but do not remove it |

## What happens

1. Docker is checked. If `docker` is not installed, the daemon is stopped, or your user may not use it, snowpea says what to do and exits with code 3. It never falls back to a local session.
2. The image is built on first use and reused after (see below).
3. The container starts as your own user, with a fresh random token, a read-only copy of your settings and credentials, and the daemon listening on a port that Docker publishes on `127.0.0.1` of this machine and nowhere else.
4. snowpea waits until the daemon answers `/health`, then starts the terminal UI against that port. The UI is told the daemon is not a local one, so it never starts or restarts a daemon on this machine.
5. When the UI exits, or on Ctrl+C, the container is stopped and removed. If snowpea itself is killed, the container notices that its input closed and stops the daemon on its own.

If the UI exits for an in-place update (exit code 75), snowpea reconnects to the same container when it is still up, and starts a new one when the daemon went away.

## The image

There is no image to pull. snowpea ships a Dockerfile inside the package and builds `snowpea-sandbox:<version>-<digest>` the first time you ask. It is based on `python:3.12-slim`, installs git, ripgrep, ffmpeg, curl, Node.js with npm and `ps`, and installs **the same snowpea version as the terminal you run**, from that version's release tag, so client and daemon always speak the same protocol. The first build downloads about a gigabyte and takes a few minutes; after that `docker images snowpea-sandbox` shows it and every start is immediate. A new snowpea version builds a new image; remove old ones with `docker rmi`.

A development checkout has no release tag to install. Point `SNOWPEA_SANDBOX_SOURCE` at a wheel (`uv build --wheel`) or at any pip requirement instead:

```bash
SNOWPEA_SANDBOX_SOURCE=dist/snowpea_agent-0.2.80-py3-none-any.whl snowpea --docker
```

With `IMAGE`, snowpea builds nothing and runs that image. It must already contain snowpea: `snowpea-core` on `PATH`, `sh`, and a writable `/home/snowpea/.snowpea`. The simple way is to build on top of the image snowpea made:

```dockerfile
FROM snowpea-sandbox:0.2.80-ab12cd34
USER root
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client
USER snowpea
```

An image that lacks snowpea, or whose snowpea speaks another major protocol, is refused with the container's own error. A different minor protocol is a warning.

## Who the agent is inside

The container runs with `--user <your uid>:<your gid>`, so files the agent creates in the project belong to you, not to root. There is no root inside: the agent can install into its home (`pip install --user`, `npm` with a prefix, downloaded binaries) but not with `apt`. If it needs system packages every time, bake them into your own image as above. The container has no privileged flags: no `--privileged`, no added capabilities, no Docker socket, no host network, and `no-new-privileges` is set.

snowpea refuses to mount `/` or your home directory (or a parent of it): that would put everything you own inside the blast radius the sandbox exists to shrink. Run it from the project, or pass `--cwd`.

## Credentials and settings

The agent needs your provider keys to call a model, so snowpea copies these files from your snowpea home (`~/.snowpea`, or `--home`) into the container's `~/.snowpea` when it starts:

- `settings.json` (provider choices and their `api_key`s, tool settings),
- `credentials.json` (bot tokens),
- `mcp-oauth.json` (MCP server logins), when you have them.

They are mounted read-only and copied one way: nothing the sandbox does reaches your real files. Sessions, memory, plugins and skills are **not** copied, and neither are environment variables.

> **The agent can read those secrets.** Anything it runs in the sandbox, and any command a prompt-injected web page talks it into, can read `~/.snowpea/settings.json` and send it somewhere. Use a provider key that you can revoke, ideally one made for this, and do not put a key you could not rotate in the settings you sandbox with.

A provider that points at `localhost` on your machine does not work from inside a container, because there `localhost` is the container. Use the machine's address instead.

The terminal's access token is separate. snowpea makes a new one for every container and hands it over on the container's standard input, so it is in no command line (`ps`), no environment, and not in `docker inspect`.

## State

By default `~/.snowpea` in the container is ephemeral: sessions, memory and anything the agent set up vanish with the container. `--docker-volume NAME` keeps it in a named Docker volume instead; the next `snowpea --docker --docker-volume NAME` finds the old sessions. Your host settings are copied over the volume's copy on every start. Only one container can use a volume at a time. Delete it with `docker volume rm NAME`.

`--docker-keep` keeps the stopped container, with its whole filesystem, so you can look at what the agent did outside the project: `docker cp <name>:/path .`, then `docker rm <name>` (snowpea prints both).

## Network

The container uses Docker's default bridge network: it can reach the internet and your local network, and it can reach services on the host through the host's address. It is not isolated from the network, only from your files. If the agent must work offline, put a firewall in front of the machine; snowpea adds no flag for it.

## What the sandbox does not protect

- **The project directory is writable.** The agent can change anything in it, including `.git`, `.snowpea/` and `.claude/`. Look at `git diff` and at hook and script files before you run something it wrote on the host.
- **The network is open** (above), and the secrets are readable (above).
- It is a container, not a virtual machine: it shares the host's kernel. Treat it as protection against accidents and a careless agent, not against a determined attacker with a kernel exploit.

## Requirements and limits

- Docker Engine or Docker Desktop that your user can run (`docker info` works). Linux and macOS; Windows is not supported yet.
- Interactive terminal UI only: `--docker` does not combine with `-c`, a subcommand, or `--remote`.
- A path you attach or mention is read inside the container, so it must be under the project directory.
- Microphone and speakers do not reach the container, so voice input and spoken replies are not available.
- No browser opens inside it: an MCP server's browser login (OAuth) cannot finish in the sandbox. Log in on the host first; the result is copied in.

# SSH test fixture

A disposable `linuxserver/openssh-server` container that `tests/test_backends.py`
uses to prove the SSH backend really executes on another host (AC-18).

```bash
docker compose -f tests/fixtures/ssh/docker-compose.yml up -d --wait
docker compose -f tests/fixtures/ssh/docker-compose.yml down -v
```

The tests start and stop it themselves and `pytest.skip` when Docker is
unavailable or the container does not come up within 60 seconds, so CI never
fails for missing infrastructure.

| | |
|---|---|
| host | `127.0.0.1` |
| port | `2222` |
| user | `snowpea` |
| key | `tests/fixtures/ssh/id_test` |
| hostname inside | `snowpea-ssh-fixture` |

## About `id_test`

`id_test` / `id_test.pub` is a committed ed25519 key pair generated with
`ssh-keygen -t ed25519 -N ""`. It is **not a secret**: it exists only to log
into a container that listens on loopback and is destroyed after the test run.
Never reuse it for a real host, and never add a passphrase-less real key here.

Regenerate it with:

```bash
ssh-keygen -t ed25519 -N "" -C "snowpea-test-fixture" -f tests/fixtures/ssh/id_test
```

The backend connects with `known_hosts=None` for this fixture, since the
container gets a fresh host key each time. Production SSH backends should pass
`knownHosts` in the backend config so the server identity is verified.

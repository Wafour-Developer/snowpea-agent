# v0.1 end-to-end smoke

`v01_smoke.sh` (macOS, Linux) and `v01_smoke.ps1` (Windows) run the fifteen
steps of plan §7.9 against a real install of the agent. They are the acceptance
evidence for AC-01 and for M8 as a whole, and CI runs them on ubuntu, macos and
windows.

```bash
tests/e2e/v01_smoke.sh              # install from this checkout (the default)
tests/e2e/v01_smoke.sh --from-url   # install from the published install.sh URL
tests/e2e/v01_smoke.sh --keep       # leave the fixture repo behind to inspect
```

```powershell
tests\e2e\v01_smoke.ps1 -FromCheckout
```

## What one run does

Every run is disposable and touches nothing you own:

| Thing | Where | Lifetime |
| --- | --- | --- |
| fixture git repo | `/tmp/snowpea-fixture` | recreated per run, deleted at exit |
| agent state | `/tmp/snowpea-e2e-home` | recreated per run, deleted at exit |
| the installed `snowpea` | `/tmp/snowpea-e2e-tools` | recreated per run, deleted at exit |

`SNOWPEA_E2E_ROOT` moves all three somewhere else. The installer runs with
`SNOWPEA_SKIP_PATH=1`, so no shell rc file is edited, and with `UV_TOOL_DIR`
pointed into the scratch directory, so your own `uv tool` installs are
untouched.

No API key is needed: `SNOWPEA_PROVIDER=fake:tests/fixtures/providers/fake/e2e.json`
replaces every model call with the deterministic scripted provider. When a step
changes what the agent is asked, the matching entry in that fixture has to
change with it — the fixture matches on a substring of the prompt.

## Reading the output

One line per step:

```
PASS 4 one file edited in accept mode
FAIL 12 exit 1; /backend docker did not report a docker backend
SKIP 10 messenger delivery needs SNOWPEA_E2E_CREDENTIALED=1 and a channel
```

The run exits 0 only when no step failed. A skipped step is never a failure,
and there are only four reasons a step skips:

- **10 and 11** need real messenger credentials. Set `SNOWPEA_E2E_CREDENTIALED=1`
  and `SNOWPEA_E2E_CHANNEL=telegram:<chat id>` to run them; the release
  pipeline is where they belong.
- **12** needs a working docker daemon.
- **7 and 8** skip when `/ralph` or `/team` is not in the command registry yet.
  The script asks the daemon (`snowpea commands list --json`) rather than
  guessing, so a registered-but-broken command fails instead of skipping.
- **14** skips when `scripts/gen_protocol.py` is not in the checkout.

## Steps

| # | What runs | What is asserted |
| --- | --- | --- |
| 1 | `installer/install.sh --from-checkout` | exit 0, `snowpea 0.1.x` |
| 2 | `snowpea setup --quick --vendor deepseek --key …` | `settings.json` names the vendor |
| 3 | `snowpea daemon status` | running, port > 0, a keepalive reason |
| 4 | `snowpea -c "edit README.md…" --mode accept` | exit 0, exactly one file in `git diff` |
| 5 | `snowpea -c "/help" --json` | all nine built-in commands listed |
| 6 | `snowpea tools list --json` | the media tools are present, each with a state |
| 7 | `snowpea -c "/ralph …" --mode auto` | exit 0, non-empty diff |
| 8 | `snowpea -c "/workers 2 …" --mode auto`, `snowpea workers status` | two tasks merged, no worktree left |
| 9 | `snowpea skill install …/sample-plugin` | the MCP tool registers and the PreToolUse hook fires |
| 10 | `snowpea job schedule --in 60s --channel …` | delivered to the channel inside 65s |
| 11 | approval timeout | `denied_by_timeout` in `logs/approvals.jsonl` |
| 12 | `snowpea -c "/backend docker" --json` | the session reports the docker backend |
| 13 | `snowpea --mode plan -c "write foo.txt"` | exit 4, no file created |
| 14 | `python scripts/gen_protocol.py --check` | no drift |
| 15 | `snowpea daemon stop` | pid gone, `daemon.json` removed |

Steps 7 and 8 run in `--mode auto` on purpose (plan §7.9): both drive multi-step
shell and git work, which would block on an approval prompt in a
non-interactive script. Accept mode's approval rules are covered by AC-13,
AC-14 and `tests/test_permission_matrix.py`, not here. Step 12 asserts the
backend switch only; that the container really is a different host is asserted
by `tests/test_backends.py`.

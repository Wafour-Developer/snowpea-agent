# sdk/

TypeScript client for the snowpea daemon (`@snowpea/sdk`, an npm workspace of the repo root).
The daemon owns all state; this package is a thin, typed WebSocket/JSON-RPC 2.0 transport plus
hand-written helpers. It is the only path the Ink TUI (`tui/`) and other clients have to the
daemon: no local state, no caching, no second transport.

## Files you will touch most

| Path | Owns |
|---|---|
| `src/protocol.ts` | **generated** — version, error codes, per-method params/results, event payloads, `MethodMap`/`EventMap`. Never hand-edit. |
| `src/client.ts` | `Client`: socket, `system.hello` handshake, reconnect + `session.resume` replay, event emitter, server→client request routing, `RpcError`/`ConnectionClosedError`/`ProtocolIncompatibleError`. |
| `src/sessions.ts` | `session.*` helpers: create/list/close/prompt/interrupt/setMode/resume. |
| `src/approvals.ts` | Approval queue + allowlist helpers, including `onApprovalRequest` (server→client). |
| `src/agents.ts`, `src/jobs.ts` | Subagent/agent/team and scheduler helpers (schema frozen early; the daemon may answer `not_implemented`). |
| `src/tools.ts` | `tool.list`, slash-command list/run, `backend.set`. |
| `src/index.ts` | Public surface: `export *` of every module plus `SDK_VERSION`. A new module that is not listed here is not exported. |
| `test/contract.test.ts` | SDK↔daemon contract tests (AC-15a base, AC-15b subagent) against a real daemon. |

## Conventions

- Every helper is `function xxx(client: Client, …)` returning `client.call(...)`, typed through the
  file-local `type P<M> = MethodMap[M]["params"]` / `type R<M>` aliases. Plain functions, no state.
- Params are assembled with a trailing `as P<"method.name">` cast; an omitted filter is `{}`.
- ESM only (`"type": "module"`, `moduleResolution: NodeNext`): **imports carry `.js`** even for `.ts`
  sources. `tsconfig.json` is `strict`, `rootDir: src`, so `dist/` is build output only.
- Errors are typed: `err instanceof RpcError`, then `err.is("not_implemented")`. Protocol codes live
  in `error.data.code`, not the JSON-RPC numeric `code` (kept as `err.rpcCode`).
- A protocol change is made in `core/snowpea_core/server/protocol.py`, then
  `uv run python scripts/gen_protocol.py`; `--check` is a required CI job.

## Easy to get wrong

- `protocol.ts` is generated alongside `docs/protocol.md`; hand-editing fails CI.
- The client is bidirectional. `approval.request` and `question.request` are methods the **daemon
  calls on the client** (`ServerMethod`), handled via `client.onRequest` / `onApprovalRequest`.
  One with no handler is answered `-32601` with `data.code: "not_implemented"`.
- Reconnect is automatic and gap-free: `call()` records session `seq` (`noteCall`), and a reconnect
  replays each tracked session with `session.resume`. Only a manual gap needs `sessions.resume()`.
- `SDK_VERSION` in `src/index.ts` is `0.1.0` while the package is `0.2.2`. It is only the
  `clientVersion` string sent at handshake — leave it unless syncing it is the task.
- Build before consuming: the TUI resolves `dist/`, which is why `tui/package.json` has a
  `prebuild`. Run `npm -w sdk run build` after touching `src/`.
- Tests spawn a real daemon (`uv run python -m snowpea_core --port 0 --home <tmp>`) against the fake
  provider scripts in `test/fixtures/`. `npm -w sdk test -- --grep base` is the CI lane (from M1);
  `--grep subagent` is the M7 lane. `tsx/esm` runs the TS directly, so tests need no build.

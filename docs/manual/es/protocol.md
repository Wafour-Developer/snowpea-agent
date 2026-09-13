# Protocolo

La referencia completa y generada es [docs/protocol.md](../../protocol.md). Esta página es la orientación que quieres antes de leerla.

Todo lo que el core puede hacer es un método JSON-RPC antes de ser una prestación de la interfaz. No hay ningún canal privado entre el daemon y la interfaz de terminal: la TUI es un cliente cualquiera, y todo lo que ella puede hacer, tu cliente también puede hacerlo.

## Fuente de verdad

`core/snowpea_core/server/protocol.py` define `PROTOCOL_VERSION`, el esquema de parámetros y de resultado de cada método, y el payload de cada evento. `scripts/gen_protocol.py` genera `docs/protocol.md` y `sdk/src/protocol.ts` a partir de él. Ninguno de esos dos ficheros generados se edita nunca a mano, y CI hace fallar la build si divergen:

```bash
uv run python scripts/gen_protocol.py --check
```

## Transporte

El daemon se enlaza a un puerto de loopback elegido al arrancar y lo registra, junto con un token de autenticación, en `$SNOWPEA_HOME/daemon.json`.

| Superficie | Endpoint | Propósito |
|---|---|---|
| WebSocket | `ws://127.0.0.1:<port>/ws` | JSON-RPC 2.0, bidireccional |
| HTTP GET | `/health` | señal de vida |
| HTTP GET | `/version` | versiones del servidor y del protocolo |
| HTTP GET | `/protocol.json` | el esquema entero en JSON |

Los endpoints HTTP son comodidades de solo lectura para sondas, instaladores y depuración. Toda llamada que cambia estado existe únicamente en el WebSocket.

## Handshake

Inmediatamente después de conectar, un cliente llama a `system.hello` con el token de `$SNOWPEA_HOME/token`, su propia versión, y la versión de protocolo contra la que fue construido. Cualquier otro método antes de eso falla con `unauthorized`. Una discrepancia de versión mayor se rechaza con `protocol_incompatible`.

```json
{"jsonrpc":"2.0","id":1,"method":"system.hello",
 "params":{"token":"…","clientVersion":"0.1.0","protocolVersion":"0.1.0"}}
```

El resultado lleva `protocolVersion`, `serverVersion` y una lista de `capabilities`, que es cómo se negocian las funcionalidades una vez congelada la versión.

## Métodos, por área

| Área | Métodos |
|---|---|
| system | `system.hello`, `system.info`, `system.health`, `system.shutdown` |
| session | `session.create`, `session.list`, `session.resume`, `session.close`, `session.prompt`, `session.interrupt`, `session.compact`, `session.deleteSaved`, `session.setMode` |
| comandos y herramientas | `command.list`, `command.run`, `tool.list` |
| aprobaciones | `approval.list`, `approval.respond`, y `permission.allowlist.add` / `list` / `remove` |
| proveedores | `provider.list`, `provider.configure`, `provider.loginWeb` |
| agentes | `agent.list`, `agent.create`, `agent.spawn`, `agent.bindChannel`, `agent.delete` |
| team | `team.start`, `team.status` |
| jobs | `job.schedule`, `job.list`, `job.cancel`, `job.runNow` |
| gateway | `gateway.bind`, `gateway.list`, `gateway.unbind` |
| memoria | `memory.search`, `memory.write` |
| skills | `skill.search`, `skill.install`, `skill.list`, `skill.remove`, `skill.reload` |
| backend | `backend.set` |

Un método va en el sentido contrario. `approval.request` es una *petición* del servidor al cliente, no una notificación: el daemon le pide al cliente una decisión y espera la respuesta. Esa bidireccionalidad es la razón de que el protocolo sea WebSocket JSON-RPC y no HTTP más un stream.

## Eventos

Las notificaciones fluyen del daemon a los clientes suscritos.

- `session.event(sessionId, seq, kind, payload, ts)` lleva todo lo que ocurre en un turno. Tipos: `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `backend.changed`, `usage`, `error`, `turn.done`.
- `approval.pending` y `approval.resolved` para la cola de aprobaciones desatendidas.
- `job.event` y `gateway.event`.
- `commands.changed` tras recargar una skill o un plugin.

Cada `session.event` lleva un `seq` que crece monótonamente dentro de su sesión. Un cliente que pierde la conexión se reconecta y llama a `session.resume(sessionId, afterSeq)` para que se le reenvíe exactamente lo que se perdió. Por eso los eventos se persisten en lugar de simplemente difundirse.

## Errores

Los errores son errores JSON-RPC con un código de texto en `error.data.code`: `unauthorized`, `protocol_incompatible`, `not_found`, `invalid_params`, `mode_denied`, `approval_denied`, `approval_timeout`, `tool_inactive`, `not_implemented`, `login_unsupported`, `search_provider_unavailable`, `browser_provider_unavailable`, `internal`.

## Usar el SDK

```ts
import { connect } from "@snowpea/sdk";

const client = await connect({ port, token, clientVersion: "1.0.0" });
const { sessionId } = await client.call("session.create", { workdir: process.cwd(), mode: "accept" });

client.on("session.event", (e) => {
  if (e.kind === "message.delta") process.stdout.write(e.payload.text);
});

client.onRequest("approval.request", async (req) => ({ decision: "deny", scope: "once" }));

await client.call("session.prompt", { sessionId, text: "what does this repo do?" });
```

El cliente se reconecta por su cuenta y reanuda desde el último `seq` que vio. `sdk/src/protocol.ts` te da los tipos de cada método y cada evento; está generado, así que no puede describir un método que el daemon no tenga.

## Versionado y la puerta de congelación

`PROTOCOL_VERSION` es semver. Antes de que arranque el IDE de escritorio de v0.2, el protocolo debe pasar una puerta de congelación: versión `1.0.0`, y ningún cambio en `docs/protocol.md` ni en el esquema generado a lo largo de tres tags de release consecutivos, con los tests de contrato del SDK pasando en los tres.

```bash
uv run python scripts/gen_protocol.py --check
git log --oneline -- docs/protocol.md sdk/src/protocol.ts
```

Tras la congelación, los cambios aditivos necesitan un bump menor y pasan por la negociación de `capabilities` en `system.hello`. Un cambio mayor significa publicar todos los clientes a la vez, que es exactamente el coste que la puerta existe para hacer visible.

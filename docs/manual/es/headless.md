# Ejecuciones sin interfaz

`snowpea -c` ejecuta un turno sin interfaz y termina con un código sobre el que puedes ramificar. Es la forma de meter snowpea en un script, en un git hook o en un job de CI.

```bash
snowpea -c "what does this repository do?"
snowpea -c "add a regression test for the parser" --mode auto
snowpea -c "summarize today's diff" --json --cwd ~/src/api --timeout 300
```

## Opciones

| Opción | Significado |
|---|---|
| `-c`, `--prompt TEXT` | el prompt; su presencia es lo que hace que la ejecución sea sin interfaz |
| `--mode plan\|accept\|auto` | modo de permisos, por defecto el del proyecto |
| `--json` | emite JSON Lines en lugar de texto |
| `--cwd DIR` | directorio de trabajo de la sesión |
| `--timeout SEC` | interrumpe y cierra la sesión después de SEC segundos |
| `--provider VENDOR` | proveedor para esta ejecución |
| `--resume SESSION_ID` | continúa una sesión guardada en lugar de abrir una nueva |
| `--approve-none` | deniega todas las aprobaciones en lugar de preguntar |
| `--home DIR` | sobrescribe `SNOWPEA_HOME` |

## Qué hace

Se asegura de que haya un daemon en ejecución, crea una sesión en `--cwd`, envía el prompt, renderiza las notificaciones `session.event` a medida que llegan, y cierra la sesión cuando termina el turno. No se lanza ninguna TUI, así que no hace falta Node.

Un comando slash es un prompt válido, porque el registro de comandos pertenece al core y no a la interfaz:

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit"
```

## Continuar una sesión guardada

`-c` normalmente abre una sesión nueva. `--resume` continúa una que ya tienes, que es la mitad sin interfaz del `/resume` de la TUI: el historial guardado se recarga y el prompt se le añade.

```bash
snowpea session list --include-closed
snowpea -c "and now write the tests" --resume s-4f2c9a1b7e30
```

`--mode` y `--provider` se ignoran con `--resume`: la sesión guardada conserva los suyos. `--resume` por sí solo no hace nada; dentro de la TUI, usa `/resume` para elegir una sesión de forma interactiva.

## Sesiones guardadas

Las sesiones persisten en `$SNOWPEA_HOME/state.db` después de cerrarse, y sus adjuntos y su audio bajo `$SNOWPEA_HOME/attachments/<id>/` y `$SNOWPEA_HOME/audio/<id>/`. Borrar una sesión guardada elimina todo eso.

```bash
snowpea session list                                  # live sessions
snowpea session list --include-closed --json          # plus the saved ones
snowpea session list --include-closed --workdir ~/src/api
snowpea session delete s-4f2c9a1b7e30                 # one saved session
snowpea session clear --workdir ~/src/api             # every saved session of one project
snowpea session clear --all                           # every saved session, everywhere
```

Una sesión viva nunca se borra; ciérrala primero. Cada fila imprime el id de la sesión, el modo, la hora de creación, el directorio de trabajo y el último prompt que vio.

## Teams y perfiles de modelo

Los teams de proyecto y el enrutado de modelos por agente son ajustes, así que se pueden configurar sin interfaz.

```bash
snowpea team list                                     # global + project teams, * marks the active one
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery

snowpea model profiles --json                         # profiles, the default, per-agent assignments
snowpea model default fast                            # models.default
snowpea model assign executor deep                    # agents.models.executor
```

`snowpea team create` escribe `<workdir>/.snowpea/settings.json` — el mismo fichero que escribe `/team create` — y rechaza un nombre de agente que no encuentre. `snowpea team delete` elimina solo teams de proyecto; un team global se edita en `$SNOWPEA_HOME/settings.json`. `snowpea model assign` lo rechaza el daemon si el id del perfil no existe.

## Códigos de salida

| Código | Significado |
|---|---|
| `0` | el turno se completó |
| `1` | el agente terminó en fallo |
| `2` | error de uso o de configuración |
| `3` | no se pudo conectar con el daemon |
| `4` | se denegó una aprobación, o el modo bloqueó la acción |
| `5` | se agotó `--timeout`; la sesión se interrumpió y se cerró |

```bash
if snowpea -c "does this repo have a failing test?" --mode plan; then
  echo "clean"
else
  echo "exit $?"
fi
```

El código `4` es el que hay que pensar. En modo plan un intento de escritura produce un error `mode_denied` y salida `4`, lo que convierte al modo plan en una puerta de solo lectura utilizable en CI.

## Salida JSON

Con `--json`, cada `session.event` recibido se escribe como un objeto JSON por línea, y la última línea es un registro de resultado:

```json
{"kind":"message.delta","sessionId":"…","seq":12,"payload":{"text":"Looking at "}}
{"kind":"tool.call","sessionId":"…","seq":13,"payload":{"callId":"c1","name":"grep","args":{"pattern":"def main"}}}
{"kind":"tool.result","sessionId":"…","seq":14,"payload":{"callId":"c1","name":"grep","ok":true,"output":"…"}}
{"kind":"turn.done","sessionId":"…","seq":20,"payload":{"turnId":"t1","reason":"complete"}}
{"kind":"result","exitCode":0,"sessionId":"…","usage":{"inputTokens":4120,"outputTokens":380}}
```

Los tipos de evento son `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `usage`, `error` y `turn.done`. El motivo de `turn.done` es uno de `complete`, `interrupted`, `error`, `denied` o `timeout`. El esquema completo del payload está en [docs/protocol.md](../../protocol.md).

```bash
snowpea -c "list the modules" --json | jq -r 'select(.kind=="message.delta") | .payload.text' | tr -d '\n'
```

## Aprobaciones sin nadie delante

Con un TTY, una petición de aprobación se convierte en un diálogo y/n en stdin. Sin él —una tubería, un runner de CI— no hay a quién preguntar, así que la petición se deniega de inmediato y el proceso sale con `4`. Dilo explícitamente cuando eso sea lo que quieres:

```bash
snowpea -c "run the linter" --approve-none
```

Las dos configuraciones honestas para automatización son el modo plan para cualquier cosa que solo necesite leer, y el modo auto dentro de un contenedor que estés dispuesto a tirar. Para la parte del contenedor, mira [Backends](backends.md).

## Subcomandos sin sesión

Algunos subcomandos llaman a un único método RPC y terminan sin crear una sesión ni contactar con ningún modelo. Son rápidos, deterministas y gratis, lo que los convierte en la prueba de humo correcta después de instalar:

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea daemon status --json
```

## En CI

```yaml
- run: curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
- run: snowpea --version
- run: snowpea tools list --json
- run: snowpea -c "/deep-research whether this dependency has a known CVE" --mode plan --timeout 600 --json
```

Fija `SNOWPEA_HOME` a un directorio local del job para que las ejecuciones no compartan estado, y recuerda que el daemon sigue corriendo después de que termine tu paso: `snowpea daemon stop` al final del job si el runner es de larga vida.

## Siguiente

[Protocolo](protocol.md) — qué está hablando en realidad la CLI.

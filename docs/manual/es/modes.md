# Modos, permisos y la allowlist

Cada herramienta lleva una etiqueta de permiso. El modo decide qué pasa con cada etiqueta.

| etiqueta | herramientas |
|---|---|
| `read` | `read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `process_list`, `memory_search`, `transcribe_audio` |
| `write` | `write_file`, `edit_file`, `git_commit`, `memory_write` |
| `exec` | `shell`, `process_kill`, `delegate_task` |
| `network` | `web_search`, `web_extract`, `browser_*`, herramientas de media, `text_to_speech`, servidores MCP por defecto |
| `send` | `schedule_create`, `schedule_list`, `schedule_cancel` |

## La matriz

| modo | read | write | exec | network | send |
|---|---|---|---|---|---|
| **plan** | permitir | denegar | denegar | permitir | denegar |
| **accept** (por defecto) | permitir | permitir | preguntar | preguntar | preguntar |
| **auto** | permitir | permitir | permitir | permitir | permitir |

**plan** es para pensar. El agente puede leer tu repositorio y buscar en la web, y no puede cambiar nada. Una llamada denegada produce un evento `error` con código `mode_denied` y termina el turno; las ejecuciones sin interfaz salen con `4`.

**accept** es el valor de trabajo por defecto, y equivale al acceptEdits de Claude Code: las lecturas y ediciones de ficheros ocurren sin preguntar, mientras que los comandos de shell, las llamadas de red y cualquier cosa que envíe un mensaje preguntan antes.

**auto** no pregunta nada. Úsalo cuando estés mirando, en un contenedor desechable, o para un job programado cuyo radio de explosión ya has pensado.

## Cambiar de modo

```
/plan
/accept
/auto
/mode
/mode show
/mode save
```

```bash
snowpea --mode plan
snowpea -c "draft a migration plan" --mode plan
```

`/mode save` escribe `"defaultMode"` en `<project>/.snowpea/settings.json`, así que la siguiente sesión en este repositorio empieza ahí. La línea de estado muestra el modo actual en todo momento.

## Aprobaciones

Cuando la política dice *preguntar*, el core crea una petición de aprobación y bloquea esa llamada a la herramienta. Las peticiones interactivas van solo a la superficie de la que vino el turno — la ventana de la TUI en la que escribiste, o la terminal que está ejecutando `snowpea -c`. Nunca aparecen en la cola de nadie más.

Una respuesta lleva un alcance:

| alcance | significado |
|---|---|
| `once` | solo esta llamada |
| `session` | cualquier cosa que coincida, durante el resto de esta sesión |
| `project` | guardado en `<project>/.snowpea/settings.json` |
| `always` | guardado en `$SNOWPEA_HOME/settings.json` |

Que nadie responda también es una respuesta. Pasados `approvals.timeoutSec` (300 segundos por defecto) la petición se deniega y el turno termina. Toda decisión, incluidos los timeouts, se añade a `$SNOWPEA_HOME/logs/approvals.jsonl`.

Las peticiones levantadas por un job programado o por un mensaje de chat entrante son *desatendidas*, y se comportan de otra forma: mira [Gateway](gateway.md).

## La allowlist

Una entrada de allowlist promueve *preguntar* a *permitir* para un comando que coincida. Nunca puede promover *denegar*, así que nada de lo que añadas aquí debilita el modo plan.

```
/allow ^ls( .*)?$
/allow ^git (status|diff|log)\b
/allow ^npm (run )?test$ --global
/allow tool:web_search
/allowlist
/allowlist remove 3
```

El patrón es una expresión regular que se compara con el comando de shell. La forma `tool:<name>` mete en la allowlist una herramienta entera. Sin `--global` la entrada acaba en los ajustes del proyecto; con él, en `$SNOWPEA_HOME/settings.json`.

Escribe patrones anclados y estrechos. `^git ` permite también `git push --force`. `^git (status|diff|log)\b` no.

## Sin interfaz y desatendido

`snowpea -c` pregunta por stdin cuando tiene un TTY. Sin él —un job de CI, una tubería— no hay a quién preguntar, así que una petición de aprobación es una denegación inmediata y el proceso sale con `4`. Hazlo explícito cuando sea lo que quieres decir:

```bash
snowpea -c "run the linter" --approve-none
```

Para CI, las combinaciones honestas son el modo plan para cualquier cosa que solo lea, o el modo auto dentro de un contenedor que estés dispuesto a perder. No recurras al modo auto en una máquina de desarrollo para silenciar un diálogo; para eso está la allowlist.

## Siguiente

[Comandos](commands.md) — la superficie completa de comandos.

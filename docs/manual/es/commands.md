# Comandos

[English](../en/commands.md) · [한국어](../ko/commands.md) · [Todas las páginas](../README.md)

Hay dos superficies de comandos. Los comandos slash se ejecutan dentro de una sesión y son propiedad del core, así que el mismo `/ralph` se comporta idénticamente en la TUI, en `snowpea -c`, en un job programado y en un mensaje de Telegram. Los subcomandos de CLI inspeccionan y configuran el daemon sin abrir una sesión en absoluto.

```bash
snowpea commands list
snowpea commands list --json
```

Eso imprime el registro en vivo, incluyendo los comandos aportados por plugins instalados. `/help` dentro de la interfaz imprime lo mismo.

## Comandos slash

### Sesión y modo

| Comando | Qué hace |
|---|---|
| `/help` | lista todos los comandos disponibles |
| `/tools` | lista las herramientas registradas con categoría, permiso y estado |
| `/compact [instrucciones]` | resume la conversación hasta ahora y continúa con el resumen |
| `/plan`, `/accept`, `/auto` | cambiar de modo |
| `/mode [plan\|accept\|auto\|save\|show]` | mostrar, cambiar, o guardar el modo por defecto del proyecto |
| `/approvals` | lista las aprobaciones desatendidas esperando una respuesta |
| `/allow <regex> [--global]` | promueve un prompt repetido a una autorización silenciosa |
| `/allowlist [remove <id>]` | muestra o depura la allowlist |
| `/backend [local\|docker\|ssh] [json]` | muestra o cambia dónde se ejecutan las herramientas |

### Terminal UI

Estos los responde la propia interfaz de terminal, no el core, así que no aparecen en `snowpea commands list` y solo funcionan en una sesión delante de la que estás sentado. [The terminal UI](tui.md) cuenta qué hace cada uno en pantalla.

| Comando | Qué hace |
|---|---|
| `/resume` | reabre la sesión en la que estuvo este directorio por última vez y la reproduce |
| `/attach <ruta>` | adjunta un fichero al siguiente prompt |
| `/voice` | activa la entrada por voz; después `Ctrl+Space` graba |
| `/rec` | empieza o para la grabación, igual que `Ctrl+Space` |
| `/tts on\|off` | lee cada respuesta al terminarse |
| `/update` | acepta la actualización ofrecida, igual que `U` |

### Trabajo

| Comando | Qué hace |
|---|---|
| `/ralph <task>` | bucle PRD: historias con criterios de aceptación, implementar, verificar, revisar hasta APPROVE |
| `/ultrawork <task>` | divide en partes independientes, las ejecuta en subagentes concurrentes, fusiona los informes |
| `/deepinit [path]` | recorre el repositorio y escribe archivos `AGENTS.md` jerárquicos |
| `/team <n> <task>` | n trabajadores, cada uno con su propio git worktree, ramas fusionadas a medida que las tareas terminan |
| `/deep-interview <idea>` | entrevista socrática que puntúa la ambigüedad y se niega a entregar hasta que la especificación se sostenga |
| `/deep-research <topic>` | investigación web multi-fuente distribuida en subagentes, respondida con citas |
| `/ralplan <task>` | planificación por consenso: planner, architect y critic debaten antes de escribir ningún código |

`/ralph` guarda su estado en `<project>/.snowpea/ralph/` como `prd.json` y `progress.md`, así que puedes leer lo que cree que está haciendo, y se detiene en `ralph.max_iterations` (10) si no logra converger. `/ultrawork` y `/deepinit` se distribuyen bajo `agents.max_concurrent` (3 por defecto). Los últimos tres son archivos `SKILL.md` bajo `core/snowpea_core/builtin_skills/`, cargados por el mismo loader que usan tus propias skills: léelos, cópialos, cámbialos.

### Generadores

| Comando | Qué hace |
|---|---|
| `/agent create "<description>"` | escribe una definición de agente en `<project>/.snowpea/agents/<name>.md` |
| `/agent list` | lista las definiciones de agentes |
| `/skill learn [name]` | convierte la sesión que acabas de terminar en `<project>/.snowpea/skills/<name>/SKILL.md` |

Un agente generado es un objetivo válido de `delegate_task` de inmediato, sin necesidad de recargar.

### Programación

| Comando | Qué hace |
|---|---|
| `/schedule "<spec>" "<task>" [--channel X] [--mode M]` | registra un job |
| `/schedule` | lista jobs, o cancela uno |

## Subcomandos de CLI

### Inspección

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea agents --json
snowpea daemon status --json
```

`tools list` y `commands list` llaman a un método RPC cada uno y terminan. No crean ninguna sesión y no llaman a ningún modelo, lo que los convierte en la prueba de humo correcta después de una instalación o en CI.

### Daemon

```bash
snowpea daemon status
snowpea daemon start
snowpea daemon stop
```

`status` imprime el puerto, el pid, el uptime, los cuatro contadores de keepalive —sesiones, jobs, vinculaciones de gateway, agentes nombrados— y si el daemon tiene intención de salir, con el motivo por el que no lo hace.

### Proveedores

```bash
snowpea provider list
snowpea provider login openai
snowpea setup --vendor deepseek --key sk-...
```

### Skills y plugins

```bash
snowpea skill list
snowpea skill search "pdf"
snowpea skill install oh-my-claudecode
snowpea skill install ./my-plugin
snowpea skill remove my-plugin
```

### Jobs

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check the build" --mode plan
snowpea job list --json
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`--at`, `--in`, `--every`, `--cron` y `--spec` son la misma opción bajo cinco nombres; usa el que mejor se lea para la programación que estés escribiendo.

### Gateway

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

### Team y servicio

```bash
snowpea team status
snowpea service install
snowpea service status
snowpea service uninstall
```

`team status` reporta el estado y el contador de reintentos de cada tarea de un team en ejecución. `service` registra el daemon para que arranque al iniciar sesión: una unidad de usuario systemd en Linux, un agente launchd en macOS, una tarea programada en Windows. Está desactivado por defecto, y solo lo necesitas si quieres que las programaciones y los gateways sobrevivan a un reinicio sin que nadie inicie sesión en una terminal.

### Opciones globales

| Opción | Significado |
|---|---|
| `--version` | imprime la versión y termina |
| `--home DIR` | sobrescribe `SNOWPEA_HOME` para esta invocación |
| `--mode plan\|accept\|auto` | modo para la sesión que se está iniciando |
| `-c`, `--prompt TEXT` | ejecuta un turno sin interfaz y termina |
| `--json` | emite JSON Lines en lugar de texto |
| `--cwd DIR` | directorio de trabajo de la sesión |
| `--timeout SEC` | aborta el turno después de SEC segundos |
| `--provider VENDOR` | proveedor para esta sesión |
| `--approve-none` | deniega todas las aprobaciones en lugar de preguntar |

## Ejecutar un comando slash sin interfaz

Como el registro vive en el core, un comando slash es un prompt válido sin interfaz:

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit" --json
```

La CLI no lo analiza. Le pasa el texto al core, que lo despacha exactamente como lo haría la TUI.

## Siguiente

[Plugins](../en/plugins.md) — añadir tus propios comandos.

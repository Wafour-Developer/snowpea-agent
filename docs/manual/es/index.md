# Manual de snowpea

snowpea es un agente de programación multi-proveedor y de código abierto que se ejecuta en tu propia máquina. Un core en Python corre como un daemon local y es el dueño de todo lo que tiene estado: sesiones, herramientas, permisos, memoria, programaciones, vinculaciones de mensajería. Los clientes se conectan a él por un protocolo WebSocket JSON-RPC documentado: hoy una interfaz de terminal en Ink, un IDE en Electron en v0.2, y lo que tú construyas sobre el SDK de TypeScript.

Otros idiomas: [English](../en/index.md) · [한국어](../ko/index.md) · [日本語](../ja/install.md) · [简体中文](../zh-CN/install.md) · [Todas las páginas](../README.md)

## Por dónde empezar

Si acabas de instalar snowpea, lee [Instalar](install.md), luego [Setup](setup.md), luego [Modos](modes.md). Con eso basta para trabajar con él a diario. Todo lo demás es superficie opcional.

| Página | Léela cuando |
|---|---|
| [Instalar](install.md) | instalas, actualizas, o un paso de la instalación falló |
| [Setup](setup.md) | eliges un proveedor, añades una clave de API, inicias sesión por el navegador, escoges proveedores de búsqueda y de navegador |
| [Modos](modes.md) | el agente pregunta demasiado, o demasiado poco |
| [Terminal UI](tui.md) | las teclas, los paneles, los adjuntos, la voz, y qué te está diciendo la pantalla |
| [Comandos](commands.md) | quieres la lista completa de comandos slash y subcomandos de CLI |
| [Adjuntos y voz](voice.md) | envías imágenes y ficheros en un prompt, le hablas al agente y haces que te responda hablando |
| [Plugins](plugins.md) | instalas o escribes skills, agentes, comandos, hooks y servidores MCP |
| [Planificador](scheduler.md) | quieres que el trabajo ocurra mientras no estás |
| [Gateway](gateway.md) | quieres hablar con el agente desde Telegram, Discord o Slack |
| [Backends](backends.md) | el código vive en un contenedor o en otra máquina |
| [Headless](headless.md) | scripteas snowpea, o lo conectas a CI |
| [Protocolo](protocol.md) | construyes un cliente contra el daemon |

## La forma de la cosa

```text
snowpea              → starts the daemon if needed, attaches the terminal UI
snowpea -c "..."     → one headless turn, no UI, deterministic exit code
snowpea <subcommand> → inspect or configure without opening a session
```

El daemon es perezoso. Arranca con el primer cliente, y se apaga solo tras un periodo de inactividad, pero únicamente cuando no hay nada que lo mantenga vivo: ninguna sesión abierta, ningún job habilitado, ninguna vinculación de gateway, ningún agente nombrado. `snowpea daemon status` te dice cuál de esas cosas lo está manteniendo abierto.

```bash
snowpea daemon status
```

## Dónde vive cada cosa

| Ruta | Qué |
|---|---|
| `$SNOWPEA_HOME` (por defecto `~/.snowpea`) | todo lo global |
| `$SNOWPEA_HOME/settings.json` | proveedores, elección de búsqueda y navegador, categorías de herramientas, timeouts |
| `$SNOWPEA_HOME/credentials.json` | tokens de bots y secretos, modo `0600` |
| `$SNOWPEA_HOME/daemon.json` | puerto, pid, token, hora de arranque, versión de protocolo |
| `$SNOWPEA_HOME/state.db` | sesiones, eventos, memoria, jobs, tareas de team, agentes nombrados |
| `$SNOWPEA_HOME/logs/` | `daemon.log`, `approvals.jsonl` |
| `$SNOWPEA_HOME/plugins/`, `skills/`, `agents/`, `commands/` | extensiones instaladas y escritas a mano |
| `<project>/.snowpea/settings.json` | modo por defecto, allowlist, backend para este repositorio |
| `<project>/.snowpea/{skills,agents,commands}/` | extensiones locales del proyecto |
| `<project>/.claude/{skills,agents,commands}/` | se leen por compatibilidad con Claude Code |

`SNOWPEA_HOME` se respeta en todas partes, así que una segunda instalación aislada está a una variable de entorno de distancia:

```bash
SNOWPEA_HOME=/tmp/snowpea-scratch snowpea daemon status
```

## Conseguir ayuda

`/help` dentro de la interfaz lista todos los comandos que tiene ahora mismo el core, incluidos los que añadieron tus plugins. Desde un shell, `snowpea commands list` imprime el mismo registro, y `snowpea tools list` imprime las herramientas con su etiqueta de permiso y si están activas.

```bash
snowpea commands list
snowpea tools list --json
```

Los bugs y las preguntas van en [GitHub issues](https://github.com/Wafour-Developer/snowpea-agent/issues). Si quieres cambiar el código, [Contributing](../../CONTRIBUTING.md) y [Architecture](../../ARCHITECTURE.md) son los dos documentos que leer primero.

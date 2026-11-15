# Plugins y skills

snowpea lee directamente la disposición de plugins de Claude Code y de Codex. Un plugin escrito para Claude Code o para Codex se instala y funciona aquí sin modificaciones, un repositorio que ya tiene un directorio `.claude/` funciona sin ningún cambio, y lo que Claude Code y Codex instalaron en esta máquina aparece solo.

## Qué es cada cosa

En esta página conviven tres cosas distintas.

| Tipo | Formato | Dónde vive | ¿Puede llevarlo la carpeta del proyecto? | ¿Ejecuta código en el daemon? |
|---|---|---|---|---|
| **Skill** | `SKILL.md` — el estándar abierto agentskills.io que comparten Claude Code y Codex | `skills/` en la carpeta personal o el proyecto | sí | no, son instrucciones |
| **Plugin (formato Claude Code)** | paquete de skills, comandos, agentes, hooks y servidores MCP con `plugin.json` o `.claude-plugin/plugin.json` | `$SNOWPEA_HOME/plugins/` | no, solo la carpeta personal | no; sus hooks y servidores MCP lanzan comandos que aprobaste |
| **Plugin (formato Codex)** | el mismo paquete con `.codex-plugin/plugin.json` (skills, `commands/`, `agents/*.md`, `hooks.json`, `.mcp.json`); los conectores de apps (`.app.json`) no se admiten | `$SNOWPEA_HOME/plugins/` | no, solo la carpeta personal | no; sus hooks y servidores MCP lanzan comandos que aprobaste |
| **Extensión** (formato de extensión de snowpea, Python) | `plugin.py` con `apply(ctx)` | `$SNOWPEA_HOME/extensions/<nombre>/` | no, solo la carpeta personal | sí, Python dentro del daemon; requiere confianza |

Una extensión se inspira en el diseño de DeepSeek Harness, pero no es compatible con los plugins de DeepSeek Harness.

Cada skill, comando, agente y plugin registra el `format` en que se escribió —`snowpea`, `claude` o `codex`— y `skill.list`, `skill.read`, `command.list` y `agent.list` lo devuelven (protocolo 1.52.0). El archivo de instrucciones del proyecto `AGENTS.md` se lee junto con `CLAUDE.md`.

## Qué se lee de Claude Code y de Codex

Además de lo que instalas en snowpea, cada recarga lee lo que las otras dos herramientas instalaron en esta máquina. Aquí esas entradas son de solo lectura: `skill.read` responde `readOnly: true`, `skill.write` nunca escribe en sus carpetas y `skill.remove` no las alcanza; quítalas con la herramienta que las instaló.

| | Claude Code (`$CLAUDE_CONFIG_DIR`, si no `~/.claude`) | Codex (`$CODEX_HOME`, si no `~/.codex`) |
|---|---|---|
| Plugins instalados | `plugins/installed_plugins.json`, omitiendo los que `enabledPlugins` de `settings.json` marca como false — fuente `claude-plugin` | `plugins/cache/<marketplace>/<plugin>/<version>/` (la versión más nueva de cada uno), omitiendo los que tienen `[plugins."<plugin>@<marketplace>"] enabled = false` en `config.toml` — fuente `codex-plugin` |
| De cada plugin | skills, comandos, agentes | skills, comandos, agentes; un plugin con conectores de apps (`.app.json`) aparece con la nota `needs Codex apps` y sus skills se cargan igual |
| Nivel de usuario | `skills/`, `agents/*.md`, `commands/*.md` — fuente `claude-global` | `skills/<name>/` (no el `skills/.system` propio de Codex), `prompts/*.md` como comandos, `agents/*.toml` como definiciones de agente — fuente `codex-global` |
| Nivel de proyecto | `<project>/.claude/{skills,agents,commands}/` — fuente `claude-project` | `<project>/.agents/skills/` — fuente `codex-project` |
| No se importa | hooks y servidores MCP de los plugins instalados | hooks y servidores MCP de los plugins instalados, `hooks.json`, los `[mcp_servers]` de `config.toml` |
| Interruptor | ninguno: se lee siempre que la carpeta existe | `skills.importCodex` (`true`); `false` no lee nada de Codex |

Los hooks y servidores MCP no se importan porque ejecutan comandos en tu nombre, y los aprobaste para Claude Code o Codex, no para snowpea. Si los quieres también aquí, instala el plugin en snowpea. Un rol de agente de Codex (`agents/<name>.toml`) se convierte en definición de agente así: `name`, `description` y `model` tal cual, `developer_instructions` como prompt, `model_reasoning_effort` como `thinking` (`none` o `minimal` es off, cualquier otro nivel on) y `sandbox_mode = "read-only"` como `permission: plan`; `nickname_candidates`, `mcp_servers` y el resto no tienen equivalente y se ignoran. Un agente importado nunca toma el nombre de un rol incorporado (`explore`, `architect`, …): el de un plugin sigue disponible como `<plugin>:<name>` y uno sin plugin se omite.

Cuando los nombres chocan, la copia propia de snowpea gana a la de Claude Code, y la de Claude Code a la de Codex. Queda una regla anterior: una skill global de Claude Code (`~/.claude/skills`) oculta una skill global de snowpea con el mismo nombre. Dentro de Codex, una skill de proyecto gana a una global, y una global a la de un plugin.

## Disposición

```
my-plugin/
  plugin.json            # {"name": "...", "version": "...", "description": "..."}
  skills/<name>/SKILL.md # a skill, which becomes a /<name> command
  agents/*.md            # agent definitions, usable as delegate_task targets
  commands/*.md          # plain markdown commands
  hooks/hooks.json       # PreToolUse / PostToolUse / Stop
  .mcp.json              # MCP servers this plugin brings
```

`.claude-plugin/plugin.json` (formato Claude Code) o `.codex-plugin/plugin.json` (formato Codex) se aceptan en lugar de `plugin.json`; el `interface.displayName` de un manifiesto de Codex se muestra como `title` del plugin. `${CLAUDE_PLUGIN_ROOT}`, `${SNOWPEA_PLUGIN_ROOT}` y el `${PLUGIN_ROOT}` de Codex se expanden al directorio del plugin. Un repositorio de marketplace añade un `marketplace.json` en su raíz (o en `.claude-plugin/`) que lista `plugins: [{name, source}]`; un marketplace de Codex guarda su catálogo en `.agents/plugins/marketplace.json`, donde `source` es `{"source": "local", "path": "./plugins/<name>"}`. Una carpeta en formato Codex se instala igual con `snowpea skill install ./<carpeta>` en `$SNOWPEA_HOME/plugins/<name>`, y `github:openai/plugins@<plugin>` instala solo ese plugin del catálogo de Codex.

## Instalar

```bash
snowpea skill install ./my-plugin
snowpea skill install https://github.com/someone/their-plugin.git
snowpea skill install oh-my-claudecode
snowpea skill list --json
snowpea skill remove my-plugin
```

Instalar copia el plugin en `$SNOWPEA_HOME/plugins/<name>` y recarga el registro. Una recarga emite una notificación `commands.changed`, así que la TUI refresca su paleta sin reiniciarse.

## Buscar

```bash
snowpea skill search "pdf"
snowpea skill search "code review" --json
```

Se consultan tres fuentes a la vez, y cada resultado lleva la `source` de la que vino: `claude-marketplace` (el `marketplace.json` de cada repositorio de marketplace registrado), `agentskills.io` y `hermes-hub`. Una fuente que falla no aporta nada en lugar de hacer fallar la búsqueda. Pasa el spec de instalación de un resultado directamente a `skill install`.

Los marketplaces registrados viven en `$SNOWPEA_HOME/marketplaces.json`, sembrado con el marketplace de oh-my-claudecode.

## SKILL.md

El front matter sigue el estándar de [agentskills.io](https://agentskills.io):

```markdown
---
name: changelog
description: Write a release changelog from the commits since the last tag.
argument-hint: "<tag>"
allowed-tools: [git_log, git_diff, read_file, write_file]
---

Collect the commits since $ARGUMENTS. Group them by type, drop noise,
and write CHANGELOG.md with the newest release on top.
```

El cuerpo se convierte en un comando `/changelog`. `$ARGUMENTS` se sustituye por lo que siguiera al comando, el cuerpo se inyecta como una instrucción, y empieza un turno. `allowed-tools` se aplica durante ese turno: una skill que solo lista herramientas de lectura no puede escribir, permita lo que permita el modo. Omítelo y la skill obtiene el conjunto de herramientas normal de la sesión. `user-invocable: false` mantiene una skill fuera de la lista de comandos dejándola disponible para el agente.

## Definiciones de agentes

```markdown
---
name: reviewer
description: Reviews a diff for correctness and missing tests.
model: inherit
tools: [read_file, grep, git_diff]
permission: plan
max_turns: 12
---

You review changes. Be specific, cite file and line, and say APPROVE or
list what must change. Never edit files yourself.
```

Déjalo en `<project>/.snowpea/agents/reviewer.md`, o deja que `/agent create "reviews diffs for missing tests"` te escriba uno. En cualquier caso se convierte en un objetivo de `delegate_task`, y `snowpea agents --json` lo lista.

## Hooks

`hooks/hooks.json` usa la forma de Claude Code:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "shell|write_file", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/guard.sh"}]}
    ]
  }
}
```

El comando recibe `{event, tool_name, tool_input, session_id, cwd}` como JSON por stdin, más `SNOWPEA_HOME` y `SNOWPEA_TOOL_NAME` en el entorno. `PreToolUse` se ejecuta después del veredicto de permisos y justo antes de la herramienta; un estado de salida 2 bloquea la llamada, y su stderr se convierte en el error que ve el modelo, así que el turno continúa con un rechazo en lugar de morir. `PostToolUse` se ejecuta justo después de que la herramienta retorne, y `Stop` cuando un turno termina sin más llamadas a herramientas. Los demás eventos de hook se parsean y se ignoran.

`${CLAUDE_PLUGIN_ROOT}`, `${SNOWPEA_PLUGIN_ROOT}` y `${SNOWPEA_PYTHON}` se expanden dentro de los comandos de hooks y de MCP, con llaves o sin ellas.

## Servidores MCP

`.mcp.json` usa el formato de Claude Code y se lee desde `$SNOWPEA_HOME`, desde el directorio del proyecto, y desde cada plugin instalado:

```json
{
  "mcpServers": {
    "notes": {"command": "${SNOWPEA_PYTHON}", "args": ["-m", "my_notes_server"]},
    "remote": {"url": "https://example.internal/mcp"}
  }
}
```

Los servidores arrancan de forma perezosa, se cachean durante la vida del daemon, y sus herramientas aparecen como `mcp__<server>__<tool>`:

```bash
snowpea tools list --json
```

El permiso por defecto es `network` por servidor y puede sobrescribirse en los ajustes bajo `mcp.permissions`.

## Dónde se busca cada cosa, y quién gana

Las raíces se recorren en este orden, y en caso de colisión de nombres gana la última, salvo que lo que traen los plugins instalados de Claude Code o Codex y las carpetas de Codex nunca reemplaza una entrada ya cargada:

1. incorporadas — `core/snowpea_core/builtin_skills/`
2. globales — `$SNOWPEA_HOME/{skills,agents,commands}/`, y luego `~/.claude/{skills,agents,commands}/` de Claude Code
3. plugins — `$SNOWPEA_HOME/plugins/*/`, y luego los plugins instalados en Claude Code
4. proyecto — `<project>/.claude/{skills,agents,commands}/`, y luego `<project>/.snowpea/{skills,agents,commands}/`
5. Codex — `<project>/.agents/skills/`, luego `~/.codex/{skills,prompts,agents}`, luego los plugins instalados en Codex

Así que una skill de proyecto sobrescribe una skill de plugin con el mismo nombre, un plugin sobrescribe una incorporada, y nada de Codex sobrescribe algo de snowpea o de Claude Code. El loader registra de dónde vino cada una, y `skill list` lo muestra.

## Extensiones

Una extensión (formato de extensión de snowpea, Python) es una carpeta `$SNOWPEA_HOME/extensions/<nombre>/` con un `plugin.py` que define `apply(ctx)` y, opcionalmente, un `plugin.json` (`name`, `description`, `version`, `engines.snowpea`, `configSchema`). Puede añadir herramientas, secciones del prompt, contexto de ejecución, ganchos sobre cada resultado de herramienta, comprobaciones al terminar un turno, comandos y servidores MCP; todo se deshace al desactivarlo, recargarlo o quitarlo, sin reiniciar. Se ejecuta dentro del daemon con sus permisos: instale solo código de confianza. Solo se busca en la carpeta personal, nunca en un proyecto. Detalles y ejemplo: [manual en inglés](../en/plugins.md#extensions).

## Siguiente

[Planificador](scheduler.md) — ejecutar trabajo mientras no estás.

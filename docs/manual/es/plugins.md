# Plugins y skills

snowpea lee directamente la disposición de plugins de Claude Code. Un plugin escrito para Claude Code se instala y funciona aquí sin modificaciones, y un repositorio que ya tiene un directorio `.claude/` funciona sin ningún cambio.

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

`.claude-plugin/plugin.json` se acepta en lugar de `plugin.json`. Un repositorio de marketplace añade un `marketplace.json` en su raíz que lista `plugins: [{name, source}]`.

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

Las raíces se recorren en este orden, y en caso de colisión de nombres gana la última:

1. incorporadas — `core/snowpea_core/builtin_skills/`
2. globales — `$SNOWPEA_HOME/{skills,agents,commands}/`
3. plugins — `$SNOWPEA_HOME/plugins/*/`
4. proyecto — `<project>/.claude/{skills,agents,commands}/`, y luego `<project>/.snowpea/{skills,agents,commands}/`

Así que una skill de proyecto sobrescribe una skill de plugin con el mismo nombre, y un plugin sobrescribe una incorporada. El loader registra de dónde vino cada una, y `skill list` lo muestra.

## Siguiente

[Planificador](scheduler.md) — ejecutar trabajo mientras no estás.

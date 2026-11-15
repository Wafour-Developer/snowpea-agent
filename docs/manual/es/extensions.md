# Escribir extensiones

Una extensión es código Python que el daemon de snowpea carga dentro de sí mismo. Puede dar al modelo herramientas nuevas, añadir texto al prompt del sistema, mantener al modelo al tanto de hechos que cambian, revisar cada resultado de herramienta, evitar que un turno termine antes de tiempo, añadir comandos con barra y ejecutar servidores MCP. Esta página contiene todo lo necesario para escribir una sin leer el código de snowpea.

Otros idiomas: [English](../en/extensions.md) · [한국어](../ko/extensions.md) · [日本語](../ja/extensions.md) · [简体中文](../zh-CN/extensions.md)

Cada archivo que aparece aquí es una extensión completa y funcional en [`examples/extensions/`](../../../examples/extensions/README.md) del repositorio, y la batería de pruebas carga cada una de ellas.

## Qué es una extensión y cuándo escribir una

snowpea tiene tres tipos de complemento (vea [Plugins](plugins.md#what-is-what)):

| Quiere | Escriba |
|---|---|
| enseñar al modelo un procedimiento, una lista de comprobación o el estilo de la casa que lee cuando hace falta | una **skill** (`SKILL.md`), sin código |
| empaquetar skills, agentes, comandos, hooks y servidores MCP para otros, en el formato de Claude Code o Codex | un **plugin** |
| cambiar cómo trabaja el propio agente: una herramienta escrita en Python, una sección del prompt, un hecho que se reenvía cuando cambia, una comprobación de cada resultado de herramienta, un motivo para seguir un turno | una **extensión** |

Elija la opción más ligera que resuelva el problema. Una skill no exige confianza; una extensión ejecuta código dentro del daemon.

## El modelo de confianza

- Una extensión se ejecuta **en el proceso del daemon, con los permisos del daemon**: puede leer y escribir todos los archivos que usted puede, abrir conexiones de red y ver todas las sesiones. Instale solo código de confianza, como haría con un script de shell.
- Solo se explora `$SNOWPEA_HOME/extensions/` (por defecto `~/.snowpea/extensions/`). La carpeta de un proyecto **nunca** se explora, así que abrir un repositorio clonado nunca ejecuta su código.
- Lo que el modelo hace con una herramienta de una extensión sigue pasando por las reglas de modo y aprobación: la herramienta declara qué tipo de acción es (`read`, `write`, `exec`, `network`, `send`) y el modo decide si se pregunta a la persona. La solicitud de aprobación nombra la extensión de la que viene la herramienta.
- `SNOWPEA_EXTENSIONS=0` en el entorno del daemon lo arranca sin ninguna extensión.

## Inicio rápido

Cree una extensión a partir de la plantilla inicial:

```bash
snowpea extension new hello
```

Eso escribe `~/.snowpea/extensions/hello/` con dos archivos. `plugin.py`:

<!-- example: examples/extensions/hello/plugin.py -->
```python
"""hello: a snowpea extension.

Created by `snowpea extension new hello`. Edit this file, then run
`snowpea extension reload hello` and look for the tool in
`snowpea tools list`. The developer guide is docs/manual/en/extensions.md.
"""

from __future__ import annotations

from typing import Any

# Import only from snowpea_core.extensions: it is the stable API.
from snowpea_core.extensions import PluginContext, Tool, ToolContext, ToolResult


async def greet(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Runs when the model calls the tool; ``args`` follows ``input_schema``."""
    who = str(args.get("name") or "world").strip()
    if len(who) > 100:
        # ok=False: the model reads ``error`` and can call again with better args.
        return ToolResult(ok=False, error="name is longer than 100 characters")
    return ToolResult(ok=True, output=f"Hello, {who}!")


def apply(ctx: PluginContext) -> None:
    """Called once when the extension loads.

    Everything registered through ``ctx`` is undone when the extension is
    disabled, reloaded or removed. If this function raises, nothing stays
    registered and `snowpea extension list` shows the error.
    """
    ctx.tools.register(
        Tool(
            name="hello_greet",
            category="hello",
            description="Greet someone by name. Use it when the user asks to be greeted.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
            },
            # read: runs without asking in every mode. Use write, exec, network
            # or send for a tool that changes files, runs programs, goes online
            # or sends messages; the mode then decides whether to ask.
            permission="read",
            run=greet,
        )
    )
    ctx.log.info("ready")  # goes to $SNOWPEA_HOME/logs/daemon.log
```

y `plugin.json`:

<!-- example: examples/extensions/hello/plugin.json -->
```json
{
  "name": "hello",
  "description": "Greets people: the starting point made by snowpea extension new.",
  "version": "0.1.0",
  "engines": {"snowpea": "^1"}
}
```

Cárguela en el daemon en ejecución (se arranca si no lo está) y busque la herramienta:

```bash
snowpea extension reload hello
snowpea tools list
```

`hello_greet` aparece seguida de `[extension:hello]`. Pida al agente que salude a alguien y llamará a la herramienta. Cambie ahora el saludo en `plugin.py`, ejecute de nuevo `snowpea extension reload hello`, y la siguiente llamada al modelo usará el código nuevo, sin reiniciar el daemon. Si se equivoca, `reload` termina con estado 1 e imprime el error con el archivo y la línea:

```bash
snowpea extension list
snowpea extension info hello
```

## Estructura de la carpeta y `plugin.json`

Una extensión es una carpeta en `$SNOWPEA_HOME/extensions/`. El nombre de la carpeta es el nombre de la extensión: letras, dígitos, `-` y `_`, hasta 64 caracteres.

| Ruta | Obligatorio | Qué es |
|---|---|---|
| `plugin.py` | sí | define `apply(ctx)`; una carpeta sin él se ignora |
| `plugin.json` | no | el manifiesto descrito abajo |
| `*.py`, subpaquetes | no | sus propios módulos, importados de forma relativa: `from . import helpers`, `from .lib import client` (una subcarpeta necesita un `__init__.py`) |
| `tests/` | no | sus pruebas; el daemon las ignora |
| cualquier otra cosa | no | archivos de datos; encuéntrelos con `Path(__file__).parent` |

Las importaciones absolutas de sus propios módulos (`import helpers`) no funcionan, a propósito: cada extensión se importa como un paquete propio, de modo que dos extensiones que tengan un `helpers.py` nunca ven el de la otra. Los paquetes de terceros deben estar instalados en el Python del daemon (el que ejecuta `snowpea`); para una dependencia pequeña en Python puro, cópiela como subpaquete e impórtela de forma relativa.

Campos de `plugin.json` (todos opcionales; un campo desconocido se registra como aviso):

| Campo | Tipo | Significado |
|---|---|---|
| `name` | cadena | si se da, debe ser igual al nombre de la carpeta |
| `title` | cadena | un nombre visible para índices de mercado |
| `description` | cadena | una línea, mostrada por `snowpea extension list` |
| `version` | cadena | su versión, mostrada por `list` e `info` (se recomienda semver) |
| `engines.snowpea` | cadena | la versión mayor de la API de extensiones para la que se escribió: `"^1"`, `"1"`, `"1.x"`, `"~1.0"` o `">=1"` (vea [Versiones](#versiones)) |
| `configSchema` | objeto | un JSON Schema para su configuración (vea [Configuración y configSchema](#configuración-y-configschema)) |

Un `plugin.json` que no sea JSON válido, tenga un campo de tipo incorrecto, un `name` distinto del de la carpeta o un `configSchema` que no sea un JSON Schema válido impide cargar la extensión, y el motivo aparece en `snowpea extension list`.

## Ciclo de vida

Cargar una extensión consiste en: **descubrir** (una carpeta con `plugin.py`) → **validar** (`plugin.json`, `engines.snowpea`, la configuración contra `configSchema`) → **importar** `plugin.py` → **aplicar** (`apply(ctx)` lo registra todo) → **ejecutar** (el daemon llama a lo registrado) → **desechar** (todo lo registrado se deshace, lo más reciente primero).

- **Al arrancar el daemon** se cargan todas las extensiones activadas, después de lo integrado y de las skills, así que un nombre que ya tiene algo integrado o una skill se rechaza con un error claro en lugar de reemplazarse en silencio.
- `apply(ctx)` puede ser una función normal o `async def`; una asíncrona se espera como máximo 30 segundos.
- Si falla cualquier paso, la extensión queda en estado `error` (o `incompatible`), el motivo aparece en `snowpea extension list`, la traza va al registro del daemon y **no queda registrado nada de ella**: lo que `apply` registró antes de lanzar la excepción se deshace.
- **Recargar** (`snowpea extension reload <nombre>`) lo desecha todo, vuelve a leer la carpeta, `plugin.json` y los ajustes, importa de nuevo todos los módulos de la extensión desde el código en disco (nunca desde un `.pyc` en caché) y vuelve a aplicar. Una extensión desactivada sigue desactivada.
- **Desactivar** y **activar** desechan y aplican; la elección se guarda en `settings.extensions.<nombre>.enabled`.
- **Borrar** la carpeta: el siguiente `snowpea extension list` (o cualquier llamada `extension.*`) la descarga.
- Las **ediciones** de `settings.extensions` —a mano, con `settings_set`, desde otro cliente— se siguen en cuanto el daemon lee el archivo cambiado (antes del siguiente turno o en cualquier llamada `extension.*`): un `enabled` cambiado carga o descarga, un `config` cambiado recarga.
- **Al detener el daemon** se desechan todas las extensiones, así que las funciones de `on_dispose` pueden cerrar lo que abrieron.

Lo que ven las sesiones en curso: las herramientas, las secciones del prompt, los proveedores de contexto de ejecución, los hooks y los comandos se consultan en vivo. Un cambio surte efecto **en la siguiente llamada al modelo** de cada sesión, incluso a mitad de un turno; una llamada a herramienta que ya se está ejecutando termina con el código con el que empezó. Un modelo que llama a una herramienta que se acaba de quitar recibe un error «unknown tool» y se adapta.

## La API

Importe todo desde `snowpea_core.extensions`. Esos nombres son la API estable; los módulos de los que provienen pueden moverse entre versiones.

| Nombre | Qué es |
|---|---|
| `PluginContext` | lo que recibe `apply(ctx)` |
| `Tool`, `ToolResult`, `ToolContext`, `PermissionTag` | herramientas |
| `PromptSection`, `PromptInputs`, `SECTION_ORDERS` | secciones del prompt del sistema |
| `ContextProvider`, `CONTEXT_ORDERS` | proveedores de contexto de ejecución |
| `PostDecision` | lo que devuelve un hook `post_execute` |
| `StopDecision`, `StopContext`, `STOP_ORDERS` | oyentes de `turn_stopping` |
| `Command`, `CommandContext` | comandos con barra |
| `ExtensionError` | la excepción de un registro que no se puede hacer |
| `API_VERSION`, `TOOL_PERMISSIONS`, `DISPOSE_TIMEOUT` | constantes |

Cada método de registro devuelve un **disposer**: una función que quita ese registro antes de tiempo. Rara vez hace falta, porque todo se quita cuando la extensión se va. Cada registro lanza `ExtensionError` cuando no se puede hacer —un objeto mal formado o un nombre que ya usa algo integrado, una skill u otra extensión—, lo que hace fallar `apply` y deja toda la extensión sin cargar.

### `ctx.tools.register(tool, *, timeout=None) -> Disposer`

Ofrece una herramienta al modelo. Se registra en el registro de herramientas del daemon, así que aparece en `snowpea tools list` y `tool.list` con `source` = `extension:<nombre>`.

- `tool`: un `Tool(name, category, description, input_schema, permission, run, parallel_safe=False)`. `name` cumple `[A-Za-z][A-Za-z0-9_-]{0,63}` y no está ocupado; `input_schema` es un JSON Schema con `"type": "object"`; `permission` es uno de `read`, `write`, `exec`, `network`, `send`; `run` es `async def run(tool_ctx, args) -> ToolResult`.
- `timeout`: segundos; una llamada que dure más falla con un error de tiempo agotado.

<!-- example: examples/extensions/hello/plugin.py -->
```python
    ctx.tools.register(
        Tool(
            name="hello_greet",
            category="hello",
            description="Greet someone by name. Use it when the user asks to be greeted.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
            },
            # read: runs without asking in every mode. Use write, exec, network
            # or send for a tool that changes files, runs programs, goes online
            # or sends messages; the mode then decides whether to ask.
            permission="read",
            run=greet,
        )
    )
```

Vea [Escribir una herramienta](#escribir-una-herramienta).

### `ctx.prompt.section(section) -> Disposer`

Añade una pieza del prompt del sistema. Se registra en el registro de secciones del prompt.

- `section`: `PromptSection(name, order, render, requires_tool=None)`. `render(inputs: PromptInputs) -> str` se ejecuta en cada llamada al modelo y devuelve el texto, o `""` para omitir la sección. `order` decide su posición (vea [Secciones del prompt](#secciones-del-prompt)). Con `requires_tool`, solo se muestra mientras se ofrece esa herramienta.

<!-- example: examples/extensions/house-style/plugin.py -->
```python
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

### `ctx.prompt.context(provider) -> Disposer`

Añade un hecho de ejecución. Se registra en el registro de contexto de ejecución.

- `provider`: `ContextProvider(name, order, render, required=False)`. `render(core, session) -> str` se ejecuta antes de cada llamada al modelo; el texto se envía al modelo solo cuando cambia. `required=True` lo mantiene también para los agentes delegados ligeros.

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

### `ctx.hooks.post_execute(name, listener, *, order=0) -> Disposer`

Ve cada llamada a herramienta terminada antes de que el modelo lea el resultado. Se registra en la cadena post-execute como `<extensión>:<name>`.

- `listener(tool_ctx, tool_name, args, result)` devuelve `None`, `PostDecision.accept(output=None, *notes)` o `PostDecision.block(feedback, *notes)`; puede ser `async`.
- `order`: los oyentes se ejecutan por `order` y luego por orden de registro.

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    ctx.hooks.post_execute("redact", redact, order=-100)
```

### `ctx.hooks.turn_stopping(name, listener, *, order=150) -> Disposer`

Se ejecuta cuando un turno está a punto de terminar. Se registra en la cadena turn-stopping.

- `listener(core, session, stop: StopContext)` devuelve `None` o un `StopDecision(text, producer, ...)`; puede ser `async`. Gana la primera decisión según `order`, y su `text` pasa a ser la siguiente instrucción.

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

### `ctx.commands.register(command) -> Disposer`

Añade un comando con barra. Se registra en la tabla de comandos (`snowpea commands list`, `command.list`, la paleta del TUI; los clientes reciben `commands.changed`).

- `command`: `Command(name, summary, run, args_schema={})`. `name` no lleva `/` delante; `run` es `async def run(cmd_ctx: CommandContext, args: str)`.

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    ctx.commands.register(
        Command(
            name="scratch",
            summary="keep, list or clear notes that last across sessions",
            run=scratch,
            args_schema={
                "type": "object",
                "properties": {
                    "arguments": {"type": "string", "description": "add <text> | clear"}
                },
            },
        )
    )
```

### `ctx.mcp.register(name, entry) -> Disposer`

Arranca un servidor MCP mientras la extensión esté cargada. Se registra en la lista de servidores MCP (`snowpea mcp list` lo muestra con la extensión como su plugin) y, cuando el servidor está listo, sus herramientas en el registro de herramientas.

- `name`: el nombre del servidor; sus herramientas son `mcp__<name>__<tool>`.
- `entry`: una entrada de `.mcp.json`: `command`, `args`, `env`, `cwd` para stdio, o `url`, `headers` para http/sse, además de `timeoutSec`, `toolTimeoutSec`, `tools.include` / `tools.exclude`.

<!-- example: examples/extensions/glossary-mcp/plugin.py -->
```python
    ctx.mcp.register(
        "glossary",
        {
            "command": sys.executable,  # the daemon's Python
            "args": [str(server)],
            "timeoutSec": 20,
        },
    )
```

### `ctx.on_dispose(fn) -> fn`

Ejecuta `fn()` cuando la extensión se descarga, en orden inverso al de registro, junto con todo lo demás. `fn` puede ser `async`; se espera como máximo `DISPOSE_TIMEOUT` (10) segundos, y una que falla o se cuelga se registra y las demás se siguen ejecutando.

<!-- example: examples/extensions/ticket-workflow/plugin.py -->
```python
    client = tracker.Tracker(ctx.config["baseUrl"], ctx.config.get("token", ""))
    # Registered first, so it runs last: after everything that uses the client is gone.
    ctx.on_dispose(client.close)
```

### `ctx.config`, `ctx.data_dir`, `ctx.log`, `ctx.name`, `ctx.path`

- `ctx.config` (dict): `settings.extensions.<nombre>.config`, ya validado contra `configSchema` y completado con los valores `default` de primer nivel. Es una copia; una configuración nueva llega al recargar.

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]
```

- `ctx.data_dir` (Path): `$SNOWPEA_HOME/extension-data/<nombre>/`, creado al usarlo por primera vez. Guarde ahí sus archivos, no en la carpeta de la extensión, para que actualizar la extensión los conserve.

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    store: Path = ctx.data_dir / "notes.json"
```

- `ctx.log` (logging.Logger): con nombre `snowpea.extensions.<nombre>`; escribe en el registro del daemon.
- `ctx.name` (str) y `ctx.path` (Path): el nombre y la carpeta de la extensión.
- `ctx.core`: el núcleo del daemon. **No** forma parte de la API estable; léalo solo cuando nada de lo anterior le dé lo que necesita, y cuente con que cambie.

## Escribir una herramienta

<!-- example: examples/extensions/file-stats/plugin.py -->
```python
"""file-stats: a tool in depth.

Shows an argument schema with required fields, a permission tag, errors the
model can act on, the session's backend for file access (so the tool works in
SSH and Docker sessions too), progress for a long call, work moved off the
event loop, a per-call timeout, and facts for surfaces in ``meta``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import posixpath
from typing import Any

from snowpea_core.extensions import PluginContext, Tool, ToolContext, ToolResult

MAX_FILES = 500

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "A file or folder, relative to the project root.",
        },
        "pattern": {
            "type": "string",
            "description": "For a folder: a glob such as '*.py'. Default: every file.",
            "default": "*",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _count(blobs: list[bytes]) -> tuple[int, int]:
    """Lines and bytes; runs in a worker thread."""
    return sum(blob.count(b"\n") for blob in blobs), sum(len(blob) for blob in blobs)


async def file_stats(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    # The session's files: on this machine, in a Docker container or over SSH.
    backend = tool_ctx.backend
    raw = str(args.get("path") or "").strip()
    if not raw:
        return ToolResult(ok=False, error="path is required")
    root = posixpath.normpath(str(backend.cwd).replace("\\", "/"))
    target = posixpath.normpath(str(backend.resolve(raw)).replace("\\", "/"))
    # A read tool still stays inside the project: say so instead of reading.
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        return ToolResult(ok=False, error=f"{raw} is outside the project; give a path inside it")
    if not await backend.exists(target):
        return ToolResult(ok=False, error=f"{raw} does not exist")
    parent, name = posixpath.split(target)
    if f"{name}/" in await backend.list_dir(parent):  # list_dir marks folders with "/"
        pattern = str(args.get("pattern") or "*")
        names = await backend.list_dir(target)
        files = [
            posixpath.join(target, entry)
            for entry in names
            if not entry.endswith("/") and fnmatch.fnmatch(entry, pattern)
        ]
    else:
        files = [target]
    if len(files) > MAX_FILES:
        return ToolResult(ok=False, error=f"{len(files)} files match; narrow the pattern")
    if tool_ctx.progress is not None:  # None when nobody is watching (tests, headless)
        await tool_ctx.progress.emit("stdout", f"reading {len(files)} files\n")
    blobs = [await backend.read_bytes(file) for file in files]
    # Counting is CPU work: keep it off the daemon's event loop.
    lines, size = await asyncio.to_thread(_count, blobs)
    return ToolResult(
        ok=True,
        output=f"{len(files)} files, {lines} lines, {size} bytes",
        meta={"files": len(files), "lines": lines, "bytes": size},  # for surfaces, not the model
    )


def apply(ctx: PluginContext) -> None:
    ctx.tools.register(
        Tool(
            name="file_stats",
            category="files",
            description=(
                "Count lines and bytes of a file, or of the files directly in a folder. "
                "Use it to size up files before reading them."
            ),
            input_schema=SCHEMA,
            permission="read",  # reads only: no approval needed in any mode
            run=file_stats,
            parallel_safe=True,  # two calls in one response may run at once
        ),
        timeout=30,  # a call that takes longer fails with a timeout error
    )
```

**El esquema y la descripción** son todo lo que el modelo sabe de la herramienta. Diga en la descripción qué hace y cuándo usarla; describa cada argumento; marque los obligatorios. El modelo suele seguir el esquema, pero no siempre: compruebe lo que dé por supuesto.

**`ToolContext`** da `tool_ctx.session` (su `workdir`, `id`, `mode`), `tool_ctx.backend`, `tool_ctx.core`, `tool_ctx.call_id` y `tool_ctx.progress`. `tool_ctx.backend` es donde están los archivos y la shell de la sesión: esta máquina, un contenedor Docker o un host SSH. Acceda al proyecto a través de él —`cwd`, `resolve(path)`, `exists`, `list_dir` (las carpetas terminan en `/`), `read_file`, `read_bytes`, `write_file`, `write_bytes`, `remove_file`, `run(command, cwd=, timeout=)`— y no con `open()` ni `subprocess`, o la herramienta actuará sobre la máquina del daemon cuando la sesión se ejecute en otro sitio.

Campos de **`ToolResult`**:

| Campo | Quién lo lee | Uso |
|---|---|---|
| `ok` | todos | `False` hace que la llamada sea un fallo |
| `output` | el modelo | el texto del resultado cuando `ok` |
| `error` | el modelo | qué salió mal, escrito para que el modelo pueda corregir su llamada |
| `meta` | los clientes (`tool.result`), no el modelo | datos estructurados para una interfaz |
| `contexts` | el modelo, antes de su siguiente llamada | notas adicionales junto al resultado |
| `path`, `diff` | los clientes | el archivo que cambió y un diff unificado para mostrar |

Devolver un `str` simple equivale a `ToolResult(ok=True, output=...)`. Una salida más larga que `tools.maxResultChars` (100 000 por defecto) se recorta a un principio y un final, y el texto completo se guarda donde el modelo puede leerlo.

**Las etiquetas de permiso** deciden, según el modo, si se pregunta a la persona:

| Etiqueta | Significa | plan | accept | auto |
|---|---|---|---|---|
| `read` | lee, no cambia nada | permitir | permitir | permitir |
| `write` | cambia archivos del proyecto | denegar | permitir | permitir |
| `exec` | ejecuta programas | preguntar | preguntar | permitir |
| `network` | accede a la red | permitir | preguntar | permitir |
| `send` | envía mensajes en nombre del usuario | denegar | preguntar | permitir |

Elija la etiqueta de lo más peligroso que pueda hacer la herramienta. En una herramienta `write`, los archivos indicados en los argumentos `path`, `paths` o `edits[].path` se guardan en un punto de control antes de la llamada, para que la persona pueda deshacer el cambio. La solicitud de aprobación dice `from extension <nombre>`.

**Errores.** Devuelva `ToolResult(ok=False, error=...)` para todo lo que el modelo pueda arreglar. Las excepciones se capturan por usted: la llamada falla con `<herramienta> (extension <nombre>) failed: <Tipo>: <mensaje> (at plugin.py:<línea>)`, la traza va al registro del daemon, y el turno y el daemon continúan; pero escriba sus propios errores, porque una traza no significa nada para el modelo.

**Progreso.** `await tool_ctx.progress.emit("stdout", text)` envía una línea a los clientes mientras la llamada se ejecuta; `progress` es `None` cuando nadie está mirando.

**Tiempos límite e interrupciones.** No hay tiempo límite salvo que pase `timeout=` a `register` o limite el trabajo usted mismo (`asyncio.wait_for`). Cuando la persona interrumpe el turno, la tarea que ejecuta su herramienta se cancela: se lanza `asyncio.CancelledError` en el `await` que esté esperando; limpie en un `finally` y deje que se propague.

**Concurrencia.** `run` se ejecuta en el bucle de eventos del daemon, compartido por todas las sesiones. Nunca lo bloquee: lleve el trabajo de archivos o de CPU a `asyncio.to_thread` y use clientes asíncronos para la red. Varias sesiones pueden llamar a su herramienta a la vez, así que proteja el estado compartido. Las llamadas de una misma respuesta del modelo se ejecutan una tras otra, salvo que la herramienta sea `parallel_safe=True` (solo permitido para herramientas `read` y `network`).

## Secciones del prompt

<!-- example: examples/extensions/house-style/plugin.py -->
```python
"""house-style: a system-prompt section.

The section sits at order 650, in the stable band (below 1000): between the
persona (600) and the reply-language rule (700). Its text depends only on the
mode, so it does not change from one model call to the next and the provider's
prompt cache stays warm.
"""

from __future__ import annotations

from snowpea_core.extensions import PluginContext, PromptInputs, PromptSection

RULES = """House style for this team:
- Commit messages: imperative mood, at most 72 characters in the subject.
- Never add a dependency without saying why in the same answer."""

PLAN_NOTE = "In plan mode, list the commits you would make, one line each."


def render(inputs: PromptInputs) -> str:
    """Runs for every model call; return "" to leave the section out."""
    if inputs.subagent:
        return ""  # delegated agents get a brief, not the house rules
    if inputs.mode == "plan":
        return f"{RULES}\n{PLAN_NOTE}"
    return RULES


def apply(ctx: PluginContext) -> None:
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

Las secciones se ordenan por `order` y luego por nombre. El orden también decide la **banda**, lo que importa para el coste: los proveedores guardan en caché el principio del prompt, y una sección que cambia invalida la caché desde su posición en adelante.

| Banda | Órdenes | Cambia | Secciones integradas (orden) |
|---|---|---|---|
| stable | por debajo de 1000 | solo con el modo, el modelo, el rol o el idioma de respuesta | identity (−1000), execution (−900), plan-continuity (−800), vendor (0), config-rule (100), search-honesty (200), memory-guidance (300), mode (400), role-preamble (500), role (510), persona (600), reply-language (700) |
| context | 1000–1999 | una vez por sesión y tras una compactación | environment (1000), context-files (1100), skills (1200), tools (1300) |
| volatile | 2000 o más | en cada turno | memory (2000), plan-line (2100), context-pressure (2200) |

Ponga el texto estable en la banda stable y haga que su `render` devuelva siempre el mismo texto. Para un hecho que cambia durante una sesión, use un proveedor de contexto de ejecución. `PromptInputs` lleva `mode`, `role`, `subagent`, `tools` (y `tool_names`), `reply_language`, `root` (la carpeta del proyecto) y más. Una sección que lanza una excepción se omite en esa llamada y se registra una vez. Ponga el nombre de la extensión como prefijo de sus secciones.

## Proveedores de contexto de ejecución

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
"""git-branch: a runtime-context provider.

Tells the model which git branch the session's project is on. The provider
runs before every model call, so it reads .git/HEAD directly instead of
starting a git process. The model is sent the text again only when it changes:
switch branches mid-session and the next model call says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snowpea_core.extensions import ContextProvider, PluginContext


def current_branch(workdir: Path) -> str:
    """The checked-out branch of the repository holding ``workdir``, or ""."""
    for folder in (workdir, *workdir.parents):
        head = folder / ".git" / "HEAD"
        if head.is_file():
            text = head.read_text(encoding="utf-8").strip()
            prefix = "ref: refs/heads/"
            return text[len(prefix) :] if text.startswith(prefix) else f"(detached at {text[:12]})"
    return ""


def render(core: Any, session: Any) -> str:
    branch = current_branch(Path(session.workdir))
    return f"Current git branch: {branch}" if branch else ""


def apply(ctx: PluginContext) -> None:
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

Todos los proveedores se renderizan antes de cada llamada al modelo en una lista ordenada. Esa lista se envía al modelo como un único `<system-reminder>` que sustituye al anterior —al empezar un turno o, a mitad de uno, antes de la siguiente llamada al modelo— **solo cuando la lista difiere de la última enviada**, y de nuevo después de que una compactación resumiera la anterior. Así, un proveedor no cuesta nada mientras su texto no cambie. Mantenga `render` rápido (sin procesos ni red: se ejecuta en cada llamada al modelo) y determinista. Órdenes integrados: goal (50), plan (100), plan-auto (110), context-pressure (900).

## Hooks

### `post_execute`

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
"""redact-secrets: a post_execute hook with a validated config.

Every tool result passes through the hook before the model reads it. Text that
looks like a credential is replaced with [redacted]. Extra patterns come from
settings.extensions.redact-secrets.config.patterns, which plugin.json's
configSchema checks before apply() runs.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, PostDecision, ToolContext, ToolResult

BUILT_IN = [
    r"sk-[A-Za-z0-9_-]{20,}",  # OpenAI-style API keys
    r"ghp_[A-Za-z0-9]{36}",  # GitHub personal access tokens
    r"AKIA[0-9A-Z]{16}",  # AWS access key ids
]


def apply(ctx: PluginContext) -> None:
    # The schema guarantees a list of strings; compiling can still fail.
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]

    def redact(
        tool_ctx: ToolContext, name: str, args: dict[str, Any], result: ToolResult
    ) -> PostDecision | None:
        text = result.output if result.ok else (result.error or "")
        cleaned = text
        for pattern in patterns:
            cleaned = pattern.sub(marker, cleaned)
        if cleaned == text:
            return None  # no change: the next listener sees the result as it was
        ctx.log.info("redacted a secret from %s", name)
        if not result.ok:
            # accept() replaces the output only; for a failed call, block with the clean text.
            return PostDecision.block(cleaned)
        return PostDecision.accept(cleaned, "A credential in this result was redacted.")

    # Order -100: before listeners at the default 0, so they never see the secret.
    ctx.hooks.post_execute("redact", redact, order=-100)
```

Cada llamada a herramienta —integrada, MCP, del host o de una extensión— pasa por la cadena después de ejecutarse y antes de que su salida se recorte y se muestre al modelo. Cada oyente devuelve una de estas opciones:

| Devuelve | Efecto |
|---|---|
| `None` | sin cambios |
| `PostDecision.accept(output=None, *notes)` | sustituye el texto que lee el modelo (si se da `output`) y añade notas, enviadas antes de la siguiente llamada del modelo |
| `PostDecision.block(feedback, *notes)` | la llamada pasa a ser un fallo cuyo error es `feedback`; los oyentes posteriores no se ejecutan |

Los oyentes se ejecutan por `order` y luego por orden de registro; no hay ninguno integrado. Un oyente que lanza una excepción se registra y se omite.

### `turn_stopping`

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
"""finish-the-job: a turn_stopping listener.

When the model is about to end its turn with an answer that still holds a
placeholder ("TODO", "FIXME", "..."), the turn goes on with one more
instruction. At most twice per turn, so a model that cannot finish is never
kept in a loop.

Order 120: after the built-in verify gate (100), which asks for a passing
check first, and before unfinished-work (150) and the Stop hooks (200). The
first listener that returns a decision wins.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, StopContext, StopDecision

PLACEHOLDER = re.compile(r"\b(TODO|FIXME)\b|<placeholder>")
MAX_PER_TURN = 2


def apply(ctx: PluginContext) -> None:
    sent: dict[tuple[str, Any], int] = {}  # (session id, turn id) -> nudges so far

    def check(core: Any, session: Any, stop: StopContext) -> StopDecision | None:
        if stop.at_budget or not PLACEHOLDER.search(stop.last_message):
            return None
        key = (session.id, session.current_turn)
        count = sent.get(key, 0)
        if count >= MAX_PER_TURN:
            return None
        sent[key] = count + 1
        if len(sent) > 1000:  # forget old turns
            sent.pop(next(iter(sent)))
        return StopDecision(
            text=(
                "Your answer still has a TODO, FIXME or placeholder. Finish that part "
                "now, or say plainly why it cannot be done."
            ),
            producer="finish-the-job",
            shown="finish-the-job: the answer still had a placeholder",
            count=count + 1,
            limit=MAX_PER_TURN,
        )

    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

Cuando el modelo responde sin llamar a ninguna herramienta, el turno terminaría. Antes se ejecutan los oyentes por `order`; el primero que devuelve un `StopDecision` gana, y su `text` se envía como la siguiente instrucción. Integrados:

| Orden | Oyente | Mantiene el turno cuando |
|---|---|---|
| 100 | `verify-gate` | se editó código sin ninguna comprobación superada después, nunca se miró una interfaz o quedan tareas abiertas |
| 150 | `unfinished-work` | la respuesta enumera trabajo que aún podría hacer ahora |
| 200 | `stop-hooks` | lo pide un hook `Stop` de Claude Code |

`StopContext` da `last_message`, `stop_continuations` y `at_budget` (la respuesta contesta a la consulta del presupuesto de rondas: déjelo terminar). `StopDecision(text, producer, via="message", shown="", count=None, limit=None)`: `producer` lo nombra a usted en el evento que muestran los clientes; `count`/`limit` les permiten mostrar «2 de 3». **Limite siempre** cuántas veces mantiene vivo un mismo turno: puede que el modelo no consiga satisfacerle, y nada más detendrá el bucle.

## Comandos

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
"""scratchpad: a slash command that keeps notes across sessions.

    /scratch add <text>   keep a note
    /scratch              list the notes
    /scratch clear        forget them

A command runs without the model: it answers with cmd_ctx.say(). The notes
live in ctx.data_dir, outside the extension's folder, so updating the
extension keeps them.
"""

from __future__ import annotations

import json
from pathlib import Path

from snowpea_core.extensions import Command, CommandContext, PluginContext


def apply(ctx: PluginContext) -> None:
    store: Path = ctx.data_dir / "notes.json"

    def read() -> list[str]:
        try:
            return list(json.loads(store.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    def write(notes: list[str]) -> None:
        store.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")

    async def scratch(cmd_ctx: CommandContext, args: str) -> None:
        verb, _, rest = args.strip().partition(" ")
        notes = read()
        if verb == "add" and rest.strip():
            write([*notes, rest.strip()])
            await cmd_ctx.say(f"Noted ({len(notes) + 1} in the scratchpad).")
        elif verb == "clear":
            write([])
            await cmd_ctx.say("Scratchpad cleared.")
        elif verb:
            await cmd_ctx.say("Usage: /scratch [add <text> | clear]")
        elif notes:
            await cmd_ctx.say("\n".join(f"{n}. {note}" for n, note in enumerate(notes, 1)))
        else:
            await cmd_ctx.say("The scratchpad is empty. Add a note with /scratch add <text>.")

    ctx.commands.register(
        Command(
            name="scratch",
            summary="keep, list or clear notes that last across sessions",
            run=scratch,
            args_schema={
                "type": "object",
                "properties": {
                    "arguments": {"type": "string", "description": "add <text> | clear"}
                },
            },
        )
    )
```

Un comando es un turno propio que no llama al modelo. `run(cmd_ctx, args)` recibe todo lo que va tras el nombre como una sola cadena. `cmd_ctx.say(text)` responde en la conversación (y el texto se conserva en ella, así que el modelo lo conoce en el siguiente turno); `cmd_ctx.session` y `cmd_ctx.core` están disponibles. Una excepción termina el turno del comando con un error que ve el usuario. No se puede tomar el nombre de un comando integrado, ni uno que tenga una skill u otra extensión. Una skill con el mismo nombre instalada después se queda con el nombre, y la siguiente recarga de la extensión falla por el conflicto.

## Servidores MCP

<!-- example: examples/extensions/glossary-mcp/plugin.py -->
```python
"""glossary-mcp: an MCP server that lives as long as the extension.

The entry has the shape of one .mcp.json entry. The server starts in the
background when the extension loads; its tools appear as
mcp__glossary__define once it is up, carry the MCP permission (network unless
settings.mcp says otherwise), and go away when the extension is disabled,
reloaded or removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from snowpea_core.extensions import PluginContext


def apply(ctx: PluginContext) -> None:
    server = Path(__file__).with_name("server.py")
    ctx.mcp.register(
        "glossary",
        {
            "command": sys.executable,  # the daemon's Python
            "args": [str(server)],
            "timeoutSec": 20,
        },
    )
```

El servidor arranca en segundo plano, así que `apply` no lo espera; un servidor que no arranca se registra y la extensión sigue cargada. El nombre no debe estar declarado en ningún otro sitio (`.mcp.json`, ajustes, un plugin). `snowpea mcp list` muestra el servidor con la extensión como propietaria; al descargarla se quitan sus herramientas y se detiene el proceso. Use esto cuando la capacidad ya exista como servidor MCP; escriba una herramienta cuando sean unas pocas líneas de Python.

## Configuración y `configSchema`

<!-- example: examples/extensions/redact-secrets/plugin.json -->
```json
{
  "name": "redact-secrets",
  "description": "Replaces credentials in tool output before the model reads it.",
  "version": "1.0.0",
  "engines": {"snowpea": "^1"},
  "configSchema": {
    "type": "object",
    "properties": {
      "patterns": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "default": [],
        "description": "More regular expressions to redact."
      },
      "replacement": {
        "type": "string",
        "default": "[redacted]",
        "description": "What a match is replaced with."
      }
    },
    "additionalProperties": false
  }
}
```

La configuración vive en `settings.json` bajo `extensions.<nombre>.config`, junto a `enabled`. Antes de que se ejecute `apply`, la configuración se valida contra `configSchema` (cualquier borrador de JSON Schema que conozca el paquete `jsonschema`; `$schema` elige uno). Una configuración no válida impide cargar la extensión, y el error nombra la ruta que falla, por ejemplo `settings.extensions.redact-secrets.config.patterns[0]: '' should be non-empty`. Las propiedades de primer nivel que falten reciben su `default`. Los clientes fijan la configuración con `extension.configure` (se valida, se guarda y la extensión se recarga); `extension.list` devuelve el esquema para que un cliente pueda generar un formulario a partir de él. Guarde secretos como tokens en la configuración solo si `settings.json` es suficientemente privado en esa máquina.

## Registro y depuración

- `ctx.log` y todo lo que registre bajo `snowpea.extensions` va a `$SNOWPEA_HOME/logs/daemon.log`, igual que las líneas del propio daemon sobre su extensión: qué registró al cargarse, por qué falló (con la traza completa), qué herramienta, sección, proveedor o hook lanzó una excepción.
- `snowpea extension list` muestra el estado de cada extensión —`loaded`, `unloaded`, `disabled`, `error`, `incompatible`— y, si falló, `Type: message (at file:line)` apuntando a su propio código.
- `snowpea extension info <nombre>` muestra la carpeta, el error, la configuración, el esquema y las herramientas, comandos y servidores MCP que registró.
- `snowpea extension reload <nombre>` termina con estado 1 cuando la extensión no se cargó, así que un bucle de editar y recargar en un script se detiene ante un error.

Errores comunes:

| Síntoma | Causa |
|---|---|
| una herramienta nunca se llama | la descripción no dice cuándo usarla, o el modo actual deniega el permiso |
| `run must be an async function` | `def run` en lugar de `async def run` |
| el daemon se atasca mientras corre una herramienta | trabajo bloqueante en el bucle de eventos; use `asyncio.to_thread` |
| `ModuleNotFoundError` en un módulo propio | `import helpers` en lugar de `from . import helpers` |
| `ModuleNotFoundError` en un paquete | no está instalado en el Python del daemon |
| tras recargar, el comportamiento es el de antes | el código guarda estado fuera de la extensión (un módulo del núcleo, un registro global); guarde el estado en cierres o en sus propios módulos |
| la caché del prompt deja de ayudar | una sección de la banda stable cuyo texto cambia; lleve ese hecho a un proveedor de contexto de ejecución |

## Probar una extensión

`snowpea_core.extensions.testing.load_extension` arranca un daemon privado en una carpeta temporal, copia su extensión en ella, la carga con el mismo gestor que usa el daemon real y abre una sesión. Al salir del bloque descarga la extensión y hace fallar la prueba si algo quedó registrado. No se llama a ningún modelo ni sale nada a la red. La prueba que acompaña al ejemplo `hello`:

<!-- example: examples/extensions/hello/tests/test_hello.py -->
```python
"""Tests for the hello extension; run with `python -m pytest tests` in its folder.

Needs pytest and pytest-asyncio next to snowpea in the same Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.extensions.testing import load_extension

EXTENSION = Path(__file__).resolve().parents[1]  # the folder holding plugin.py


@pytest.mark.asyncio
async def test_greets_by_name(tmp_path: Path) -> None:
    # A private daemon in tmp_path; the extension is loaded as the real one would be.
    async with load_extension(EXTENSION, home=tmp_path) as ext:
        assert ext.registrations == ["tool:hello_greet"]
        result = await ext.call_tool("hello_greet", {"name": "Ada"})
        assert result.ok and result.output == "Hello, Ada!"
        too_long = await ext.call_tool("hello_greet", {"name": "x" * 101})
        assert not too_long.ok and "longer than 100" in too_long.error
    # Leaving the block unloads it and fails the test if anything stayed registered.
```

`load_extension(folder, *, config=None, home=None, check=True)` entrega un objeto con:

| Miembro | Hace |
|---|---|
| `call_tool(name, args)` | llama a una herramienta como el bucle del agente y luego a los hooks `post_execute`; devuelve el `ToolResult` |
| `system_prompt()` | el prompt del sistema que recibiría la siguiente llamada al modelo de la sesión |
| `runtime_context()` | `{nombre del proveedor: texto}` en este momento |
| `stop_turn(last_message)` | lo que decide la cadena `turn_stopping` |
| `run_command(name, args)` | ejecuta un comando con barra; devuelve lo que dijo |
| `reload(config=None)` | recarga, opcionalmente con una configuración nueva |
| `registrations`, `extension`, `session`, `core` | lo que registró, su estado y error, la sesión, el núcleo |

Con `check=True`, un fallo de carga lanza `ExtensionLoadError` con el error de la extensión, así que la prueba de una configuración incorrecta es `pytest.raises(ExtensionLoadError, match=...)`.

## Versiones

`snowpea_core.extensions.API_VERSION` es la versión mayor de esta API (ahora `1`). Solo cambia cuando algo de lo que depende una extensión se elimina o cambia de significado: un miembro o argumento de `ctx`, un campo o método de un nombre exportado por `snowpea_core.extensions`, la firma de llamada de un `run`, `render` u oyente, o una promesa de orden de esta página. Añadir miembros, argumentos opcionales, campos y nuevas secciones integradas no la cambia. `ctx.core` y cualquier módulo que no sea `snowpea_core.extensions` quedan fuera de esta promesa.

Indique en `plugin.json` la versión mayor para la que escribió: `"engines": {"snowpea": "^1"}`. Un daemon con otra versión mayor de la API muestra la extensión como `incompatible`, con ambas versiones en el motivo, y no la carga. Versione su propia extensión en `version`.

## Distribuir

Todavía no hay un mercado de extensiones. Para compartir una, publique su carpeta —un repositorio git cuya raíz contenga `plugin.py`, o un zip de la carpeta— y pida a la gente que la ponga en `$SNOWPEA_HOME/extensions/<nombre>/` (por ejemplo `git clone <repositorio> ~/.snowpea/extensions/<nombre>`) y ejecute `snowpea extension reload <nombre>`. Actualizar es `git pull` más una recarga; los datos de `ctx.data_dir` se conservan. Como una extensión se ejecuta con los permisos del daemon, explique a sus usuarios qué hace, qué envía y adónde, y qué permiso declara cada herramienta. `scripts/gen_extension_index.py` del repositorio escribe un `index.json` estático que enumera carpetas de extensiones, para quien quiera alojar una lista.

## Referencia de la CLI y del RPC

```bash
snowpea extension list
snowpea extension info hello --json
snowpea extension enable hello
snowpea extension disable hello
snowpea extension reload hello
snowpea extension reload
snowpea extension new hello
```

| Comando | Hace | Estado de salida |
|---|---|---|
| `snowpea extension list [--json]` | todas las extensiones, su estado y su error | 0 |
| `snowpea extension info <nombre> [--json]` | una extensión, con sus herramientas, comandos, servidores MCP, configuración y esquema | 1 si está en `error` o `incompatible` |
| `snowpea extension enable <nombre>` | la activa y la carga | 1 si no se cargó |
| `snowpea extension disable <nombre>` | la descarga y la deja desactivada | 0 |
| `snowpea extension reload [<nombre>]` | recarga una, o todas las activadas | 1 si alguna no se cargó |
| `snowpea extension new <nombre>` | escribe la plantilla inicial; no necesita daemon | 2 si la carpeta ya existe |

Todos menos `new` hablan con el daemon mediante estos métodos (capacidad `extensions`):

| Método | Parámetros | Resultado |
|---|---|---|
| `extension.list` | `{}` | `{extensions: ExtensionInfo[]}`; vuelve a explorar la carpeta |
| `extension.enable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.disable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.reload` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.configure` | `{name, config}` | `{extension: ExtensionInfo}`; error `invalid_params` que nombra la ruta que falla cuando la configuración no cumple `configSchema` |

`ExtensionInfo`: `name`, `path`, `description`, `version`, `enabled`, `state` (`loaded`, `unloaded`, `disabled`, `error`, `incompatible`), `error` (o null), `effects` (cuántos registros mantiene), `configSchema` (o null), `config`. Los esquemas completos están en la [referencia del protocolo](../../protocol.md).

## Solución de problemas

| Lo que ve | Qué hacer |
|---|---|
| la extensión no aparece en `snowpea extension list` | la carpeta debe estar directamente en `$SNOWPEA_HOME/extensions/` y contener `plugin.py`; compruebe `SNOWPEA_HOME` |
| estado `unloaded` | se añadió después de arrancar el daemon: `snowpea extension reload <nombre>` |
| estado `error`, `plugin.json:…` | corrija el manifiesto como indica el mensaje |
| estado `error`, `settings.extensions.<nombre>.config…` | corrija la configuración en esa ruta, o fíjela con `extension.configure` |
| estado `incompatible` | la extensión se escribió para otra versión mayor de la API; consiga una versión que coincida |
| `… is already registered by builtin` | elija otro nombre; las herramientas y comandos integrados no se pueden reemplazar |
| `… is already registered by extension:<otra>` | dos extensiones quieren el mismo nombre; renombre una o desactive la otra |
| una herramienta falla con `(extension <nombre>) failed:` | una excepción en su herramienta; la ubicación y la traza del registro del daemon dicen dónde |
| una herramienta falla con `timed out after` | se alcanzó el `timeout` que pasó a `register` |
| las herramientas del servidor MCP no aparecen nunca | el registro del daemon dice por qué no arrancó el servidor; pruebe la entrada con `snowpea mcp test` |
| no pasa nada y no hay error | el daemon se arrancó con `SNOWPEA_EXTENSIONS=0`, o la extensión está desactivada en `settings.json` |

## Siguiente

[Plugins](plugins.md): skills, plugins de Claude Code y Codex, hooks y servidores MCP.

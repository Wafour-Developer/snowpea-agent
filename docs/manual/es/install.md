# Instalar

[English](../en/install.md) · [한국어](../ko/install.md) · [Todas las páginas](../README.md)

snowpea necesita dos runtimes: Python 3.11+ (gestionado por [uv](https://docs.astral.sh/uv/)) y Node 20+. El instalador coloca ambos si faltan. La interfaz de terminal viene incluida dentro del wheel de Python, así que no hay que ejecutar `npm install` en tu máquina.

## Instalación en una línea

**macOS, Linux, WSL2**

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
```

**Windows (PowerShell)**

```powershell
iwr https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.ps1 | iex
```

Luego compruébalo:

```bash
snowpea --version
```

El script es idempotente: volver a ejecutarlo actualiza la instalación existente. Instala uv mediante el instalador oficial, se asegura de que `node --version` reporte 20 o superior, instala el comando `snowpea` como una herramienta de uv, y añade `~/.local/bin` a tu perfil de shell si aún no está en `PATH`. En Windows, el directorio de datos es `%LOCALAPPDATA%\snowpea` en lugar de `~/.snowpea`.

Para ver qué haría sin tocar nada, descárgalo primero y pasa `--dry-run`:

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh -o install.sh
sh install.sh --dry-run
```

## Instalación manual

Si prefieres no canalizar un script directamente a un shell, o si el instalador de una línea falló en algún paso que quieres hacer tú mismo:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, if missing
uv tool install snowpea-agent
snowpea --version
```

Node 20+ debe estar en `PATH` para la interfaz de terminal. Las ejecuciones sin interfaz (`snowpea -c`) y todos los `snowpea <subcommand>` funcionan sin Node; solo la TUI lo necesita.

## Anular el origen de la instalación

El instalador acepta una anulación del origen, que es lo que usan CI y las instalaciones sin conexión:

```bash
sh install.sh --from-checkout
SNOWPEA_WHEEL_URL=./snowpea_agent-0.1.0-py3-none-any.whl sh install.sh
SNOWPEA_INSTALL_SOURCE=git+https://github.com/Wafour-Developer/snowpea-agent sh install.sh
```

`SNOWPEA_BIN_DIR` cambia dónde acaba el ejecutable `snowpea`, y `--from-checkout` instala el checkout en el que vive el propio script.

## Desde un checkout

```bash
git clone https://github.com/Wafour-Developer/snowpea-agent.git
cd snowpea-agent
uv sync
npm ci
npm run build
uv run snowpea --version
```

`npm run build` genera `tui/dist/snowpea-tui.js`, que `snowpea` busca después del paquete incluido. Mientras trabajas en la interfaz, apunta `SNOWPEA_TUI_ENTRY` a tu propio archivo de entrada para saltarte por completo el paquete incluido.

## Actualizar

snowpea comprueba si hay una versión más nueva una vez al día, en segundo plano, y cachea la respuesta en `$SNOWPEA_HOME/update-check.json`. Nunca se instala nada sin que tú lo digas.

**En la interfaz de terminal.** Cuando hay una actualización esperando, aparece un banner en la parte superior de la pantalla:

```text
⬆ Update available v0.1.2 (current v0.1.1) — press U or type /update
```

Pulsa `U` (o escribe `/update`) y responde `y`. La actualización corre en segundo plano, el banner informa del progreso, y al terminar snowpea se reinicia solo en la versión nueva. Un turno en marcha nunca se interrumpe.

**Desde la línea de comandos.**

```bash
snowpea update --check
snowpea update
```

`--check` informa de la versión actual y de la más reciente y no instala nada. Sin él, `snowpea update` ejecuta la actualización, detiene el daemon, y te dice que vuelvas a arrancar `snowpea`. `snowpea --version` también menciona una actualización pendiente, usando solo la respuesta cacheada, así que nunca espera a la red.

La actualización escribe su salida en `$SNOWPEA_HOME/logs/update.log`. Ejecuta `uv tool install --force --reinstall`; si `uv` no está en el `PATH`, se usa en su lugar el método que el instalador registró en `$SNOWPEA_HOME/install.json`, y si no se sabe nada, snowpea imprime el comando a ejecutar a mano en lugar de adivinar.

Dos ajustes controlan esto en `$SNOWPEA_HOME/settings.json`:

```json
{ "updates": { "check": true, "channel": "auto" } }
```

`check: false` apaga la comprobación diaria en segundo plano, dejando `/update` y `snowpea update` funcionando bajo demanda. `channel` es `auto` (PyPI cuando el paquete está publicado ahí, y los tags de git del repositorio en caso contrario), `pypi`, o `git`.

Para actualizar a mano en su lugar:

```bash
uv tool upgrade snowpea-agent
snowpea daemon stop
snowpea --version
```

Detén el daemon después de actualizar a mano. Un daemon en ejecución mantiene el código antiguo en memoria, y el siguiente cliente que se conecte negociaría con una versión de protocolo que ya no coincide con la instalada.

## Desinstalar

```bash
snowpea daemon stop
uv tool uninstall snowpea-agent
```

Eso deja tus datos intactos. Para eliminarlos también, borra `$SNOWPEA_HOME` (`~/.snowpea`, o `%LOCALAPPDATA%\snowpea` en Windows). Si registraste el daemon como un servicio, primero desregístralo:

```bash
snowpea service uninstall
```

## Cuando algo sale mal

**`snowpea: command not found` justo después de instalar.** `~/.local/bin` no está en tu `PATH` en este shell. Abre una nueva terminal, o vuelve a cargar tu perfil de shell. El instalador añade la línea, pero no puede cambiar el shell en el que ya estás.

**Node falta o es demasiado antiguo.** La TUI no arrancará. Instala Node 20+ desde tu gestor de paquetes o [nodejs.org](https://nodejs.org), luego vuelve a ejecutar `snowpea`. Todo lo demás sigue funcionando mientras tanto:

```bash
snowpea -c "hello" --json
```

**El daemon no arranca.** Revisa `$SNOWPEA_HOME/logs/daemon.log`, y comprueba si un proceso obsoleto está reteniendo el puerto registrado:

```bash
snowpea daemon status
snowpea daemon stop
snowpea daemon start
```

`daemon status` lee `$SNOWPEA_HOME/daemon.json` y verifica que el pid esté vivo. Un archivo obsoleto con un pid muerto se puede borrar sin riesgo.

**Proxy corporativo o máquina sin conexión.** `uv tool install` necesita PyPI. Configura `HTTPS_PROXY` antes de ejecutar el instalador, o instala desde un wheel que traigas tú mismo con `uv tool install ./snowpea_agent-0.1.0-py3-none-any.whl`.

## Siguiente

[Setup](setup.md) — elige un proveedor y deja una clave configurada.

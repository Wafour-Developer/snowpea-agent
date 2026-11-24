# Integraciones de hooks de CLI (beta)

Cuando Claude Code, Codex, Cursor agent o Antigravity (agy) se ejecutan dentro de un terminal de snowpea, snowpea necesita saber qué están haciendo: qué sesión es, si están trabajando, si han terminado o si esperan una respuesta. Cada una de estas CLI puede llamar a un hook cuando ocurre algo de eso. La integración registra un pequeño script de hook de snowpea en el archivo de hooks de cada CLI, para que la CLI le avise a snowpea.

Fuera de un terminal de snowpea, el hook no hace nada. Instalar la integración no cambia cómo se comporta la CLI cuando la ejecutas tú directamente.

Esta función está en beta, igual que las [sesiones de terminal](terminals.md).

Otros idiomas: [English](../en/integrations.md) · [한국어](../ko/integrations.md) · [日本語](../ja/integrations.md) · [简体中文](../zh-CN/integrations.md) · [todas las páginas](../README.md)

Relacionado: [Sesiones de terminal (beta)](terminals.md) · [Sesiones externas (beta)](../en/external-sessions.md)

## Comandos

```bash
snowpea integration status                  # las cuatro CLI
snowpea integration install claude          # una CLI
snowpea integration install all             # todas las CLI cuya carpeta de configuración exista
snowpea integration uninstall codex
snowpea integration status agy --json
```

`install`, `uninstall` y `status` aceptan `claude`, `codex`, `cursor`, `agy` o `all` (por defecto). Trabajan directamente sobre los archivos, así que el daemon no tiene que estar en marcha.

## Qué archivos toca cada CLI

| CLI | Carpeta de configuración (variable de entorno para cambiarla, valor por defecto) | Archivos | Eventos y lo que aprende snowpea |
|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR`, o `~/.claude` | `settings.json` | `SessionStart` (id de sesión; al arrancar, reanudar, limpiar, compactar o bifurcar) · `UserPromptSubmit` → working · `Stop` → done · `Notification` con `permission_prompt` o `elicitation_dialog` → blocked |
| Codex | `$CODEX_HOME`, o `~/.codex` | `hooks.json` y `config.toml` | `SessionStart` (id de sesión) · `UserPromptSubmit` → working · `Stop` → done. Activa la marca `hooks` de `[features]` en `config.toml` si no estaba activada |
| Cursor agent | `$CURSOR_CONFIG_DIR`, o `~/.cursor` | `hooks.json` | `sessionStart` (id de sesión) · `stop` → done |
| Antigravity (agy) | `$ANTIGRAVITY_CLI_CONFIG_DIR`, o `~/.gemini/config` | `hooks.json`, en un bloque `snowpea` | `PreInvocation` (id de sesión) |

Cada instalación toca además dos archivos de snowpea:

- `$SNOWPEA_HOME/hooks/snowpea-agent-state.sh` (`.ps1` en Windows): el script de hook que comparten las cuatro CLI. No hace nada a menos que `SNOWPEA_TERMINAL_ID` esté definido, y entonces pasa el evento de la CLI al núcleo de snowpea.
- `$SNOWPEA_HOME/integrations/state.json`: un registro de exactamente lo que snowpea añadió a cada CLI, para que la desinstalación lo pueda deshacer.

Al actualizar snowpea y reinstalar, el script de hook se sobrescribe. Pon tus propios hooks en otro archivo, no en este.

Solo Claude Code informa del estado blocked mediante sus hooks. Para Codex, Cursor agent y agy, snowpea lee de la pantalla un permiso o una pregunta, usando las reglas de pantalla de cada CLI.

snowpea solo localiza sus hooks por la cadena de comando exacta que escribió. Tus otros hooks se quedan como están.

## Qué hace la instalación

- Añade las entradas de snowpea a los archivos anteriores y no cambia nada más. Se conservan el formato, los comentarios y el orden de tus propias entradas.
- Escribe cada archivo de forma atómica (un archivo temporal y luego un renombrado), conserva los permisos y vuelve a leer el resultado para comprobarlo.
- Rechaza un archivo que sea un enlace duro, porque escribir en él podría cambiar un segundo archivo que no esperabas.
- No cambia ni un byte si la integración ya está al día, así que ejecutar la instalación dos veces es seguro.
- Guarda una copia de seguridad antes del primer cambio en un archivo existente: `<archivo>.snowpea-backup-<fecha>`, en la misma carpeta. La desinstalación no borra las copias.
- Requiere que la carpeta de configuración de la CLI exista. Si no existe, `install <cli>` te pide que ejecutes antes la CLI una vez. `install all` omite una CLI cuya carpeta falte y lo indica.

## Qué hace la desinstalación

La desinstalación quita exactamente lo que añadió la instalación, y nada más:

- Solo se eliminan las entradas de snowpea, y los grupos que quedan vacíos por ello. Tus hooks se conservan.
- Un archivo que creó la instalación se borra cuando no le queda contenido. Un archivo vacío que ya existía se restaura tal como estaba.
- En Codex, la marca `hooks` de `[features]` solo se desactiva si la instalación la activó. Si ya estaba activada, se queda activada.
- El script de hook se borra cuando ninguna CLI conserva una integración.

## Valores de estado

`status` muestra una línea por CLI: el estado, la carpeta de configuración y un motivo cuando lo hay. La nota `[CLI not found on PATH]` indica que la propia CLI no está instalada, así que la integración todavía no hace nada.

| Estado | Significado | Qué hacer |
|---|---|---|
| `not-installed` | no hay entradas de snowpea en la configuración de la CLI | `snowpea integration install <cli>` |
| `current` | todo está en su sitio y al día | nada |
| `outdated` | la instaló una versión antigua de snowpea, o los eventos registrados difieren de los de esta versión | `snowpea integration install <cli>` |
| `modified` | una de las entradas de snowpea se editó o se borró a mano, o se desactivó la marca de Codex | desinstalar e instalar de nuevo, o devolver el archivo a su estado original |

## Aplicación de escritorio y web

La página Integrations de los ajustes de la aplicación de escritorio y de snowpea web muestra las mismas filas, con los archivos que toca cada CLI, y ejecuta las mismas acciones de instalar y desinstalar. La página y sus acciones son solo para la persona que está en la máquina local: un inicio de sesión remoto no puede verlas ni cambiarlas.

## Variables de entorno

| Variable | Para qué sirve |
|---|---|
| `SNOWPEA_HOME` | dónde están el script de hook y `integrations/state.json` (por defecto `~/.snowpea`) |
| `CLAUDE_CONFIG_DIR` | carpeta de configuración de Claude Code |
| `CODEX_HOME` | carpeta de configuración de Codex |
| `CURSOR_CONFIG_DIR` | carpeta de configuración de Cursor agent |
| `ANTIGRAVITY_CLI_CONFIG_DIR` | carpeta de configuración de agy |

Si arrancas una CLI con una de estas variables definida, instala la integración con la misma variable definida, para que escriba en la carpeta que lee la CLI.

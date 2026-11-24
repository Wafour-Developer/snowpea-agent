# Sesiones de terminal (beta)

Una sesión de terminal es un programa real que se ejecuta dentro de un terminal en snowpea: una shell, la CLI de snowpea o una de las CLI externas (Claude Code, Codex, agy, Cursor agent). Ves su pantalla, escribes en ella, y snowpea lleva la cuenta de si está trabajando, esperando a que respondas o ha terminado.

Esta función está en beta. Las reglas para leer la pantalla de cada CLI siguen lo que imprime la CLI, así que una versión nueva puede cambiar lo que snowpea ve. Si un estado parece incorrecto, el propio terminal es siempre la referencia.

Otros idiomas: [English](../en/terminals.md) · [한국어](../ko/terminals.md) · [日本語](../ja/terminals.md) · [简体中文](../zh-CN/terminals.md) · [todas las páginas](../README.md)

Relacionado: [Sesiones externas (beta)](../en/external-sessions.md) · [Modo Jarvis (beta)](../en/jarvis.md) · [Integraciones de hooks de CLI (beta)](integrations.md) · [snowpea web](web.md) · [Terminal UI](tui.md)

## Qué es una sesión de terminal

- El daemon inicia un pequeño proceso holder por cada terminal. El holder es dueño del pseudoterminal del programa, guarda la última 1 MiB de salida y registra el código de salida.
- Los terminales se abren desde la aplicación de escritorio, desde snowpea web o desde Jarvis. El comando `snowpea term` sirve para listar, leer, escribir y adjuntarse a terminales que ya existen.
- Como máximo se ejecutan `terminals.max` terminales a la vez (por defecto 16, entre 1 y 64).
- Perfiles: `shell`, `snowpea`, `claude`, `codex`, `agy`, `cursor`.

## Sesiones de CLI externas como terminales

Con `jarvis.external.view` en `terminal` (el valor por defecto), una sesión externa se abre como terminal: la CLI se ejecuta en un panel, en lugar de la vista de chat. Ponlo en `chat` para conservar la vista de chat estructurada de antes. Los clientes que no piden una vista mantienen la vista de chat.

- Una sesión nueva arranca la CLI en la carpeta del proyecto que elegiste.
- Una sesión existente se reanuda con el comando de reanudación de la propia CLI (`claude --resume <id>`, `codex resume <id>`, y los equivalentes de agy y Cursor). El id de sesión viene de la integración de hooks, así que ejecuta antes [`snowpea integration install`](integrations.md) si quieres que la reanudación funcione desde un terminal.
- Una sesión que ya está activa en otro terminal solo se puede ver, como antes.

En la aplicación, el menú **+** del proyecto → **New external session…** pregunta **Open as**: **Terminal** o **Chat** (preseleccionado según `jarvis.external.view`). Para un terminal, muestra las opciones de permisos de la CLI ([más abajo](#opciones-de-permisos)) en lugar de los modos del chat. **Load external session…** ofrece **Resume in a terminal** en cada sesión que ahora mismo no ejecuta nada.

La CLI se ejecuta dentro de tu shell interactiva, así que siguen valiendo tu PATH y herramientas como nvm. Cuando la CLI termina, el panel vuelve a la shell, salvo que `terminals.keepShell` esté desactivado.

## Una sesión de snowpea en un terminal

Cada sesión de snowpea se puede mostrar de dos formas: como chat, o como la CLI de snowpea ejecutándose en un terminal adjunto a la misma sesión. Ambas vistas son clientes de la misma sesión, así que puedes tenerlas abiertas a la vez. En la aplicación, la cabecera de la conversación tiene una pestaña **Terminal** junto a **Chat** y **Trajectory** (solo con un daemon que lo admita). La pestaña reutiliza el terminal en marcha de la sesión, o arranca uno.

- La vista por defecto la fija `ui.sessionView`: `chat` (por defecto) o `terminal`. Se guarda en los ajustes de este daemon, así que todas las ventanas de la aplicación y las páginas web conectadas a él abren las sesiones de la misma manera. Cámbiala en **Settings → Terminal**.
- Para adjuntarte desde una shell a una sesión que ya existe en el daemon local, ejecuta `snowpea --session <id>`. La TUI reanuda esa sesión en lugar de crear una nueva.

## Ajustes

| Clave | Por defecto | Qué hace |
|---|---|---|
| `ui.sessionView` | `chat` | vista por defecto de una sesión de snowpea |
| `jarvis.external.view` | `terminal` | cómo se abre una sesión de CLI externa: `terminal` o `chat` |
| `terminals.max` | `16` | número máximo de terminales a la vez (1 a 64) |
| `terminals.keepShell` | `true` | al terminar la CLI, vuelve a la shell en vez de cerrar el panel |
| `terminals.headlessCols` / `terminals.headlessRows` | `160` / `48` | el tamaño que usa un terminal mientras no hay ninguna vista adjunta |
| `terminals.sounds.blocked` | `true` | sonido y aviso cuando un terminal te necesita |
| `terminals.sounds.done` | `true` | sonido y aviso cuando un terminal termina y no estás mirando |

Los ajustes `terminals.*` y `ui.*` pertenecen a la persona que está en la máquina local. Una conexión remota o por túnel no puede cambiarlos.

En la aplicación, **Settings → Terminal** fija las dos vistas por defecto, `terminals.keepShell` y los dos avisos. Cada interruptor de aviso activa o desactiva a la vez su sonido y su notificación; **Play the sound** te permite oírlo. Los sonidos son dos tonos cortos que genera la propia aplicación.

## Estado: qué significan los puntos

Cada terminal está en uno de cinco estados, y la aplicación lo muestra con un punto:

| Punto | Estado | Significado |
|---|---|---|
| needs you | `blocked` o `error` | el programa espera una respuesta (un permiso o una pregunta) o ha fallado |
| done | `done` | un turno ha terminado y nadie ha mirado el terminal desde entonces (`seen` es false) |
| working | `working` | un turno está en marcha |
| idle | `idle` | no se está ejecutando nada |

Un estado bloqueado tiene además un tipo: `permission`, `question` o `auth`. La barra lateral agrupa los puntos de un proyecto en este orden: needs you, done, working, idle.

En la aplicación, los puntos son rojos con un anillo (needs you), verdes (done), del color de acento y parpadeando (working; fijos cuando tu sistema pide reducir el movimiento) y grises (idle). Aparecen en las pestañas de terminal, en la fila de una sesión externa en la barra lateral, en la fila de una conversación mientras su terminal está abierto, en la fila del proyecto (el resumen) y en la pestaña **Terminals** de la vista de Jarvis.

- Entrar en un terminal haciendo clic lo marca como visto, así que un punto done pasa a gris.
- Cuando un terminal empieza a necesitarte, la aplicación reproduce un sonido y muestra un aviso. Cuando uno termina, hace lo mismo solo si el panel de ese terminal no tiene el teclado o la ventana está en segundo plano. Los avisos salen solo de la ventana principal.
- **Alt+↑ / Alt+↓** salta al terminal anterior o siguiente que te necesita, o que ha terminado y nadie ha mirado todavía. Una sesión externa se abre en su propia vista; cualquier otro terminal se abre en la pantalla Terminal.

snowpea toma el estado de la primera fuente que tenga una opinión, de mayor a menor prioridad:

1. el propio programa (la TUI de snowpea informa de su estado);
2. un permiso o una pregunta visibles en pantalla, leídos por las reglas de pantalla de esa CLI;
3. los informes de hooks de la CLI ([integraciones](integrations.md));
4. solo las reglas de pantalla.

## Opciones de permisos

Al arrancar una CLI en un terminal puedes elegir su modo de permisos. Las opciones vienen de los modos propios de cada CLI:

| CLI | Opciones ofrecidas | Peligrosas (en rojo, confirmar antes) |
|---|---|---|
| Claude Code | Accept edits, Plan, Auto, Don't ask | Bypass permissions (`--dangerously-skip-permissions`) |
| Codex | Read only, Default | Full access (`--dangerously-bypass-approvals-and-sandbox`) |
| agy | Accept edits, Auto, Plan | Skip permissions (`--dangerously-skip-permissions`) |
| Cursor agent | Ask, Plan | Run everything (`--yolo`) |

En la aplicación eliges como máximo una opción: la CLI admite un solo modo de permisos. Una opción que el daemon rechaza en esta conexión se muestra desactivada, con el motivo, y por el túnel con **Unlock…**.

Una opción peligrosa:

- muestra una insignia roja donde aparezca, y la aplicación te pide que confirmes el riesgo antes de arrancar;
- se rechaza cuando Jarvis o cualquier herramienta la pide, con independencia de los ajustes;
- se rechaza mientras `web.remote.maxMode` esté por debajo de `auto`;
- se rechaza por el túnel web si el terminal no está desbloqueado ([snowpea web](web.md#una-dirección-pública-con-un-túnel)).

Jarvis nunca responde a un permiso en un terminal arrancado con una opción peligrosa. Los modos no peligrosos, como Accept edits o Don't ask, también los concede solo la persona desde la aplicación local, no Jarvis. Jarvis arranca los terminales de CLI solo con los permisos por defecto de la CLI.

## En la aplicación

- **La pantalla Terminal y la pestaña de terminal del dock.** En snowpea web muestran los terminales del daemon, con **New shell** para la carpeta de un proyecto. La aplicación de escritorio además mantiene sus propias shells: un interruptor elige **This computer** o **snowpea terminals**.
- **La franja sobre un terminal** muestra su punto y su título, una insignia roja **Permissions skipped** en un terminal arrancado con una opción peligrosa, **Exited (code)** cuando terminó, y **Close terminal**, que pregunta antes porque termina el programa. Cerrar un panel o una ventana no termina el terminal.
- **Ventana propia** (aplicación de escritorio): el botón de la franja abre solo ese terminal en una ventana aparte. Todas las ventanas muestran el mismo terminal, y cerrar una deja funcionando las demás.
- **Solo observar.** Cuando el daemon deja que una página solo observe un terminal, el panel deja de enviar teclas, muestra una insignia **Watching** y dice que es de solo observación.
- **Por el túnel web**, un panel que no está desbloqueado muestra **Unlock…**, que pide la contraseña web una vez para este inicio de sesión ([snowpea web](web.md#una-dirección-pública-con-un-túnel)).

## Los terminales sobreviven a un reinicio del daemon

Como el holder funciona por su cuenta, un terminal sigue ejecutándose cuando el daemon se reinicia o se actualiza. Al volver a arrancar, el daemon se reconecta a cada holder vivo y redibuja cada pantalla a partir de la salida reciente del holder. Un terminal cuyo holder ha muerto (por ejemplo, tras reiniciar el equipo) aparece como perdido, y puedes reanudar su sesión de CLI.

```bash
snowpea daemon stop                    # el daemon se detiene; los terminales siguen ejecutándose
snowpea daemon stop --and-terminals    # el daemon y todas las sesiones de terminal terminan
```

## El comando `snowpea term`

```bash
snowpea term ls                        # terminales activos
snowpea term ls --all                  # incluye también los terminados
snowpea term read <id> --lines 100     # la pantalla como texto plano, con 100 líneas de historial
snowpea term send <id> "ejecuta las pruebas"   # escribe el texto y pulsa Enter
snowpea term send <id> --no-submit "borrador"  # escribe sin pulsar Enter
snowpea term attach <id>               # toma el control del terminal en esta ventana
snowpea term attach <id> --observe     # solo mirar: nunca escribe, nunca cambia el tamaño
```

- `attach` pone tu terminal en modo raw y muestra la sesión. **Ctrl+\\** se desadjunta y la sesión sigue ejecutándose. `attach` necesita un terminal interactivo y todavía no es compatible con Windows; en Windows usa `read` y `send`, o abre el terminal en la aplicación.
- `send` espera a que dejes de escribir en ese terminal y luego escribe el texto. Rechaza un terminal bloqueado por un permiso, salvo que añadas `--force`. Usa `--force` solo cuando hayas leído la pantalla y sepas qué pregunta el permiso.
- `read` solo muestra la pantalla. Nunca cambia nada.

## Jarvis y los terminales (beta)

Con el modo Jarvis activado, Jarvis ve los terminales de los proyectos a los que tiene acceso y dispone de estas herramientas:

| Herramienta | Qué hace |
|---|---|
| `terminal_list` | lista los terminales, con su estado y quién los inició |
| `terminal_read` | el texto de pantalla, el estado y el título de un terminal |
| `terminal_send` | escribe un mensaje en un terminal de CLI y lo envía |
| `terminal_keys` | pulsa teclas con nombre (enter, escape, up, y, 1 …) |
| `terminal_interrupt` | interrumpe lo que esté haciendo la CLI |
| `terminal_wait` | espera a que el terminal deje de trabajar, se bloquee, termine, o muestre un texto que coincida |
| `terminal_create` | arranca Claude Code, Codex, agy o Cursor en un proyecto con sus permisos por defecto y, opcionalmente, un primer prompt |

Las reglas:

- Jarvis solo escribe en terminales de CLI. Nunca escribe en una shell normal, nunca usa `force` y nunca escribe en un terminal arrancado con una opción peligrosa.
- Jarvis solo escribe por tu petición o por trabajo que le hayas delegado. Cada lectura y cada instrucción queda registrada en el registro de acceso (`jarvis.access`) y en el libro de decisiones (ledger).
- `terminal_create` nunca activa una opción peligrosa y marca el terminal como iniciado por Jarvis.

Lo que despierta a Jarvis:

- un terminal delegado que pasa a `blocked` o `error`, o llega a `done`, despierta a Jarvis de inmediato (dentro de sus agrupaciones y presupuesto habituales);
- cualquier otro terminal va al resumen de 30 segundos;
- cuando un terminal que Jarvis inició se bloquea, da error o termina, Jarvis recibe una nota con las últimas 20 líneas de ese terminal. La nota es un evento para Jarvis; nunca se escribe en el terminal.

Responder a un permiso dentro de un terminal: Jarvis solo responde a un permiso de la CLI cuando se cumplen todas estas condiciones: el terminal está completamente delegado y no lo tienes retenido tú; no es un terminal peligroso; la regla de pantalla ha podido leer el comando que pregunta el permiso; y el motor de delegación clasifica ese comando como `safe` o `risky`. Para los comandos `guarded` o `never`, y para cualquier otro caso, el permiso es tuyo. Cada respuesta y cada rechazo de Jarvis se escriben en el libro de decisiones. Los niveles de delegación se explican en [Modo Jarvis](../en/jarvis.md#delegation).

# The terminal UI

`snowpea` sin argumentos abre la interfaz de terminal. Es un cliente delgado: cada decisión — qué herramienta puede ejecutarse, qué significa un comando, cuándo compactar — pertenece al daemon, y esta página trata de la superficie por la que salen esas decisiones.

Otros idiomas: [English](../en/tui.md) · [한국어](../ko/tui.md) · [日本語](../ja/tui.md) · [简体中文](../zh-CN/tui.md) · [Todas las páginas](../README.md)

## La pantalla de inicio

Lo primero que imprime una sesión es el logotipo, ajustado al ancho de tu terminal, y luego tres líneas que dicen dónde estás:

```text
██████████  ██      ██  ██████████  ██      ██  ██████████  ██████████  ██████████
██          ████    ██  ██      ██  ██      ██  ██      ██  ██          ██      ██
██████████  ██  ██  ██  ██      ██  ██      ██  ██████████  ██████████  ██████████
        ██  ██    ████  ██      ██  ██  ██  ██  ██          ██          ██      ██
        ██  ██      ██  ██      ██  ████  ████  ██          ██          ██      ██
██████████  ██      ██  ██████████  ██      ██  ██          ██████████  ██      ██
🌱 snowpea v0.1.2

          Open-source multi-vendor coding agent and personal AI assistant
        v0.1.2 · anthropic/claude-sonnet-4-5 · /home/you/project · ACCEPT

Last session: 26m ago · "add the worktree parallel session story to the IDE plan"
Press R or type /resume to continue it
```

El bloque de la última sesión aparece cuando el daemon todavía tiene una sesión abierta para este directorio — otra terminal, una ejecución sin interfaz, una sesión que dejó un cierre inesperado. `R` con la entrada vacía la reproduce en esta ventana; `/resume` abre en su lugar el selector sobre todas las sesiones guardadas. Si el daemon no tiene ninguna, el bloque no se muestra: una oferta que no se puede aceptar es peor que ninguna oferta.

El banner se imprime una sola vez. Sube con el resto de la salida y no vuelve a dibujarse.

## Sesiones guardadas

La oferta de la pantalla de inicio es solo la más reciente. Todo lo que esta máquina ha ejecutado alguna vez sigue en disco, y `/sessions` abre el selector sobre ello:

```text
╭──────────────────────────────────────────────────────────────────────╮
│ ❯  01J9F2… · 2026-03-14 09:41 · fix the flaky worktree test          │
│    01J9DR… · 2026-03-13 18:02 · add the scheduler reminder story     │
│    01J9C7… · 2026-03-13 11:26 · (no prompt)                          │
│    Cancel                                                            │
│ ↑↓ move · Enter resume · Esc cancel                                  │
╰──────────────────────────────────────────────────────────────────────╯
```

El listado incluye las sesiones cerradas, no solo las que el daemon todavía mantiene abiertas, y cada fila lleva el último prompt que vio esa sesión, así que una fila es identificable sin recordar ningún id. Las filas van de la más reciente a la más antigua, están acotadas a este directorio, y la sesión en la que ya estás no se te ofrece.

| Qué escribes | Qué hace |
|---|---|
| `/sessions` | abre el selector |
| `/resume` | el mismo selector |
| `/resume <sessionId>` | reabre esa sesión directamente, sin selector |
| `R` con la entrada vacía | se salta el selector y toma la sesión más reciente de este directorio |
| `/session delete <id>` | borra una sesión guardada |
| `/session clear` | borra las sesiones guardadas de este directorio |
| `/session clear --all` | borra todas las sesiones guardadas de esta máquina |

Reanudar reproduce el historial de la sesión en esta ventana y la continúa: el directorio de trabajo, el modo, el proveedor, el modelo y el team vuelven con ella, tanto si el daemon la seguía teniendo abierta como si no.

Borrar se lleva los mensajes, los eventos y la fila de la sesión a la vez, y con ellos los adjuntos y los ficheros de voz que esa sesión poseía, porque un hilo borrado por lo que se pegó en él no debería dejar atrás lo pegado. Una sesión viva nunca se borra: ciérrala primero, y hasta entonces `clear` la salta. Reanudar, en cambio, necesita un turno en reposo: `/resume` y `/sessions` se niegan mientras algo se está ejecutando, porque cambiar la transcripción por debajo de un turno vivo lo partiría en dos.

## La disposición

La interfaz dibuja en línea, como `git log`, no como una aplicación de pantalla completa. La salida terminada se entrega a la terminal, así que el scrollback, el ratón y tu `Ctrl+Shift+F` siguen siendo los de siempre. Solo la parte de abajo está viva.

```text
 › explain the retry logic                 ← scrollback: yours, never redrawn
 ⏺ Read 3 files (128 lines)
 ◆ The retry lives in `client.ts`…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← the live region starts here
 > the next thing you type
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

De arriba abajo, el panel inferior es: la línea de resumen del modo, la línea de estado, el aviso de contexto cuando lo hay, y luego las filas de agentes. El modo va primero a propósito: es la línea que decide qué le está permitido hacer al siguiente turno, así que se sitúa lo más cerca posible de lo que estás escribiendo. La entrada queda encima de ellas, con el indicador de trabajo encima de la entrada. Nada por debajo de la entrada se redibuja si no ha cambiado.

## Mientras trabaja

Un turno muestra una línea sobre la entrada, y esa línea dice lo que está pasando de verdad:

| Línea | Significa |
|---|---|
| `✢ Pondering… (12s · ↓ 3.7k tokens)` | el modelo piensa; el verbo cambia cada pocos segundos |
| `✳ Running shell: npm test… (4s · …)` | una herramienta está en vuelo, con su nombre y su argumento |
| `✶ 3 agents working… (1m 2s · …)` | el turno ha delegado |
| `✳ /ralph… (3m 10s · …)` | un flujo de comando se ha quedado con el turno |
| `⏸ Waiting for approval` | está bloqueado esperándote |

El reloj cuenta este turno y los tokens son los de este turno, no los de la sesión; los totales de la sesión están en la línea de estado. `Esc` interrumpe.

Cuando termina una tanda de llamadas a herramientas, se pliega en una sola línea del scrollback en lugar de una tarjeta por llamada:

```text
⏺ Ran 2 shell commands (34 lines)
⏺ Read 3 files (128 lines)
```

Una llamada que falló conserva su tarjeta con su salida, porque esa es la que hay que leer. `Ctrl+O` despliega la llamada a herramienta o el diff más reciente mientras sigue en vivo.

## Modos

`Shift+Tab` rota accept → auto → plan → accept. El modo está en la línea de estado y en la línea de resumen, y [Modos](modes.md) explica qué permite cada uno. `Ctrl+P` activa y desactiva el modo plan sin rotar.

También hay un selector, para cuando quieres un modo concreto en vez del siguiente. `↓` más allá de la entrada de historial más reciente lleva el cursor a la línea de resumen; `Enter` ahí lo abre:

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to choose mode
❯  accept mode
   auto mode
   plan mode
```

Empieza en el modo en el que estás, `Enter` toma el resaltado y `Esc` deja el modo como estaba. En cualquier caso la línea de resumen del modo se dibuja encima de la línea de estado, no debajo.

## Aprobaciones

Cuando el daemon pregunta, pregunta con un menú. `↑`/`↓` mueven, `Enter` toma la fila resaltada, `Esc` rechaza:

```text
╭──────────────────────────────────────────────────────╮
│ Approval required                                    │
│ shell risk=high timeout=300s                         │
│   command: rm -rf build                              │
│                                                      │
│ ❯  Yes   (y)                                         │
│    Yes, and don't ask again this session   (a)       │
│    Yes for this project   (p)                        │
│      adds an allowlist rule the daemon keeps         │
│    No   (n)                                          │
│ ↑↓ move · Enter confirm · Esc cancel                 │
╰──────────────────────────────────────────────────────╯
```

Cuando el daemon tiene algo de lo que avisarte —un comando que sale del directorio de trabajo, un riesgo que los argumentos por sí solos no muestran— la petición lleva un `note`, y se renderiza en rojo encima de los argumentos tanto en la TUI como en la CLI sin interfaz:

```text
shell risk=high timeout=300s
  ⚠ this deletes a directory outside the working tree
  command: rm -rf ../build
```

El cursor empieza en `Yes`, así que `Enter` significa sí. `y`, `a`, `p` y `n` siguen funcionando directamente. Mientras el diálogo está abierto se queda con el teclado: nada de lo que escribas llega al borrador de detrás, y `Shift+Tab` no cambia el modo.

Las aprobaciones que levanta un turno sin nadie delante — un job programado, un mensaje de Telegram — van a una cola. `Ctrl+R` le pasa el teclado a esa cola; `/approvals` la lista.

## Diffs

Un cambio de fichero aparece donde ocurrió, dentro de la conversación:

```text
✎ Edited README.md  (+4 −2)
--- a/README.md
+++ b/README.md
@@ -1,5 +1,7 @@
 # snowpea
-an agent
+an open-source multi-vendor coding agent
… 3 more lines (Ctrl+O)
```

Un fichero nuevo se lee como `✚ Created notes.md (7 lines)`. Los parches largos se cortan a doce líneas; `Ctrl+O` abre el más reciente mientras sigue en la región viva.

## Contexto

La línea de estado lleva lo llena que está la ventana de contexto del modelo:

```text
ctx 34% (68k/200k)
```

Va atenuada hasta el 70%, ámbar a partir de ahí, roja desde el 85%, y pasado el 80% aparece una fila sobre el resumen que dice qué hacer:

```text
[!!] context 85% — /compact to free space
```

`/compact [instrucciones]` resume la conversación hasta ahora y continúa desde el resumen; las instrucciones opcionales dicen qué conservar. El daemon también compacta por su cuenta antes de que un turno pase de `context.autoCompactPercent`. En ambos casos la transcripción deja constancia:

```text
───────────── compacted (68.0k → 12.1k tokens) ─────────────
```

`ctx 12.3k used` sin porcentaje significa que el daemon no conoce la ventana de ese modelo. `snowpea session context` y el ajuste `providers.<vendor>.context_window` están en [Comandos](commands.md).

## Escribir

`↑` recorre hacia atrás tus prompts anteriores y `↓` vuelve hacia lo que estabas escribiendo. El historial es por máquina, no por sesión: vive en `$SNOWPEA_HOME/tui-history.jsonl`, guarda las últimas 500 entradas y no registra dos veces seguidas el mismo prompt.

`↓` más allá de la entrada más reciente hace otra cosa: saca el cursor de la entrada y lo lleva a las filas de abajo. Primero la línea de resumen, donde `Enter` abre el selector de modo:

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to choose mode
```

Después las filas de agentes, una a una. `Esc` o `↑` vuelven arriba, a la entrada.

### Enviar mientras está trabajando

No tienes que esperar a que termine un turno. Un prompt enviado mientras hay uno en marcha se acepta y se encola en lugar de rechazarse, y la cola se vacía por orden de llegada: un turno cada vez contra un solo historial, así que nunca se ejecutan dos bucles de proveedor sobre la misma conversación. Los adjuntos se capturan cuando pulsas `Enter`, así que un chip encolado ahora sigue siendo el fichero que querías cuando le llegue su turno. La cola está solo en memoria; no sobrevive a un reinicio del daemon.

`Esc` descarta la cola junto con el turno en marcha. Interrumpir significa «para lo que te he pedido», y eso tiene que incluir los mensajes de seguimiento que siguen esperando, o al Stop le seguiría la cola ejecutándose igualmente. Cada prompt descartado se reporta a los clientes como `turn.dequeued` con motivo `dropped`, seguido de su propio `turn.done`, así que nada que estuviera esperando ese id de turno se queda colgado.

Los clientes también ven `turn.queued` cuando un prompt entra y `turn.dequeued` con motivo `started` cuando sale uno. La interfaz de terminal todavía no dibuja un indicador de cola (pendiente): hasta que lo haga, un prompt encolado simplemente espera en silencio a que empiece su turno.

### Mirar dentro de un agente

`Enter` sobre la fila de un agente sustituye la transcripción por la conversación de ese agente — el encargo que recibió, sus llamadas a herramientas, su respuesta:

```text
╭────────────────────────────────────────────────────────────────────╮
│ ◯ executor · Implement story IDE-004      running · 28s · ↓ 159.1k │
│ › Implement story IDE-004 (Worktree parallel sessions)             │
│ ✓ read_file path=docs/stories/IDE-004.md (1 lines)                 │
│ ◆ Added the worktree manager and its tests; 3 files changed.       │
│ ↑↓ PgUp/PgDn scroll · Esc back to the main transcript              │
╰────────────────────────────────────────────────────────────────────╯
```

Cada delegado corre en una sesión propia, y esta vista es esa sesión: se reproduce y después se sigue en vivo. `Esc`, o `Enter` sobre `● main`, vuelve. `Ctrl+A` abre el panel más allá de sus reglas de plegado, así que se listan los agentes inactivos y lo que esté detrás de `↓ N more`.

## Actualizaciones

Cuando hay una versión más nueva la línea de estado lo dice, y `U` con la entrada vacía — o `/update` — abre la confirmación. La actualización corre, el daemon se reinicia y la interfaz vuelve en la versión nueva.

```text
snowpea v0.1.2 → v0.1.3 (U to update)
```

## Adjuntos

Pega o suelta la ruta de un fichero en la entrada y se convierte en un chip en vez de en texto:

```text
[📎 screenshot.png 1.2MB] (backspace removes the last · Ctrl+X clears)
> what is wrong with this layout?
```

Entiende lo que una terminal entrega de verdad: una ruta, varias a la vez, una ruta con los espacios escapados, una ruta con los espacios sin escapar, una URL `file://`, las comillas que añadió un gestor de ficheros. `Ctrl+V` toma una imagen directamente del portapapeles del sistema — `wl-paste`, `xclip`, AppleScript o PowerShell, lo que haya en esta máquina — y la guarda bajo `$SNOWPEA_HOME/tmp/`. `/attach <path>` lo hace a mano.

`Backspace` con la entrada vacía quita el chip más reciente y `Ctrl+X` los quita todos. Al enviar el prompt los ficheros van con él, y la transcripción dice cuáles:

```text
› what is wrong with this layout?
  📎 screenshot.png
```

Los ficheros se envían como rutas, así que no se copia nada. Qué hace el modelo después con ellos — y el límite de 20MB, el reescalado, qué pasa con un modelo que no ve — está en [Adjuntos y voz](voice.md).

## Voz

La voz necesita un backend, y quien los tiene es el daemon. `snowpea setup audio` los configura; [Adjuntos y voz](voice.md) lista lo que necesita cada uno.

| Tecla o comando | Qué hace |
|---|---|
| `/voice` | activa la entrada por voz |
| `Ctrl+Space`, o `/rec` | empieza a grabar; otra vez para parar |
| `/tts on`, `/tts off` | lee cada respuesta al terminarse |
| `Esc` | detiene una respuesta que se está leyendo |

Mientras graba, el hueco del indicador cuenta:

```text
● REC 00:07
```

Al parar, transcribe y deja el texto en el borrador en lugar de enviarlo, porque el reconocimiento de voz se equivoca lo bastante a menudo como para leerlo antes. La grabación ocurre en el daemon cuando el daemon tiene micrófono, y en esta máquina cuando no.

`🔊` en la línea de estado significa que las respuestas se están leyendo. Cuando falta una capacidad, el comando lo dice con las palabras del propio daemon en vez de no hacer nada:

```text
voice input needs speech-to-text: no transcription backend: install the whisper CLI, set an OpenAI API key, or …
```

## Pantalla completa

`--fullscreen` cambia a una disposición sobre el buffer alternativo: la transcripción pasa a ser una ventana que la interfaz desplaza ella misma con `PgUp`/`PgDn` y `Ctrl+U`/`Ctrl+D`, y al salir no queda nada en tu scrollback.

```bash
snowpea --fullscreen
```

Gasta menos ancho de banda en una conexión lenta, porque solo se redibujan las filas que cambiaron. También te quita el scrollback de la terminal durante la sesión, y por eso no es lo predeterminado.

## Teclas

| Tecla | Qué hace |
|---|---|
| `Enter` | enviar, o confirmar la opción resaltada |
| `Shift+Tab` | rotar el modo |
| `Ctrl+P` | alternar el modo plan |
| `↑` / `↓` | prompts anteriores; `↓` más allá del último entra en el panel |
| `Esc` | interrumpir el turno, parar la lectura, o salir de la vista de un agente |
| `Ctrl+O` | desplegar la llamada a herramienta o el diff más reciente |
| `Ctrl+A` | abrir del todo el panel de agentes |
| `Ctrl+R` | ir a la cola de aprobaciones desatendidas |
| `Ctrl+V` | adjuntar una imagen del portapapeles |
| `Ctrl+X` | vaciar los adjuntos |
| `Ctrl+Space` | empezar o parar la grabación |
| `U` | aceptar la actualización ofrecida |
| `R` | continuar la sesión que ofreció la pantalla de inicio |
| `F1` | ayuda |
| `Ctrl+C` | salir |

`/help` lista todos los comandos que tiene el daemon, incluidos los que añadieron tus plugins, y repite esta tabla.

## Tablas de Markdown

Las tablas de Markdown de las respuestas se renderizan con bordes alineados. El dimensionado de columnas tiene en cuenta los anchos de terminal del coreano, del CJK y de los emoji, y las rutas o frases largas se ajustan dentro de su celda. Cuando hay demasiadas columnas para que quepan, los valores se apilan bajo sus etiquetas de columna. Las vistas en línea y a pantalla completa usan el mismo renderizador; el código fuente de una tabla dentro de un bloque de código se queda literal.

## Agentes incorporados y personalizados

`/agent list` incluye los roles empaquetados (`architect`, `critic`, `executor`, `explorer`, `test-engineer`, `verifier`) junto a las definiciones personalizadas. Los roles incorporados no necesitan ningún fichero creado por el usuario y tienen source `builtin`. Una definición personalizada con el mismo nombre sobrescribe a la incorporada; las definiciones de proyecto tienen precedencia sobre las globales. Los mismos nombres están disponibles a través del argumento `agent` de `delegate_task`.

## Avisos de actualización al arrancar

Cada arranque comprueba si hay actualizaciones en segundo plano. `/update` siempre se salta una caché negativa antigua y vuelve a comprobar; cuando no hay nada más nuevo, informa de que la build ya está al día en lugar de mostrar un fallo de instalación. Cuando aparece un banner, pulsa `U` con la entrada vacía o escribe `/update` para abrir la confirmación. Elige `y` para instalar y reiniciar, o `n`/Esc para posponer. Una `u` minúscula escribiendo normalmente no es un atajo de actualización.

Las instalaciones desde `main`/`master` de Git comparan el commit instalado, así que no hace falta un bump de versión para detectar commits nuevos. Las comprobaciones fallidas, las builds sin cambios y los downgrades no disparan ninguna instalación. Las instalaciones desde PyPI/release mantienen las comprobaciones basadas en versión.

Si una actualización automática antigua deja el arranque fallando con `Cannot read properties of undefined (reading 'rawCall')`, reinstala el `main` actual:

```sh
uv tool install --force --reinstall 'snowpea-agent[images] @ git+https://github.com/Wafour-Developer/snowpea-agent@main'
```

Después de reinstalar, y una vez que no haya trabajo en marcha, usa `snowpea daemon stop` y luego lanza `snowpea` para cargar el código nuevo del daemon.

La ayuda se mantiene dentro de la altura de la terminal. Desplázate con `↑`/`↓` o `PgUp`/`PgDn`; ciérrala con **Esc, F1, q o Enter**. Esc dentro de la ayuda no interrumpe el turno en marcha.

La entrada, el estado de conexión/modelo, el resumen de modo y la lista de agentes de la parte inferior están separados por reglas que ocupan todo el ancho de contenido de la terminal, lo que hace fácil distinguir la región activa.

## Teams y delegación corta

El primer `snowpea setup` crea un team `default` a partir de los roles incorporados. `/team create delivery architect executor verifier` crea un team de proyecto a partir de agentes existentes, lo activa de inmediato, y rechaza nombres desconocidos. Gestiónalo con `/team list`, `/team use <name>` y `/team delete <name>`. Con un team activo, el pie muestra solo su nombre y sus miembros, y la delegación automática queda confinada a esa plantilla.

Escribe `$executor fix the tests` para delegar directamente sin un comando largo. Los nombres desconocidos o fuera del team fallan en lugar de convertirse silenciosamente en un agente genérico. La delegación interna que omite un nombre usa de forma determinista `executor` si existe, y si no el primer miembro del team.

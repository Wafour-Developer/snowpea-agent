# The terminal UI

[English](../en/tui.md) · [한국어](../ko/tui.md) · [Todas las páginas](../README.md)

`snowpea` sin argumentos abre la interfaz de terminal. Es un cliente delgado: cada decisión — qué herramienta puede ejecutarse, qué significa un comando, cuándo compactar — pertenece al daemon, y esta página trata de la superficie por la que salen esas decisiones.

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

El bloque de la última sesión aparece cuando el daemon todavía tiene una sesión abierta para este directorio — otra terminal, una ejecución headless, una sesión que dejó un cierre inesperado. `R` con la entrada vacía, o `/resume`, la reproduce en esta ventana. Si el daemon no tiene ninguna, el bloque no se muestra: una oferta que no se puede aceptar es peor que ninguna oferta.

El banner se imprime una sola vez. Sube con el resto de la salida y no vuelve a dibujarse.

## La disposición

La interfaz dibuja en línea, como `git log`, no como una aplicación de pantalla completa. La salida terminada se entrega a la terminal, así que el scrollback, el ratón y tu `Ctrl+Shift+F` siguen siendo los de siempre. Solo la parte de abajo está viva.

```text
 › explica la lógica de reintento           ← scrollback: tuyo, nunca se redibuja
 ⏺ Read 3 files (128 lines)
 ◆ El reintento vive en `client.ts`…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← aquí empieza la región viva
 > lo próximo que escribas
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

De arriba abajo, el panel inferior es: la línea de estado, el aviso de contexto cuando lo hay, la línea de resumen y las filas de agentes. La entrada está encima de ellas, y el indicador de trabajo encima de la entrada. Nada por debajo de la entrada se redibuja si no ha cambiado.

## Mientras trabaja

Un turno muestra una línea sobre la entrada, y esa línea dice lo que está pasando de verdad:

| Línea | Significa |
|---|---|
| `✢ Pondering… (12s · ↓ 3.7k tokens)` | el modelo piensa; el verbo cambia cada pocos segundos |
| `✳ Running shell: npm test… (4s · …)` | una herramienta está en vuelo, con su nombre y su argumento |
| `✶ 3 agents working… (1m 2s · …)` | el turno ha delegado |
| `✳ /ralph… (3m 10s · …)` | un flujo de comando se ha quedado con el turno |
| `⏸ Waiting for approval` | está bloqueado esperándote |

El reloj y los tokens son de este turno, no de la sesión; los totales de la sesión están en la línea de estado. `Esc` interrumpe.

Cuando termina una tanda de llamadas a herramientas, se pliega en una sola línea del scrollback en lugar de una tarjeta por llamada:

```text
⏺ Ran 2 shell commands (34 lines)
⏺ Read 3 files (128 lines)
```

Una llamada que falló conserva su tarjeta con su salida, porque esa es la que hay que leer. `Ctrl+O` despliega la llamada o el diff más reciente mientras siguen en la región viva.

## Modos

`Shift+Tab` rota accept → auto → plan → accept. El modo está en la línea de estado y en la de resumen, y [Modes](../en/modes.md) explica qué permite cada uno. `Ctrl+P` activa y desactiva el modo plan sin rotar.

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

`ctx 12.3k used` sin porcentaje significa que el daemon no conoce la ventana de ese modelo. `snowpea session context` y el ajuste `providers.<vendor>.context_window` están en [Commands](commands.md).

## Escribir

`↑` recorre hacia atrás tus prompts anteriores y `↓` vuelve hacia lo que estabas escribiendo. El historial es por máquina, no por sesión: vive en `$SNOWPEA_HOME/tui-history.jsonl`, guarda las últimas 500 entradas y no registra dos veces seguidas el mismo prompt.

`↓` más allá de la entrada más reciente hace otra cosa: saca el cursor de la entrada y lo lleva a las filas de abajo. Primero la línea de resumen, donde `Enter` lista lo que está corriendo:

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to list them
    ◦ Ran shell: npm -w tui test · 12s
```

Después las filas de agentes, una a una. `Esc` o `↑` vuelven arriba, a la entrada.

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
> ¿qué está mal en este layout?
```

Entiende lo que una terminal entrega de verdad: una ruta, varias a la vez, una ruta con los espacios escapados, una ruta con los espacios sin escapar, una URL `file://`, las comillas que añadió un gestor de ficheros. `Ctrl+V` toma una imagen directamente del portapapeles del sistema — `wl-paste`, `xclip`, AppleScript o PowerShell, lo que haya en esta máquina — y la guarda bajo `$SNOWPEA_HOME/tmp/`. `/attach <ruta>` lo hace a mano.

`Backspace` con la entrada vacía quita el chip más reciente y `Ctrl+X` los quita todos. Al enviar el prompt los ficheros van con él, y la transcripción dice cuáles:

```text
› ¿qué está mal en este layout?
  📎 screenshot.png
```

Los ficheros se envían como rutas, así que no se copia nada. Qué hace el modelo después con ellos — y el límite de 20MB, el reescalado, qué pasa con un modelo que no ve — está en [Attachments and voice](../en/voice.md).

## Voz

La voz necesita un backend, y quien los tiene es el daemon. `snowpea setup audio` los configura; [Attachments and voice](../en/voice.md) lista lo que necesita cada uno.

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
| `Esc` | interrumpir el turno, parar la lectura, salir de la vista de un agente |
| `Ctrl+O` | desplegar la llamada o el diff más reciente |
| `Ctrl+A` | abrir del todo el panel de agentes |
| `Ctrl+R` | ir a la cola de aprobaciones pendientes |
| `Ctrl+V` | adjuntar una imagen del portapapeles |
| `Ctrl+X` | vaciar los adjuntos |
| `Ctrl+Space` | empezar o parar la grabación |
| `U` | aceptar la actualización ofrecida |
| `R` | continuar la sesión que ofreció la pantalla de inicio |
| `F1` | ayuda |
| `Ctrl+C` | salir |

`/help` lista todos los comandos que tiene el daemon, incluidos los que añadieron tus plugins, y repite esta tabla.

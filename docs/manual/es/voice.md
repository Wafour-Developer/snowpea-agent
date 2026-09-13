# Adjuntos y voz

Dos cosas que impiden que una terminal sea solo texto: soltar una imagen en un prompt, y hablarle al agente en lugar de escribirle.

Otros idiomas: [English](../en/voice.md) · [한국어](../ko/voice.md) · [Todas las páginas](../README.md)

Esta página es la mitad del daemon: qué se puede adjuntar, qué backends de voz existen, y qué necesita cada uno. Las teclas que lo hacen —los chips, `Ctrl+V`, `/voice`, `Ctrl+Space`— están en [Terminal UI](tui.md).

## Adjuntos

Pega o arrastra un fichero a la entrada y se convierte en un chip de adjunto. Envía el prompt y el fichero va con él:

```text
[📎 screenshot.png 1.2MB]
> what is wrong with this layout?
```

Qué se puede adjuntar:

| Tipo | Formatos | Qué recibe el modelo |
|---|---|---|
| Imágenes | PNG, JPEG, GIF, WebP | la imagen en sí, si el modelo ve |
| Texto | `.txt`, `.md`, ficheros de código, JSON | el texto del fichero, incrustado en el turno |
| PDF | `application/pdf` | el documento, en los proveedores que aceptan uno |
| Cualquier otra cosa | — | el nombre y el tipo, para que el agente sepa que debe leerlo con una herramienta |

Las reglas, todas aplicadas por el daemon:

- **El tipo viene de los bytes, no del nombre.** Un binario llamado `notes.txt` se trata como un binario.
- **20MB por adjunto.** Más grande se rechaza con un mensaje que nombra el límite. Un adjunto pegado (inline) está además limitado por el tamaño del frame del websocket, alrededor de 3MB: cualquier cosa mayor tiene que enviarse como ruta, que es lo que la interfaz de terminal hace de todos modos.
- **Las imágenes se reescalan a 1568px en el lado largo** cuando Pillow está instalado. Los instaladores lo añaden; mira [Instalar](install.md). Sin él, las imágenes se envían a tamaño completo, lo que sigue funcionando y solo cuesta más tokens.
- **Un fichero al que apuntas nunca se copia.** Solo se almacenan los bytes pegados, bajo `$SNOWPEA_HOME/attachments/<session>/<sha256>.<ext>`, deduplicados por contenido: la misma captura pegada diez veces es un solo fichero.

### Modelos que no ven

No todos los modelos pueden ver. Cuando el activo no puede, una imagen no hace fallar el turno: llega como un marcador sobre el que el modelo puede actuar.

```text
[image attached: screenshot.png] (this model cannot see images; ask the user to
describe it, or read the file from disk with a tool)
```

Los modelos de Anthropic y de Gemini se tratan todos como capaces de ver. Para los proveedores compatibles con OpenAI decide el nombre del modelo, y un nombre no reconocido —un GGUF local, un modelo publicado la semana pasada— se asume que es solo de texto, porque degradar es mejor que una petición fallida. Si tu modelo local sí ve imágenes y snowpea no está de acuerdo, dilo en un issue con el id del modelo.

## Voz

La voz funciona cuando la máquina tiene algo con lo que hacer el trabajo. No se requiere nada, y lo que falte se reporta en lugar de saltárselo en silencio.

```bash
snowpea setup audio
```

Esa sección hace dos preguntas —cómo escuchar y cómo hablar— y muestra qué backends están realmente instalados aquí.

### Voz a texto

| Proveedor | Necesita | Notas |
|---|---|---|
| `local-whisper` | `whisper` o `faster-whisper` en el `PATH` | nada sale de la máquina |
| `openai` | la clave de API de OpenAI que ya configuraste | `whisper-1` o `gpt-4o-transcribe` |
| `command` | una plantilla de comando que contenga `{path}` | stdout es la transcripción |
| `auto` (por defecto) | — | whisper local, luego OpenAI, luego el comando |
| `off` | — | no escuchar nunca |

`auto` prefiere la CLI local deliberadamente: la transcripción es la única vía por la que, si no, el audio de tu habitación saldría de la máquina.

### Texto a voz

| Proveedor | Necesita | Notas |
|---|---|---|
| `studio` | el servidor MCP snowpea-studio | las mejores voces, si tienes uno en marcha |
| `openai` | la clave de API de OpenAI | `tts-1` o `gpt-4o-mini-tts` |
| `edge-tts` | `edge-tts` en el `PATH` | voces neuronales, usa la red |
| `piper` | `piper` en el `PATH` | voces neuronales locales |
| `say` | macOS | incorporado |
| `espeak-ng` | `espeak-ng` en el `PATH` | pequeño, robótico, en todas partes |
| `powershell` | Windows | SAPI, incorporado |
| `command` | una plantilla con `{text}` y `{out}` | escribe un fichero de audio |
| `auto` (por defecto) | — | studio, luego OpenAI, luego el primero local |
| `off` | — | no hablar nunca |

### Grabación

Grabar el micrófono necesita uno de `rec` (sox), `arecord` (alsa-utils) o `ffmpeg`. La reproducción necesita uno de `afplay`, `paplay`, `aplay`, `ffplay` o `mpv`. En una máquina sin ninguno de los dos, la interfaz de terminal todavía puede enviarte un fichero de audio para que lo reproduzcas tú.

### El agente también puede usar la voz

Dos herramientas, para que el agente pueda hacer este trabajo por su cuenta:

| Herramienta | Qué hace |
|---|---|
| `transcribe_audio` | lee un fichero de audio y devuelve lo que se dijo |
| `text_to_speech` | dice algo y devuelve el fichero de audio; `play: true` lo dice en voz alta aquí |

Ambas están inactivas hasta que existe un backend, y `snowpea tools list` lo dice:

```bash
snowpea tools list
```

## Ajustes

Todo lo anterior vive bajo `audio` en `$SNOWPEA_HOME/settings.json`, y el daemon recoge una edición sin reiniciarse:

```json
{
  "audio": {
    "stt": { "provider": "auto", "command": null, "model": null },
    "tts": {
      "enabled": true,
      "provider": "auto",
      "voice": null,
      "autoSpeak": false
    },
    "player": null,
    "recorder": null
  }
}
```

`autoSpeak` lee en voz alta todas las respuestas: el daemon sintetiza la respuesta según termina, la reproduce si esta máquina tiene un reproductor, y emite un evento de sesión `audio.spoken` que lleva el fichero en cualquier caso, para que un cliente en otra máquina pueda reproducirlo él mismo. Hablar nunca afecta al turno: un backend ausente o un reproductor roto son una línea de log y una respuesta silenciosa, no una respuesta fallida. `player` y `recorder` fuerzan una herramienta concreta en lugar de la primera que se encuentre.

## Cuando no pasa nada

Pregúntale al daemon qué puede hacer. Toda capacidad que esté apagada viene con el motivo por el que lo está: qué paquete instalar, qué clave configurar:

```bash
snowpea tools list --json
```

El mismo informe es lo que lee la interfaz de terminal al arrancar, y otra vez cada vez que cambia un ajuste. Cuando `/voice` o `/tts` dicen que falta un backend, esa frase es de este informe, no una conjetura, y basta con instalar el backend, sin reiniciar nada.

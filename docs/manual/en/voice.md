# Attachments and voice

Two things that stop a terminal from being text-only: dropping an image into a prompt, and talking to the agent instead of typing at it.

Other languages: [한국어](../ko/voice.md) · [all pages](../README.md)

This page is the daemon's half: what can be attached, which speech backends exist, and what each one needs. The keys that do it — the chips, `Ctrl+V`, `/voice`, `Ctrl+Space` — are in [Terminal UI](tui.md).

## Attachments

Paste or drag a file into the prompt and it becomes an attachment chip. Send the prompt and the file goes with it:

```text
[📎 screenshot.png 1.2MB]
> what is wrong with this layout?
```

What can be attached:

| Kind | Types | What the model gets |
|---|---|---|
| Images | PNG, JPEG, GIF, WebP | the picture itself, if the model has vision |
| Text | `.txt`, `.md`, source files, JSON | the file's text, inlined in the turn |
| PDF | `application/pdf` | the document, on vendors that accept one |
| Anything else | — | the name and type, so the agent knows to read it with a tool |

The rules, all enforced by the daemon:

- **The type comes from the bytes, not the name.** A binary called `notes.txt` is treated as a binary.
- **20MB per attachment.** Bigger is refused with a message naming the limit. A pasted (inline) attachment is further limited by the websocket frame size, around 3MB — anything larger has to be sent as a path, which is what the terminal UI does anyway.
- **Images are downscaled to 1568px on the long edge** when Pillow is installed. The installers add it; see [Install](install.md). Without it, images are sent at full size, which still works and just costs more tokens.
- **A file you point at is never copied.** Only pasted bytes are stored, under `$SNOWPEA_HOME/attachments/<session>/<sha256>.<ext>`, deduplicated by content — the same screenshot pasted ten times is one file.

### Models without vision

Not every model can see. When the active one cannot, an image does not fail the turn: it arrives as a marker the model can act on.

```text
[image attached: screenshot.png] (this model cannot see images; ask the user to
describe it, or read the file from disk with a tool)
```

Anthropic and Gemini models are all treated as vision-capable. For OpenAI-compatible vendors the model name decides, and an unrecognised name — a local GGUF, a model released last week — is assumed text-only, because degrading is better than a failed request. If your local model does see images and snowpea disagrees, say so in an issue with the model id.

## Voice

Voice works when the machine has something to do the work with. Nothing is required, and anything missing is reported rather than silently skipped.

```bash
snowpea setup audio
```

That section asks two questions — how to listen, and how to speak — and shows which backends are actually installed here.

### Installing from multiple surfaces

`audio.install` is single-flight per target key (`engine`, or `engine+voice` for a voice download).
If another window or CLI asks for the same target while one run is already active, the daemon refuses
the second request with RPC code `install_running` and includes the running install metadata (engine,
optional voice, latest progress snapshot). Clients should attach to that running job instead of retrying.

### Speech to text

| Provider | Needs | Notes |
|---|---|---|
| `local-whisper` | `whisper` or `faster-whisper` on `PATH` | nothing leaves the machine |
| `openai` | the OpenAI API key you already configured | `whisper-1` or `gpt-4o-transcribe` |
| `command` | a command template containing `{path}` | stdout is the transcript |
| `auto` (default) | — | local whisper, then OpenAI, then the command |
| `off` | — | never listen |

`auto` prefers the local CLI deliberately: transcription is the one path where audio of your room would otherwise leave the machine.

### Text to speech

| Provider | Needs | Notes |
|---|---|---|
| `studio` | the snowpea-studio MCP server | the best voices, if you run one |
| `openai` | the OpenAI API key | `tts-1` or `gpt-4o-mini-tts` |
| `edge-tts` | `edge-tts` on `PATH` | neural voices, uses the network |
| `piper` | `piper` on `PATH` | local neural voices |
| `say` | macOS | built in |
| `espeak-ng` | `espeak-ng` on `PATH` | small, robotic, everywhere |
| `powershell` | Windows | SAPI, built in |
| `command` | a template with `{text}` and `{out}` | writes an audio file |
| `auto` (default) | — | studio, then OpenAI, then the first local one |
| `off` | — | never speak |

### Recording

Recording the microphone needs one of `rec` (sox), `arecord` (alsa-utils) or `ffmpeg`. Playback needs one of `afplay`, `paplay`, `aplay`, `ffplay` or `mpv`. On a machine with neither, the terminal UI can still send you an audio file to play yourself.

### The agent can use voice too

Two tools, so the agent can do this work on its own:

| Tool | What it does |
|---|---|
| `transcribe_audio` | reads an audio file and returns what was said |
| `text_to_speech` | says something and returns the audio file; `play: true` speaks it here |

Both are inactive until a backend exists, and `snowpea tools list` says so:

```bash
snowpea tools list
```

## Settings

Everything above lives under `audio` in `$SNOWPEA_HOME/settings.json`, and the daemon picks up an edit without a restart:

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

`autoSpeak` reads every reply aloud: the daemon synthesises the reply as it finishes, plays it if this machine has a player, and emits an `audio.spoken` session event carrying the file either way — so a client on another machine can play it itself. Speaking never affects the turn: a missing backend or a broken player is a log line and a silent reply, not a failed answer. `player` and `recorder` force one tool instead of the first one found.

## When nothing happens

Ask the daemon what it can do. Every capability that is off comes with the reason it is off — which package to install, which key to set:

```bash
snowpea tools list --json
```

The same report is what the terminal UI reads on startup, and again whenever a setting changes. When `/voice` or `/tts` says a backend is missing, that sentence is this report's, not a guess — and installing the backend is enough, with no restart.

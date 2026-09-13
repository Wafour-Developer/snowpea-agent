# M10 Contract — Attachments, Vision Content Parts and Voice I/O (binding; ships CORE-multimodal)

Retrofit contract for [`deviations/CORE-multimodal.md`](deviations/CORE-multimodal.md). Builds on
[`m1-core-contract.md`](m1-core-contract.md) §5 (providers) and §6 (tools),
[`m2-tools-contract.md`](m2-tools-contract.md) §2 (media tools) and
[`m3-providers-setup-contract.md`](m3-providers-setup-contract.md) §5 (setup wizard).
Source of record: `core/snowpea_core/{attachments,audio}/`, `providers/content.py`,
`server/audio_handlers.py`.

Acceptance criteria: **AC-27 … AC-31**.

## 1. Attachments (`attachments/`)

`session.prompt(sessionId, text, attachments?: list[Attachment])`. The wire model
`protocol.Attachment` is `{kind: "file"|"image"|"text" = "file", name?, path?, data?, mimeType?,
size?, text?}`. "Exactly one of `path`, `data` and `text` carries the content" is documented on the
model but enforced downstream, in `attachments.model.Attachment.from_payload`, which tries `data`,
then `path`, and otherwise raises `attachment: needs either data or path`. The **core** dataclass
`attachments.model.Attachment` is a different, narrower shape (`name, mime, size, sha256, data,
path`) and MUST NOT be confused with the wire model.

- **The sniffed MIME type beats the client's, except when the client claims an image.**
  `attachments/model.py::_resolve_mime` returns the declared type only when the bytes sniff to
  `application/octet-stream` **and** the declared type is not an image. So a binary named
  `notes.txt` is `application/octet-stream`, and a declared `image/png` whose bytes have no PNG
  header travels as a named file rather than as a broken image block.
- **A file the user points at is referenced, never copied.** `AttachmentStore.save` returns the
  attachment untouched when `attachment.path is not None`; only inline (base64) attachments are
  written, to `<SNOWPEA_HOME>/attachments/<session>/<sha256>.<ext>` (via a `.tmp` + `replace`).
  Content addressing means pasting the same screenshot ten times costs one file.
- **Cap: `attachments.model.MAX_BYTES = 20 * 1024 * 1024`.** The websocket server is constructed
  with `max_msg_size=32 * 1024 * 1024` (an inline argument in `server/transport_ws.py`, not a named
  constant), so a 20MB file still fits once base64 has added a third. The thing that refuses an
  oversized file MUST be the check that can name the limit, not a dropped connection.
- **`session.prompt` stashes rather than widening `start_turn`.** `attachments/pending.py` is a
  per-session take-once queue: `server/session_handlers.py::_accept_attachments` validates, stores
  and calls `pending.stash`. `agent/loop.py::start_turn` calls `pending.take(session.id)` at
  **enqueue** time and pins the result onto the `QueuedTurn` (M9 §5), precisely so a queued
  follow-up cannot steal an earlier prompt's images; `_drive` falls back to `pending.take` only for
  direct `run_turn` callers that passed no attachments. A slash command clears the stash (nothing
  would consume the bytes), and `pending.stash` **replaces** rather than appends, so a second prompt
  before the turn starts cannot accumulate.
- **`kind: "text"` attachments are inlined into the prompt text**, each rendered as `[name]` +
  newline + body, joined by blank lines and appended to the prompt after a blank line. They are not
  stored.
- **The prompt text is never rewritten for file or image attachments.** The text the user typed
  reaches the model as typed; the marker lives on the content block, produced by
  `Attachment.describe()` as `[image: shot.png]` / `[file: notes.pdf]`.
- Image downscaling above `MAX_IMAGE_EDGE = 1568` on the long edge is **best-effort**:
  `attachments.model.downscale_image` returns the input unchanged on a Pillow `ImportError`, when
  the image is already small enough, when `is_animated`, or on any other exception. Pillow ships as
  the `[images]` extra (`pyproject.toml`), `update.with_images` (`IMAGES_EXTRA = "images"`) carries
  it through every upgrade path, and `SNOWPEA_SKIP_IMAGES=1` opts out **in the installers**
  (`installer/install.sh`, `installer/install.ps1`) — it is not read by any Python module.

**AC-27.** History MUST store a block list with paths, never base64
(`providers/content.py::history_blocks`, `BLOCK_TYPES = {"text", "image", "file"}`):

```jsonc
ChatMessage.content = [
  {"type": "text",  "text": "what is in this?"},
  {"type": "image", "text": "[image: shot.png]", "name": "shot.png",
   "mime": "image/png", "size": 48213, "sha256": "…",
   "path": "/…/<sha256>.png"}          // present only when the attachment has a path
]
```

Three properties fall out and all three are required: the session store stays small; a resumed
session can still send the picture because the bytes are re-read when the request is built; and the
`text` marker on every block means the token estimate, the history renderer and the fake provider's
matcher all see something sensible without decoding anything. **A turn with no attachments MUST keep
a plain string** (`agent/loop.py` uses `history_blocks(...) if attachments else text`), so every
pre-existing request is byte-identical. An attachment whose file has gone degrades to
`[image: shot.png] (no longer on disk)` rather than raising — a deleted screenshot must not make a
resumed session unanswerable.

**Session deletion purges attachments.** `session.deleteSaved` and `session.delete` both run
`server/session_handlers.py::_purge_session_files`, which calls
`AttachmentStore(core.paths.attachments_dir).purge(session_id)` and removes
`core.paths.audio_dir / session_id`. (The orphaned-files gap recorded in M1 §17-2 is closed.)

## 2. Vision content parts (`providers/content.py`)

**AC-28.** `supports_vision(vendor, model)` is an **allowlist**, and an unknown model is assumed
text-only: `True` for `vendor in VISION_VENDORS = {"anthropic", "gemini"}`, and for any other vendor
whose model name contains one of the 38 `VISION_HINTS` substrings (`gpt-4o`, `gpt-5`, `o3`, `claude`,
`gemini`, `grok-*-vision`, `pixtral`, `llava`, `qwen*-vl`, `glm-4v`, `internvl`, `-vl`, …). Anything
else gets the text fallback: `[image attached: shot.png]` followed by `NO_VISION_NOTE` —
`(this model cannot see images; ask the user to describe it, or read the file from disk with a
tool)`. The alternative default (assume vision, let the vendor 400) turns an unknown model into a
failed turn rather than a degraded one.

- `to_openai(parts, *, vision: bool = True)` MUST return a plain string whenever `not vision` or the
  parts carry no media, so a text-only turn produces exactly the request shape the pre-attachment
  code produced.
- The decision is taken in `providers/normalize.py::build_openai_request`, the one place that knows
  both the preset and the resolved model; it calls
  `messages_to_openai(messages, vision=content.supports_vision(preset.id, model))`.
  `messages_to_openai(..., *, vision: bool = False)` defaults to **off**, so a caller that forgets
  the flag degrades rather than sending blocks a text-only endpoint would reject.
- Anthropic needs no flag: `to_anthropic(parts)` has no `vision` parameter (every model sees).
  Gemini takes `inlineData` unconditionally in `to_gemini`. The Codex/Responses path has its own
  `messages_to_responses(..., vision: bool = True)` fed by the same `supports_vision`.

## 3. Voice (`audio/`, `server/audio_handlers.py`)

Five RPCs, listed in `audio_handlers.HANDLED_METHODS` and registered in `protocol.IMPLEMENTED_METHODS`:

| method | params | result |
|---|---|---|
| `audio.capabilities` | – | `stt`, `tts`, `ttsProvider`, `voice`, `record`, `play`, `autoSpeak`, `sttProviders`, `ttsProviders`, `players`, `recorders`, **`reasons`** |
| `audio.transcribe` | `path?`, `data?`, `mime?`, `language?`, `sessionId?` | `text`, `provider` |
| `audio.speak` | `text`, `sessionId?`, `voice?`, `play: bool = false` | `path`, `mime`, `provider`, `voice?`, `played: bool = false` |
| `audio.record.start` | `sessionId?` | `path`, `recording`, `mime`, `text?`, `provider?` |
| `audio.record.stop` | `sessionId?`, `transcribe: bool = false` | same `AudioRecordResult` |

**AC-29.** Both directions are **provider chains, not single integrations, and the local backend
wins.** STT (`audio/stt.py`) is `AUTO_ORDER = ("local-whisper", "openai", "command")`: transcription
is the one path where audio of the user's room would otherwise leave the machine, so an installed
whisper CLI outranks the hosted API. TTS (`audio/tts.py`) is
`AUTO_ORDER = ("studio", "openai", "edge-tts", "piper", "say", "espeak-ng", "powershell",
"command")`, where `studio` is the snowpea-studio MCP forward. The practical requirement: **speech
MUST work on a bare Linux box with `espeak-ng` and nothing configured.**

**AC-30.** Every audio capability reports *why* it is off. `audio.capabilities` returns
`reasons: dict[str, str]`, keyed `stt` / `tts` / `record` / `play` and populated only when that
capability is unavailable — e.g. `"no recorder found; install sox (rec), alsa-utils (arecord) or
ffmpeg"` and `"no speech backend: configure the snowpea-studio MCP server, set an OpenAI API key, or
install edge-tts, piper, say or espeak-ng"`. A voice feature that silently does nothing is
indistinguishable from a bug, and the client cannot guess what the daemon's machine is missing.

Further rules:

- `audio.speak` defaults to synthesise-and-return (`play: false`), because the client is usually
  where the speakers are. `play: true` plays on the daemon's machine; a playback failure is logged
  at `info` and reported as `played: false`, never a failed call — the audio exists either way.
- **Speaking can never fail a turn.** `agent/loop.py::speak_reply` wraps its whole body in
  `except Exception` and logs at `info`: no backend, a synthesiser that times out, a player that
  exits non-zero.
- `audio.spoken` is a **session event kind** (`protocol.AudioSpoken`, registered in
  `SESSION_EVENT_MODELS`), not a top-level notification. `speak_reply` emits it after
  `message.done` and before `finish_turn` sends `turn.done`, so it is ordered with the reply it
  speaks and replayed on resume. Payload: `path`, `mime`, `provider`, `played`, `voice`. A failed
  playback still emits it with `played: false` so a client on another machine can play the file
  itself.
- Recorders are **per session** (`audio_handlers._RECORDERS`, keyed by session id), and
  `Recorder.stop()` / `Recorder.cancel()` MUST drain via `communicate()`, not a bare
  `process.wait()` — awaiting `wait()` hangs forever when the capture tool is chatty, because
  asyncio resolves the exit future only after every pipe closes. `_drain` falls back to `wait()`
  only after `BrokenPipeError` / `ConnectionResetError` / `ValueError`.
  `tests/test_audio.py::test_stop_drains_a_chatty_recorder` is the regression.
- The `audio/` package MUST NOT import `Settings`. `audio.AudioConfig` is a plain frozen dataclass
  mirroring `settings.audio`, built by the server layer, so the whole package unit-tests without a
  settings tree and the dependency points one way. `settings.audio` itself is typed
  (`config/settings.py`: `SttSettings`, `TtsSettings`, `AudioSettings`, and
  `Settings.audio: AudioSettings`), while `audio_handlers.audio_config` reads through the tolerant
  `_block` / `_get` accessors that take either a model or a dict, so a hand-written block with
  missing keys still works.
- OpenAI credentials are **borrowed, not asked for twice**: both OpenAI backends read the key and
  base URL the provider registry already resolved
  (`ProviderRegistry.api_key_for("openai")` / `base_url_for("openai")`, env fallback included).
- `"off"` is a real answer, distinct from `"auto"`, in both directions (`audio.OFF`,
  `AudioConfig.stt_off` / `tts_off`), and the two produce different `reasons` text: "switched off"
  versus "no backend".

## 4. Tools

`tools/audio_tools.py` registers exactly two tools; `tools/media.py` registers none of them
(`REGISTERED = ("image_generate", "video_generate", "music_generate")`) but its `FORWARDS` table
keeps `"text_to_speech": "generate_speech"`, because that is how the audio code reaches studio.

| tool | declared tag | resolved per call (`permission_for`) |
|---|---|---|
| `transcribe_audio` | `read` | `network` when the resolved backend is in `HOSTED = {"openai", "studio"}`, else `read` |
| `text_to_speech` | `network` | the same rule |

`transcribe_audio` is *declared* `read` because the tag describes the tool's guaranteed effect — it
reads a local file — and making every transcription `network` would have made the common
local-whisper case ask for a permission it does not need. `read` rather than `exec` for a local
backend is deliberate: the mode matrix (M1 §7) gates egress and changes to your project, and a local
backend does neither; tagging it `exec` would deny "read this out to me" in plan mode. Both tools are
registered `state="inactive"` with a `tool_inactive: … is unavailable — <reason>` message until a
backend exists, mirroring the media tools; `audio_tools.refresh_state(core)` flips them, and
`config/hot_reload.py::rebind` calls it inside a `try/except` so a reload can never fail on it.

## 5. Setup wizard

See M3 §5. The Audio section sits between Browser and Tools in `setup/wizard.py::FULL_ORDER`,
carries **no circled numeral** (the other screens print ①–⑥ and renumbering three of them for
cosmetics was rejected), and is two screens plus conditional prompts: the STT list is the screen
proper; the TTS list, the voice name, the "read replies aloud" toggle and the "test the voice now"
offer follow it and are asked **only when the user actually picked a TTS backend** (`_ask_for_audio`
returns early when the choice is Off). A non-interactive run asks nothing and
`WizardState.audio_block()` writes
`{"stt": {"provider": "auto"}, "tts": {"enabled": true, "provider": "auto", "autoSpeak": false}}`.
Picking Off for speech also clears auto-speak. Rows for backends that are not installed are shown
**inactive, not hidden** (`setup/catalog.py` sets `active = cid in {"auto", "off"} or cid in
usable`) — the answer to "why can't I use piper" should be on the screen rather than missing from it.
The section is also reachable directly as `snowpea setup audio` / `snowpea setup voice`.

## 6. Tests and docs

**AC-31.** `tests/test_multimodal_rpc.py` (validation, storage, sniffing, the text fallback; its
`test_an_oversized_attachment_is_refused` monkeypatches `attachments.model.MAX_BYTES` down to 1024
rather than pushing 20MB through the socket) and `tests/test_audio.py` (chain resolution, `reasons`,
the recorder drain regression) MUST pass with no network and no audio hardware. Adjacent suites:
`tests/test_attachments.py`, `tests/test_multimodal_flow.py`, `tests/test_audio_tools.py`,
`tests/test_setup_audio.py`, `tests/test_provider_content.py`.

TUI coverage: `tui/test/{attachments,attach-ui,voice,audio-runtime,audio-tools,clipboard}` —
`attachments.test.ts`, `attach-ui.test.tsx`, `voice.test.ts`, `audio-runtime.test.ts`,
`audio-tools.test.ts`, `clipboard.test.ts`.

One manual page covers both halves — `docs/manual/en/voice.md` and `docs/manual/ko/voice.md` —
because attachments and voice are one story from the user's side; the tool tables in
`docs/manual/{en,ko}/modes.md` carry `transcribe_audio` under `read` and `text_to_speech` under
`network`.

# Deviations — CORE-multimodal (attachments, vision content parts, voice I/O)

Prompt attachments (paste, drag, clipboard image), provider-neutral content parts with a
text-only fallback, and voice in both directions: speech-to-text, text-to-speech, recording and
playback — as RPCs, as built-in agent tools, and as a section of the first-run setup wizard.
Recorded per `docs/design/deviations/README.md`.

This file covers phase A (the `attachments/`, `providers/content.py` and `audio/` packages) and
the first half of phase B (protocol, handlers, wizard, tools). The agent-loop wiring — turning a
turn's attachments into provider content parts, the typed `settings.audio` model and registering
the two audio tools — is owned by another story and is not here yet; §9 says exactly where the
seam is.

## Attachments

1. **The sniffed MIME type beats the client's, except when the client claims an image.**
   `attachments/model.py` decides the media type from magic bytes, so a binary named `notes.txt`
   is `application/octet-stream`. The one case where the client is believed is bytes that sniff to
   octet-stream *and* a declared type that is not an image: a declared `image/png` whose bytes have
   no PNG header would be rejected by the vendor anyway, so it travels as a named file instead of
   as a broken image block.

2. **A file the user points at is referenced, never copied.** `AttachmentStore.save` only writes
   inline (base64) attachments, under `<SNOWPEA_HOME>/attachments/<session>/<sha256>.<ext>`.
   Content addressing means pasting the same screenshot ten times costs one file. A path-backed
   attachment is returned untouched — copying a 15MB video the user merely pointed at would be a
   surprise, and the path is still valid on resume because the daemon and the file share a machine.

3. **Image downscaling is best-effort and Pillow is not a dependency.** Images over 1568px on the
   long edge are downscaled when Pillow is importable and passed through unchanged when it is not
   (and when the image is animated, where resizing would drop every frame but the first). A
   slightly large image costs tokens; a dropped one costs the answer. **Open question for the
   lead:** whether to add Pillow as an optional extra, since in the current environment the
   downscale path never runs.

4. **The 20MB cap is not the effective limit for inline attachments.** The websocket transport
   refuses any frame over 4MB (`transport_ws.py`), and base64 inflates by a third, so inline
   `data` realistically tops out around 3MB. Bigger files must be sent as `path`. The cap in
   `model.py` still exists because `path` attachments and future transports are not frame-limited.
   Raising `max_msg_size` was out of scope for this story; `tests/test_multimodal_rpc.py` lowers
   `MAX_BYTES` rather than trying to push 20MB through the socket, and says why.

## Content parts

5. **Vision support is an allowlist, and an unknown model is assumed text-only.**
   `providers/content.supports_vision` answers True for every Anthropic and Gemini model, and for
   OpenAI-compatible models whose name matches `VISION_HINTS`. Anything else — a local GGUF, a new
   model id — gets the text fallback: `[image attached: shot.png]` plus a note telling the model it
   cannot see it. The alternative default (assume vision, let the vendor 400) turns an unknown
   model into a failed turn rather than a degraded one.

6. **`to_openai` returns a plain string whenever it can.** A turn with no media, or any turn to a
   text-only model, produces exactly the string the pre-attachment code produced, so no existing
   request shape changes and no vendor sees a content array it did not before.

## Audio

7. **Both voice directions are provider chains, not single integrations, and the local backend
   wins.** STT tries `local-whisper`, then `openai`, then a user `command`: transcription is the
   one path where audio of the user's room would otherwise leave the machine, so an installed
   whisper CLI outranks the hosted API. TTS tries `studio` (the existing snowpea-studio MCP
   forward), then `openai`, then `edge-tts` / `piper` / `say` / `espeak-ng` / `powershell`, then a
   user `command`. The practical effect is that speech works on a bare Linux box with `espeak-ng`
   and nothing configured at all — the previous `text_to_speech` tool was dead without studio.

8. **Every audio capability reports *why* it is off.** `audio.capabilities` returns `reasons`, a
   per-capability sentence ("install sox (rec), alsa-utils (arecord) or ffmpeg"). A voice feature
   that silently does nothing is indistinguishable from a bug, and the client cannot guess what the
   daemon's machine is missing.

9. **`session.prompt` stashes attachments instead of passing them to `start_turn`.**
   `attachments/pending.py` is a per-session, take-once queue: the handler validates, stores and
   stashes; the agent loop will `pending.take(session.id)` at the top of the turn. The alternative
   — widening `agent.loop.start_turn`'s signature — was not available to this story, and the queue
   turns out to be the better seam anyway: a slash command clears it (nothing would consume the
   bytes), and a second prompt before the turn starts replaces rather than appends, so an
   interrupted turn cannot leak its images into the next one. **The loop wiring is the one piece
   still missing**; until it lands, attachments are stored and named in the prompt text
   (`[image: shot.png]`) but not sent to the model as image blocks.

10. **`settings.audio` is an untyped extra for now.** `Settings` is `extra="allow"`, so the wizard
    writes and reads the block through `setattr`/`getattr` and `audio_handlers.audio_config` parses
    it defensively (dict or model, missing keys tolerated). The typed `AudioSettings` model belongs
    to the story that owns `config/settings.py`; when it lands, nothing here has to change, because
    every read already goes through one lenient accessor.

11. **The audio backends never import `Settings`.** `audio.AudioConfig` is a plain frozen dataclass
    mirroring the settings block, built by the server layer. That is what lets the whole `audio/`
    package be unit-tested without a settings tree, and it keeps the dependency pointing one way.

12. **OpenAI credentials are borrowed, not asked for twice.** Both the `openai` STT and TTS
    backends read the key and base URL the provider registry already resolved
    (`api_key_for("openai")`), including its environment-variable fallback. A user who configured
    OpenAI as a chat provider gets voice for free.

13. **`audio.speak` returns a path; playing is opt-in and non-fatal.** The default is synthesise-
    and-return, because the client is usually where the speakers are. `play: true` plays on the
    daemon's machine, and a playback failure is logged and reported as `played: false` rather than
    failing the call — the audio exists either way. There is no `audio.spoken` notification yet:
    nothing emits it until auto-speak is wired into the loop.

14. **Recorders are per session and `stop()` drains the child's pipes.** Writing the recorder tests
    turned up a real deadlock: awaiting a bare `process.wait()` hangs forever if the capture tool is
    chatty, because asyncio resolves the exit future only after every pipe closes and an undrained
    stderr stops being read at the stream's high-water mark. `stop()`/`cancel()` use `communicate()`;
    `tests/test_audio.py::test_stop_drains_a_chatty_recorder` is the regression.

## Setup wizard

15. **The Audio section has no circled numeral.** The existing screens are ①–⑥; inserting Audio
    between Browser and Tools would have renumbered Tools, Gateway and Done, churning three other
    stories' title constants and tests for cosmetics. The section is titled "Audio — voice in and
    out" and `FULL_ORDER` becomes providers → search → browser → **audio** → tools → gateway → done.
    This changes the screen order fixed by `m3-providers-setup-contract.md` §5;
    `tests/test_setup_wizard.py` was updated to match and says why.

16. **The section is two screens plus conditional prompts.** The speech-to-text list is the screen
    proper; the text-to-speech list, the voice name, the "read replies aloud" toggle and the "test
    the voice now" offer follow it, the way the providers screen follows its list with a key and a
    model prompt. The follow-ups are asked **only when the user actually picked a TTS backend** —
    leaving both lists on Automatic (or Skip) means "work it out from what is installed", and a
    wizard that then asks three more questions has not listened. A non-interactive run therefore
    writes `{"stt": {"provider": "auto"}, "tts": {"enabled": true, "provider": "auto",
    "autoSpeak": false}}` and asks nothing.

17. **`"off"` is a real answer, distinct from `"auto"`.** Off means never listen / never speak;
    auto means decide from what is installed. Picking Off for speech also clears auto-speak, since
    "read every reply aloud" with no voice is not a state worth persisting.

18. **Rows for backends that are not installed are shown inactive, not hidden.** The answer to "why
    can't I use piper" should be on the screen rather than missing from it.

## Tools

19. **`tools/audio_tools.py` replaces the studio-only `text_to_speech`, keeping its name and
    schema.** It adds one optional argument, `play`, and runs the whole chain. It is written but
    **not registered yet** — `tools/registry.py` and `tools/media.py` belong to another story, so
    swapping the registration (and dropping the media forward) is a follow-up. `refresh_state`
    mirrors `media.refresh_state`: a tool with no backend stays `inactive` with a reason rather
    than vanishing from `tool.list`.

20. **`transcribe_audio` is tagged `read`, not `network`.** The tag describes the tool's guaranteed
    effect: it reads a local file. The hosted backend also uploads it, which the description says
    plainly; making every transcription `network` would have made the common local-whisper case ask
    for a permission it does not need. **Flagged for review** — if the lead prefers the
    conservative tag, it is a one-line change, or a `permission_for` hook could tag per call based
    on the resolved provider.

## Verification at the time of this change

- `SNOWPEA_SKIP_BROWSER_TESTS=1 uv run pytest -q` — 882 passed, 7 skipped (Pillow, shellcheck, sox,
  and the three pre-existing skips).
- `uv run ruff check core tests` — all checks passed.
- `uv run mypy core` — no issues found in 162 source files.
- `uv run python scripts/gen_protocol.py --check` — `sdk/src/protocol.ts` and `docs/protocol.md`
  both `ok`.
- `npm -w sdk test` — 5 passing, 1 failing: `base: a denied approval ends the turn with reason
  'denied'`. Not this story: the in-flight change to `agent/loop.py` (uncommitted,
  `MAX_DENIALS_PER_TURN = 3`) deliberately lets a turn survive a denial, and the SDK contract test
  has not been updated yet. Nothing in this story touches approvals or turn outcomes.

# Deviations — CORE-multimodal (attachments, vision content parts, voice I/O)

Prompt attachments (paste, drag, clipboard image), provider-neutral content parts with a
text-only fallback, and voice in both directions: speech-to-text, text-to-speech, recording and
playback — as RPCs, as built-in agent tools, and as a section of the first-run setup wizard.
Recorded per `docs/design/deviations/README.md`.

Complete: the `attachments/`, `providers/content.py` and `audio/` packages; the protocol,
handlers and setup wizard; the agent loop, the three provider adapters, the typed `settings.audio`
model and the two registered tools.

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
   stashes, and `agent/loop.py` calls `pending.take(session.id)` at the top of the turn. The
   alternative — widening `start_turn`'s signature — would have put a fourth positional concern on
   a function three other stories were editing, and the queue turns out to be the better seam
   anyway: a slash command clears it (nothing would consume the bytes), and a second prompt before
   the turn starts replaces rather than appends, so an interrupted turn cannot leak its images into
   the next one.

10. **History stores a block list with paths, never base64.** A user turn with attachments becomes
    `ChatMessage.content = [{"type": "text", …}, {"type": "image", "path": …, "sha256": …,
    "text": "[image: shot.png]"}]`. Three things fall out of that shape: the session store stays
    small (no megabytes of base64 in SQLite), a resumed session can still send the picture because
    the bytes are re-read when the request is built, and the `text` marker on every block means the
    token estimate, history rendering and the fake provider's matcher all see something sensible
    without decoding anything. A turn with no attachments keeps a plain string, so every existing
    request is byte-identical to before.

11. **An attachment whose file has gone degrades to a marker.** `parts_from_blocks` answers
    `[image: shot.png] (no longer on disk)` rather than raising — a deleted screenshot must not
    make a resumed session unanswerable.

12. **The prompt text is not rewritten.** An earlier revision appended `[image: shot.png]` to the
    user's text in `session_handlers`; now the blocks carry the marker, so the text the user typed
    reaches the model as typed.

13. **`settings.audio` is typed (`AudioSettings`/`SttSettings`/`TtsSettings`), and readers stayed
    lenient.** `audio_handlers.audio_config` still reads through one tolerant accessor that takes
    either a model or a dict, so a hand-written block with missing keys works and the wizard's
    `setattr` path did not have to change. `"off"` is understood by both directions as a real
    answer, distinct from `"auto"`.

14. **Hot reload needed one line, not a rebind.** Everything audio reads `core.settings` at call
    time, which `hot_reload`'s own docstring says is strictly better than being rebound. The
    exception is the two tools' `active`/`inactive` state, which is a snapshot: `rebind` now calls
    `audio_tools.refresh_state`, guarded so a reload can never fail on it.

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

## Provider adapters

15. **Vision is decided in `build_openai_request`, not in the adapter's message loop.** It is the
    one place that knows both the preset and the resolved model, so `messages_to_openai` takes an
    explicit `vision` flag that defaults to `False` — the safe answer if some future caller forgets
    it. Anthropic has no such flag (every model sees), and Gemini takes `inlineData` unconditionally.

16. **Pillow ships by default but is still an extra.** `pyproject` declares
    `[project.optional-dependencies] images = ["pillow>=10"]`, and both installers add it with
    `uv tool install --with 'pillow>=10'` rather than `snowpea-agent[images]`: the install source
    may be a git URL, a wheel path or an editable checkout, and only the flag spells the same thing
    for all three. `SNOWPEA_SKIP_IMAGES=1` opts out. `update.update_command` carries the same flag
    through every upgrade path, or an upgrade would silently drop downscaling.

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
    schema.** It adds one optional argument, `play`, and runs the whole chain, so speech now works
    on a machine with nothing but `espeak-ng`. `media.py` no longer registers a speech tool — its
    `FORWARDS` table keeps the `generate_speech` entry, because that is how the audio code reaches
    studio, and a new `REGISTERED` tuple is what `refresh_state` iterates. Both audio tools are
    `inactive` with a reason until a backend exists, mirroring the media tools.

20. **`transcribe_audio` is tagged `read`, not `network`.** The tag describes the tool's guaranteed
    effect: it reads a local file. The hosted backend also uploads it, which the description says
    plainly; making every transcription `network` would have made the common local-whisper case ask
    for a permission it does not need. A `permission_for` hook could tag per call from the resolved
    provider if that trade is ever judged wrong.

21. **The tool descriptions carry usage rules, not just definitions.** "do not guess at contents
    you have not transcribed", "keep the text short enough to listen to" — the convention the
    prompt-library story established for the other tools.

## The lead's four rulings

23. **The websocket frame cap is 32MB** (`transport_ws.MAX_MESSAGE_BYTES`), up from aiohttp's 4MB
    default, so a 20MB attachment still fits once base64 has added a third. The attachment cap
    stays at 20MB: the thing that refuses an oversized file should be the check that can name the
    limit, not a dropped connection.

24. **The screen-order change is recorded in the contract.**
    `docs/design/m3-providers-setup-contract.md` §5 now names the Audio section and says why it
    carries no circled numeral.

25. **Both audio tools re-tag themselves per call.** `permission_for` resolves the backend and
    answers `network` when it is hosted (`openai`, `studio`) and `read` when it is local. `read`
    rather than `exec` or `write` for the local case is deliberate: what the mode matrix gates is
    egress and changes to your project, and a local backend does neither — it spawns a known binary
    and writes into `SNOWPEA_HOME`. Tagging it `exec` would deny it in plan mode, where "read this
    out to me" is a reasonable thing to ask. `text_to_speech` is still *declared* `network`, the
    stricter of its two possibilities, so a hook that ever fails cannot widen it.

26. **The installers ask for `snowpea-agent[images]`, not `--with pillow`.** Two spellings, because
    one does not fit every source: a URL takes the PEP 508 direct reference
    (`snowpea-agent[images] @ git+https://…`), and a path or a plain package name takes the extra
    inline (`/opt/snowpea[images]`). Both were checked against uv. The payoff over `--with` is that
    `install.json` records a source that already carries the extra, so `snowpea update` repeats it
    with no second flag; `update.with_images` applies the same two-spelling rule to the uv, pipx
    and pip upgrade paths.

## Auto-speak

27. **`audio.spoken` is a session event, not a top-level notification.** It belongs to a session's
    timeline — it happens between `message.done` and `turn.done` — so it is ordered with the reply
    it speaks and replayed on resume like every other session event. Clients read it from
    `session.event` with `kind: "audio.spoken"`.

28. **Speaking can never fail a turn.** `loop.speak_reply` swallows everything: no backend, a
    synthesiser that times out, a player that exits non-zero. A failed *playback* is not even a
    failed speak — the event still carries the path, with `played: false`, so a client on another
    machine (or one whose daemon has no speakers) can play the file itself.

## Documentation

29. **One manual page covers both halves.** `docs/manual/{en,ko}/voice.md` — attachments and voice
    are one story from the user's side ("the terminal is not text-only any more"), and splitting
    them would have meant two pages that each explain half of `audio.capabilities`. Linked from
    both index pages and `docs/manual/README.md`; the tool tables in `modes.md` gained the two new
    tools.

## Verification at the time of this change

- `SNOWPEA_SKIP_BROWSER_TESTS=1 uv run pytest -q` — 894 passed, 7 skipped, 2 failed. Both failures
  are `tests/test_docs_cli.py`, and both are another story's in-flight `docs/manual/*/tui.md`
  (`snowpea --fullscreen` is not a flag yet, and `docs/manual/zh-CN/tui.md` does not exist). The
  171 tests this story owns pass; `uv run python scripts/check_docs_cli.py` reports no problem in
  any file this story touched.
- `uv run ruff check core tests` — all checks passed.
- `uv run mypy core` — no issues found in 162 source files.
- `uv run python scripts/gen_protocol.py --check` — `sdk/src/protocol.ts` and `docs/protocol.md`
  both `ok`.
- `npm -w sdk test` — 6 passing.
- `uv run python scripts/check_docs_cli.py` — 560 invocations and every relative link in 44 files.

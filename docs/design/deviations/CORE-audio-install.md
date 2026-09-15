# CORE-audio-install — installing the local voice engines

`audio.install` obtains the voice engines the daemon can get without root, and
the two CPU-only local engines are now the recommended defaults. This file
records what was **verified against upstream** and what is still open, because
the whole feature hangs on third-party URLs and package names.

## Verified 2026-09-15

| Fact | Source |
|---|---|
| SenseVoiceSmall asset `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2`, from the `asr-models` release of `k2-fsa/sherpa-onnx` | sherpa-onnx SenseVoice pretrained-model page |
| `silero_vad.onnx` from the same release; needed for VAD-split decoding | same page |
| Korean streaming model `sherpa-onnx-streaming-zipformer-korean-2024-06-16.tar.bz2`, fp32 and int8 variants | sherpa-onnx streaming-zipformer page |
| English streaming model `sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2`, int8 encoder and joiner, LibriSpeech | same page |
| Supertonic: repo `supertone-inc/supertonic`, PyPI `supertonic` 1.3.1 (2026-05-18), Python ≥ 3.9, onnxruntime / numpy / soundfile / huggingface-hub | PyPI project page |
| Supertonic API: `TTS(auto_download=True)`, `get_voice_style(voice_name=…)`, `synthesize(text=, voice_style=, lang=)`, `save_audio(wav, path)` | PyPI project page |
| Supertonic preset voices M1-M5, F1-F5; `M1` is the documented example | upstream `py/README.md` |
| Supertonic licences: code MIT, **models OpenRAIL-M** | PyPI project page and the Hugging Face model card |
| Supertonic 3 released 2026-04-29, 31 languages including Korean and English | upstream README |

## Open: no checksums are pinned

`SpeechModel.sha256` is `None` on every row. Upstream publishes no digest
beside these release assets, and a digest invented here would fail verification
for every user while protecting none. `verify()` already refuses a mismatch, so
filling the table in later is a data change with no code change — the tests
assert the *format* of a digest rather than its presence, so a real one can be
added row by row.

Until then the protections are: HTTPS to a pinned GitHub release prefix, the
`data` extraction filter plus an explicit member check so no archive path
escapes its directory, and a stamp file written only after every expected file
is present.

**To close this**, download each asset once, record `sha256sum`, and put the
digests in `MODELS`. Nothing else has to change.

## Open: the Supertonic CLI flags are not documented

Upstream ships `supertonic tts` and `supertonic serve` console scripts, but its
README documents neither one's flags. The Python API *is* documented exactly,
so `SupertonicTTS` runs that instead, in a child interpreter, with the request
handed over on stdin as JSON.

Two consequences, both deliberate:

- Supertonic is the one engine installed with `pip install --user` rather than
  `uv tool install` (`EngineInstall.isolated = False`). A `uv tool` venv is
  exactly where our interpreter cannot import from.
- `SUPERTONIC_SCRIPT` is a module constant and is never formatted with user
  text. Nothing anyone types becomes part of a program.

## The chain is gone: two states, per direction

`auto` was removed as a value. `audio.stt.provider` / `audio.tts.provider` is
**unset** (that direction of voice is off) or **one engine id** (pinned). An
older file's `"auto"` reads as unset and is rewritten away on the next save
(`config/settings.LEGACY_AUTO`).

The chain could not tell a user why they got silence. It tried five backends,
and when none worked the only honest report — "nothing is set up" — was the one
sentence the code could never produce. Now `audio.capabilities` says exactly
one of:

| State | Reason |
|---|---|
| nothing pinned | `no engine set — install or pick one in setup` |
| pinned, missing | `engine <id> is not installed` |

plus `sttPinned` / `ttsPinned` and `sttEffective` / `ttsEffective`, so a surface
can render "Not set" rather than inventing a default it would never get.

`RECOMMENDED_ORDER` (the old `AUTO_ORDER`) survives with a narrower job: it is
what the wizard suggests installing first and what detection lists first. It
resolves nothing.

**The media tools keep a chain.** `text_to_speech` and `transcribe_audio` are
tools the *model* calls deliberately, and refusing one because the user has not
chosen a voice for their own replies would be a non sequitur. They go through
`resolve_any`, which walks `RECOMMENDED_ORDER` — studio included, which is what
keeps a configured studio server working. Voice in and out never come through
there.

## Installing is not choosing

`audio.install` puts an engine on the machine and **changes no setting**. The
screen returns with that engine active and the status line says
`X is installed, not selected — pick it to use it` until it is pinned. The two
were one action once; separating them is what makes "I installed it and nothing
happened" impossible to reach by accident.

## Every row leads somewhere

A list where some entries do nothing when chosen is a list that lies about
being a choice. `screens/audio.row_action` gives every row one:

| Row | Picking it |
|---|---|
| installable, missing | installs it, then comes back with it active |
| system package | shows the platform command, offers to run it, re-checks |
| custom command | asks for the template, validates the placeholders, self-tests 3s, pins |
| hosted (OpenAI) | asks for the key (masked), pins |
| Off | unsets: that direction is off |
| installed | pins it; a pinned row is marked `★` |

Engines that could never work here are not listed at all
(`catalog.PLATFORM_ONLY`: macOS `say`, Windows `powershell`).

## Staged install progress

A log line cannot say how far along a 400MB download is.
`audio.install.progress` carries `stage`, `step`, `steps`, and where they can
be known `percent`, `bytesDone`, `bytesTotal` — additive, so a client that only
reads `line` is unaffected. The sequences are per engine rather than a constant
every engine pretends to: a package install has three stages, a model install
six, piper's four.

## The opening acknowledgement

With `audio.tts.autoSpeak` on, the line the agent writes *before its first tool
call* is spoken too, trimmed to two sentences or 240 characters. A spoken
request answered by a silent minute feels dead, and the agent has already
written what it is about to do. Only the first such line of a turn — later ones
are thinking aloud, and narrating a whole turn is not what anyone asked for.
Never in a delegated or unattended turn, which has nobody in the room.
`audio.spoken` gains `utterance: "reply" | "ack"`; `audio.tts.speakAck`
(default true) turns it off.

## Supertonic has no per-language models — verified

The brief assumed per-language model assets to list and install. There are
none. Verified 2026-09-15 against PyPI and the model card: Supertonic 3 is
**one multilingual model**, ~400MB into `~/.cache/supertonic3/`, covering 31
languages plus an `na` fallback, with ten preset voice styles (M1-M5, F1-F5)
that are not language-specific. `lang` is a synthesis-time parameter, not a
model selector.

So `audio.voices` reports Supertonic's styles as `language: "*"`, every one
`installed` once the package is there and none of them a download. Choosing a
different style per language is still worth offering — that is what the
mapping is for — it simply costs nothing extra.

Piper is the engine that *does* work the way the brief described: one file per
voice, each tied to a language, `installed: false` until fetched.

## Voices are per language

`audio.tts.voice` became `audio.tts.voices`, a mapping:

```json
{ "audio": { "tts": { "voices": { "ko": "F2", "en": "M1", "*": "M1" } } } }
```

Resolution is the reply's own language, then `*`, then the engine's default —
and the default is a real answer, not a gap: forcing one of an engine's voices
on a language it was not recorded for sounds worse than letting it choose. The
old single `voice` string reads as `voices["*"]`, so nothing has to migrate.

`audio.voices {engine}` lists what an engine offers *whether or not it is
installed*, because choosing between engines means seeing what each would give
you. `sample` is the field that says whether a preview can actually be played.
`audio.install {engine, voice}` fetches one, with the same staged progress, and
**does not select it**.

## `audio.stt.language`: not a hint for every engine

`"auto"` (default) means nobody forced a language, not that there is none. An
engine that detects for itself does that; one that cannot takes the session's
reply language. A BCP-47 tag forces it.

The distinction that matters: a **sherpa Zipformer is single-language**, so the
setting picks the *model*. Asking the English model for Korean would produce
confident nonsense, so it refuses and the capability reason names the model
that would work:

```
sherpa-onnx-zipformer-en does not speak ko; install sherpa-onnx-zipformer-ko
```

`"auto"` is never passed to an engine's command line — whisper would go looking
for a language called "auto" — and `capabilities` reports `sttLanguage` plus
`sttLanguageSource` (`setting` | `reply` | `detect`) so a surface can say who
decided.

## Why the chains changed

Both `auto` chains now put local engines ahead of hosted ones.

- **STT**: `sherpa-onnx-sensevoice` → the Korean and English zipformers →
  `local-whisper` → `openai` → `command`. Transcription is the one place audio
  of the user's room would otherwise leave the machine.
- **TTS**: `supertonic` → `edge-tts` → `piper` → `say` / `espeak-ng` /
  `powershell` → `command` → `openai` → `studio`.

`studio` stays last rather than being removed: the `text_to_speech` media tool
resolves through the same chain, and forwarding to a configured studio server
is what that tool is. It is no longer a voice *choice* in the catalog.

One visible consequence: `text_to_speech`'s permission tag is now `read` rather
than `network` on a machine with both an OpenAI key and a local voice, because
the tag follows the backend that will actually run and that backend sends
nothing anywhere. `tests/test_audio_tools.py` pins both halves.

## Where it lives

`core/snowpea_core/audio/{install,stt_models,stt,tts}.py`,
`core/snowpea_core/setup/{catalog,screens/audio,wizard}.py`,
`core/snowpea_core/server/{protocol,audio_handlers,settings_handlers}.py`,
`core/snowpea_core/cli/commands.py`. Tests:
`tests/test_audio_install.py`, `tests/test_audio_local_engines.py`.

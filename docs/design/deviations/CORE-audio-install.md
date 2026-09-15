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

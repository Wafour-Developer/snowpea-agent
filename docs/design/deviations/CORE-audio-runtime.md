# CORE-audio-runtime — the interpreter snowpea owns for the voice engines

Two of the local voice families are Python libraries, not command line tools.
Installing them as if they were commands left a successful install with nothing
to detect, which is what the wizard was reporting: engine installed, row still
says *Install…*.

## Why

- **`sherpa-onnx`.** The wheel provides `sherpa_onnx` (`OfflineRecognizer`,
  `OnlineRecognizer`, `VoiceActivityDetector`) and one console script,
  `sherpa-onnx-cli`, which does not import without `click`. It ships **no**
  `sherpa-onnx` / `sherpa-onnx-offline` executable, so
  `shutil.which("sherpa-onnx-offline")` was never going to be true after
  `uv tool install sherpa-onnx`.
- **`supertonic`.** Documented as a Python API. It was installed with
  `[sys.executable, -m, pip, install, --user, …]`, and the deployed daemon runs
  from `~/.local/share/uv/tools/snowpea-agent/bin/python`, which has no `pip`
  module; a venv ignores `--user` anyway. The install could not succeed in the
  shipped configuration.

Installing into the daemon's own environment is not an option either: it is a
`uv tool` venv that `uv` owns and rewrites on update.

## The layout

`$SNOWPEA_HOME/audio-runtime/` is a venv snowpea creates and owns
(`core/snowpea_core/audio/runtime.py`).

| Piece | What it is |
|---|---|
| `runtime_dir(home)` / `runtime_python(home)` | the directory and its interpreter (`bin/python`, `Scripts/python.exe` on Windows) |
| `ensure_runtime(home, progress)` | creates it if missing: `uv venv` when `uv` is on PATH, else `python -m venv` (which runs `ensurepip` in the *new* environment, so a pip-less parent is fine), else `python3 -m venv`. Idempotent. |
| `runtime_install_argv(home, package)` | `uv pip install --python <py> <package>`, or `<py> -m pip install <package>` |
| `has_module(home, module)` | a filesystem probe of the runtime's `site-packages`. Availability is asked on every `audio.capabilities` call, so this must not spawn a process. |

`EngineInstall.runtime` (which replaced `isolated`) says which engines go
there: `sherpa-onnx-sensevoice`, `sherpa-onnx-zipformer-ko`,
`sherpa-onnx-zipformer-en`, `supertonic`. Everything else — `piper`,
`edge-tts`, `faster-whisper` — is a command and still goes through
`uv tool install` / `pipx` / `pip install --user`. A runtime install reports an
extra `runtime` stage between `resolve` and `install`, so the progress bar
counts the wait.

## How the engines run

Both run a child of the runtime interpreter with a constant `-c` script and a
JSON request on stdin, so no path and no setting is ever part of a program.
`SherpaOnnxSTT.argv()` returns the interpreter command and
`SherpaOnnxSTT.request(audio)` the request; `SupertonicTTS` already worked this
way and only changed which interpreter it names.

`SHERPA_SCRIPT` reads the wav with the standard library rather than
`sherpa_onnx.read_wave`: that helper is absent from the installed 1.13.8 wheel,
and the wheel does not depend on numpy (its only requirement is
`sherpa-onnx-core`), so a numpy-returning helper would not be importable in a
fresh runtime either. `accept_waveform` takes any sequence of floats, which a
stdlib `array` already is.

## Verified live 2026-09-15

Against a scratch `SNOWPEA_HOME` with the SenseVoice model already downloaded:

- `snowpea audio install sherpa-onnx-sensevoice` created the venv with
  `uv venv` and installed `sherpa-onnx==1.13.8`.
- `snowpea audio install supertonic` installed `supertonic==1.3.1` with
  onnxruntime and numpy into the same runtime.
- `SupertonicTTS(home=…).synthesize(...)` produced a 44.1 kHz wav, and
  `SherpaOnnxSTT("sherpa-onnx-sensevoice", home=…).transcribe(...)` read it
  back as the exact sentence — through the whole-stream path at 44.1 kHz and
  through the VAD-split path at 16 kHz.

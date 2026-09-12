"""Voice as agent capabilities: ``transcribe_audio`` and ``text_to_speech``.

Voice must not be a TUI-only feature.  The same backends behind the ``audio.*``
RPCs are offered to the model as tools, so an agent can listen to a voice memo
the user dropped in the repo, or answer out loud, without anything in the
prompt knowing which provider is installed.

``text_to_speech`` here is the replacement for the studio-only forward in
:mod:`snowpea_core.tools.media`: same name, same schema plus an optional
``play``, but it runs the whole provider chain, so it still works on a machine
with nothing but ``espeak-ng``.  The tools are built here and registered by
the tool registry; this module keeps no global state.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.audio import capabilities
from snowpea_core.audio.player import AudioError
from snowpea_core.audio.player import play as play_audio
from snowpea_core.server.audio_handlers import audio_config, audio_dir_for, speech_caller
from snowpea_core.server.protocol import PermissionTag
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.tools.audio")

INACTIVE = "tool_inactive"

#: Tool name -> the capability key it needs in ``audio.capabilities``.
NEEDS: dict[str, str] = {"transcribe_audio": "stt", "text_to_speech": "tts"}


#: Backends that send the audio (or the text) to somebody else's machine.
HOSTED: frozenset[str] = frozenset({"openai", "studio"})


def _tag_for(core: Any, resolve: str) -> PermissionTag:
    """``network`` when the resolved backend is hosted, else ``read``.

    The tag has to describe what *this* call does, and that is not a property
    of the tool: transcribing through a local whisper reads a file you already
    have, and transcribing through OpenAI uploads it.  ``read`` rather than
    ``exec`` or ``write`` for the local case is deliberate — what the mode
    matrix gates is egress and changes to your project, and a local backend
    does neither: it spawns a known binary and writes into ``SNOWPEA_HOME``.
    Tagging it ``exec`` would deny it in plan mode, where "read this out to me"
    is a perfectly reasonable thing to ask.
    """
    from snowpea_core.server.audio_handlers import audio_config, speech_caller

    config = audio_config(core)
    provider = config.stt() if resolve == "stt" else config.tts(speech_caller(core))
    name = getattr(provider, "name", "")
    return "network" if name in HOSTED else "read"


def permission_for_transcribe(args: dict[str, Any], session: Any, core: Any) -> PermissionTag:
    """``transcribe_audio``'s tag for one call."""
    return _tag_for(core, "stt")


def permission_for_speech(args: dict[str, Any], session: Any, core: Any) -> PermissionTag:
    """``text_to_speech``'s tag for one call."""
    return _tag_for(core, "tts")


def _session_id(ctx: ToolContext) -> str | None:
    return getattr(ctx.session, "id", None)


def refresh_state(core: Core) -> dict[str, str]:
    """Set each audio tool's state from what this machine can do.

    A tool with no backend stays ``inactive`` rather than disappearing, so
    ``tool.list`` can show it greyed out with a reason — the same shape the
    media tools use.
    """
    report = capabilities(audio_config(core), caller=speech_caller(core))
    states: dict[str, str] = {}
    for name, key in NEEDS.items():
        ready = bool(report.get(key))
        state = "active" if ready else "inactive"
        states[name] = state
        try:
            core.tools.set_state(name, state)  # type: ignore[arg-type]
        except KeyError:  # pragma: no cover - the tool may not be registered yet
            log.debug("audio tool %s is not registered", name)
    return states


def _unavailable(core: Core, key: str, tool: str) -> str:
    """The error text for a tool whose backend is missing."""
    report = capabilities(audio_config(core), caller=speech_caller(core))
    reason = report.get("reasons", {}).get(key, "no backend is configured")
    return f"{INACTIVE}: {tool} is unavailable — {reason}"


async def run_transcribe_audio(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """``transcribe_audio`` — read a sound file and return what was said."""
    core = ctx.core
    config = audio_config(core)
    provider = config.stt()
    if provider is None:
        return ToolResult(ok=False, error=_unavailable(core, "stt", "transcribe_audio"))
    raw = str(args.get("path") or "").strip()
    if not raw:
        return ToolResult(ok=False, error="transcribe_audio needs a path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(ctx.session.workdir) / path
    if not path.is_file():
        return ToolResult(ok=False, error=f"no such audio file: {path}")
    language = args.get("language")
    if language and hasattr(provider, "language"):
        provider.language = str(language)  # type: ignore[attr-defined]
    try:
        transcript = await provider.transcribe(path, None)
    except AudioError as exc:
        return ToolResult(ok=False, error=f"{exc.code}: {exc}")
    return ToolResult(
        ok=True,
        output=transcript.text or "(silence)",
        meta={"provider": transcript.provider, "path": str(path)},
    )


async def run_text_to_speech(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """``text_to_speech`` — say something, through whatever backend exists."""
    core = ctx.core
    config = audio_config(core)
    caller = speech_caller(core)
    provider = config.tts(caller)
    if provider is None:
        return ToolResult(ok=False, error=_unavailable(core, "tts", "text_to_speech"))
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="text_to_speech needs text")
    try:
        speech = await provider.synthesize(
            text,
            out_dir=audio_dir_for(core, _session_id(ctx)),
            voice=args.get("voice") or config.voice,
            language=args.get("language"),
        )
    except AudioError as exc:
        return ToolResult(ok=False, error=f"{exc.code}: {exc}")
    played = False
    if args.get("play"):
        try:
            await play_audio(speech.path, preferred=config.player)
            played = True
        except AudioError as exc:
            log.info("text_to_speech could not play locally: %s", exc)
    detail = " and played it" if played else ""
    return ToolResult(
        ok=True,
        output=f"Wrote speech to {speech.path}{detail}.",
        path=str(speech.path),
        meta={
            "provider": speech.provider,
            "mime": speech.mime,
            "voice": speech.voice,
            "played": played,
        },
    )


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="transcribe_audio",
        category="audio",
        description=(
            "Transcribe a recording to text. Use it when the user points at an audio or "
            "video-sound file, or asks what a voice memo says; do not guess at contents you "
            "have not transcribed. Runs locally when a whisper CLI is installed, otherwise "
            "through the configured speech-to-text provider."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Audio file to transcribe; a relative path resolves in the workdir."
                    ),
                },
                "language": {
                    "type": "string",
                    "description": "BCP-47 hint, e.g. ko or en, when the audio is not English.",
                },
            },
            "required": ["path"],
        },
        # Reading a file locally.  ``permission_for`` raises this to ``network``
        # for the call when the resolved backend is a hosted one.
        permission="read",
        permission_for=permission_for_transcribe,
        run=run_transcribe_audio,
        state="inactive",
        source="audio",
    ),
    Tool(
        name="text_to_speech",
        category="audio",
        description=(
            "Synthesise speech from text and return the audio file. Use it when the user asks "
            "to hear something, or asks for an audio file of some text; set play to speak it "
            "out loud on this machine. Keep the text short enough to listen to."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to say."},
                "voice": {"type": "string", "description": "Voice id; defaults to the setting."},
                "language": {"type": "string", "description": "BCP-47 language tag, e.g. ko."},
                "play": {
                    "type": "boolean",
                    "description": "Play the audio on this machine as well as returning it.",
                },
            },
            "required": ["text"],
        },
        # Declared at the stricter of the two: ``permission_for`` lowers it to
        # ``read`` for the call when the voice is a local one.
        permission="network",
        permission_for=permission_for_speech,
        run=run_text_to_speech,
        state="inactive",
        source="audio",
    ),
)


__all__ = [
    "HOSTED",
    "INACTIVE",
    "NEEDS",
    "TOOLS",
    "permission_for_speech",
    "permission_for_transcribe",
    "refresh_state",
    "run_text_to_speech",
    "run_transcribe_audio",
]

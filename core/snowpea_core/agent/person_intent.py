"""What the person asked of this turn, read from their own words only.

Two readings, both off ``session.user_turn`` (the text and mid-turn steers the
person typed — never a tool result, a summary or a daemon nudge):

* :func:`finish_requested` — the person told the agent to carry the work
  through ("끝까지 진행해줘", "finish it", "keep going until it's done").  A
  turn like that gets a generous continuation cap
  (``agent.finish_request_continuations``) instead of ending on a checkpoint
  the model wrote itself.
* :func:`person_language` / :func:`language_note` — the language the person
  writes in.  A long turn fills up with English: tool output, the compaction
  summary, the verify-gate and budget nudges.  A model reading those last
  drifts into English even though the person wrote Korean, so every note the
  daemon appends after the person's words carries one line restating their
  language.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

#: Phrases that ask for the work to be carried through to the end.
_FINISH = re.compile(
    r"끝까지|마무리\s*(?:까지|해|지어)|완료(?:될|할)\s*때\s*까지|될\s*때\s*까지|"
    r"다\s*(?:끝내|마치|완성)|멈추지\s*(?:말|마)|중단\s*없이|쉬지\s*말고|쭉\s*(?:진행|해)|"
    r"\bfinish\s+(?:it|this|that|the\s+(?:job|work|task)|everything|up)\b|"
    r"\buntil\s+(?:it(?:'s|\s+is)\s+|everything(?:'s|\s+is)\s+|)(?:done|finished|complete)\b|"
    r"\ball\s+the\s+way\b|\b(?:to|till|until)\s+the\s+end\b|\bkeep\s+going\b|"
    r"\bdon'?t\s+stop\b|\bwithout\s+stopping\b|\bsee\s+it\s+through\b|"
    r"\bcarry\s+(?:it|this|that)\s+through\b",
    re.IGNORECASE,
)


def person_words(session: Any) -> list[str]:
    """The person's words for the running turn, newest first."""
    turn = getattr(session, "user_turn", None)
    if not isinstance(turn, dict):
        return []
    steers = [str(s) for s in (turn.get("steers") or []) if str(s).strip()]
    words = list(reversed(steers))
    text = str(turn.get("text") or "")
    if text.strip():
        words.append(text)
    return words


def finish_requested(session: Any) -> bool:
    """True when the person asked, this turn, for the work to be finished."""
    if getattr(session, "is_subagent", False):
        return False
    return any(_FINISH.search(words) for words in person_words(session))


def person_language(core: Core | None, session: Any) -> str | None:
    """The language to answer in: the setting or UI locale, else the person's own words.

    ``None`` when nothing the person wrote says (no words at all).
    """
    from snowpea_core.agent.agent import session_reply_language
    from snowpea_core.util.lang import detect_language

    configured = (session_reply_language(core, session) or "auto").strip()
    if configured and configured.lower() != "auto":
        return configured
    words = person_words(session)
    if not words:
        from snowpea_core.session import compaction

        history = getattr(session, "history", None)
        latest = compaction.latest_user_words(history.snapshot()) if history is not None else ""
        words = [latest] if latest.strip() else []
    for text in words:
        if any(ch.isalpha() for ch in text):
            return detect_language(text)
    return None


def language_note(core: Core | None, session: Any) -> str:
    """One line restating the person's language, or ``""`` when it is English or unknown.

    English needs no line: the notes it would follow are English already.
    """
    from snowpea_core.prompts.compose import language_name

    tag = person_language(core, session)
    if not tag or tag.lower().split("-")[0].split("_")[0] == "en":
        return ""
    return (
        f"Write your reply to the person in {language_name(tag)}, the language they "
        "write in, even though this note and the tool output are in English. Keep code, "
        "paths and commands as they are."
    )


def with_language(core: Core | None, session: Any, text: str) -> str:
    """``text`` with :func:`language_note` appended on its own line when there is one."""
    note = language_note(core, session)
    if not note or note in text:
        return text
    return f"{text}\n{note}" if text else note


__all__ = [
    "finish_requested",
    "language_note",
    "person_language",
    "person_words",
    "with_language",
]

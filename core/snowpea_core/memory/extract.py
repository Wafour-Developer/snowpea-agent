"""Suggested preferences: one lasting preference from the person's own words (C7).

After a completed turn of the person's own words (never an agent's task, a
job or a subagent), :func:`extract_preference` asks the model whether that
message states a lasting preference.  Only the message goes in: no tool
results, page text or history, so nothing a page said can become a memory.

``memory.autoExtract`` decides what happens to the answer: ``"suggest"``
keeps it as a :class:`Candidate` and sends ``memory.candidate``; ``"on"``
stores it as ``auto-pref``.  Either way it passes the C6 value check first.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from snowpea_core.agent.definition import DefinitionError, complete_text, parse_generated_json
from snowpea_core.memory.safety import sensitive_kind
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage

EXTRACT_SYSTEM = load("workflows/memory-extract")

CATEGORIES = ("shopping", "output", "site")

#: The person's message as the extractor sees it; a pasted page past this is
#: not a preference anyway.
MAX_MESSAGE_CHARS = 2000

#: Longest preference kept; a longer answer is a summary, not a preference.
MAX_PREFERENCE_CHARS = 200

#: Candidates waiting for ``memory.confirm``; the oldest goes first.
MAX_CANDIDATES = 32


@dataclass
class Candidate:
    text: str
    category: str
    session_id: str


def extract_messages(text: str) -> list[ChatMessage]:
    message = text.strip()[:MAX_MESSAGE_CHARS]
    return [
        ChatMessage(role="system", content=EXTRACT_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                "Preference extraction. The person's message:\n\n"
                f"{message}\n\nReply with the JSON object only."
            ),
        ),
    ]


async def extract_preference(provider: Any, text: str) -> tuple[str, str] | None:
    """``(preference, category)`` from ``text``, or ``None`` when it states none."""
    reply = await complete_text(provider, extract_messages(text))
    try:
        data = parse_generated_json(reply)
    except DefinitionError:
        return None
    preference = " ".join(str(data.get("preference") or "").split())
    category = str(data.get("category") or "")
    if not preference or category not in CATEGORIES:
        return None
    if len(preference) > MAX_PREFERENCE_CHARS or sensitive_kind(preference) is not None:
        return None
    return preference, category


class Candidates:
    """Proposed preferences by id, in memory only (a restart drops them).

    A text already pending, or one the person dropped, is not proposed again
    while the daemon runs.
    """

    def __init__(self) -> None:
        self._pending: OrderedDict[str, Candidate] = OrderedDict()
        self._dropped: OrderedDict[str, None] = OrderedDict()

    def known(self, text: str) -> bool:
        return text in self._dropped or any(c.text == text for c in self._pending.values())

    def add(self, candidate: Candidate) -> str | None:
        """The new candidate's id, or ``None`` when its text is known already."""
        if self.known(candidate.text):
            return None
        candidate_id = f"mc-{secrets.token_hex(6)}"
        self._pending[candidate_id] = candidate
        while len(self._pending) > MAX_CANDIDATES:
            self._pending.popitem(last=False)
        return candidate_id

    def take(self, candidate_id: str) -> Candidate | None:
        return self._pending.pop(candidate_id, None)

    def drop(self, candidate: Candidate) -> None:
        self._dropped[candidate.text] = None
        while len(self._dropped) > MAX_CANDIDATES:
            self._dropped.popitem(last=False)


__all__ = ["CATEGORIES", "Candidate", "Candidates", "extract_messages", "extract_preference"]

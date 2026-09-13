"""Which language a piece of user text is written in.

Deliberately tiny and deterministic: no model call, no dependency, no
probabilistic guess that could differ between two runs on the same text.  It
exists for one decision — what output language a delegated subagent is told to
answer in when ``agent.replyLanguage`` is ``auto`` — so a wrong guess costs a
line in a brief, never a failed turn.

Script beats vocabulary, in this order:

* Hangul                          -> ``ko``
* kana (hiragana / katakana)      -> ``ja``
* Cyrillic                        -> ``ru``
* Han with no kana and no Hangul  -> ``zh``
* anything else                   -> ``en``

Han-only Japanese therefore reads as ``zh``; that is the price of not carrying
a dictionary, and Japanese prose of any length contains kana.
"""

from __future__ import annotations

#: What :func:`detect_language` falls back to, including for empty text.
DEFAULT_LANGUAGE = "en"

#: Every tag this module can return.
LANGUAGES = ("ko", "ja", "zh", "ru", "en")


def _is_hangul(code: int) -> bool:
    return (
        0xAC00 <= code <= 0xD7A3  # syllables
        or 0x1100 <= code <= 0x11FF  # jamo
        or 0x3130 <= code <= 0x318F  # compatibility jamo
    )


def _is_kana(code: int) -> bool:
    return (
        0x3040 <= code <= 0x309F  # hiragana
        or 0x30A0 <= code <= 0x30FF  # katakana
        or 0xFF66 <= code <= 0xFF9D  # halfwidth katakana
    )


def _is_cyrillic(code: int) -> bool:
    return 0x0400 <= code <= 0x052F


def _is_han(code: int) -> bool:
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF


def detect_language(text: str) -> str:
    """A language tag for ``text``; :data:`DEFAULT_LANGUAGE` when unsure."""
    if not text:
        return DEFAULT_LANGUAGE
    kana = cyrillic = han = 0
    for character in text:
        code = ord(character)
        if _is_hangul(code):
            return "ko"
        if _is_kana(code):
            kana += 1
        elif _is_cyrillic(code):
            cyrillic += 1
        elif _is_han(code):
            han += 1
    if kana:
        return "ja"
    if cyrillic:
        return "ru"
    if han:
        return "zh"
    return DEFAULT_LANGUAGE


__all__ = ["DEFAULT_LANGUAGE", "LANGUAGES", "detect_language"]

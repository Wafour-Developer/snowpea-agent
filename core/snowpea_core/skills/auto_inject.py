"""When a skill's ``autoInject`` keyword counts as said (1.55.0).

The old test was a case-insensitive substring: "글" (writing) matched inside
"글로벌" (global), and "go" inside "google".  The rule now:

* Text and keyword are compared case-folded, with runs of white space as one
  space; a keyword may be a phrase ("pull request").
* A match must start at a word boundary: the character before it is not a
  letter or digit (any script).
* It must end at a word boundary too — or, for a keyword ending in a Hangul
  syllable, be followed by a Korean particle and then a boundary ("글을",
  "메일로", "깃허브에서"): the whole rest of that Hangul run must be one of
  :data:`PARTICLES`.  "글로벌" does not match "글" because "로벌" is not one.
* A keyword written in CJK script (Hangul, Kana, Han) must be at least two
  characters: a single syllable is a part of too many words to mean anything.
"""

from __future__ import annotations

import re
import unicodedata

#: Korean particles (조사) and the copula endings a keyword may carry, longest
#: first so "에서" is tried before "에".
PARTICLES: tuple[str, ...] = tuple(
    sorted(
        {
            "은", "는", "이", "가", "을", "를", "에", "에서", "에게", "께", "한테", "의",
            "도", "로", "으로", "와", "과", "랑", "이랑", "만", "까지", "부터", "보다",
            "처럼", "같이", "하고", "이나", "나", "이든", "든", "이라", "라", "이란", "란",
            "이야", "야", "이에요", "예요", "이고", "고", "에서는", "에는", "으로는", "로는",
            "에도", "에서도", "으로도", "로도", "만큼", "마다", "께서", "한테서", "에게서",
            "좀", "요",
        },
        key=len,
        reverse=True,
    )
)

_SPACE = re.compile(r"\s+")


def _is_hangul(char: str) -> bool:
    return "가" <= char <= "힣" or "ᄀ" <= char <= "ᇿ" or "㄰" <= char <= "㆏"


def is_cjk(char: str) -> bool:
    """True for a Hangul, Kana or Han character."""
    if _is_hangul(char):
        return True
    name = unicodedata.name(char, "")
    return name.startswith(("CJK UNIFIED", "CJK COMPATIBILITY", "HIRAGANA", "KATAKANA"))


def _word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _normal(text: str) -> str:
    return _SPACE.sub(" ", text.casefold()).strip()


def usable(keyword: str) -> bool:
    """False for a keyword that can never match (empty, or one CJK character)."""
    word = _normal(keyword)
    if not word:
        return False
    return not (len(word) < 2 and any(is_cjk(char) for char in word))


def keyword_matches(keyword: str, text: str) -> bool:
    """True when ``keyword`` occurs in ``text`` as a whole word or phrase (module rule)."""
    word = _normal(keyword)
    haystack = _normal(text)
    if not usable(word) or not haystack:
        return False
    start = 0
    while True:
        index = haystack.find(word, start)
        if index < 0:
            return False
        start = index + 1
        if index > 0 and _word_char(haystack[index - 1]) and _word_char(word[0]):
            continue
        end = index + len(word)
        if end >= len(haystack) or not _word_char(haystack[end]) or not _word_char(word[-1]):
            return True
        if _is_hangul(word[-1]) and _is_hangul(haystack[end]):
            run_end = end
            while run_end < len(haystack) and _is_hangul(haystack[run_end]):
                run_end += 1
            if haystack[end:run_end] in PARTICLES:
                return True


def first_match(keywords: list[str], text: str) -> str | None:
    """The first of ``keywords`` that :func:`keyword_matches` ``text``."""
    return next((word for word in keywords if keyword_matches(word, text)), None)


__all__ = ["PARTICLES", "first_match", "is_cjk", "keyword_matches", "usable"]

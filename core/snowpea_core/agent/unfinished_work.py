"""Unfinished work on stop: a turn does not end on its own "to do next time" list.

A model that ran out of steam — not out of budget — tends to close a long turn
with a status report: "체크포인트입니다 … 남은 일 (다음 턴): 1. … 2. …".  It
asked nothing, nothing blocks it, and the steps it lists are ones it could do
now with its tools; the harness ended the turn anyway because the answer had
no tool call, and the person had to type "계속" again and again.

:func:`detect` reads the final answer.  It says "continue" only when the
answer, in its own words, defers work it could do now, and says nothing that
waits on the person.  The phrase table is :data:`MARKERS` (work remains) and
:data:`WAITING` (it waits on the person, or only offers optional extras); a
match in ``WAITING`` always wins, and code blocks and inline code are ignored.
Precision over recall: a missed checkpoint costs one "계속", a wrong
continuation costs a turn that will not stop.

The ``unfinished-work`` listener on the ``turn.stopping`` chain
(:mod:`agent.turn_stopping`) applies it, bounded by ``agent.unfinished_continue``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

#: The instruction the model gets when its answer listed work that remains.
INSTRUCTION = (
    "[system] You stopped, but your own message lists work that remains. If you need "
    "something only the user can give (a decision, a credential, access), say exactly "
    "what and stop. Otherwise continue now and finish the listed steps yourself with "
    "your tools; do not hand them to a later turn."
)


@dataclass(frozen=True)
class Marker:
    """One phrase that says work remains."""

    lang: str
    #: ``deferral``: enough on its own ("next turn", "ran out of time");
    #: ``heading``: counts only when listed steps follow it ("남은 일: 1. …").
    kind: Literal["deferral", "heading"]
    pattern: str


def _m(lang: str, kind: Literal["deferral", "heading"], *patterns: str) -> list[Marker]:
    return [Marker(lang, kind, p) for p in patterns]


#: Phrases that say work remains, per language.  ``heading`` entries are the
#: words a status report puts above a list; they need list items after them.
MARKERS: tuple[Marker, ...] = (
    # Korean
    *_m("ko", "deferral",
        r"다음\s*턴", r"다음\s*세션", r"이어서\s*(?:진행)?하겠",
        r"이번\s*턴(?:에서는|에는|엔)[^\n]{0,80}?못"),
    *_m("ko", "heading",
        r"남은\s*(?:일|작업|단계|과제)", r"다음\s*단계", r"아직\s*못", r"미완료"),
    # English
    *_m("en", "deferral",
        r"\bnext\s+(?:turn|session)\b", r"\bran\s+out\s+of\s+(?:time|rounds|budget|turns?)\b",
        r"\bcould(?:n'?t|\s+not)\s+get\s+to\b",
        r"\b(?:in|for)\s+a\s+(?:later|follow-up|future)\s+turn\b"),
    *_m("en", "heading",
        r"\bremaining\s+(?:work|steps|tasks|items)\b", r"\bnext\s+steps?\b",
        r"\bstill\s+to\s+do\b", r"\bleft\s+to\s+do\b", r"\bnot\s+yet\s+done\b",
        r"\bnot\s+done\s+yet\b", r"^[ \t#*]*(?-i:TODO)\b"),
    # Japanese
    *_m("ja", "deferral", r"次の?ターン", r"時間切れ"),
    *_m("ja", "heading",
        r"残りの?(?:作業|タスク|手順)", r"残タスク", r"未完了", r"次の?ステップ"),
    # Chinese
    *_m("zh", "deferral", r"下一轮", r"下一个?回合", r"下次(?:再)?继续"),
    *_m("zh", "heading",
        r"剩[余下]的?(?:工作|任务|步骤)", r"待完成", r"下一步", r"尚未完成"),
    # Spanish
    *_m("es", "deferral",
        r"\b(?:pr[oó]ximo|siguiente)\s+turno\b", r"\bme\s+qued[eé]\s+sin\s+(?:tiempo|rondas)\b"),
    *_m("es", "heading",
        r"\bpr[oó]ximos\s+pasos\b", r"\bsiguientes\s+pasos\b",
        r"\b(?:trabajo|tareas?)\s+pendientes?\b", r"\bquedan?\s+por\s+hacer\b"),
)

#: Phrases that wait on the person — a question, a choice, a credential, an
#: outside blocker — or only offer optional extras ("원하시면 …", "let me know
#: if…").  Any one of them and the turn ends as it would have.  A line that
#: ends in a question mark (or opens with ``¿``) counts too; :func:`waiting`
#: checks that apart, past trailing markdown.
WAITING: tuple[tuple[str, str], ...] = (
    ("ko", r"할까요|될까요|드릴까요|하시겠(?:어요|습니까)|원하시(?:면|는)|필요하시면"),
    ("ko", r"(?:알려|말씀해|확인해|선택해|결정해|승인해|제공해|정해)\s*주(?:세요|십시오|시면)"),
    ("ko", r"드릴\s*수\s*있습니다|기다리(?:고|는|겠)|막혀|권한이\s*(?:없|필요)"),
    ("ko", r"키가\s*필요|자격\s*증명|로그인이\s*필요"),
    ("en", r"\blet\s+me\s+know\b|\bwould\s+you\s+like\b|\bdo\s+you\s+want\b"),
    ("en", r"\b(?:should|shall)\s+i\b|\bif\s+you(?:'d|\s+would)?\s+(?:like|want|prefer)\b"),
    ("en", r"\bplease\s+(?:confirm|provide|choose|decide|share|send|approve|grant)\b"),
    ("en", r"\bi\s+can\s+also\b|\bhappy\s+to\b|\bwaiting\s+(?:for|on)\s+(?:you|your)\b"),
    ("en", r"\bblocked\s+(?:on|by)\b|\bcredentials?\b|\bapi\s+key\b|\byour\s+(?:call|decision)\b"),
    ("en", r"\bneed\s+(?:your|you\s+to|access|permission)\b"),
    ("ja", r"ますか|でしょうか|ください|ご希望|必要であれば|必要なら|お知らせ|認証情報"),
    ("ja", r"権限が(?:必要|ない)|待って(?:い|お)"),
    ("zh", r"请(?:确认|告诉|提供|选择|决定|批准)|是否需要|要不要|如果(?:您|你)?需要"),
    ("zh", r"需要(?:您|你)|等待(?:您|你)|权限|凭据|密钥"),
    ("es", r"\bav[ií]same\b|\bd[ií]me\b|\bconf[ií]rma(?:me)?\b"),
    ("es", r"\bsi\s+(?:quieres|lo\s+deseas|prefieres)\b"),
    ("es", r"\bnecesito\s+(?:que|acceso|tu|tus|permiso)\b|\bbloquead[oa]\b|\bcredenciales\b"),
    ("es", r"\bpuedo\s+tambi[eé]n\b|\besperando\s+(?:tu|su)\b"),
)

_FLAGS = re.IGNORECASE | re.MULTILINE
_MARKERS = [(m, re.compile(m.pattern, _FLAGS)) for m in MARKERS]
_WAITING = [re.compile(p, _FLAGS) for _lang, p in WAITING]
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.DOTALL)
#: A numbered, bulleted or checkbox item at the start of a line.
_LIST_LINE = re.compile(r"^[ \t]*(?:\d{1,2}[.)]|[-*•·]|\[ \]|[①-⑳])[ \t]+\S", re.MULTILINE)
#: "1. … 2. …" run together on one line.
_INLINE_LIST = re.compile(r"(?:^|\s)1[.)]\s+\S.{0,400}?\s2[.)]\s+\S", re.DOTALL)
#: How far after a heading its list may start.
_LIST_REACH = 400


def _plain(text: str) -> str:
    return _CODE.sub(" ", text or "")


def waiting(text: str) -> bool:
    """True when the answer waits on the person or only offers optional extras."""
    plain = _plain(text)
    for line in plain.splitlines():
        line = line.strip().rstrip("*_)\"'」』）").rstrip()
        if line.endswith(("?", "？")) or line.startswith("¿"):
            return True
    return any(pattern.search(plain) for pattern in _WAITING)


def remaining(text: str) -> Marker | None:
    """The first marker that says work remains, or ``None``."""
    plain = _plain(text)
    for marker, pattern in _MARKERS:
        for match in pattern.finditer(plain):
            if marker.kind == "deferral":
                return marker
            after = plain[match.end() : match.end() + _LIST_REACH]
            if _LIST_LINE.search(after) or _INLINE_LIST.search(after):
                return marker
    return None


def detect(text: str) -> bool:
    """True when the final answer lists work it could do now and waits on nobody."""
    return remaining(text) is not None and not waiting(text)


__all__ = ["INSTRUCTION", "MARKERS", "WAITING", "Marker", "detect", "remaining", "waiting"]

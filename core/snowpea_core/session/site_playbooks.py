"""Typed site playbooks (protocol 1.38.0): a site's procedure for one task.

A playbook is the "how" beside site memory's "where": ordered segments of
steps that name *actions* of the origin's entries, typed slots in place of
every value, and a fixed vocabulary of checks.  It holds no locator, no typed
text, no path, no date and no choice name; the host keeps those in RAM for
the turn.  The same content rule as site entries applies to every string
(:mod:`snowpea_core.session.site_memory`), and a step that names a login
field is refused outright.

Playbooks live in their own table, outside the 30-entry / 24 KiB budget of
page entries.  Core stores and returns them; it never puts them in a prompt.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from snowpea_core.session import site_memory as sm
from snowpea_core.session.site_memory import SiteMemoryError

MAX_PLAYBOOKS_PER_ORIGIN = 10
MAX_PLAYBOOK_BYTES = 16 * 1024
MAX_HISTORY = 10
#: A playbook nobody verified or changed for this long is dropped.
UNVERIFIED_DAYS = sm.UNVERIFIED_DAYS

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
#: A path on the origin: one slash at the start (not ``//host`` or ``/\\host``),
#: no query, no fragment.  Values belong in slots, not in a path.
_PATH = re.compile(r"/(?![/\\])[^\s?#\\]*")
#: The host's MAC over a playbook: opaque to core, stored as given.
_OPAQUE = re.compile(r"(?:[a-z0-9-]{3,16}:)?[A-Za-z0-9+/_-]{32,128}={0,2}")
#: Strings that are identifiers or dates, not prose: shape-checked instead of
#: run through the personal-data filter (a day is eight digits).
_SHAPED_KEYS = frozenset({"mac", "day", "lastVerified", "verifiedAt"})

#: What a restore brings back; the user's flags and the run history stay.
_STRUCTURAL = ("slots", "segments", "goal", "intents", "start", "skills", "pitfalls")

EDIT_OPS = (
    "rename",
    "alwaysAsk",
    "retire",
    "unretire",
    "restore",
    "acknowledge",
    "removeSegment",
    "removePitfall",
)


def new_id() -> str:
    return f"pb_{secrets.token_hex(8)}"


def _walk(value: Any, path: str):  # noqa: ANN202 - generator of (path, key, text)
    if isinstance(value, str):
        yield path, path.rsplit(".", 1)[-1].split("[", 1)[0], value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def check_playbook(body: dict[str, Any], login_actions: set[str]) -> None:
    """Refuse what the wire schema cannot express.

    ``body`` is a ``SitePlaybookInput`` dump; ``login_actions`` are the names of
    the origin's actions flagged ``loginField``.  Field paths start with
    ``playbook.``.
    """
    slots = {slot["name"] for slot in body.get("slots") or []}
    if len(slots) != len(body.get("slots") or []):
        raise SiteMemoryError("playbook.slots", "duplicate slot name")
    seen: set[str] = set()
    for si, segment in enumerate(body.get("segments") or []):
        at = f"playbook.segments[{si}]"
        if segment["name"] in seen:
            raise SiteMemoryError(f"{at}.name", f"duplicate segment name {segment['name']!r}")
        seen.add(segment["name"])
        page = segment.get("page")
        if page is not None and not _PATH.fullmatch(page):
            raise SiteMemoryError(f"{at}.page", "a query-free path on the origin, '/...'")
        for ti, step in enumerate(segment.get("steps") or []):
            where = f"{at}.steps[{ti}]"
            _check_refs(step, where, slots, login_actions)
            url = step.get("url")
            if url is not None and not _PATH.fullmatch(url):
                raise SiteMemoryError(f"{where}.url", "a query-free path on the origin, '/...'")
        for ci, check in enumerate(segment.get("checks") or []):
            _check_refs(check, f"{at}.checks[{ci}]", slots, login_actions)
    for hi, row in enumerate(body.get("history") or []):
        if not _DAY.fullmatch(row["day"]):
            raise SiteMemoryError(f"playbook.history[{hi}].day", "a YYYY-MM-DD day")
        if row["done"] > row["total"]:
            raise SiteMemoryError(f"playbook.history[{hi}].done", "more segments done than total")
    for pi, pitfall in enumerate(body.get("pitfalls") or []):
        day = pitfall.get("lastVerified")
        if day is not None and not _DAY.fullmatch(day):
            raise SiteMemoryError(f"playbook.pitfalls[{pi}].lastVerified", "a YYYY-MM-DD day")
    for key in ("verifiedAt",):
        if body.get(key) is not None and not _DAY.fullmatch(body[key]):
            raise SiteMemoryError(f"playbook.{key}", "a YYYY-MM-DD day")
    start = body.get("start")
    if start is not None and not (
        _PATH.fullmatch(start) or re.fullmatch(r"[a-z0-9-]{1,40}", start)
    ):
        raise SiteMemoryError("playbook.start", "a query-free path '/...' or a page type")
    mac = body.get("mac")
    if mac is not None and not _OPAQUE.fullmatch(mac):
        raise SiteMemoryError("playbook.mac", "must be a digest (hex or base64, 32+ characters)")
    for path, key, text in _walk(body, "playbook"):
        if key in _SHAPED_KEYS:
            continue
        try:
            sm.check_text(text, path)
        except SiteMemoryError:
            raise
    if sm.entry_size(body) > MAX_PLAYBOOK_BYTES:
        raise SiteMemoryError("playbook", f"larger than {MAX_PLAYBOOK_BYTES} bytes serialized")


def _check_refs(item: dict[str, Any], where: str, slots: set[str], logins: set[str]) -> None:
    slot = item.get("slot")
    if slot is not None and slot not in slots:
        raise SiteMemoryError(f"{where}.slot", f"no slot named {slot!r} in this playbook")
    for field in ("action", "via"):
        name = item.get(field)
        if name is not None and name in logins:
            raise SiteMemoryError(f"{where}.{field}", "a login field is never part of a playbook")


# ---------------------------------------------------------------------------
# storing
# ---------------------------------------------------------------------------


def _structure(body: dict[str, Any]) -> str:
    return json.dumps(
        [body.get("slots"), body.get("segments")], sort_keys=True, ensure_ascii=False
    )


def plain_body(playbook: dict[str, Any]) -> dict[str, Any]:
    """A stored playbook as an input body: no identity, no previous version."""
    skip = {"id", "origin", "version", "prev", "createdAt", "updatedAt"}
    return {k: copy.deepcopy(v) for k, v in playbook.items() if k not in skip}


def check_stored(stored: dict[str, Any]) -> None:
    """The stored row (with ``prev``) may use twice a playbook's budget, no more."""
    if sm.entry_size(stored) > 2 * MAX_PLAYBOOK_BYTES:
        raise SiteMemoryError(
            "playbook", f"larger than {2 * MAX_PLAYBOOK_BYTES} bytes with its previous version"
        )


def build_put(
    data: dict[str, Any],
    existing: dict[str, Any] | None,
    origin: str,
    now: datetime,
) -> dict[str, Any]:
    """The playbook to store for a ``site.playbook.put``.

    Core owns identity, ``version`` and ``prev``: a change of slots or segments
    keeps the stored body one deep as ``prev`` and counts the version up.
    Fields the host leaves out (``alwaysAsk``, ``acked``, an empty ``history``)
    keep what is stored, so a re-learn does not undo what the user set.
    """
    stamp = sm.iso(now)
    body = copy.deepcopy(data)
    body = {k: v for k, v in body.items() if v is not None or k in ("mac", "start")}
    if existing is None:
        body.setdefault("alwaysAsk", False)
        body.setdefault("acked", False)
        body["history"] = (body.get("history") or [])[-MAX_HISTORY:]
        return {
            **body,
            "id": new_id(),
            "origin": origin,
            "version": 1,
            "prev": None,
            "createdAt": stamp,
            "updatedAt": stamp,
        }
    for key in ("alwaysAsk", "acked"):
        body.setdefault(key, bool(existing.get(key)))
    body["history"] = (body.get("history") or existing.get("history") or [])[-MAX_HISTORY:]
    changed = _structure(body) != _structure(existing)
    return {
        **body,
        "id": existing["id"],
        "origin": origin,
        "version": int(existing["version"]) + (1 if changed else 0),
        "prev": plain_body(existing) if changed else existing.get("prev"),
        "createdAt": existing["createdAt"],
        "updatedAt": stamp,
    }


def append_run(
    playbook: dict[str, Any],
    result: str,
    done: int,
    total: int,
    rounds: int | None,
    now: datetime,
) -> dict[str, Any]:
    if done > total:
        raise SiteMemoryError("done", "more segments done than total")
    row: dict[str, Any] = {
        "day": now.astimezone(UTC).date().isoformat(),
        "result": result,
        "done": done,
        "total": total,
        "rounds": rounds,
    }
    playbook["history"] = [*(playbook.get("history") or []), row][-MAX_HISTORY:]
    if result == "ok":
        # Only a success keeps a playbook past retention: one that always
        # fails ages out.
        playbook["updatedAt"] = sm.iso(now)
        playbook["verifiedAt"] = now.astimezone(UTC).date().isoformat()
    return playbook


def apply_edit(
    playbook: dict[str, Any],
    siblings: list[dict[str, Any]],
    op: str,
    *,
    name: str | None,
    value: bool | None,
    segment: str | None,
    index: int | None,
    now: datetime,
) -> dict[str, Any]:
    """One user edit.  Raises :class:`SiteMemoryError` naming ``op`` or its argument."""
    stamp = sm.iso(now)
    if op == "rename":
        if not name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
            raise SiteMemoryError("name", "a name of lower-case letters, digits and '-'")
        sm.check_text(name, "name")
        if any(other["name"] == name for other in siblings):
            raise SiteMemoryError("name", f"{name!r} is already a playbook of this origin")
        playbook["name"] = name
    elif op == "alwaysAsk":
        if value is None:
            raise SiteMemoryError("value", "alwaysAsk needs true or false")
        playbook["alwaysAsk"] = value
    elif op == "retire":
        playbook["state"] = "retired"
    elif op == "unretire":
        ok = any(row.get("result") == "ok" for row in playbook.get("history") or [])
        playbook["state"] = "verified" if ok else "candidate"
    elif op == "acknowledge":
        playbook["acked"] = True
    elif op == "restore":
        prev = playbook.get("prev")
        if not prev:
            raise SiteMemoryError("op", "there is no previous version")
        current = plain_body(playbook)
        for key in _STRUCTURAL:
            if key in prev:
                playbook[key] = copy.deepcopy(prev[key])
        playbook["prev"] = current
        playbook["version"] = int(playbook["version"]) + 1
        playbook["state"] = "candidate"
    elif op == "removeSegment":
        names = [s["name"] for s in playbook.get("segments") or []]
        if segment not in names:
            raise SiteMemoryError("segment", f"no segment named {segment!r}")
        playbook["prev"] = plain_body(playbook)
        playbook["segments"] = [s for s in playbook["segments"] if s["name"] != segment]
        playbook["version"] = int(playbook["version"]) + 1
    elif op == "removePitfall":
        pitfalls = playbook.get("pitfalls") or []
        if index is None or not 0 <= index < len(pitfalls):
            raise SiteMemoryError("index", "no pitfall at that index")
        playbook["pitfalls"] = [p for i, p in enumerate(pitfalls) if i != index]
    else:  # pragma: no cover - the schema enumerates the ops
        raise SiteMemoryError("op", f"unknown op {op!r}")
    playbook["updatedAt"] = stamp
    return playbook


def retention_cutoff(now: datetime) -> str:
    return sm.iso(now - timedelta(days=UNVERIFIED_DAYS))


__all__ = [
    "EDIT_OPS",
    "MAX_HISTORY",
    "MAX_PLAYBOOKS_PER_ORIGIN",
    "MAX_PLAYBOOK_BYTES",
    "append_run",
    "apply_edit",
    "build_put",
    "check_playbook",
    "check_stored",
    "plain_body",
    "new_id",
    "retention_cutoff",
]

"""Skill discovery and installation (M6 contract §1).

``skill.search`` aggregates three sources, each labelled on every hit:

===================  ==========================================================
``claude-marketplace``  ``marketplace.json`` of each registered Claude Code
                        marketplace repo (``$SNOWPEA_HOME/marketplaces.json``,
                        seeded with the oh-my-claudecode repo).
``agentskills.io``      ``GET https://agentskills.io/api/v1/skills?q=<query>``
``hermes-hub``          ``GET https://hermes-hub.ai/api/v1/skills?q=<query>``
===================  ==========================================================

Everything network-facing goes through :data:`FETCHER`, so the tests swap one
object for a fixture reader instead of patching call sites.  A source that
fails (offline, 404, malformed JSON) contributes nothing and never fails the
search as a whole.

``skill.install(source)`` accepts a local path, a git URL, ``<marketplace>/<plugin>``
or the ``oh-my-claudecode`` shortcut, and lands the plugin in
``$SNOWPEA_HOME/plugins/<name>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("snowpea.skills.marketplace")

SOURCE_CLAUDE = "claude-marketplace"
SOURCE_AGENTSKILLS = "agentskills.io"
SOURCE_HERMES = "hermes-hub"
SOURCES: tuple[str, ...] = (SOURCE_CLAUDE, SOURCE_AGENTSKILLS, SOURCE_HERMES)

AGENTSKILLS_ENDPOINT = "https://agentskills.io/api/v1/skills"
HERMES_ENDPOINT = "https://hermes-hub.ai/api/v1/skills"

#: ``skill.install oh-my-claudecode`` means this repository.
SHORTCUTS: dict[str, str] = {
    "oh-my-claudecode": "https://github.com/Yeachan-Heo/oh-my-claudecode",
    "omc": "https://github.com/Yeachan-Heo/oh-my-claudecode",
}

DEFAULT_MARKETPLACES: list[dict[str, str]] = [
    {
        "name": "oh-my-claudecode",
        "url": (
            "https://raw.githubusercontent.com/Yeachan-Heo/oh-my-claudecode"
            "/main/.claude-plugin/marketplace.json"
        ),
        "repo": "https://github.com/Yeachan-Heo/oh-my-claudecode",
    }
]

#: Where the registered marketplaces live under ``$SNOWPEA_HOME``.
MARKETPLACES_FILE = "marketplaces.json"

CLONE_TIMEOUT_SEC = 300.0


class InstallError(RuntimeError):
    """The source could not be resolved, fetched or copied."""


@dataclass
class SkillHit:
    """One search result; ``install_spec`` is what ``skill.install`` accepts."""

    id: str
    name: str
    description: str
    source: str
    install_spec: str = ""


@dataclass
class SourceResult:
    """What one search source produced, and why it produced nothing."""

    hits: list[SkillHit] = field(default_factory=list)
    #: ``"<source>: <reason>"`` for every source that could not be reached.
    unavailable: list[str] = field(default_factory=list)


@dataclass
class SearchReport:
    """Every source merged.  An empty ``hits`` with a non-empty ``unavailable``
    means "offline", not "nothing matched", and callers must say so."""

    hits: list[SkillHit] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP layer (replaced by the tests)
# ---------------------------------------------------------------------------


class HttpFetcher:
    """Fetch JSON from a URL, or read it from a local path."""

    async def get_json(self, url: str) -> Any:
        if not url.startswith(("http://", "https://")):
            path = Path(url[7:] if url.startswith("file://") else url).expanduser()
            return json.loads(path.read_text(encoding="utf-8"))
        from snowpea_core.tools import http_util

        async with http_util.new_client(timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()


#: The fetcher every source uses; tests replace it with a fixture reader.
FETCHER: HttpFetcher = HttpFetcher()


def set_fetcher(fetcher: HttpFetcher) -> HttpFetcher:
    global FETCHER
    FETCHER = fetcher
    return FETCHER


# ---------------------------------------------------------------------------
# registered marketplaces
# ---------------------------------------------------------------------------


def marketplaces_path(home: Path | str) -> Path:
    return Path(home) / MARKETPLACES_FILE


def load_marketplaces(home: Path | str) -> list[dict[str, str]]:
    """Registered marketplaces; the defaults when the file is missing."""
    path = marketplaces_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(entry) for entry in DEFAULT_MARKETPLACES]
    entries = raw.get("marketplaces") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return [dict(entry) for entry in DEFAULT_MARKETPLACES]
    out: list[dict[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") and entry.get("url"):
            out.append({str(k): str(v) for k, v in entry.items()})
    return out or [dict(entry) for entry in DEFAULT_MARKETPLACES]


def save_marketplaces(home: Path | str, entries: list[dict[str, str]]) -> None:
    path = marketplaces_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"marketplaces": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _matches(query: str, *fields: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    return any(needle in (field or "").lower() for field in fields)


def _items(payload: Any) -> list[dict[str, Any]]:
    """Accept a bare list or the usual ``{skills|items|plugins|results: [...]}``."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("skills", "items", "plugins", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _hit(item: dict[str, Any], source: str, default_spec: str = "") -> SkillHit:
    name = str(item.get("name") or item.get("id") or "").strip()
    identifier = str(item.get("id") or name).strip()
    spec = str(
        item.get("installSpec")
        or item.get("install")
        or item.get("source")
        or item.get("repository")
        or item.get("url")
        or default_spec
        or identifier
    ).strip()
    return SkillHit(
        id=identifier or name,
        name=name or identifier,
        description=str(item.get("description") or item.get("summary") or "").strip(),
        source=source,
        install_spec=spec,
    )


async def search_claude_marketplaces(query: str, home: Path | str) -> SourceResult:
    """Plugins listed in the ``marketplace.json`` of every registered repo."""
    result = SourceResult()
    hits = result.hits
    for entry in load_marketplaces(home):
        url = entry.get("url", "")
        market = entry.get("name", "")
        try:
            payload = await FETCHER.get_json(url)
        except Exception as exc:  # noqa: BLE001 - one dead repo must not fail the search
            log.info("marketplace %s unavailable: %s", market or url, exc)
            result.unavailable.append(f"{SOURCE_CLAUDE} ({market or url}): {_reason(exc)}")
            continue
        for item in _items(payload):
            name = str(item.get("name") or "").strip()
            if not name or not _matches(
                query, name, str(item.get("description") or ""), *_keywords(item)
            ):
                continue
            hits.append(_hit(item, SOURCE_CLAUDE, default_spec=f"{market}/{name}"))
    return result


def _reason(exc: BaseException) -> str:
    """Short, readable cause for an unreachable source."""
    text = str(exc).strip()
    return text or type(exc).__name__


def _keywords(item: dict[str, Any]) -> list[str]:
    value = item.get("keywords") or item.get("tags") or []
    if isinstance(value, list):
        return [str(part) for part in value]
    return [str(value)]


async def _search_endpoint(query: str, endpoint: str, source: str) -> SourceResult:
    url = f"{endpoint}?q={query.strip().replace(' ', '+')}"
    try:
        payload = await FETCHER.get_json(url)
    except Exception as exc:  # noqa: BLE001 - offline is reported, never raised
        log.info("%s unavailable: %s", source, exc)
        return SourceResult(unavailable=[f"{source}: {_reason(exc)}"])
    return SourceResult(
        hits=[
            _hit(item, source)
            for item in _items(payload)
            if _matches(query, str(item.get("name") or ""), str(item.get("description") or ""))
        ]
    )


async def search_agentskills(query: str) -> SourceResult:
    return await _search_endpoint(query, AGENTSKILLS_ENDPOINT, SOURCE_AGENTSKILLS)


async def search_hermes_hub(query: str) -> SourceResult:
    return await _search_endpoint(query, HERMES_ENDPOINT, SOURCE_HERMES)


async def search(query: str, home: Path | str) -> SearchReport:
    """Every source, concurrently, in the documented order.

    A source that cannot be reached is named in ``unavailable`` instead of
    silently contributing nothing, so an offline run reads as "offline" rather
    than "no such skill".
    """
    results = await asyncio.gather(
        search_claude_marketplaces(query, home),
        search_agentskills(query),
        search_hermes_hub(query),
        _hosted(query),
    )
    report = SearchReport()
    for result in results:
        report.hits.extend(result.hits)
        report.unavailable.extend(result.unavailable)
    return report


async def _hosted(query: str) -> SourceResult:
    """The snowpea.ai registry; the v0.1 stub answers with nothing."""
    from snowpea_core.skills import registry_client

    try:
        items = await registry_client.CLIENT.search(query)
    except Exception as exc:  # noqa: BLE001
        return SourceResult(unavailable=[f"snowpea.ai: {_reason(exc)}"])
    return SourceResult(hits=[_hit(item, "snowpea.ai") for item in items])


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def is_git_url(source: str) -> bool:
    text = source.strip()
    return (
        text.startswith(("git@", "ssh://", "git://"))
        or text.endswith(".git")
        or (text.startswith(("http://", "https://")) and "/" in text[8:])
    )


def plugin_name_from(source: str) -> str:
    text = source.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.rsplit("/", 1)[-1] or "plugin"


async def _clone(url: str, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "1",
        url,
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(process.communicate(), CLONE_TIMEOUT_SEC)
    except TimeoutError as exc:
        process.kill()
        raise InstallError(f"git clone {url} timed out") from exc
    if process.returncode != 0:
        raise InstallError(f"git clone {url} failed: {(err or b'').decode('utf-8', 'replace')}")


def read_plugin_json(root: Path) -> dict[str, Any]:
    """``plugin.json`` from the root or from ``.claude-plugin/``."""
    for candidate in (root / "plugin.json", root / ".claude-plugin" / "plugin.json"):
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            return raw
    return {}


async def resolve_marketplace_entry(spec: str, home: Path | str) -> str | None:
    """``<marketplace>/<plugin>`` -> a git URL or a local path, or ``None``."""
    market_name, _, plugin = spec.partition("/")
    if not plugin:
        return None
    for entry in load_marketplaces(home):
        if entry.get("name") != market_name:
            continue
        try:
            payload = await FETCHER.get_json(entry.get("url", ""))
        except Exception as exc:  # noqa: BLE001
            raise InstallError(f"marketplace {market_name} is unavailable: {exc}") from exc
        for item in _items(payload):
            if str(item.get("name") or "") != plugin:
                continue
            source = str(item.get("source") or item.get("url") or "").strip()
            if not source:
                return entry.get("repo") or None
            if source.startswith(("http://", "https://", "git@")):
                return source
            base = entry.get("repo") or ""
            return f"{base}#{source}" if base else source
    return None


async def install(source: str, plugins_dir: Path, home: Path | str) -> Path:
    """Put the plugin named by ``source`` under ``plugins_dir``; return its path.

    Accepts a local path, a git URL, ``<marketplace>/<plugin>`` or a shortcut.
    """
    spec = source.strip()
    if not spec:
        raise InstallError("skill.install needs a source")

    local = Path(spec).expanduser()
    if local.exists() and local.is_dir():
        name = str(read_plugin_json(local).get("name") or local.name)
        target = plugins_dir / name
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(local, target)
        return target

    if spec in SHORTCUTS:
        spec = SHORTCUTS[spec]

    if is_git_url(spec):
        url, _, _subdir = spec.partition("#")
        target = plugins_dir / plugin_name_from(url)
        await _clone(url, target)
        return target

    if "/" in spec:
        resolved = await resolve_marketplace_entry(spec, home)
        if resolved:
            url, _, _subdir = resolved.partition("#")
            target = plugins_dir / spec.split("/", 1)[1]
            await _clone(url, target)
            return target

    raise InstallError(
        f"could not resolve {source!r} to a plugin: it is not a directory "
        f"(relative paths resolve against the daemon's own directory, {Path.cwd()}), "
        "not a git URL, not <marketplace>/<plugin> for a registered marketplace, "
        f"and not one of the shortcuts {', '.join(sorted(SHORTCUTS))}"
    )


__all__ = [
    "AGENTSKILLS_ENDPOINT",
    "CLONE_TIMEOUT_SEC",
    "DEFAULT_MARKETPLACES",
    "FETCHER",
    "HERMES_ENDPOINT",
    "HttpFetcher",
    "InstallError",
    "MARKETPLACES_FILE",
    "SHORTCUTS",
    "SOURCES",
    "SOURCE_AGENTSKILLS",
    "SOURCE_CLAUDE",
    "SOURCE_HERMES",
    "SearchReport",
    "SkillHit",
    "SourceResult",
    "install",
    "is_git_url",
    "load_marketplaces",
    "plugin_name_from",
    "read_plugin_json",
    "resolve_marketplace_entry",
    "save_marketplaces",
    "search",
    "search_agentskills",
    "search_claude_marketplaces",
    "search_hermes_hub",
    "set_fetcher",
]

"""Skill discovery and installation (M6 contract §1, CORE-registry-client §4b).

``skill.search`` aggregates two sources:

===================  ==========================================================
``claude-marketplace``  ``marketplace.json`` of each registered Claude Code
                        marketplace repo (``$SNOWPEA_HOME/marketplaces.json``,
                        seeded with the oh-my-claudecode repo).
the hosted registry     ``GET /v1/skills?sources=all&live=1`` on
                        ``registry_client.CLIENT`` — itself a federation of the
                        registry's own published skills plus mirrored/live
                        hits from other hubs (ClawHub, Claude marketplaces).
                        Each hit already carries its own ``source``/
                        ``sourceLabel``, so no adapter is needed per hub here.
===================  ==========================================================

``agentskills.io`` and ``hermes-hub`` adapters were removed: agentskills.io is
the Agent Skills *specification* site with no skill-listing API, and
hermes-hub.ai does not resolve. Both are now handled, disabled, on the
registry side (``GET /v1/sources``) instead of being guessed at here.

Everything network-facing for the local marketplace scan goes through
:data:`FETCHER`, so the tests swap one object for a fixture reader instead of
patching call sites. A source that fails (offline, 404, malformed JSON)
contributes nothing and never fails the search as a whole.

``skill.install(source)`` accepts a local path, a git URL,
``<marketplace>/<plugin>``, the ``oh-my-claudecode`` shortcut, or any
registry-issued install spec: ``registry:<id>`` and ``clawhub:<id>`` resolve
through the registry's generic download proxy; ``github:<owner>/<repo>[@plugin]``
goes straight to ``git clone`` and is never asked of the registry at all — a
Claude-marketplace item's registry ``id`` (e.g. ``claude-marketplaces:...``) is
not the same string as its ``github:`` installSpec, so a registry lookup for
the latter always 404s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("snowpea.skills.marketplace")

SOURCE_CLAUDE = "claude-marketplace"
#: Fallback label for a hosted-registry hit that carries neither ``source``
#: nor ``sourceLabel`` (should not happen against a real registry, but a test
#: double or a future API bump must not crash on it) and for the outer
#: "the whole registry is unreachable" failure.
SOURCE_REGISTRY = "snowpea-registry"
SOURCES: tuple[str, ...] = (SOURCE_CLAUDE, SOURCE_REGISTRY)

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
    #: Popularity, when the source publishes it.  Zero means "not published",
    #: never "nobody liked it", so callers must not print a bare 0.
    rating: float = 0.0
    downloads: int = 0


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
        rating=_number(item, "rating", "averageRating", "stars"),
        downloads=int(_number(item, "downloads", "downloadCount", "installs")),
    )


def _number(item: dict[str, Any], *keys: str) -> float:
    """The first of ``keys`` that holds a number; ``0.0`` when none does."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


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


async def search(query: str, home: Path | str) -> SearchReport:
    """Every source, concurrently, in the documented order.

    A source that cannot be reached is named in ``unavailable`` instead of
    silently contributing nothing, so an offline run reads as "offline" rather
    than "no such skill".
    """
    results = await asyncio.gather(
        search_claude_marketplaces(query, home),
        _hosted(query),
    )
    report = SearchReport()
    for result in results:
        report.hits.extend(result.hits)
        report.unavailable.extend(result.unavailable)
    return report


async def _hosted(query: str) -> SourceResult:
    """The hosted, federated registry; offline or unset contributes nothing.

    A registry that answers carries per-item ``source``/``sourceLabel`` (its
    own skills as well as anything it mirrors or fanned out to live), plus a
    per-hub ``unavailable`` list when a ``live=1`` fan-out partially failed
    (one dead hub costs its own results, not the whole search). A registry
    that cannot be reached at all is reported the same way every other source
    is: one line naming it, never an exception.
    """
    from snowpea_core.skills import registry_client

    client = registry_client.CLIENT
    try:
        if hasattr(client, "search_with_sources"):
            items, hub_failures = await client.search_with_sources(query)  # type: ignore[union-attr]
        else:
            items, hub_failures = await client.search(query), []
    except Exception as exc:  # noqa: BLE001
        return SourceResult(unavailable=[f"{SOURCE_REGISTRY}: {_reason(exc)}"])
    hits = [
        _hit(item, str(item.get("sourceLabel") or item.get("source") or SOURCE_REGISTRY))
        for item in items
    ]
    unavailable = [
        f"{hub.get('label') or hub.get('id') or SOURCE_REGISTRY}: "
        f"{hub.get('reason') or 'unavailable'}"
        for hub in hub_failures
        if isinstance(hub, dict)
    ]
    return SourceResult(hits=hits, unavailable=unavailable)


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

    Accepts a local path, a git URL, ``<marketplace>/<plugin>``, a shortcut, or
    any registry-issued install spec (``registry:<id>``, ``clawhub:<id>``,
    ``github:<owner>/<repo>[@plugin]``, ...).
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

    if spec.startswith("github:"):
        return await _install_github_spec(spec, plugins_dir, home)

    if _has_external_scheme(spec):
        return await _install_from_registry(spec, plugins_dir, home)

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


def _has_external_scheme(spec: str) -> bool:
    """True for ``registry:``/``clawhub:``/``github:``-shaped install specs.

    Excludes ``git@host:path`` (scp-style git) and ``http(s)://`` (already
    handled by :func:`is_git_url`) — anything whose "scheme" is not a bare
    alphanumeric token is left to the other resolvers.
    """
    if spec.startswith(("http://", "https://")):
        return False
    scheme, sep, rest = spec.partition(":")
    if not sep or not rest:
        return False
    return bool(scheme) and scheme.replace("-", "").isalnum()


#: A single extracted skill package larger than this is refused (path safety
#: caps the archive itself; this caps what it expands to, against zip bombs).
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024


async def _install_from_registry(spec: str, plugins_dir: Path, home: Path | str) -> Path:
    """``registry:<id>`` or ``clawhub:<id>`` — via the registry's generic
    download proxy.

    ``github:`` specs never reach here in the normal ``install()`` flow (see
    ``_install_github_spec``, called first): a Claude-marketplace item's
    registry ``id`` (``claude-marketplaces:...``) is not the same string as
    its ``github:`` installSpec, so asking the registry to download the
    installSpec 404s every time. The ``_fallback_install`` call below stays
    as a safety net in case a ``github:`` spec reaches here some other way —
    a 404 is treated exactly like a 501 (``RegistryNotFetchable`` is a
    ``RegistryError``), since either way the registry could not serve a zip.
    """
    from snowpea_core.skills import registry_client

    if spec.startswith("registry:") and not spec.split(":", 1)[1].strip():
        raise InstallError("registry: needs a skill id, e.g. registry:ralplan")

    client = registry_client.CLIENT
    if not hasattr(client, "download"):
        client = registry_client.HttpRegistryClient(registry_client.resolve_url())
    try:
        downloaded = await client.download(spec)  # type: ignore[union-attr]
    except registry_client.RegistryError as registry_exc:
        try:
            fallback = await _fallback_install(spec, plugins_dir, home)
        except Exception as clone_exc:  # noqa: BLE001 - reported alongside the registry failure
            raise InstallError(
                f"{spec}: registry: {registry_exc}; git clone: {clone_exc}"
            ) from clone_exc
        if fallback is not None:
            return fallback
        raise InstallError(f"{spec}: {registry_exc}") from registry_exc
    except Exception as exc:  # noqa: BLE001 - network/timeout errors, reported the same way
        raise InstallError(f"{spec}: {exc}") from exc

    name = _name_from_spec(spec)
    target = plugins_dir / name
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _extract_zip_safely(downloaded.content, target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


async def _fallback_install(spec: str, plugins_dir: Path, home: Path | str) -> Path | None:
    """A ``github:`` spec has a client-side fallback (git clone); anything
    else has none, so ``None`` tells the caller to surface the registry's
    reason as-is."""
    if not spec.startswith("github:"):
        return None
    return await _install_github_spec(spec, plugins_dir, home)


#: Where a monorepo marketplace's own manifest usually lives, checked in order.
GITHUB_MARKETPLACE_PATHS: tuple[str, ...] = (
    ".claude-plugin/marketplace.json",
    "marketplace.json",
)


async def _install_github_spec(spec: str, plugins_dir: Path, home: Path | str) -> Path:
    """``github:<owner>/<repo>[@plugin]`` — cloned directly, never through the
    registry (see the note on :func:`_install_from_registry`).

    With no ``@plugin`` the whole repo is cloned, named after the repo (the
    same simplification a plain git URL already makes). With ``@plugin``,
    only that plugin's directory is installed — its subdirectory is resolved
    from a locally registered marketplace pointed at the same repo, or
    failing that, straight from the repo's own ``marketplace.json`` /
    ``.claude-plugin/marketplace.json`` on GitHub.
    """
    rest = spec[len("github:") :]
    repo_spec, _, plugin = rest.partition("@")
    repo_spec = repo_spec.strip()
    if not repo_spec:
        raise InstallError(f"{spec}: expected github:<owner>/<repo>[@plugin]")
    url = f"https://github.com/{repo_spec}.git"

    if not plugin:
        target = plugins_dir / plugin_name_from(repo_spec)
        await _clone(url, target)
        return target

    subdir = await _github_plugin_subdir(repo_spec, plugin, home)
    target = plugins_dir / plugin
    await _clone_subdir(url, subdir, target)
    return target


async def _github_plugin_subdir(repo_spec: str, plugin: str, home: Path | str) -> str | None:
    """``plugin``'s subdirectory inside ``repo_spec``'s marketplace.json, or
    ``None`` when no entry names it (install the whole repo instead)."""
    repo_url = f"https://github.com/{repo_spec}"
    for entry in load_marketplaces(home):
        if str(entry.get("repo", "")).rstrip("/") != repo_url:
            continue
        subdir = await _find_plugin_source(entry.get("url", ""), plugin)
        if subdir is not None:
            return subdir
    for branch in ("main", "master"):
        for path in GITHUB_MARKETPLACE_PATHS:
            manifest_url = f"https://raw.githubusercontent.com/{repo_spec}/{branch}/{path}"
            subdir = await _find_plugin_source(manifest_url, plugin)
            if subdir is not None:
                return subdir
    return None


async def _find_plugin_source(manifest_url: str, plugin: str) -> str | None:
    """``plugin``'s ``source`` field in the ``marketplace.json`` at
    ``manifest_url``, or ``None`` when the manifest is unreachable or has no
    such entry — either way, the caller tries the next candidate."""
    if not manifest_url:
        return None
    try:
        payload = await FETCHER.get_json(manifest_url)
    except Exception:  # noqa: BLE001 - an unreachable manifest just means "try the next one"
        return None
    for item in _items(payload):
        if str(item.get("name") or "") == plugin:
            return str(item.get("source") or "").strip() or None
    return None


async def _clone_subdir(url: str, subdir: str | None, target: Path) -> None:
    """Clone ``url`` and install only ``subdir`` at ``target``; the whole repo
    when ``subdir`` is falsy — "no marketplace.json named it" still has to
    install *something* rather than nothing."""
    if not subdir:
        await _clone(url, target)
        return
    with tempfile.TemporaryDirectory(prefix="snowpea-clone-") as tmp:
        checkout = (Path(tmp) / "repo").resolve()
        await _clone(url, checkout)
        cleaned = subdir.strip("/").replace("\\", "/")
        if not cleaned or "\x00" in cleaned or any(part == ".." for part in cleaned.split("/")):
            raise InstallError(f"{url}#{subdir}: unsafe subdirectory")
        source_dir = (checkout / cleaned).resolve()
        if source_dir != checkout and checkout not in source_dir.parents:
            raise InstallError(f"{url}#{subdir}: unsafe subdirectory")
        if not source_dir.is_dir():
            raise InstallError(f"{url}#{subdir}: no such directory in the repo")
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target)


def _name_from_spec(spec: str) -> str:
    """A directory-safe plugin name for any install spec, prefixed or not.

    ``registry:ralplan`` -> ``ralplan``; ``clawhub:@cua/driver`` -> ``driver``
    (its last path segment); ``github:owner/repo@plugin`` -> ``plugin`` (what
    is actually being installed out of the monorepo), or ``repo`` with no
    ``@plugin`` suffix.
    """
    text = spec.split(":", 1)[1] if ":" in spec else spec
    text = text.strip()
    if not text:
        return spec.split(":", 1)[0] or "plugin"
    if "@" in text[1:]:
        _head, _, tail = text.rpartition("@")
        if tail:
            return plugin_name_from(tail)
    return plugin_name_from(text.lstrip("@"))


def _extract_zip_safely(content: bytes, target: Path) -> None:
    """Unzip ``content`` into ``target``, refusing path traversal and zip bombs.

    Entries are ``<id>/<path>`` (the registry's own convention); a single
    top-level directory wrapping everything is stripped so the result lands
    directly in ``target`` rather than ``target/<id>/...``.
    """
    import io
    import zipfile

    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        total = sum(info.file_size for info in infos)
        if total > MAX_EXTRACTED_BYTES:
            raise InstallError(
                f"package expands to {total} bytes, over the {MAX_EXTRACTED_BYTES} byte cap"
            )
        names = [info.filename for info in infos]
        prefix = _common_wrapper_dir(names)
        for info in infos:
            name = info.filename
            if prefix:
                name = name[len(prefix) :]
            path = _safe_join(target, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, path.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _common_wrapper_dir(names: list[str]) -> str:
    """The single top-level ``<dir>/`` every entry shares, or ``""``."""
    if not names:
        return ""
    firsts = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(firsts) == 1 and all("/" in name for name in names):
        return f"{next(iter(firsts))}/"
    return ""


def _safe_join(base: Path, relative: str) -> Path:
    """``base / relative``, refusing anything that would escape ``base``."""
    text = relative.replace("\\", "/")
    if not text or text.startswith("/") or "\x00" in text or any(
        part in ("..", "") for part in text.split("/")[:-1]
    ) or text.split("/")[-1] in ("..",):
        raise InstallError(f"unsafe path in package: {relative!r}")
    resolved = (base / text).resolve()
    if resolved != base.resolve() and base.resolve() not in resolved.parents:
        raise InstallError(f"unsafe path in package: {relative!r}")
    return resolved


__all__ = [
    "CLONE_TIMEOUT_SEC",
    "DEFAULT_MARKETPLACES",
    "FETCHER",
    "HttpFetcher",
    "InstallError",
    "MARKETPLACES_FILE",
    "MAX_EXTRACTED_BYTES",
    "SHORTCUTS",
    "SOURCES",
    "SOURCE_CLAUDE",
    "SOURCE_REGISTRY",
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
    "search_claude_marketplaces",
    "set_fetcher",
]

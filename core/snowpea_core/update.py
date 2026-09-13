"""Update checking and self-upgrade (CORE-update).

Two pieces, both driven from ``system.checkUpdate`` / ``system.update``:

* :func:`check_update` asks PyPI (or the repository's git tags) what the newest
  release is, caches the answer in ``$SNOWPEA_HOME/update-check.json`` for
  :data:`CACHE_TTL_SEC`, and **never raises** — a network failure comes back as
  ``available=False`` plus an ``error`` string.
* :func:`start_update` runs the upgrade in a detached subprocess whose output
  lands in ``$SNOWPEA_HOME/logs/update.log``.  ``uv tool install`` is the
  normal path; when ``uv`` is not on PATH the recorded install method in
  ``$SNOWPEA_HOME/install.json`` (written by the installers) decides what to
  run instead, and if nothing is known the caller is handed the command to run
  by hand rather than a guess.

The daemon never restarts itself in place: an upgrade rewrites the files on
disk while the old code keeps running, so ``system.info`` reports
``restartRequired`` and ``system.restart`` shuts the daemon down for the next
launch to pick the new version up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from snowpea_core import __version__
from snowpea_core.config.paths import Paths, resolve_home
from snowpea_core.config.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.update")

#: The repository releases are cut from.
REPO = "Wafour-Developer/snowpea-agent"
REPO_URL = f"https://github.com/{REPO}"
TAGS_URL = f"https://api.github.com/repos/{REPO}/tags"
COMMITS_URL = f"https://api.github.com/repos/{REPO}/commits"
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{REPO}"
#: Distribution name on PyPI; ``system.update`` hands this to the installer.
PACKAGE = "snowpea-agent"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"

#: Every outbound request in this module gets the same short budget.
HTTP_TIMEOUT_SEC = 5.0
#: How long a cached answer is reused before the network is asked again.
CACHE_TTL_SEC = 24 * 60 * 60
#: How long the upgrade subprocess may run before the watcher gives up on it.
#: The subprocess is detached, so giving up only stops the reporting.
UPDATE_TIMEOUT_SEC = 30 * 60
#: How often the watcher looks at the upgrade subprocess.
POLL_INTERVAL_SEC = 0.1

#: Set to ``0``/``false``/``no`` to keep the daemon from checking on its own.
CHECK_ENV = "SNOWPEA_UPDATE_CHECK"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_GIT_SOURCE_RE = re.compile(r"^(?:git\+)?(?P<url>[^@]+?)(?:@(?P<ref>[^#]+))?(?:#.*)?$")
_TRACKED_BRANCHES = {"main", "master"}


# ---------------------------------------------------------------------------
# install provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitInstall:
    """A git branch install we can compare against GitHub safely."""

    branch: str
    installed_revision: str | None


def _is_sha(text: str | None) -> bool:
    return bool(text and _SHA_RE.match(text.strip()))


def _same_revision(left: str | None, right: str | None) -> bool:
    """True when two git SHAs are certainly the same revision.

    Only a full 40-character match counts.  Prefix matching used to be enough,
    but a 7-character abbreviation — GitHub's own default — can collide, and a
    false match skipped the ancestry ``compare`` call entirely, reporting a
    genuinely newer commit as "already on this" (CORE-fixes-v017 R7).  Being
    unsure is cheap: the caller just runs the comparison, which answers
    ``identical`` for a revision that really is the same.
    """
    if not (_is_sha(left) and _is_sha(right)):
        return False
    a, b = str(left).strip().lower(), str(right).strip().lower()
    return len(a) == len(b) == 40 and a == b


def _clean_git_url(url: str) -> str:
    cleaned = url.strip()
    if cleaned.startswith("git+"):
        cleaned = cleaned[4:]
    cleaned = cleaned.removesuffix(".git").rstrip("/").lower()
    return cleaned


def _split_git_source(source: str) -> tuple[str, str | None] | None:
    match = _GIT_SOURCE_RE.match(source.split(" @ ", 1)[-1].strip())
    if match is None:
        return None
    url = _clean_git_url(match.group("url"))
    if url != REPO_URL.lower():
        return None
    ref = match.group("ref")
    return url, ref.strip() if ref else None


def _read_direct_url_json() -> dict[str, Any]:
    """PEP 610 provenance written by pip/uv for direct URL installs."""
    try:
        text = metadata.distribution(PACKAGE).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - broken metadata must not break startup
        log.debug("could not read direct_url.json", exc_info=True)
        return {}
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def git_install_provenance(paths: Paths) -> GitInstall | None:
    """Return the current same-repo branch install, if one is known.

    A branch name alone is not enough to announce an update; we also need the
    installed commit from direct_url.json before comparing it with GitHub.
    """
    install = read_install_json(paths)
    source = str(install.get("source") or "")
    direct = _read_direct_url_json()
    direct_ref: str | None = None
    direct_revision: str | None = None
    url = direct.get("url")
    if isinstance(url, str) and _clean_git_url(url) == REPO_URL.lower():
        vcs_info = direct.get("vcs_info")
        if isinstance(vcs_info, dict) and str(vcs_info.get("vcs", "")).lower() == "git":
            requested = vcs_info.get("requested_revision")
            commit = vcs_info.get("commit_id")
            if isinstance(requested, str):
                direct_ref = requested.strip() or None
            if isinstance(commit, str) and _is_sha(commit):
                direct_revision = commit.strip()

    # A no-ref git install follows the repository's default branch (main).
    split = _split_git_source(source) if source else None
    recorded_branch = (split[1] or "main") if split is not None else None
    branch: str | None
    if direct_revision is not None and direct_ref is None:
        branch = "main"
    elif direct_ref and _is_sha(direct_ref) and recorded_branch in _TRACKED_BRANCHES:
        branch = recorded_branch
    else:
        branch = direct_ref or recorded_branch
    if branch not in _TRACKED_BRANCHES:
        return None
    return GitInstall(branch=branch, installed_revision=direct_revision)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``"v1.2.3"`` → ``(1, 2, 3)``; anything else → ``None``."""
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer(latest: str, current: str) -> bool:
    """True when ``latest`` is a strictly higher release than ``current``."""
    left, right = parse_version(latest), parse_version(current)
    if left is None or right is None:
        return False
    return left > right


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_seconds(checked_at: str) -> float | None:
    """Seconds since ``checked_at``, or ``None`` when it cannot be read."""
    try:
        stamp = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds()


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def read_cache(paths: Paths, *, ttl: float = CACHE_TTL_SEC) -> dict[str, Any] | None:
    """The cached answer while it is younger than ``ttl``, else ``None``."""
    try:
        payload = json.loads(paths.update_check_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    age = _age_seconds(str(payload.get("checkedAt", "")))
    if age is None or age > ttl or age < -ttl:
        return None
    return payload


def read_cache_any(paths: Paths) -> dict[str, Any] | None:
    """The cached answer whatever its age; used by ``snowpea --version``."""
    try:
        payload = json.loads(paths.update_check_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_cache(paths: Paths, payload: dict[str, Any]) -> None:
    """Persist one answer; a write failure is never fatal."""
    try:
        paths.ensure()
        target = paths.update_check_json
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError:  # pragma: no cover - disk failure
        log.debug("could not write %s", paths.update_check_json, exc_info=True)


def version_suffix(home: Path | str | None = None) -> str:
    """``" (update available: v0.1.2)"`` for ``snowpea --version``, or ``""``.

    Reads only the cache, so it never touches the network and never blocks.
    """
    payload = read_cache_any(Paths(home=resolve_home(home)))
    if not payload or not payload.get("available"):
        return ""
    latest = str(payload.get("latest") or "")
    if payload.get("installKey"):
        install = git_install_provenance(Paths(home=resolve_home(home)))
        if (
            install
            and payload["installKey"]
            == f"git:{install.branch}:{install.installed_revision}:{__version__}"
        ):
            return f" (update available: v{latest.lstrip('v')})"
        return ""
    if not latest or not is_newer(latest, __version__):
        return ""
    return f" (update available: v{latest.lstrip('v')})"


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


def _new_client() -> httpx.AsyncClient:
    """The HTTP client both lookups use; patched wholesale in tests."""
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SEC,
        follow_redirects=True,
        headers={"User-Agent": f"snowpea-agent/{__version__}"},
    )


async def _pypi_latest(client: httpx.AsyncClient) -> str | None:
    """Newest version on PyPI, or ``None`` when the project is not there."""
    response = await client.get(PYPI_URL)
    if response.status_code != 200:
        return None
    payload = response.json()
    version = (payload.get("info") or {}).get("version") if isinstance(payload, dict) else None
    return str(version) if version else None


async def _git_latest(client: httpx.AsyncClient) -> tuple[str | None, str | None]:
    """``(version, tag)`` of the highest ``vX.Y.Z`` tag in the repository."""
    response = await client.get(TAGS_URL, headers={"Accept": "application/vnd.github+json"})
    if response.status_code != 200:
        return None, None
    payload = response.json()
    if not isinstance(payload, list):
        return None, None
    best: tuple[tuple[int, int, int], str] | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        parsed = parse_version(name)
        if parsed is not None and (best is None or parsed > best[0]):
            best = (parsed, name)
    if best is None:
        return None, None
    return ".".join(str(part) for part in best[0]), best[1]


async def _git_version_at(client: httpx.AsyncClient, revision: str) -> str | None:
    """Package version declared by an exact repository revision."""
    response = await client.get(f"{RAW_VERSION_URL}/{revision}/core/snowpea_core/__init__.py")
    if response.status_code != 200:
        return None
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', response.text, re.MULTILINE)
    return match.group(1) if match and parse_version(match.group(1)) else None


def _answer(
    *,
    latest: str,
    channel: str,
    source: str,
    release_url: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    current = __version__
    return {
        "current": current,
        "latest": latest,
        "available": error is None and is_newer(latest, current),
        "channel": channel,
        "source": source,
        "releaseUrl": release_url,
        "checkedAt": _utc_now(),
        "cached": False,
        "error": error,
    }


async def _check_git_branch(paths: Paths, install: GitInstall, force: bool) -> dict[str, Any]:
    revision = install.installed_revision
    source = f"git+{REPO_URL}@{install.branch}"
    if not revision:
        return _answer(
            latest=__version__,
            channel="git",
            source=source,
            error="cannot determine the installed git revision; reinstall from main",
        )
    key = f"git:{install.branch}:{revision}:{__version__}"
    cached = None if force else read_cache(paths)
    # A *negative* answer is safe to cache: nothing will be installed from it.
    # A positive one is not — a force-push inside the 24h window would have it
    # offering a commit whose ancestry was never re-checked (R8).
    if (
        cached
        and cached.get("installKey") == key
        and not cached.get("error")
        and not cached.get("available")
    ):
        return {**cached, "cached": True}
    try:
        async with _new_client() as client:
            response = await client.get(f"{COMMITS_URL}/{install.branch}")
            if response.status_code != 200:
                raise ValueError(f"GitHub update check returned HTTP {response.status_code}")
            latest = response.json().get("sha")
            if not isinstance(latest, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", latest):
                raise ValueError("GitHub returned an invalid commit")
            available = False
            if not _same_revision(revision, latest):
                compared = await client.get(
                    f"https://api.github.com/repos/{REPO}/compare/{revision}...{latest}"
                )
                if compared.status_code != 200:
                    raise ValueError("could not verify update ancestry")
                status = compared.json().get("status")
                if status not in ("ahead", "behind", "identical"):
                    raise ValueError("branch history diverged; refusing an automatic replacement")
                available = status == "ahead"
        # A branch install tracks commits, but the prompt should still name a
        # newer release version when the branch contains one.  Otherwise an
        # old daemon misleadingly offers "v0.1.2+newsha from v0.1.2+oldsha"
        # even when that new commit is the v0.1.3 release.  Tag discovery is
        # presentation-only: a failure must not invalidate the ancestry check.
        display_version = __version__
        try:
            commit_version = await _git_version_at(client, latest)
            if commit_version and is_newer(commit_version, display_version):
                display_version = commit_version
        except Exception:  # noqa: BLE001 - commit updates work without tags
            log.debug("could not read the version at the update commit", exc_info=True)
        answer = _answer(
            latest=f"{display_version}+{latest[:8]}",
            channel="git",
            source=f"git+{REPO_URL}@{latest}",
            release_url=f"{REPO_URL}/commit/{latest}",
        )
        answer.update(
            current=f"{__version__}+{revision[:8]}",
            available=available,
            installKey=key,
            trackingSource=source,
        )
        write_cache(paths, answer)
        return answer
    except Exception as exc:  # noqa: BLE001 - never break startup on a network error
        return _answer(latest=__version__, channel="git", source=source, error=str(exc))


async def check_update(paths: Paths, settings: Settings, *, force: bool = False) -> dict[str, Any]:
    """Answer ``system.checkUpdate``. Never raises.

    ``force`` skips the cache.  The configured channel (``updates.channel``)
    pins the source; ``"auto"`` prefers PyPI and uses the repository's git tags
    when the package is not published there.
    """
    current = __version__
    configured = str(getattr(settings.updates, "channel", "auto") or "auto").lower()

    install = git_install_provenance(paths) if configured != "pypi" else None
    if install is not None:
        return await _check_git_branch(paths, install, force)

    if not force:
        cached = read_cache(paths)
        if (
            cached is not None
            and not cached.get("installKey")
            and cached.get("configured", "auto") == configured
        ):
            answer = dict(cached)
            answer["current"] = current
            answer["cached"] = True
            answer["available"] = not answer.get("error") and is_newer(
                str(answer.get("latest") or current), current
            )
            return answer

    try:
        async with _new_client() as client:
            latest: str | None = None
            tag: str | None = None
            channel = "git"
            if configured != "git":
                latest = await _pypi_latest(client)
                if latest is not None:
                    channel = "pypi"
            if latest is None:
                channel = "git"
                latest, tag = await _git_latest(client)
    except Exception as exc:  # noqa: BLE001 - a check must never break a caller
        log.debug("update check failed", exc_info=True)
        return _answer(
            latest=current,
            channel="pypi" if configured == "pypi" else "git",
            source=PACKAGE if configured == "pypi" else f"git+{REPO_URL}",
            error=f"{type(exc).__name__}: {exc}",
        )

    if latest is None:
        return _answer(
            latest=current,
            channel="git",
            source=f"git+{REPO_URL}",
            error="no published release could be found",
        )

    if channel == "pypi":
        answer = _answer(
            latest=latest,
            channel="pypi",
            source=PACKAGE,
            release_url=f"https://pypi.org/project/{PACKAGE}/{latest}/",
        )
    else:
        ref = tag or f"v{latest}"
        answer = _answer(
            latest=latest,
            channel="git",
            source=f"git+{REPO_URL}@{ref}",
            release_url=f"{REPO_URL}/releases/tag/{ref}",
        )
    answer["configured"] = configured
    write_cache(paths, answer)
    return answer


def check_enabled(settings: Settings) -> bool:
    """Whether the daemon may check on its own; the env var wins."""
    override = os.environ.get(CHECK_ENV)
    if override is not None:
        return override.strip().lower() not in ("0", "false", "no", "off")
    return bool(getattr(settings.updates, "check", True))


# ---------------------------------------------------------------------------
# the upgrade
# ---------------------------------------------------------------------------


def read_install_json(paths: Paths) -> dict[str, Any]:
    """``$SNOWPEA_HOME/install.json``, or ``{}`` when it is absent or corrupt."""
    try:
        payload = json.loads(paths.install_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


#: The image-downscaling extra (Pillow).  Carried through every upgrade path,
#: or an upgrade would silently drop the downscaling the installers put there.
IMAGES_EXTRA = "images"


def with_images(source: str) -> str:
    """``source`` with the ``images`` extra requested.

    A URL needs the PEP 508 direct-reference form (``name[extra] @ url``); a
    path or a plain package name takes the extra on the end.  A source that
    already asks for an extra is left alone.
    """
    if "[" in source:
        return source
    if "://" in source:
        return f"{PACKAGE}[{IMAGES_EXTRA}] @ {source}"
    return f"{source}[{IMAGES_EXTRA}]"


def write_install_json(paths: Paths, method: str, source: str) -> None:
    """Record how snowpea was installed (used by the installers and tests)."""
    paths.ensure()
    payload = {"method": method, "source": source, "time": _utc_now()}
    paths.install_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def update_command(paths: Paths, source: str) -> list[str] | None:
    """The argv that upgrades snowpea to ``source``, or ``None`` if unknown.

    ``uv tool install`` is how the installers put snowpea on the machine, so it
    is what the upgrade uses whenever ``uv`` is on PATH.  Otherwise the method
    the installer recorded in ``install.json`` decides, and an unrecognised or
    missing record yields ``None`` so the caller can print a manual command
    instead of running something that was never used to install anything.
    """
    method = str(read_install_json(paths).get("method", "")).strip().lower()
    requirement = with_images(source)
    if shutil.which("uv"):
        return ["uv", "tool", "install", "--force", "--reinstall", requirement]
    if method == "pipx" and shutil.which("pipx"):
        return ["pipx", "install", "--force", requirement]
    if method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", requirement]
    return None


def manual_command(source: str) -> str:
    """What to tell the user to run when nothing can be run for them."""
    return f"uv tool install --force --reinstall {with_images(source)!r}"


def start_update(paths: Paths, command: list[str]) -> subprocess.Popen[bytes]:
    """Spawn the upgrade detached, appending its output to ``logs/update.log``.

    The parent's copy of the log handle is closed as soon as the child has its
    own dup of it, so a long upgrade never keeps a file object alive here.
    """
    paths.ensure()
    handle = paths.update_log.open("ab")
    try:
        handle.write(f"\n=== {_utc_now()} {shlex.join(command)}\n".encode())
        handle.flush()
        return subprocess.Popen(  # noqa: S603 - argv is built above, never user text
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(paths.home),
        )
    finally:
        handle.close()


async def notify_progress(core: Core, phase: str, message: str) -> None:
    """Broadcast ``system.updateProgress`` to every connected client."""
    hub = getattr(core, "hub", None)
    if hub is None:  # pragma: no cover - a core without a hub is a test double
        return
    try:
        await hub.notify("system.updateProgress", {"phase": phase, "message": message})
    except Exception:  # noqa: BLE001 - a dead socket must not break the upgrade
        log.debug("could not broadcast updateProgress", exc_info=True)


async def wait_for_exit(
    process: subprocess.Popen[bytes],
    *,
    timeout: float = UPDATE_TIMEOUT_SEC,
    poll_interval: float = POLL_INTERVAL_SEC,
) -> int | None:
    """Poll ``process`` until it exits; ``None`` when ``timeout`` runs out.

    Deliberately a poll rather than ``asyncio.to_thread(process.wait)``: a
    thread parked in ``wait()`` cannot be cancelled, so an installer that hung
    would keep a non-daemon executor thread alive for the life of the process
    (and block interpreter shutdown with it). Polling makes the wait ordinary
    cancellable async work, which is what ``Daemon.stop`` relies on.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        code = process.poll()
        if code is not None:
            return code
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


async def watch_update(
    core: Core,
    process: subprocess.Popen[bytes],
    latest: str,
    tracking_source: str | None = None,
    version_reader: Callable[[], str | None] | None = None,
) -> None:
    """Wait for the upgrade to finish, then announce ``done`` or ``failed``.

    ``version_reader`` exists so callers — tests above all — can say what the
    freshly installed executable reports without shelling out to whatever
    ``snowpea`` happens to be on the host's PATH (CORE-fixes-v017 F2).
    """
    code = await wait_for_exit(process)
    if code is None:
        await notify_progress(
            core,
            "failed",
            f"the upgrade did not finish within {UPDATE_TIMEOUT_SEC:.0f}s; "
            f"see {core.paths.update_log}",
        )
        return
    if code == 0:
        if tracking_source:
            try:
                method = str(read_install_json(core.paths).get("method") or "uv")
                write_install_json(core.paths, method, tracking_source)
            except OSError:
                log.warning("could not persist update tracking source", exc_info=True)
        core.restart_required = True
        read_version = version_reader or installed_cli_version
        installed = read_version() or latest.lstrip("v")
        await notify_progress(core, "done", f"updated to v{installed}")
    else:
        await notify_progress(
            core, "failed", f"the upgrade exited with status {code}; see {core.paths.update_log}"
        )


def installed_cli_version() -> str | None:
    """Ask the freshly replaced executable what version is now on disk."""
    executable = shutil.which("snowpea")
    if not executable:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - executable came from PATH
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\bsnowpea\s+v?([0-9]+\.[0-9]+\.[0-9]+)", completed.stdout)
    return match.group(1) if completed.returncode == 0 and match else None


async def background_check(core: Core) -> None:
    """The daemon's own daily check; swallows everything it can go wrong with."""
    try:
        answer = await check_update(core.paths, core.settings)
    except Exception:  # noqa: BLE001 - defensive: check_update already catches
        log.debug("background update check failed", exc_info=True)
        return
    if answer.get("available"):
        log.info("snowpea v%s is available (running v%s)", answer["latest"], answer["current"])


__all__ = [
    "CACHE_TTL_SEC",
    "CHECK_ENV",
    "HTTP_TIMEOUT_SEC",
    "IMAGES_EXTRA",
    "PACKAGE",
    "POLL_INTERVAL_SEC",
    "PYPI_URL",
    "REPO",
    "REPO_URL",
    "TAGS_URL",
    "UPDATE_TIMEOUT_SEC",
    "background_check",
    "check_enabled",
    "check_update",
    "is_newer",
    "manual_command",
    "notify_progress",
    "parse_version",
    "read_cache",
    "read_cache_any",
    "read_install_json",
    "with_images",
    "start_update",
    "update_command",
    "version_suffix",
    "wait_for_exit",
    "watch_update",
    "write_cache",
    "write_install_json",
]

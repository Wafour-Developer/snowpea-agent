"""Finding, checking, loading and unloading extensions (M16 Phase 3).

``$SNOWPEA_HOME/extensions/<name>/plugin.py`` is an extension; ``plugin.json``
beside it describes it.  ``settings.extensions.<name>`` holds ``enabled``
(default true: the user put the folder there) and ``config``.

Loading one is: read and check ``plugin.json`` → check ``engines.snowpea``
against :data:`~snowpea_core.extensions.api.API_VERSION` → validate the config
against ``configSchema`` → import ``plugin.py`` → ``apply(ctx)``.  Any step
that fails leaves the extension in state ``error`` (or ``incompatible``) with
``Type: message (at file:line)``, logs the traceback to the daemon log, and
leaves nothing registered.

The folder is imported as a package of its own (``snowpea_ext_<name>_<hash>``),
always from source, so ``from . import helpers`` works, two extensions never
share a module, and a reload runs the code as it is on disk now.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.extensions.api import API_VERSION, PluginContext, describe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.extensions")

FOLDER = "extensions"
ENTRY = "plugin.py"
MANIFEST = "plugin.json"
#: Folder names an extension may have (it is also its name).
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
#: ``plugin.json`` fields and the JSON type each must have.
MANIFEST_FIELDS: dict[str, type] = {
    "name": str,
    "title": str,
    "description": str,
    "version": str,
    "engines": dict,
    "configSchema": dict,
}
#: Seconds ``apply`` may take (an async one is awaited).
APPLY_TIMEOUT = 30.0


def enabled_by_env() -> bool:
    """``SNOWPEA_EXTENSIONS=0`` starts the daemon without extensions."""
    return os.environ.get("SNOWPEA_EXTENSIONS", "1").strip().lower() not in ("0", "false", "off")


@dataclass
class Extension:
    name: str
    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    #: ``loaded`` | ``unloaded`` | ``disabled`` | ``error`` | ``incompatible``
    state: str = "unloaded"
    error: str = ""
    #: What is wrong with the folder itself (bad ``plugin.json``, bad name).
    problem: str = ""
    context: PluginContext | None = None

    def info(self) -> dict[str, Any]:
        schema = self.manifest.get("configSchema")
        return {
            "name": self.name,
            "path": str(self.path),
            "description": str(self.manifest.get("description") or ""),
            "version": str(self.manifest.get("version") or ""),
            "enabled": self.enabled,
            "state": self.state,
            "error": self.error or None,
            "effects": self.context.effects if self.context is not None else 0,
            "configSchema": schema if isinstance(schema, dict) else None,
        }


def _settings_for(core: Core, name: str) -> dict[str, Any]:
    block = getattr(core.settings, "extensions", None)
    if isinstance(block, dict):
        entry = block.get(name)
        return entry if isinstance(entry, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# plugin.json, engines.snowpea, configSchema
# ---------------------------------------------------------------------------


def read_manifest(folder: Path) -> tuple[dict[str, Any], str]:
    """``(manifest, problem)``; ``problem`` is ``""`` when the file is fine or absent."""
    path = folder / MANIFEST
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return {}, f"{MANIFEST}: cannot read it: {exc}"
    except json.JSONDecodeError as exc:
        return {}, f"{MANIFEST}:{exc.lineno}: not valid JSON: {exc.msg} (column {exc.colno})"
    if not isinstance(data, dict):
        return {}, f"{MANIFEST}: must be a JSON object"
    for key, kind in MANIFEST_FIELDS.items():
        if key in data and not isinstance(data[key], kind):
            wanted = "a string" if kind is str else "an object"
            return data, f"{MANIFEST}: {key} must be {wanted}"
    unknown = sorted(set(data) - set(MANIFEST_FIELDS))
    if unknown:
        log.warning("extension %s: %s has unknown fields %s", folder.name, MANIFEST, unknown)
    if "name" in data and data["name"] != folder.name:
        return data, (
            f"{MANIFEST}: name {data['name']!r} differs from the folder name "
            f"{folder.name!r}; rename one so they match"
        )
    engines = data.get("engines") or {}
    if "snowpea" in engines and not isinstance(engines["snowpea"], str):
        return data, f"{MANIFEST}: engines.snowpea must be a string such as \"^1\""
    schema = data.get("configSchema")
    if isinstance(schema, dict):
        import jsonschema

        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except jsonschema.SchemaError as exc:
            path_text = "/".join(str(part) for part in exc.absolute_path) or "(root)"
            return data, (
                f"{MANIFEST}: configSchema is not a valid JSON Schema at {path_text}: "
                f"{exc.message}"
            )
    return data, ""


_RANGE_RE = re.compile(r"^(\^|~|>=|=)?\s*v?(\d+)(?:\.(?:\d+|x|\*))*$")


def compatible(manifest: dict[str, Any], api: int = API_VERSION) -> tuple[bool, str]:
    """Whether ``engines.snowpea`` admits API major ``api``; ``(ok, why not)``.

    ``"^1"``, ``"~1.2"``, ``"1"``, ``"1.x"`` and ``"=1"`` mean major 1 exactly;
    ``">=1"`` means 1 or later.  No ``engines.snowpea`` means any.
    """
    wanted = str((manifest.get("engines") or {}).get("snowpea") or "").strip()
    if not wanted:
        return True, ""
    match = _RANGE_RE.match(wanted)
    if match is None:
        return False, (
            f'engines.snowpea {wanted!r} is not understood; use "^{api}" '
            "(this daemon's extension API major)"
        )
    operator, major = match.group(1) or "", int(match.group(2))
    ok = major <= api if operator == ">=" else major == api
    if ok:
        return True, ""
    return False, f"written for extension API {wanted}; this daemon provides API {api}"


def validate_config(manifest: dict[str, Any], config: Any) -> tuple[dict[str, Any], str]:
    """``(config with schema defaults, problem)`` for ``settings.extensions.<n>.config``."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return {}, f"config must be an object, got {type(config).__name__}"
    schema = manifest.get("configSchema")
    config = dict(config)
    if not isinstance(schema, dict):
        return config, ""
    for key, spec in (schema.get("properties") or {}).items():
        if key not in config and isinstance(spec, dict) and "default" in spec:
            config[key] = spec["default"]
    import jsonschema

    validator = jsonschema.validators.validator_for(schema)(schema)
    error = jsonschema.exceptions.best_match(validator.iter_errors(config))
    if error is None:
        return config, ""
    location = "config" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    )
    return config, f"{location}: {error.message}"


# ---------------------------------------------------------------------------
# importing an extension folder as a package
# ---------------------------------------------------------------------------


def package_name(name: str) -> str:
    """The import name of an extension's folder; distinct for distinct names."""
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"snowpea_ext_{re.sub(r'[^0-9A-Za-z_]', '_', name)}_{digest}"


class _FreshLoader(importlib.machinery.SourceFileLoader):
    """Compiles from source every time: a reload never runs a stale ``.pyc``."""

    def get_code(self, fullname: str) -> types.CodeType:
        source = self.get_data(self.path)
        return compile(source, self.path, "exec", dont_inherit=True)


class _ExtensionFinder(importlib.abc.MetaPathFinder):
    """Resolves ``snowpea_ext_…`` packages to their extension folders."""

    def __init__(self) -> None:
        self.roots: dict[str, Path] = {}

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        top, _, rest = fullname.partition(".")
        root = self.roots.get(top)
        if root is None or not rest:
            return None
        base = root.joinpath(*rest.split("."))
        init = base / "__init__.py"
        if init.is_file():
            return importlib.util.spec_from_file_location(
                fullname,
                init,
                loader=_FreshLoader(fullname, str(init)),
                submodule_search_locations=[str(base)],
            )
        module = base.with_name(base.name + ".py")
        if module.is_file():
            return importlib.util.spec_from_file_location(
                fullname, module, loader=_FreshLoader(fullname, str(module))
            )
        return None


_FINDER = _ExtensionFinder()


def _install_finder() -> None:
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)


def _import_entry(name: str, folder: Path) -> types.ModuleType:
    """Import ``folder/plugin.py`` as ``<package>.plugin``, fresh from disk."""
    _install_finder()
    package = package_name(name)
    _purge_modules(package)
    importlib.invalidate_caches()
    _FINDER.roots[package] = folder
    spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
    spec.submodule_search_locations = [str(folder)]
    pkg = importlib.util.module_from_spec(spec)
    pkg.__path__ = [str(folder)]
    sys.modules[package] = pkg
    return importlib.import_module(f"{package}.plugin")


def _purge_modules(package: str) -> None:
    for key in [key for key in sys.modules if key == package or key.startswith(package + ".")]:
        del sys.modules[key]


def _forget_package(name: str) -> None:
    package = package_name(name)
    _purge_modules(package)
    _FINDER.roots.pop(package, None)


# ---------------------------------------------------------------------------
# `snowpea extension new`
# ---------------------------------------------------------------------------

TEMPLATE_DIR = Path(__file__).parent / "template"


def render_template(name: str) -> dict[str, str]:
    """``{file name: text}`` of a new extension called ``name``."""
    ident = re.sub(r"[^0-9A-Za-z_]", "_", name)
    if not ident[:1].isalpha():
        ident = f"x{ident}"
    out: dict[str, str] = {}
    for source in sorted(TEMPLATE_DIR.glob("*.tmpl")):
        text = source.read_text(encoding="utf-8")
        out[source.name[: -len(".tmpl")]] = text.replace("{{name}}", name).replace(
            "{{ident}}", ident
        )
    return out


def scaffold(home: Path | str, name: str) -> Path:
    """Write a new extension into ``<home>/extensions/<name>``; returns the folder.

    Raises ``ValueError`` for an invalid name and ``FileExistsError`` when the
    folder is already there.
    """
    if not NAME_RE.match(name):
        raise ValueError(
            f"{name!r} is not a valid extension name (letters, digits, '-' and '_', up to 64)"
        )
    folder = Path(home) / FOLDER / name
    if folder.exists():
        raise FileExistsError(f"{folder} already exists")
    folder.mkdir(parents=True)
    from snowpea_core.util.atomic import write_atomic

    for file_name, text in render_template(name).items():
        write_atomic(folder / file_name, text, mode=0o644)
    return folder


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------


class ExtensionManager:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.extensions: dict[str, Extension] = {}

    @property
    def root(self) -> Path:
        return Path(self.core.paths.home) / FOLDER

    def discover(self) -> list[Extension]:
        """Re-read the folder.  Records of loaded extensions are kept; see :meth:`rescan`."""
        found: dict[str, Extension] = {}
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                if not folder.is_dir() or not (folder / ENTRY).is_file():
                    continue
                name = folder.name
                manifest, problem = read_manifest(folder)
                if not problem and not NAME_RE.match(name):
                    problem = (
                        f"folder name {name!r} is not a valid extension name "
                        "(letters, digits, '-' and '_', up to 64)"
                    )
                ext = self.extensions.get(name) or Extension(name=name, path=folder)
                ext.manifest, ext.problem = manifest, problem
                found[name] = ext
        for name, ext in self.extensions.items():
            if name not in found and ext.context is not None:
                # Gone from disk while loaded: kept until rescan() unloads it.
                found[name] = ext
                ext.problem = "its folder was removed"
        self.extensions = found
        for ext in found.values():
            ext.enabled = bool(_settings_for(self.core, ext.name).get("enabled", True))
            if ext.context is None and ext.state in ("loaded", "unloaded", "disabled"):
                ext.state = "unloaded" if ext.enabled else "disabled"
        return list(found.values())

    async def rescan(self) -> list[Extension]:
        """:meth:`discover`, then unload what disappeared from disk."""
        self.discover()
        for name in [n for n, e in self.extensions.items() if not (e.path / ENTRY).is_file()]:
            await self.unload(name)
            self.extensions.pop(name, None)
        return list(self.extensions.values())

    async def load_all(self) -> list[dict[str, Any]]:
        for ext in await self.rescan():
            if ext.enabled and ext.context is None:
                await self.load(ext.name)
        return [ext.info() for ext in self.extensions.values()]

    async def load(self, name: str) -> Extension:
        ext = self.extensions[name]
        if ext.context is not None:
            return ext
        if ext.problem:
            return self._failed(ext, "error", ext.problem)
        ok, why = compatible(ext.manifest)
        if not ok:
            return self._failed(ext, "incompatible", why)
        config, problem = validate_config(
            ext.manifest, _settings_for(self.core, name).get("config")
        )
        if problem:
            return self._failed(ext, "error", f"settings.extensions.{name}.{problem}")
        ctx = PluginContext(self.core, name, config, path=ext.path)
        try:
            module = _import_entry(name, ext.path)
            apply = getattr(module, "apply", None)
            if not callable(apply):
                raise TypeError(f"{ENTRY} defines no apply(ctx) function")
            try:
                outcome = apply(ctx)
                if inspect.isawaitable(outcome):
                    task = asyncio.ensure_future(outcome)
                    done, _ = await asyncio.wait({task}, timeout=APPLY_TIMEOUT)
                    if not done:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise TimeoutError(f"apply(ctx) did not finish within {APPLY_TIMEOUT:g}s")
                    task.result()
            except BaseException:
                await self._dispose(ctx, name)  # leave nothing half-registered
                raise
        except Exception as exc:  # noqa: BLE001 - one extension must not stop the daemon
            _forget_package(name)
            return self._failed(ext, "error", describe(exc, ext.path), exc)
        ext.context, ext.state, ext.error = ctx, "loaded", ""
        if ctx.effects:
            log.info("extension %s loaded: %s", name, ", ".join(ctx.registrations))
        else:
            log.warning("extension %s loaded but apply(ctx) registered nothing", name)
        await self._changed(ctx)
        return ext

    def _failed(
        self, ext: Extension, state: str, error: str, exc: BaseException | None = None
    ) -> Extension:
        ext.state, ext.error, ext.context = state, error, None
        log.warning(
            "extension %s %s: %s",
            ext.name,
            "is incompatible" if state == "incompatible" else "failed to load",
            error,
            exc_info=exc,
        )
        return ext

    async def _dispose(self, ctx: PluginContext, name: str) -> None:
        problems = await ctx.dispose()
        if problems:
            log.warning("extension %s unloaded with errors: %s", name, "; ".join(problems))

    async def unload(self, name: str) -> Extension:
        ext = self.extensions[name]
        ctx = ext.context
        if ctx is not None:
            ext.context = None
            await self._dispose(ctx, name)
            log.info("extension %s unloaded", name)
            await self._changed(ctx)
        _forget_package(name)
        ext.state = "unloaded" if ext.enabled else "disabled"
        ext.error = ""
        return ext

    async def unload_all(self) -> None:
        """Unload everything, newest extension first (daemon shutdown)."""
        for name in reversed(list(self.extensions)):
            try:
                await self.unload(name)
            except Exception:  # noqa: BLE001 - keep going
                log.warning("could not unload extension %s", name, exc_info=True)

    async def reload(self, name: str) -> Extension:
        """Unload, re-read the folder and the settings, and load again if enabled."""
        await self.unload(name)
        await self.rescan()
        ext = self.extensions.get(name)
        if ext is None:
            raise KeyError(name)
        if not ext.enabled:
            ext.state = "disabled"
            return ext
        return await self.load(name)

    async def set_enabled(self, name: str, enabled: bool) -> Extension:
        ext = self.extensions[name]
        ext.enabled = enabled
        block = getattr(self.core.settings, "extensions", None)
        if isinstance(block, dict):
            block.setdefault(name, {})["enabled"] = enabled
        if enabled:
            return await self.load(name)
        return await self.unload(name)

    async def apply_settings(self) -> None:
        """``settings.extensions`` changed on disk: follow ``enabled`` and ``config``."""
        if not enabled_by_env():
            return  # SNOWPEA_EXTENSIONS=0: only an explicit extension.* call loads one
        await self.rescan()
        for name, ext in list(self.extensions.items()):
            config, _problem = validate_config(
                ext.manifest, _settings_for(self.core, name).get("config")
            )
            if not ext.enabled:
                if ext.context is not None:
                    await self.unload(name)
                ext.state = "disabled"
            elif ext.context is None:
                if ext.state != "incompatible":
                    await self.load(name)
            elif ext.context.config != config:
                await self.reload(name)

    async def _changed(self, ctx: PluginContext) -> None:
        """A tool, section or command came or went: drop caches, tell clients."""
        try:
            from snowpea_core.agent.agent import invalidate_tools

            invalidate_tools()
        except Exception:  # noqa: BLE001
            pass
        if not ctx._commands_changed:
            return
        hub = getattr(self.core, "hub", None)
        if hub is None:
            return
        payload = {
            "commands": [info.model_dump(mode="json") for info in self.core.commands.list()],
            "reason": "extension",
        }
        try:
            await hub.notify("commands.changed", payload)
        except Exception:  # noqa: BLE001 - a dead socket must not fail the load
            log.debug("could not announce commands.changed", exc_info=True)


__all__ = [
    "APPLY_TIMEOUT",
    "ENTRY",
    "FOLDER",
    "MANIFEST",
    "Extension",
    "ExtensionManager",
    "compatible",
    "enabled_by_env",
    "package_name",
    "read_manifest",
    "render_template",
    "scaffold",
    "validate_config",
]

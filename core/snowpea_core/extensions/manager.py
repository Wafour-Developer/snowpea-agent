"""Finding, loading, enabling and unloading extensions (M16 Phase 3).

``$SNOWPEA_HOME/extensions/<name>/plugin.py`` is a plugin; ``plugin.json``
beside it describes it.  ``settings.extensions.<name>`` holds ``enabled``
(default true: the user put the folder there) and ``config``.  An extension whose
``apply`` raises is reported with its error and leaves nothing behind.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.extensions.api import API_VERSION, PluginContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.extensions")

FOLDER = "extensions"
ENTRY = "plugin.py"
MANIFEST = "plugin.json"


@dataclass
class Extension:
    name: str
    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    #: ``loaded`` | ``disabled`` | ``error`` | ``incompatible``
    state: str = "disabled"
    error: str = ""
    context: PluginContext | None = None

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "description": str(self.manifest.get("description") or ""),
            "version": str(self.manifest.get("version") or ""),
            "enabled": self.enabled,
            "state": self.state,
            "error": self.error or None,
            "effects": self.context.effects if self.context is not None else 0,
            "configSchema": self.manifest.get("configSchema"),
        }


def _settings_for(core: Core, name: str) -> dict[str, Any]:
    block = getattr(core.settings, "extensions", None)
    if isinstance(block, dict):
        entry = block.get(name)
        return entry if isinstance(entry, dict) else {}
    return {}


def _compatible(manifest: dict[str, Any]) -> bool:
    wanted = str((manifest.get("engines") or {}).get("snowpea") or "").strip()
    if not wanted:
        return True
    digits = "".join(ch for ch in wanted.lstrip("^~>=v ") if ch.isdigit() or ch == ".")
    try:
        return int(digits.split(".")[0]) <= API_VERSION
    except (ValueError, IndexError):
        return True


class ExtensionManager:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.extensions: dict[str, Extension] = {}

    @property
    def root(self) -> Path:
        return Path(self.core.paths.home) / FOLDER

    def discover(self) -> list[Extension]:
        found: dict[str, Extension] = {}
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                if not (folder / ENTRY).is_file():
                    continue
                manifest: dict[str, Any] = {}
                if (folder / MANIFEST).is_file():
                    try:
                        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        manifest = {}
                name = str(manifest.get("name") or folder.name)
                existing = self.extensions.get(name)
                if existing is not None:
                    existing.manifest = manifest
                    found[name] = existing
                    continue
                found[name] = Extension(name=name, path=folder, manifest=manifest)
        # A folder that went away is unloaded.
        for name, ext in self.extensions.items():
            if name not in found and ext.context is not None:
                ext.context.dispose()
        self.extensions = found
        for ext in found.values():
            ext.enabled = bool(_settings_for(self.core, ext.name).get("enabled", True))
        return list(found.values())

    def load_all(self) -> list[dict[str, Any]]:
        for ext in self.discover():
            if ext.enabled and ext.context is None:
                self.load(ext.name)
            elif not ext.enabled:
                ext.state = "disabled"
        return [ext.info() for ext in self.extensions.values()]

    def load(self, name: str) -> Extension:
        ext = self.extensions[name]
        if ext.context is not None:
            return ext
        if not _compatible(ext.manifest):
            ext.state, ext.error = "incompatible", f"needs extension API {ext.manifest['engines']}"
            return ext
        module_name = f"snowpea_extension_{name.replace('-', '_')}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, ext.path / ENTRY)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {ext.path / ENTRY}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            apply = getattr(module, "apply", None)
            if not callable(apply):
                raise TypeError("plugin.py defines no apply(ctx)")
            ctx = PluginContext(self.core, name, _settings_for(self.core, name).get("config"))
            try:
                apply(ctx)
            except Exception:
                ctx.dispose()  # leave nothing half-registered
                raise
        except Exception as exc:  # noqa: BLE001 - one plugin must not stop the daemon
            sys.modules.pop(module_name, None)
            ext.state, ext.error, ext.context = "error", f"{type(exc).__name__}: {exc}", None
            log.warning("extension %s failed to load: %s", name, ext.error)
            return ext
        ext.context, ext.state, ext.error = ctx, "loaded", ""
        log.info("extension %s loaded (%d registrations)", name, ctx.effects)
        self._prompt_changed()
        return ext

    def unload(self, name: str) -> Extension:
        ext = self.extensions[name]
        if ext.context is not None:
            problems = ext.context.dispose()
            ext.context = None
            if problems:
                log.warning("extension %s unloaded with errors: %s", name, "; ".join(problems))
            self._prompt_changed()
        sys.modules.pop(f"snowpea_extension_{name.replace('-', '_')}", None)
        ext.state = "disabled" if not ext.enabled else "unloaded"
        return ext

    def reload(self, name: str) -> Extension:
        self.unload(name)
        self.discover()
        return self.load(name)

    def set_enabled(self, name: str, enabled: bool) -> Extension:
        ext = self.extensions[name]
        ext.enabled = enabled
        block = getattr(self.core.settings, "extensions", None)
        if isinstance(block, dict):
            block.setdefault(name, {})["enabled"] = enabled
        return self.load(name) if enabled else self.unload(name)

    def _prompt_changed(self) -> None:
        """A section or a tool came or went: the cached prompt pieces are stale."""
        try:
            from snowpea_core.agent.agent import invalidate_tools

            invalidate_tools()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["ENTRY", "FOLDER", "MANIFEST", "Extension", "ExtensionManager"]

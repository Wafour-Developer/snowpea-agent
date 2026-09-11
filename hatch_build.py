"""Hatchling build hook: fold the Ink TUI bundle into the wheel.

M8 contract §1.  The user's machine never runs ``npm install`` (plan §2.5 C1),
so ``tui/dist/snowpea-tui.js`` — produced by ``npm -w tui run build`` — has to
travel inside the wheel as package data at
``snowpea_core/tui/dist/snowpea-tui.js``.  ``cli/main.py`` looks there second,
after ``SNOWPEA_TUI_ENTRY`` and before the repo checkout.

The bundle is a build output, so it is git-ignored; the ``artifacts`` entries in
``pyproject.toml`` are what let the copy survive the VCS filter.

A missing bundle fails the build, because a wheel without it would install a
``snowpea`` that cannot start.  ``SNOWPEA_SKIP_TUI=1`` opts out, for the
Python-only builds CI does before the Node toolchain is set up.

The work lives in :func:`copy_bundle`, which knows nothing about hatchling, so
``tests/test_installer.py`` can exercise it without the build environment.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

try:  # pragma: no cover - present in the build env, absent in the test env
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # pragma: no cover
    BuildHookInterface = object  # type: ignore[assignment, misc]

#: Where esbuild writes, relative to the repo root.
SOURCE_BUNDLE = Path("tui") / "dist" / "snowpea-tui.js"

#: Where the wheel carries it, relative to the repo root.
TARGET_DIR = Path("core") / "snowpea_core" / "tui" / "dist"

#: Where it lands inside the wheel.
WHEEL_PATH = "snowpea_core/tui/dist/snowpea-tui.js"

#: Copied alongside the bundle when present (debugging aid, not required).
OPTIONAL_SIBLINGS = ("snowpea-tui.js.map",)


class MissingBundle(FileNotFoundError):
    """``tui/dist/snowpea-tui.js`` is not there and the build may not skip it."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def copy_bundle(root: Path | str, *, skip: bool | None = None) -> Path | None:
    """Copy the bundle into the package; return where it landed, or ``None``.

    ``None`` means the build goes ahead without a TUI, which only happens under
    ``SNOWPEA_SKIP_TUI=1``.  Raises :class:`MissingBundle` when the bundle is
    absent and skipping was not asked for.
    """
    root = Path(root)
    source = root / SOURCE_BUNDLE
    target_dir = root / TARGET_DIR
    target = target_dir / SOURCE_BUNDLE.name
    if skip is None:
        skip = _truthy(os.environ.get("SNOWPEA_SKIP_TUI"))

    if not source.is_file():
        if target.is_file():
            # Building a wheel out of an sdist: the sdist already carries the
            # copy this hook made when the sdist itself was built.
            return target
        if skip:
            return None
        raise MissingBundle(
            f"the TUI bundle is missing: {source}\n"
            "Build it first:  npm ci && npm -w tui run build\n"
            "Or set SNOWPEA_SKIP_TUI=1 to build a wheel without the TUI."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    for sibling in OPTIONAL_SIBLINGS:
        extra = source.with_name(sibling)
        if extra.is_file():
            shutil.copy2(extra, target_dir / sibling)
    return target


class SnowpeaTuiBuildHook(BuildHookInterface):  # type: ignore[misc, valid-type]
    """Copy ``tui/dist/snowpea-tui.js`` into the package before the wheel is zipped."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        target = copy_bundle(self.root)
        if target is None:
            self.app.display_warning(
                f"SNOWPEA_SKIP_TUI=1: building without {SOURCE_BUNDLE.as_posix()}; "
                "the resulting wheel cannot launch the TUI"
            )
            return
        # Belt and braces for the wheel: `artifacts` should already cover this.
        if self.target_name == "wheel":
            build_data.setdefault("force_include", {})[str(target)] = WHEEL_PATH


__all__ = [
    "OPTIONAL_SIBLINGS",
    "SOURCE_BUNDLE",
    "TARGET_DIR",
    "WHEEL_PATH",
    "MissingBundle",
    "SnowpeaTuiBuildHook",
    "copy_bundle",
]

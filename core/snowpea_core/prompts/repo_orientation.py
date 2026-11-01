"""A short orientation block for the environment: layout, scripts, check commands.

Every coding session used to spend its first three to six rounds on ``ls`` and
``cat`` finding the same things.  This reads them once, with the environment
block, so it is rebuilt only on compaction and the cached prefix stays put.
A snapshot like the git status next to it: it does not update.

Only a directory that looks like a project (a ``.git`` or a manifest) is
described, so a session opened in a home directory never walks it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from pathlib import Path

log = logging.getLogger(__name__)

#: Tree entries shown in all, and children listed under one directory.
TREE_MAX_ENTRIES = 60
TREE_CHILDREN_MAX = 12
SCRIPTS_MAX = 10
COMMAND_CLIP = 70

MANIFESTS = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "Makefile",
    "Cargo.toml",
    "go.mod",
)

_SKIP_DIRS = frozenset(
    {
        "node_modules", "dist", "build", "out", "target", "coverage", "venv",
        "env", "site-packages", "__pycache__", "vendor", "bower_components",
    }
)
_KEEP_HIDDEN = frozenset({".github", ".gitlab", ".storybook", ".vscode"})
_TEST_SCRIPTS = ("test", "tests", "test:unit", "unit")
_LINT_SCRIPTS = ("lint", "eslint", "check:lint")
_TYPE_SCRIPTS = ("typecheck", "type-check", "tsc", "types", "check:types")


def _add(commands: dict[str, list[str]], kind: str, command: str) -> None:
    found = commands.setdefault(kind, [])
    if command not in found and len(found) < 2:
        found.append(command)


def _is_project(root: Path) -> bool:
    return (root / ".git").exists() or any((root / name).is_file() for name in MANIFESTS)


def _skipped(name: str, is_dir: bool) -> bool:
    if name.startswith("."):
        return not (is_dir and name in _KEEP_HIDDEN)
    return is_dir and (name in _SKIP_DIRS or name.endswith(".egg-info"))


def _children(path: Path) -> list[tuple[str, bool]]:
    """``(name, is_dir)`` of a directory's visible entries, directories first."""
    try:
        with os.scandir(path) as entries:
            rows = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    except OSError:
        return []
    rows = [row for row in rows if not _skipped(*row)]
    return sorted(rows, key=lambda row: (not row[1], row[0].lower()))


def tree(root: Path) -> str:
    """A depth-2 layout: one line per top-level directory, files on one line."""
    top = _children(root)
    files = [name for name, is_dir in top if not is_dir]
    lines: list[str] = []
    budget = TREE_MAX_ENTRIES
    for name, is_dir in top:
        if not is_dir:
            continue
        if budget <= 0:
            lines.append("…")
            break
        inner = _children(root / name)
        shown = inner[: min(TREE_CHILDREN_MAX, max(budget, 1))]
        budget -= 1 + len(shown)
        names = ", ".join(child + ("/" if child_dir else "") for child, child_dir in shown)
        more = f", … (+{len(inner) - len(shown)})" if len(inner) > len(shown) else ""
        lines.append(f"{name}/ {names}{more}".rstrip())
    if files:
        shown_files = files[:TREE_CHILDREN_MAX]
        extra = f", … (+{len(files) - len(shown_files)})" if len(files) > len(shown_files) else ""
        lines.append("files: " + ", ".join(shown_files) + extra)
    return "\n".join(f"  {_clip_line(line)}" for line in lines)


def _clip_line(text: str, limit: int = 170) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= COMMAND_CLIP else text[: COMMAND_CLIP - 1] + "…"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _package_manager(root: Path) -> str:
    for lock, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"),
                          ("bun.lock", "bun")):
        if (root / lock).is_file():
            return manager
    return "npm"


def _run(manager: str, script: str) -> str:
    if script == "test" and manager in ("npm", "yarn", "pnpm", "bun"):
        return f"{manager} test"
    return f"{manager} run {script}"


def _pick(scripts: dict, names: tuple[str, ...]) -> str | None:
    return next((name for name in names if name in scripts), None)


def _node(root: Path, scripts_out: list[str], commands: dict[str, list[str]]) -> None:
    data = _read_json(root / "package.json")
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        return
    manager = _package_manager(root)
    rows = [f"{name}: {_clip(str(body))}" for name, body in list(scripts.items())[:SCRIPTS_MAX]]
    more = f" (+{len(scripts) - SCRIPTS_MAX} more)" if len(scripts) > SCRIPTS_MAX else ""
    scripts_out.append(f"package.json ({manager}) — " + "; ".join(rows) + more)
    kinds = (("test", _TEST_SCRIPTS), ("lint", _LINT_SCRIPTS), ("typecheck", _TYPE_SCRIPTS))
    for kind, names in kinds:
        name = _pick(scripts, names)
        if name:
            _add(commands, kind, _run(manager, name))


def _python(root: Path, scripts_out: list[str], commands: dict[str, list[str]]) -> None:
    data = _read_toml(root / "pyproject.toml")
    tools = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    entry = project.get("scripts") if isinstance(project.get("scripts"), dict) else {}
    parts = []
    if tools:
        parts.append("tools " + ", ".join(sorted(tools)[:8]))
    if entry:
        parts.append("scripts " + ", ".join(sorted(entry)[:6]))
    if parts:
        scripts_out.append("pyproject.toml — " + "; ".join(parts))
    has_tests = (root / "tests").is_dir() or (root / "test").is_dir()
    if "pytest" in tools or has_tests and (data or (root / "setup.py").is_file()):
        _add(commands, "test", "pytest")
    if "ruff" in tools:
        _add(commands, "lint", "ruff check")
    elif "flake8" in tools or (root / ".flake8").is_file():
        _add(commands, "lint", "flake8")
    for name in ("mypy", "pyright"):
        configured = (root / f"{name}.ini").is_file() or (root / "pyrightconfig.json").is_file()
        if name in tools or configured:
            _add(commands, "typecheck", name if name == "mypy" else "pyright")
            break


def _make(root: Path, scripts_out: list[str], commands: dict[str, list[str]]) -> None:
    try:
        text = (root / "Makefile").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    targets = list(dict.fromkeys(re.findall(r"^([A-Za-z][\w.-]*)\s*:(?!=)", text, re.MULTILINE)))
    targets = [name for name in targets if not name.startswith(".")]
    if not targets:
        return
    scripts_out.append("Makefile — " + ", ".join(targets[:SCRIPTS_MAX]))
    for kind, names in (("test", ("test", "tests", "check")), ("lint", ("lint",)),
                        ("typecheck", ("typecheck", "types"))):
        name = _pick(dict.fromkeys(targets), names)
        if name:
            _add(commands, kind, f"make {name}")


def _others(root: Path, scripts_out: list[str], commands: dict[str, list[str]]) -> None:
    if (root / "Cargo.toml").is_file():
        scripts_out.append("Cargo.toml")
        _add(commands, "test", "cargo test")
        _add(commands, "lint", "cargo clippy")
    if (root / "go.mod").is_file():
        scripts_out.append("go.mod")
        _add(commands, "test", "go test ./...")
        _add(commands, "lint", "go vet ./...")


def orientation(workdir: Path | str) -> str:
    """The orientation block for ``workdir``, or ``""`` when it is not a project."""
    try:
        root = Path(workdir)
        if not root.is_dir() or not _is_project(root):
            return ""
        scripts: list[str] = []
        commands: dict[str, list[str]] = {}
        for reader in (_node, _python, _make, _others):
            reader(root, scripts, commands)
        lines = ["", "Repository (a snapshot from when this session started)"]
        layout = tree(root)
        if layout:
            lines.append("- Layout (two levels):")
            lines.append(layout)
        for row in scripts:
            lines.append(f"- Manifest: {_clip_line(row, 400)}")
        if commands:
            lines.append(
                "- Check commands (detected, not verified): "
                + "; ".join(
                    f"{kind} " + " or ".join(f"`{one}`" for one in found)
                    for kind, found in commands.items()
                )
            )
        return "\n".join(lines) if len(lines) > 2 else ""
    except Exception:  # noqa: BLE001 - orientation is a nicety, never a failed prompt
        log.debug("could not describe %s", workdir, exc_info=True)
        return ""


__all__ = ["orientation", "tree"]

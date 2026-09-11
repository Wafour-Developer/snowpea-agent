"""What the machine already has, so the wizard can suggest instead of ask.

Three things get detected:

* **vendor keys in the environment** — ``ANTHROPIC_API_KEY`` and friends, read
  straight off each preset's ``env_keys``.  A detected key is never copied into
  ``settings.json``; the registry already falls back to the environment, so the
  wizard only says so.
* **an existing config to import from** — ``~/.hermes/config.yaml`` or a Claude
  Code ``settings.json``.  Only their existence is reported; importing them is
  a later story.
* **runtimes** — ``uv`` and ``node`` on ``PATH``, because the TUI needs node.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from snowpea_core.providers.presets import PRESETS

#: Config files worth mentioning, in the order they are checked.
IMPORT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("hermes", "~/.hermes/config.yaml"),
    ("claude-code", "~/.claude/settings.json"),
    ("claude-code", "~/.config/claude/settings.json"),
)


@dataclass
class Detected:
    """The findings, ready to be turned into hint lines."""

    #: vendor -> the environment variable that holds its key.
    env_keys: dict[str, str] = field(default_factory=dict)
    #: ``[("hermes", Path("~/.hermes/config.yaml"))]``.
    imports: list[tuple[str, Path]] = field(default_factory=list)
    has_uv: bool = False
    has_node: bool = False

    def hints(self) -> list[str]:
        """One short line per finding, shown above the providers screen."""
        lines: list[str] = []
        for vendor, name in self.env_keys.items():
            lines.append(f"found {name} in the environment — {vendor} works without a key here")
        for kind, path in self.imports:
            lines.append(f"found an existing {kind} config at {path}")
        if not self.has_node:
            lines.append("node is not on PATH — the TUI needs it; the CLI works without it")
        return lines


def detect(env: dict[str, str] | None = None, home: Path | None = None) -> Detected:
    """Scan the environment and the usual config paths."""
    environ = env if env is not None else dict(os.environ)
    found = Detected(
        has_uv=shutil.which("uv") is not None,
        has_node=shutil.which("node") is not None,
    )
    for vendor, preset in PRESETS.items():
        for name in preset.env_keys:
            if environ.get(name):
                found.env_keys[vendor] = name
                break
    base = home or Path.home()
    for kind, raw in IMPORT_CANDIDATES:
        path = Path(raw.replace("~", str(base), 1)) if raw.startswith("~") else Path(raw)
        if path.exists():
            found.imports.append((kind, path))
    return found


__all__ = ["IMPORT_CANDIDATES", "Detected", "detect"]

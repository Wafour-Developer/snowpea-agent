#!/usr/bin/env python3
"""Check (or scrub) provider fixtures for leftover secrets — M3 contract §4.

Recorded fixtures are committed, so a stray ``Authorization`` header or a live
key would be published.  ``--check`` is the CI gate; ``--write`` rewrites the
files with the offending values replaced by ``***``.

Usage::

    uv run python scripts/scrub_fixtures.py --check tests/fixtures/providers
    uv run python scripts/scrub_fixtures.py --write tests/fixtures/providers

Exit codes: ``0`` clean, ``1`` secrets found (or files rewritten), ``2`` usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REDACTED = "***"

#: Patterns that must never appear in a committed fixture.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("google key", re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}")),
    ("bearer token", re.compile(r"(?i)\bBearer\s+(?!\*{3})\S+")),
    ("api_key= parameter", re.compile(r"(?i)\bapi[_-]?key=(?!\*{3})[^&\s\"']+")),
    ("key= parameter", re.compile(r"(?i)[?&]key=(?!\*{3})[^&\s\"']+")),
    ("access token", re.compile(r"(?i)\"(access|refresh)_token\"\s*:\s*\"(?!\*{3})[^\"]+\"")),
    ("session cookie", re.compile(r"(?i)\"(set-)?cookie\"\s*:\s*\"(?!\*{3})[^\"]+\"")),
]

#: JSON keys whose value is replaced wholesale when ``--write`` is used.
SECRET_KEYS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "cookie",
        "set-cookie",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "organization",
        "org_id",
        "account_id",
    }
)


def findings(text: str) -> list[str]:
    """Every secret-shaped match in ``text``."""
    found: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            found.append(f"{label}: {match.group(0)[:60]}")
    return found


def redact(value: Any) -> Any:
    """Recursively replace secret-shaped keys and values."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SECRET_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and findings(value):
        return REDACTED
    return value


def fixture_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.rglob("*.json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scrub_fixtures.py", description=__doc__)
    parser.add_argument("target", type=Path, help="fixture file or directory")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="fail if a secret is present")
    group.add_argument("--write", action="store_true", help="rewrite the files, redacting secrets")
    args = parser.parse_args(argv)

    target: Path = args.target
    if not target.exists():
        print(f"scrub_fixtures: no such path: {target}", file=sys.stderr)
        return 2

    files = fixture_files(target)
    if not files:
        print(f"scrub_fixtures: no .json fixtures under {target}", file=sys.stderr)
        return 2

    dirty = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        hits = findings(text)
        if not hits:
            continue
        dirty += 1
        if args.write:
            payload = redact(json.loads(text))
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"scrubbed {path}")
            continue
        print(f"{path}: {len(hits)} possible secret(s)", file=sys.stderr)
        for hit in hits[:10]:
            print(f"  {hit}", file=sys.stderr)

    if dirty:
        if args.write:
            print(f"scrub_fixtures: rewrote {dirty} file(s)")
        else:
            print(
                f"scrub_fixtures: {dirty} of {len(files)} fixture(s) contain secrets",
                file=sys.stderr,
            )
        return 1
    print(f"scrub_fixtures: {len(files)} fixture(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

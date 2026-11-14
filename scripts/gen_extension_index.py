#!/usr/bin/env python3
"""Write a plugin market index (``index.json``, format 1) from plugin folders.

A market is one static file anyone can host; snowpea reads it to list plugins a
person may install (M16 Phase 3, after dsh's plugin-market index). This reads
``plugin.json`` from each folder given (never running ``plugin.py``) and writes:

    {"format": 1, "title": ..., "updatedAt": ..., "plugins": [
        {"name", "title", "description", "version", "source", "engines": {"snowpea"}}
    ]}

Usage: gen_extension_index.py --title "My plugins" --base-url https://example/p out.json DIR...
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def entry(folder: Path, base_url: str) -> dict | None:
    manifest_path = folder / "plugin.json"
    if not (folder / "plugin.py").is_file() or not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = str(manifest.get("name") or folder.name)
    return {
        "name": name,
        "title": str(manifest.get("title") or name),
        "description": str(manifest.get("description") or ""),
        "version": str(manifest.get("version") or "0.0.0"),
        "source": f"{base_url.rstrip('/')}/{name}.tar.gz" if base_url else str(folder),
        "engines": {"snowpea": str((manifest.get("engines") or {}).get("snowpea") or "^1")},
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--title", default="snowpea plugins")
    parser.add_argument("--base-url", default="")
    parser.add_argument("out")
    parser.add_argument("dirs", nargs="+")
    args = parser.parse_args(argv)
    plugins = []
    for raw in args.dirs:
        found = entry(Path(raw), args.base_url)
        if found is None:
            print(f"skipped {raw}: no plugin.py and plugin.json", file=sys.stderr)
            continue
        plugins.append(found)
    index = {
        "format": 1,
        "title": args.title,
        "updatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": sorted(plugins, key=lambda item: item["name"]),
    }
    Path(args.out).write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(f"wrote {len(plugins)} plugin(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

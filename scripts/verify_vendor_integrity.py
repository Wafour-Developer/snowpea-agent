#!/usr/bin/env python3
"""Verify the integrity of files vendored from hermes-agent.

The rule enforced here is **not** byte-identity with upstream.  Vendored files
may be modified; what must hold is::

    upstream_original  +  committed_patch  ==  working_copy   (byte-exact)

and, when a file carries no patch::

    header_line + "\n" + upstream_original  ==  working_copy  (byte-exact)

Checks performed for every entry of ``docs/vendoring-map.json``:

  a. ``sha256($HERMES_REF/<upstream_path>)`` equals the recorded ``sha256``.
  b. if ``patch`` is set, applying it to the pristine upstream bytes reproduces
     the working copy byte for byte; if ``patch`` is null, the working copy is
     the upstream bytes with exactly the provenance header line prepended.
  c. the working copy's first line equals the provenance header.
  d. every file under ``core/snowpea_core/vendor/hermes/`` (excluding
     ``__init__.py``, ``README*`` and ``__pycache__``) appears in the map.

Exit code is 0 when everything holds, 1 otherwise; each problem is reported on
its own line as ``FAIL <kind> <path>``.

Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UPSTREAM_REPO = "https://github.com/NousResearch/hermes-agent"
UPSTREAM_COMMIT = "8d79c2ff57bba4b07e5b37ed90387b16541aef53"
UPSTREAM_LICENSE = "MIT (Copyright 2025 Nous Research)"

HEADER_LINE = f"# Vendored from hermes-agent @ {UPSTREAM_COMMIT}, MIT"
HEADER_BYTES = HEADER_LINE.encode("utf-8") + b"\n"

DEFAULT_REF = "/tmp/hermes-ref"
VENDOR_SUBDIR = "core/snowpea_core/vendor/hermes"
PATCH_SUBDIR = "core/snowpea_core/vendor/patches"
DEFAULT_MAP = "docs/vendoring-map.json"

REQUIRED_KEYS = ("upstream_path", "upstream_commit", "sha256", "destination", "patch", "reason")

EXCLUDED_NAMES = {"__init__.py"}
GENERATED_BEGIN = "<!-- BEGIN GENERATED: entries -->"
GENERATED_END = "<!-- END GENERATED: entries -->"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def load_map(map_path: Path) -> list[dict]:
    if not map_path.exists():
        raise SystemExit(f"FAIL map-missing {map_path}")
    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"FAIL map-invalid {map_path}: {exc}") from exc
    if not isinstance(data, list):
        raise SystemExit(f"FAIL map-invalid {map_path}: top level must be a list")
    return data


def write_map(map_path: Path, entries: list[dict]) -> None:
    map_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [{k: entry.get(k) for k in REQUIRED_KEYS} for entry in entries]
    map_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def vendored_files(repo_root: Path) -> list[Path]:
    root = repo_root / VENDOR_SUBDIR
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        if path.name in EXCLUDED_NAMES or path.name.upper().startswith("README"):
            continue
        found.append(path)
    return found


# --------------------------------------------------------------------------- #
# unified diff: produce + apply
# --------------------------------------------------------------------------- #
def make_patch(rel: str, original: bytes, working: bytes) -> str:
    """Unified diff taking the pristine upstream bytes to the working copy."""
    a = original.decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
    b = working.decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
    diff = difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
    out = []
    for line in diff:
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)


def _apply_with(tool: list[str], rel: str, original: bytes, patch_text: str) -> bytes | None:
    exe = shutil.which(tool[0])
    if exe is None:
        return None
    with tempfile.TemporaryDirectory(prefix="vendor-patch-") as tmp:
        work = Path(tmp)
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original)
        patch_file = work / "__patch.diff"
        patch_file.write_text(patch_text, encoding="utf-8")
        proc = subprocess.run(
            [exe, *tool[1:], str(patch_file)],
            cwd=work,
            capture_output=True,
        )
        if proc.returncode != 0:
            return None
        return read_bytes(target)


def _apply_pure_python(original: bytes, patch_text: str) -> bytes | None:
    """Strict in-process unified-diff applier (fallback when no tool exists)."""
    src = original.decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
    out: list[str] = []
    cursor = 0
    lines = patch_text.splitlines(keepends=True)
    i = 0
    saw_hunk = False
    while i < len(lines):
        line = lines[i]
        if not line.startswith("@@"):
            i += 1
            continue
        saw_hunk = True
        try:
            spec = line.split("@@")[1].strip()
            old_spec = spec.split(" ")[0]
            start = int(old_spec[1:].split(",")[0])
        except (IndexError, ValueError):
            return None
        old_start = max(start - 1, 0)
        if old_start < cursor:
            return None
        out.extend(src[cursor:old_start])
        cursor = old_start
        i += 1
        while i < len(lines):
            hunk = lines[i]
            if hunk.startswith("@@"):
                break
            if hunk.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            tag, body = hunk[:1], hunk[1:]
            # The marker on the next line means this line had no trailing
            # newline upstream; make_patch added one so the diff stays parsable.
            if i + 1 < len(lines) and lines[i + 1].startswith("\\") and body.endswith("\n"):
                body = body[:-1]
            if tag == " ":
                if cursor >= len(src) or src[cursor] != body:
                    return None
                out.append(src[cursor])
                cursor += 1
            elif tag == "-":
                if cursor >= len(src) or src[cursor] != body:
                    return None
                cursor += 1
            elif tag == "+":
                out.append(body)
            elif hunk.strip() == "":
                if cursor >= len(src) or src[cursor] not in ("\n", ""):
                    return None
                out.append(src[cursor])
                cursor += 1
            else:
                break
            i += 1
    if not saw_hunk:
        return None
    out.extend(src[cursor:])
    return "".join(out).encode("utf-8", errors="surrogateescape")


def apply_patch(rel: str, original: bytes, patch_text: str) -> bytes | None:
    for tool in (
        ["git", "apply", "-p1", "--whitespace=nowarn", "--unsafe-paths"],
        ["patch", "-p1", "-s", "--no-backup-if-mismatch", "-i"],
    ):
        result = _apply_with(tool, rel, original, patch_text)
        if result is not None:
            return result
    return _apply_pure_python(original, patch_text)


# --------------------------------------------------------------------------- #
# markdown rendering
# --------------------------------------------------------------------------- #
def _cell(value) -> str:
    if value in (None, ""):
        return "—"
    return str(value).replace("|", "\\|")


def render_table(entries: list[dict]) -> str:
    head = (
        "| upstream path | upstream commit | file sha256 | destination | patch path | reason |\n"
        "|---|---|---|---|---|---|\n"
    )
    if not entries:
        return head + "\n_No files are vendored yet (0 entries)._\n"
    rows = []
    for entry in entries:
        rows.append(
            "| {} | {} | `{}` | {} | {} | {} |".format(
                _cell(entry.get("upstream_path")),
                _cell((entry.get("upstream_commit") or "")[:12]),
                _cell(entry.get("sha256")),
                _cell(entry.get("destination")),
                _cell(entry.get("patch")),
                _cell(entry.get("reason")),
            )
        )
    return head + "\n".join(rows) + "\n"


TEMPLATE_PATH = Path(__file__).resolve().parent / "vendoring_map_template.md"


def md_template(table: str) -> str:
    """Render the hand-written template with the current constants and table."""
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    substitutions = {
        "@@UPSTREAM_REPO@@": UPSTREAM_REPO,
        "@@UPSTREAM_COMMIT@@": UPSTREAM_COMMIT,
        "@@UPSTREAM_LICENSE@@": UPSTREAM_LICENSE,
        "@@DEFAULT_REF@@": DEFAULT_REF,
        "@@GENERATED_BEGIN@@": GENERATED_BEGIN,
        "@@GENERATED_END@@": GENERATED_END,
        "@@TABLE@@": table.rstrip("\n"),
    }
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return text


def render_md(repo_root: Path, entries: list[dict]) -> Path:
    md_path = repo_root / "docs" / "vendoring-map.md"
    table = render_table(entries)
    if md_path.exists():
        text = md_path.read_text(encoding="utf-8")
        if GENERATED_BEGIN in text and GENERATED_END in text:
            pre = text.split(GENERATED_BEGIN)[0]
            post = text.split(GENERATED_END, 1)[1]
            md_path.write_text(
                f"{pre}{GENERATED_BEGIN}\n{table.rstrip(chr(10))}\n{GENERATED_END}{post}",
                encoding="utf-8",
            )
            return md_path
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_template(table), encoding="utf-8")
    return md_path


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def verify(
    repo_root: Path,
    entries: list[dict],
    ref: Path | None,
    allow_missing_ref: bool,
) -> list[str]:
    problems: list[str] = []
    vendor_root = repo_root / VENDOR_SUBDIR
    mapped: set[Path] = set()

    ref_available = ref is not None and ref.is_dir()
    if not ref_available:
        target = str(ref) if ref else "(unset)"
        if not entries:
            # Nothing is vendored, so there is nothing to compare against the
            # reference clone. An empty map verifies without one.
            pass
        elif allow_missing_ref:
            print(f"WARN missing-ref {target} (sha256 checks skipped)", file=sys.stderr)
        else:
            problems.append(f"FAIL missing-ref {target}")

    for index, entry in enumerate(entries):
        label = entry.get("destination") or f"<entry {index}>"

        missing_keys = [k for k in REQUIRED_KEYS if k not in entry]
        if missing_keys:
            problems.append(f"FAIL bad-entry {label} (missing keys: {', '.join(missing_keys)})")
            continue

        dest = repo_root / entry["destination"]
        mapped.add(dest.resolve())

        working = read_bytes(dest)
        if working is None:
            problems.append(f"FAIL missing-file {entry['destination']}")
            continue

        first_line = working.split(b"\n", 1)[0]
        if first_line != HEADER_LINE.encode("utf-8"):
            problems.append(f"FAIL header {entry['destination']}")

        if entry["upstream_commit"] != UPSTREAM_COMMIT:
            print(
                f"WARN commit-drift {entry['destination']} "
                f"(entry {entry['upstream_commit']} != script {UPSTREAM_COMMIT})",
                file=sys.stderr,
            )

        if not ref_available:
            continue

        original_path = ref / entry["upstream_path"]
        original = read_bytes(original_path)
        if original is None:
            problems.append(f"FAIL missing-upstream {entry['upstream_path']}")
            continue

        if sha256_bytes(original) != entry["sha256"]:
            problems.append(f"FAIL sha256 {entry['upstream_path']}")
            continue

        patch_rel = entry.get("patch")
        if patch_rel:
            patch_path = repo_root / patch_rel
            patch_text = None
            if patch_path.exists():
                patch_text = patch_path.read_text(encoding="utf-8")
            if not patch_text:
                problems.append(f"FAIL patch-missing {entry['destination']}")
                continue
            rel = str(Path(entry["destination"]).relative_to(VENDOR_SUBDIR))
            patched = apply_patch(rel, original, patch_text)
            if patched is None:
                problems.append(f"FAIL patch-apply {entry['destination']}")
            elif patched != working:
                problems.append(f"FAIL patch-mismatch {entry['destination']}")
        else:
            if working != HEADER_BYTES + original:
                problems.append(f"FAIL unpatched-drift {entry['destination']}")

    for path in vendored_files(repo_root):
        if path.resolve() not in mapped:
            problems.append(f"FAIL unmapped {path.relative_to(repo_root)}")

    _ = vendor_root
    return problems


# --------------------------------------------------------------------------- #
# mutating helpers
# --------------------------------------------------------------------------- #
def patch_path_for(destination: str) -> str:
    rel = Path(destination).relative_to(VENDOR_SUBDIR)
    return str(Path(PATCH_SUBDIR) / f"{rel}.patch")


def cmd_add(
    repo_root: Path,
    entries: list[dict],
    ref: Path,
    upstream: str,
    destination: str,
    reason: str,
) -> int:
    src = ref / upstream
    original = read_bytes(src)
    if original is None:
        print(f"FAIL missing-upstream {upstream}", file=sys.stderr)
        return 1
    if any(e.get("destination") == destination for e in entries):
        print(f"FAIL duplicate-entry {destination}", file=sys.stderr)
        return 1
    dest = repo_root / destination
    try:
        Path(destination).relative_to(VENDOR_SUBDIR)
    except ValueError:
        print(
            f"FAIL bad-destination {destination} (must live under {VENDOR_SUBDIR})",
            file=sys.stderr,
        )
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(HEADER_BYTES + original)
    entries.append(
        {
            "upstream_path": upstream,
            "upstream_commit": UPSTREAM_COMMIT,
            "sha256": sha256_bytes(original),
            "destination": destination,
            "patch": None,
            "reason": reason,
        }
    )
    entries.sort(key=lambda e: e["destination"])
    print(f"added {destination} (sha256 {sha256_bytes(original)})")
    return 0


def cmd_update_patch(repo_root: Path, entries: list[dict], ref: Path, destination: str) -> int:
    entry = next((e for e in entries if e.get("destination") == destination), None)
    if entry is None:
        print(f"FAIL unknown-entry {destination}", file=sys.stderr)
        return 1
    original = read_bytes(ref / entry["upstream_path"])
    if original is None:
        print(f"FAIL missing-upstream {entry['upstream_path']}", file=sys.stderr)
        return 1
    working = read_bytes(repo_root / destination)
    if working is None:
        print(f"FAIL missing-file {destination}", file=sys.stderr)
        return 1

    rel_patch = patch_path_for(destination)
    patch_file = repo_root / rel_patch

    if working == HEADER_BYTES + original:
        entry["patch"] = None
        if patch_file.exists():
            patch_file.unlink()
        print(f"no local modifications; cleared patch for {destination}")
        return 0

    rel = str(Path(destination).relative_to(VENDOR_SUBDIR))
    patch_text = make_patch(rel, original, working)
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch_text, encoding="utf-8")
    entry["patch"] = rel_patch
    print(f"wrote {rel_patch}")
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--map", default=DEFAULT_MAP, help=f"path to the JSON map (default: {DEFAULT_MAP})"
    )
    parser.add_argument(
        "--repo-root", default=None, help="repository root (default: the map file's grandparent)"
    )
    parser.add_argument(
        "--ref",
        default=None,
        help=f"hermes reference clone (default: $HERMES_REF or {DEFAULT_REF})",
    )
    parser.add_argument(
        "--allow-missing-ref",
        action="store_true",
        help="warn instead of failing when the ref clone is absent",
    )
    parser.add_argument(
        "--render-md",
        action="store_true",
        help="regenerate docs/vendoring-map.md from the JSON map",
    )
    parser.add_argument(
        "--add",
        nargs=2,
        metavar=("UPSTREAM_PATH", "DESTINATION"),
        help="vendor a new file from the ref clone",
    )
    parser.add_argument("--reason", default=None, help="reason recorded for --add")
    parser.add_argument(
        "--update-patch",
        metavar="DESTINATION",
        help="regenerate the patch for a vendored file",
    )
    args = parser.parse_args(argv)

    map_path = Path(args.map).resolve()
    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    elif os.environ.get("SNOWPEA_REPO_ROOT"):
        repo_root = Path(os.environ["SNOWPEA_REPO_ROOT"]).resolve()
    else:
        repo_root = map_path.parent.parent

    ref_raw = args.ref or os.environ.get("HERMES_REF") or DEFAULT_REF
    ref = Path(ref_raw).resolve()

    if args.add and not args.reason:
        parser.error("--add requires --reason")

    if not map_path.exists() and (args.add or args.render_md):
        write_map(map_path, [])

    entries = load_map(map_path)
    mutated = False
    status = 0

    if args.add:
        if not ref.is_dir():
            print(f"FAIL missing-ref {ref}", file=sys.stderr)
            return 1
        status = cmd_add(repo_root, entries, ref, args.add[0], args.add[1], args.reason)
        if status:
            return status
        mutated = True

    if args.update_patch:
        if not ref.is_dir():
            print(f"FAIL missing-ref {ref}", file=sys.stderr)
            return 1
        status = cmd_update_patch(repo_root, entries, ref, args.update_patch)
        if status:
            return status
        mutated = True

    if mutated:
        write_map(map_path, entries)

    if args.render_md or mutated:
        out = render_md(repo_root, entries)
        print(f"rendered {out}")

    if args.add or args.update_patch or args.render_md:
        return 0

    problems = verify(repo_root, entries, ref, args.allow_missing_ref)
    if problems:
        for problem in problems:
            print(problem)
        return 1
    print(f"OK vendor-integrity {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

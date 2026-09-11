"""Behaviour tests for ``scripts/verify_vendor_integrity.py``.

Each test fabricates a miniature world inside ``tmp_path``: a fake read-only
reference clone, a repository root with ``docs/vendoring-map.json``, and zero or
more vendored files. The script is then run as a subprocess exactly as CI runs it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify_vendor_integrity.py"
COMMIT = "8d79c2ff57bba4b07e5b37ed90387b16541aef53"
HEADER = f"# Vendored from hermes-agent @ {COMMIT}, MIT\n"

VENDOR_SUBDIR = "core/snowpea_core/vendor/hermes"
UPSTREAM_REL = "tools/sample_tool.py"
DEST_REL = f"{VENDOR_SUBDIR}/tools/sample_tool.py"
PATCH_REL = "core/snowpea_core/vendor/patches/tools/sample_tool.py.patch"

ORIGINAL = '"""Sample upstream tool."""\n\n\ndef run(cmd):\n    return cmd\n'


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """A fake ref clone + repo root with an empty map."""
    ref = tmp_path / "hermes-ref"
    (ref / "tools").mkdir(parents=True)
    (ref / UPSTREAM_REL).write_text(ORIGINAL, encoding="utf-8")

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / VENDOR_SUBDIR).mkdir(parents=True)
    map_path = repo / "docs" / "vendoring-map.json"
    map_path.write_text("[]\n", encoding="utf-8")
    return {"ref": ref, "repo": repo, "map": map_path}


def run_script(world: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--map",
            str(world["map"]),
            "--repo-root",
            str(world["repo"]),
            "--ref",
            str(world["ref"]),
            *args,
        ],
        capture_output=True,
        text=True,
    )


def write_map(world: dict, entries: list[dict]) -> None:
    world["map"].write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def base_entry(patch: str | None = None, reason: str = "test fixture") -> dict:
    return {
        "upstream_path": UPSTREAM_REL,
        "upstream_commit": COMMIT,
        "sha256": hashlib.sha256(ORIGINAL.encode("utf-8")).hexdigest(),
        "destination": DEST_REL,
        "patch": patch,
        "reason": reason,
    }


def write_vendored(world: dict, text: str) -> Path:
    dest = world["repo"] / DEST_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


def test_empty_map_passes(world: dict) -> None:
    result = run_script(world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK vendor-integrity" in result.stdout


def test_unmodified_file_with_header_passes(world: dict) -> None:
    write_vendored(world, HEADER + ORIGINAL)
    write_map(world, [base_entry()])

    result = run_script(world)
    assert result.returncode == 0, result.stdout + result.stderr


def test_modified_file_with_correct_patch_passes(world: dict) -> None:
    modified = HEADER + ORIGINAL.replace("return cmd", "return cmd.strip()")
    write_vendored(world, modified)
    write_map(world, [base_entry()])

    gen = run_script(world, "--update-patch", DEST_REL)
    assert gen.returncode == 0, gen.stdout + gen.stderr
    assert (world["repo"] / PATCH_REL).exists()
    assert json.loads(world["map"].read_text())[0]["patch"] == PATCH_REL

    result = run_script(world)
    assert result.returncode == 0, result.stdout + result.stderr


def test_stale_patch_fails_and_names_the_file(world: dict) -> None:
    write_vendored(world, HEADER + ORIGINAL.replace("return cmd", "return cmd.strip()"))
    write_map(world, [base_entry()])
    assert run_script(world, "--update-patch", DEST_REL).returncode == 0

    # Edit the file again without refreshing the patch.
    write_vendored(world, HEADER + ORIGINAL.replace("return cmd", "return cmd.upper()"))

    result = run_script(world)
    assert result.returncode == 1
    assert DEST_REL in result.stdout
    assert "FAIL" in result.stdout


def test_missing_patch_file_fails_and_names_the_file(world: dict) -> None:
    write_vendored(world, HEADER + ORIGINAL.replace("return cmd", "return cmd.strip()"))
    write_map(world, [base_entry(patch=PATCH_REL)])

    result = run_script(world)
    assert result.returncode == 1
    assert f"FAIL patch-missing {DEST_REL}" in result.stdout


def test_modified_file_without_patch_fails(world: dict) -> None:
    write_vendored(world, HEADER + ORIGINAL.replace("return cmd", "return cmd.strip()"))
    write_map(world, [base_entry(patch=None)])

    result = run_script(world)
    assert result.returncode == 1
    assert f"FAIL unpatched-drift {DEST_REL}" in result.stdout


def test_unmapped_vendored_file_fails(world: dict) -> None:
    orphan = world["repo"] / VENDOR_SUBDIR / "tools" / "orphan.py"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(HEADER + ORIGINAL, encoding="utf-8")

    result = run_script(world)
    assert result.returncode == 1
    assert f"FAIL unmapped {VENDOR_SUBDIR}/tools/orphan.py" in result.stdout


def test_missing_header_fails(world: dict) -> None:
    write_vendored(world, ORIGINAL)
    write_map(world, [base_entry()])

    result = run_script(world)
    assert result.returncode == 1
    assert f"FAIL header {DEST_REL}" in result.stdout


def test_wrong_upstream_sha256_fails(world: dict) -> None:
    write_vendored(world, HEADER + ORIGINAL)
    entry = base_entry()
    entry["sha256"] = "0" * 64
    write_map(world, [entry])

    result = run_script(world)
    assert result.returncode == 1
    assert f"FAIL sha256 {UPSTREAM_REL}" in result.stdout


def test_missing_ref_fails_without_flag_and_passes_with_it(world: dict) -> None:
    write_vendored(world, HEADER + ORIGINAL)
    write_map(world, [base_entry()])
    absent = str(world["ref"].parent / "does-not-exist")

    strict = run_script(world, "--ref", absent)
    assert strict.returncode == 1
    assert "FAIL missing-ref" in strict.stdout

    lenient = run_script(world, "--ref", absent, "--allow-missing-ref")
    assert lenient.returncode == 0, lenient.stdout + lenient.stderr


def test_add_helper_copies_file_and_records_entry(world: dict) -> None:
    result = run_script(world, "--add", UPSTREAM_REL, DEST_REL, "--reason", "shell backend")
    assert result.returncode == 0, result.stdout + result.stderr

    dest = world["repo"] / DEST_REL
    assert dest.read_text(encoding="utf-8") == HEADER + ORIGINAL

    entries = json.loads(world["map"].read_text())
    assert len(entries) == 1
    assert entries[0]["upstream_path"] == UPSTREAM_REL
    assert entries[0]["patch"] is None
    assert entries[0]["reason"] == "shell backend"
    assert entries[0]["sha256"] == hashlib.sha256(ORIGINAL.encode("utf-8")).hexdigest()

    assert run_script(world).returncode == 0
    rendered = (world["repo"] / "docs" / "vendoring-map.md").read_text(encoding="utf-8")
    assert DEST_REL in rendered


def test_update_patch_clears_patch_when_modifications_are_reverted(world: dict) -> None:
    write_vendored(world, HEADER + ORIGINAL.replace("return cmd", "return cmd.strip()"))
    write_map(world, [base_entry()])
    assert run_script(world, "--update-patch", DEST_REL).returncode == 0
    assert (world["repo"] / PATCH_REL).exists()

    write_vendored(world, HEADER + ORIGINAL)
    assert run_script(world, "--update-patch", DEST_REL).returncode == 0
    assert not (world["repo"] / PATCH_REL).exists()
    assert json.loads(world["map"].read_text())[0]["patch"] is None
    assert run_script(world).returncode == 0

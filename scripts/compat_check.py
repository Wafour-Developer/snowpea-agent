#!/usr/bin/env python3
"""Backward-compatibility check: does a NEW core still work with the clients
that were ALREADY released?

    python scripts/compat_check.py --core <new core checkout> --clients 3

The owner's rule: an engine-only upgrade must never break shipped clients.
The last N released desktop tags (vX.Y.Z in the snowpea-ide repo) and the core
tag each shipped with are checked out as git worktrees (the main checkouts are
never touched), and then, all against the NEW core on a throwaway home and a
free port:

* desktop  - that tag's own real-core Playwright specs (e2e/real-*.spec.ts),
             built with that tag's own SDK, pointed at the new core through
             SNOWPEA_E2E_CORE_BIN.
* sdk      - that core tag's SDK contract tests (sdk/test/contract.test.ts),
             which spawn a daemon with `uv run python -m snowpea_core`; a `uv`
             shim on PATH turns that into the new core's interpreter.
* smoke    - scripts/compat_smoke.mjs driven by the old SDK build: hello,
             session.create, a turn, an approval, a question, session.list,
             close. The TUI is an SDK client, and no TUI test talks to a live
             daemon, so this is also its row (when a tag does have such a
             test, it is run instead of the N/A).

A failing test whose only error is a protocol-version regex pin in the old
spec (`toMatch(/^1\\.(5[5-9]|...)/)` against the new version) is reported as
PIN and does not count as a break.

Exit status: 1 when any row is FAIL, else 0.

Safety: every core runs on `--home /tmp/claude-1000/compat-*` with a free
port; nothing here stops, restarts or reinstalls a running daemon, and the
only processes killed are ones this script started (by PID).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK_ROOT = Path("/mnt/data/work/mediagen")
# Throwaway worktrees go under branch_job/, never beside the real projects (owner rule 2026-11-23).
JOB_ROOT = WORK_ROOT / "branch_job"
TMP_ROOT = Path("/tmp/claude-1000")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Core and desktop are tagged by one release run, minutes apart.
PAIRING_SLACK_SEC = 30 * 60


@dataclass
class Row:
    client: str
    tag: str
    status: str  # PASS / FAIL / PIN / N/A
    failing: list[str] = field(default_factory=list)
    note: str = ""


def log(message: str) -> None:
    print(f"[compat] {message}", flush=True)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def git(repo: Path, *args: str) -> str:
    result = run(["git", "-C", str(repo), *args])
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# tags


def release_tags(repo: Path) -> list[str]:
    tags = [t for t in git(repo, "tag", "--list", "v*").splitlines() if TAG_RE.match(t)]
    return sorted(tags, key=lambda t: tuple(int(p) for p in TAG_RE.match(t).groups()))  # type: ignore[union-attr]


def tag_time(repo: Path, tag: str) -> int:
    return int(git(repo, "log", "-1", "--format=%ct", tag))


def matching_core_tag(core_repo: Path, core_tags: list[str], desktop_time: int) -> str | None:
    """The core release cut with a desktop release: the newest core tag at or
    before the desktop tag (plus slack), as long as it is the same release run."""
    best: tuple[int, str] | None = None
    for tag in core_tags:
        t = tag_time(core_repo, tag)
        if t <= desktop_time + PAIRING_SLACK_SEC and (best is None or t > best[0]):
            best = (t, tag)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# worktrees


def ensure_worktree(repo: Path, path: Path, tag: str) -> None:
    if path.exists():
        head = git(path, "rev-parse", "HEAD")
        if head != git(repo, "rev-parse", f"{tag}^{{commit}}"):
            raise RuntimeError(f"{path} exists but is not at {tag}")
        return
    git(repo, "worktree", "add", "--detach", str(path), tag)


def compose_node_modules(src: Path, dst: Path, overrides: dict[str, Path]) -> None:
    """A node_modules whose entries link to `src`'s, except `@snowpea/*`, which
    link to the worktree's own packages: an old release must build with its own
    SDK, not the main checkout's."""
    dst.mkdir(exist_ok=True)
    for entry in sorted(src.iterdir()):
        link = dst / entry.name
        if entry.name == "@snowpea" or link.is_symlink() or link.exists():
            continue
        link.symlink_to(entry)
    scope = dst / "@snowpea"
    scope.mkdir(exist_ok=True)
    for name, target in overrides.items():
        link = scope / name
        if link.is_symlink() and Path(os.readlink(link)) != target:
            link.unlink()
        if not link.is_symlink():
            link.symlink_to(target)


def prepare_core_worktree(core_repo_main: Path, wt: Path) -> None:
    compose_node_modules(
        core_repo_main / "node_modules", wt / "node_modules", {"sdk": wt / "sdk", "tui": wt / "tui"}
    )
    for pkg in ("sdk", "tui"):
        main_nm = core_repo_main / pkg / "node_modules"
        if main_nm.is_dir() and not (wt / pkg / "node_modules").exists():
            (wt / pkg / "node_modules").symlink_to(main_nm)
    if not (wt / "sdk" / "dist" / "index.js").exists():
        tsc = wt / "node_modules" / ".bin" / "tsc"
        result = run([str(tsc), "-p", "tsconfig.json"], cwd=wt / "sdk")
        if result.returncode != 0:
            raise RuntimeError(
                f"sdk build failed in {wt}:\n{result.stdout[-1500:]}{result.stderr[-1500:]}"
            )


def prepare_ide_worktree(ide_main: Path, wt: Path, core_wt: Path) -> None:
    compose_node_modules(ide_main / "node_modules", wt / "node_modules", {"sdk": core_wt / "sdk"})
    if not (wt / "out" / "main" / "index.js").exists():
        log(f"building the desktop app at {wt.name} (its own SDK from {core_wt.name})")
        result = run([str(wt / "node_modules" / ".bin" / "electron-vite"), "build"], cwd=wt)
        if result.returncode != 0:
            raise RuntimeError(
                f"desktop build failed in {wt}:\n{result.stdout[-2000:]}{result.stderr[-2000:]}"
            )
    # The remote sign-in specs skip themselves without the web build.
    if (wt / "vite.web.config.ts").exists() and not (wt / "out" / "web" / "index.html").exists():
        result = run(
            [str(wt / "node_modules" / ".bin" / "vite"), "build", "--config", "vite.web.config.ts"],
            cwd=wt,
        )
        if result.returncode != 0:
            raise RuntimeError(f"web build failed in {wt}:\n{result.stdout[-1500:]}")


# ---------------------------------------------------------------------------
# the new core


def core_python(core: Path) -> Path:
    for candidate in (core / ".venv" / "bin" / "python", core / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    raise SystemExit(
        f"no virtualenv in {core}: run `uv sync` there first (expected {core}/.venv/bin/python)"
    )


def core_bin(core: Path) -> Path:
    exe = core / ".venv" / "bin" / "snowpea-core"
    if not exe.exists():
        raise SystemExit(f"no {exe}: run `uv sync` in the new core checkout first")
    return exe


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_tmp(prefix: str) -> Path:
    TMP_ROOT.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"compat-{prefix}-", dir=TMP_ROOT))


def uv_shim_dir(core: Path) -> Path:
    """A directory with a `uv` that ignores `run python` and execs the new
    core's interpreter, so the old SDK contract test's
    `uv run python -m snowpea_core ...` starts the new core."""
    shim_dir = make_tmp("shim")
    shim = shim_dir / "uv"
    shim.write_text(f'#!/bin/sh\nshift 2\nexec "{core_python(core)}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim_dir


# ---------------------------------------------------------------------------
# desktop: the old tag's real-core Playwright specs against the new core


def is_pin(message: str) -> bool:
    """A protocol-version regex pin in an old spec: `toMatch(/^1\\.(…)/)`
    against a version string the pin predates."""
    text = ANSI_RE.sub("", message)
    return bool(
        re.search(r"Expected pattern:?\s*/\^?1\\?\.", text)
        and re.search(r"Received string:\s*\"1\.\d+\.\d+\"", text)
    )


def walk_playwright(suites: list, prefix: str = "") -> list[tuple[str, str, str]]:
    """(title, outcome, error text) for every test in a Playwright JSON report."""
    found: list[tuple[str, str, str]] = []
    for suite in suites:
        title = (
            f"{prefix} > {suite['title']}"
            if prefix and suite.get("title")
            else suite.get("title", prefix)
        )
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                name = f"{suite.get('file') or title}: {spec['title']}"
                errors = " ".join(
                    (err.get("message", "") + " " + err.get("stack", ""))
                    for res in test.get("results", [])
                    for err in ([res["error"]] if res.get("error") else res.get("errors", []))
                )
                found.append((name, test.get("status", "unknown"), errors))
        found.extend(walk_playwright(suite.get("suites", []), title))
    return found


def run_desktop(
    tag: str, ide_main: Path, core_main: Path, new_core: Path, core_tag: str, args
) -> Row:
    ide_wt = args.work_root / f"compat-ide-{tag}"
    core_wt = args.work_root / f"compat-core-{core_tag}"
    client = f"desktop (real-core e2e, sdk of {core_tag})"
    try:
        ensure_worktree(ide_main, ide_wt, tag)
        ensure_worktree(core_main, core_wt, core_tag)
        prepare_core_worktree(core_main, core_wt)
        prepare_ide_worktree(ide_main, ide_wt, core_wt)
    except Exception as exc:  # noqa: BLE001 - a setup failure is a row, not a crash
        return Row(client, tag, "FAIL", [], f"setup: {exc}")

    specs = sorted(
        p.relative_to(ide_wt).as_posix() for p in (ide_wt / "e2e").glob("real-*.spec.ts")
    )
    if not specs:
        return Row(client, tag, "N/A", [], "no e2e/real-*.spec.ts in this tag")
    work = make_tmp("pw")
    report = work / "report.json"
    tmux = Path(tempfile.mkdtemp(prefix="tm", dir=TMP_ROOT))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("TMUX", "TMUX_PANE", "DISPLAY", "SNOWPEA_CORE_BIN")
    }
    scratch = work / "tmp"  # the harness makes its throwaway homes under os.tmpdir()
    scratch.mkdir()
    env.update(
        TMPDIR=str(scratch),
        TMUX_TMPDIR=str(tmux),
        SNOWPEA_REPO=str(core_wt),  # fixtures (fake CLIs, scripts) of the matching release
        SNOWPEA_E2E_CORE_BIN=str(core_bin(new_core)),  # ...but the engine under test is the new one
        PLAYWRIGHT_JSON_OUTPUT_NAME=str(report),
    )
    cmd = [
        "xvfb-run",
        "-a",
        str(ide_wt / "node_modules" / ".bin" / "playwright"),
        "test",
        *specs,
        "--retries=1",
        "--reporter=list,json",
    ]
    log(f"{tag}: {len(specs)} real-core specs against {new_core} (log {work}/run.log)")
    with open(work / "run.log", "w") as out:
        proc = subprocess.Popen(cmd, cwd=ide_wt, env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=args.desktop_timeout)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)  # our own PID only
            proc.wait(timeout=60)
            return Row(
                client,
                tag,
                "FAIL",
                [],
                f"timed out after {args.desktop_timeout}s, log {work}/run.log",
            )
    if not report.exists():
        tail = (work / "run.log").read_text()[-600:]
        return Row(client, tag, "FAIL", [], f"no report; log {work}/run.log: {tail.strip()[-300:]}")
    tests = walk_playwright(json.loads(report.read_text()).get("suites", []))
    failed = [(n, e) for n, s, e in tests if s in ("unexpected", "failed", "timedOut")]
    flaky = [n for n, s, _ in tests if s == "flaky"]
    skipped = sum(1 for _, s, _ in tests if s == "skipped")
    note = f"{len(tests)} tests ({skipped} skipped), log {work}/run.log" + (
        f"; flaky: {', '.join(flaky)}" if flaky else ""
    )
    if not failed:
        return Row(client, tag, "PASS", [], note)
    real = [n for n, e in failed if not is_pin(e)]
    pins = [n for n, e in failed if is_pin(e)]
    if real:
        return Row(client, tag, "FAIL", real, note + (f"; pins: {', '.join(pins)}" if pins else ""))
    return Row(client, tag, "PIN", pins, note)


# ---------------------------------------------------------------------------
# sdk / tui / smoke: old core tag's clients against the new core


def run_sdk_contract(core_tag: str, core_wt: Path, new_core: Path, args) -> Row:
    client = "sdk contract (mocha)"
    if not (core_wt / "sdk" / "test" / "contract.test.ts").exists():
        return Row(client, core_tag, "N/A", [], "no sdk/test/contract.test.ts in this tag")
    out = make_tmp("mocha") / "report.json"
    env = {**os.environ, "PATH": f"{uv_shim_dir(new_core)}{os.pathsep}{os.environ['PATH']}"}
    cmd = [
        str(core_wt / "node_modules" / ".bin" / "mocha"),
        "--reporter",
        "json",
        "--reporter-option",
        f"output={out}",
    ]
    result = run(cmd, cwd=core_wt / "sdk", env=env, timeout=args.sdk_timeout)
    if not out.exists():
        return Row(
            client,
            core_tag,
            "FAIL",
            [],
            f"no report: {(result.stdout + result.stderr)[-400:].strip()}",
        )
    data = json.loads(out.read_text())
    failures = [
        f["fullTitle"] + " -- " + ANSI_RE.sub("", f["err"].get("message", ""))[:200]
        for f in data["failures"]
    ]
    return Row(
        client,
        core_tag,
        "FAIL" if failures else "PASS",
        failures,
        f"{data['stats']['tests']} tests",
    )


def run_tui_live(core_tag: str, core_wt: Path, new_core: Path, args) -> Row:
    client = "tui"
    live = [
        p.name
        for p in sorted((core_wt / "tui" / "test").glob("*.test.ts*"))
        if re.search(r"""['"]snowpea_core['"]|['"]uv['"]""", p.read_text(errors="replace"))
    ]
    if not live:
        return Row(
            client,
            core_tag,
            "N/A",
            [],
            "no TUI test talks to a live daemon; see sdk rows (the TUI is an SDK client)",
        )
    env = {**os.environ, "PATH": f"{uv_shim_dir(new_core)}{os.pathsep}{os.environ['PATH']}"}
    out = make_tmp("vitest") / "report.json"
    cmd = [
        str(core_wt / "node_modules" / ".bin" / "vitest"),
        "run",
        "--reporter=json",
        f"--outputFile={out}",
        *[f"test/{n}" for n in live],
    ]
    run(cmd, cwd=core_wt / "tui", env=env, timeout=args.sdk_timeout)
    if not out.exists():
        return Row(client, core_tag, "FAIL", [], "vitest wrote no report")
    data = json.loads(out.read_text())
    failing = [
        a["fullName"]
        for f in data["testResults"]
        for a in f["assertionResults"]
        if a["status"] == "failed"
    ]
    return Row(
        client,
        core_tag,
        "FAIL" if failing else "PASS",
        failing,
        f"{len(live)} live-daemon test files",
    )


SMOKE_SCRIPT = {
    "steps": [
        {"match": "hello", "text": "Hi from fake"},
        {
            "match": "run ls",
            "tool_calls": [{"name": "shell", "arguments": {"command": "ls"}}],
            "text": "done",
        },
        {"after_tool": "shell", "text": "listed"},
        {
            "match": "ask me",
            "tool_calls": [
                {
                    "name": "ask_user",
                    "arguments": {
                        "header": "Pick",
                        "question": "Which one?",
                        "options": [
                            {"label": "alpha", "description": "first"},
                            {"label": "beta", "description": "second"},
                        ],
                    },
                }
            ],
            "text": "asked",
        },
        {"after_tool": "ask_user", "text": "thanks"},
    ]
}


def run_smoke(core_tag: str, core_wt: Path, new_core: Path) -> Row:
    client = "sdk smoke (old SDK build)"
    home = make_tmp("home")
    work = make_tmp("work")
    script = home / "smoke-provider.json"
    script.write_text(json.dumps(SMOKE_SCRIPT))
    port = free_port()
    env = {
        **os.environ,
        "SNOWPEA_HOME": str(home),
        "SNOWPEA_PROVIDER": f"fake:{script}",
        "SNOWPEA_TEST": "1",
    }
    log_path = home / "daemon.out"
    with open(log_path, "w") as out:
        daemon = subprocess.Popen(
            [
                str(core_python(new_core)),
                "-m",
                "snowpea_core",
                "--port",
                str(port),
                "--home",
                str(home),
            ],
            cwd=new_core,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        info: dict = {}
        deadline = time.time() + 60
        while time.time() < deadline and daemon.poll() is None:
            try:
                info = json.loads((home / "daemon.json").read_text())
                if info.get("port") and info.get("token"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        if not info.get("token"):
            return Row(
                client,
                core_tag,
                "FAIL",
                [],
                f"daemon did not start: {log_path.read_text()[-300:].strip()}",
            )
        result = run(
            [
                "node",
                str(HERE / "compat_smoke.mjs"),
                str(core_wt / "sdk" / "dist"),
                str(info["port"]),
                info["token"],
                str(work),
            ],
            timeout=180,
        )
        line = (result.stdout.strip().splitlines() or ["{}"])[-1]
        try:
            report = json.loads(line)
        except ValueError:
            return Row(
                client,
                core_tag,
                "FAIL",
                [],
                f"smoke printed no JSON: {(result.stdout + result.stderr)[-300:].strip()}",
            )
        if report.get("ok"):
            return Row(client, core_tag, "PASS", [], "steps: " + ", ".join(report["done"]))
        return Row(
            client,
            core_tag,
            "FAIL",
            [f"{report.get('step')}: {report.get('error')}"],
            "done: " + ", ".join(report.get("done", [])),
        )
    finally:
        if daemon.poll() is None:
            daemon.terminate()  # the PID this function started, nothing else
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()


def run_core_clients(core_tag: str, core_main: Path, new_core: Path, args) -> list[Row]:
    core_wt = args.work_root / f"compat-core-{core_tag}"
    try:
        ensure_worktree(core_main, core_wt, core_tag)
        prepare_core_worktree(core_main, core_wt)
    except Exception as exc:  # noqa: BLE001
        return [Row("sdk/tui", core_tag, "FAIL", [], f"setup: {exc}")]
    rows = []
    for fn in (
        lambda: run_sdk_contract(core_tag, core_wt, new_core, args),
        lambda: run_smoke(core_tag, core_wt, new_core),
        lambda: run_tui_live(core_tag, core_wt, new_core, args),
    ):
        try:
            rows.append(fn())
        except Exception as exc:  # noqa: BLE001
            rows.append(Row("sdk/tui", core_tag, "FAIL", [], f"harness error: {exc}"))
    return rows


# ---------------------------------------------------------------------------


def print_table(rows: list[Row]) -> None:
    width = max(len(r.client) for r in rows)
    print()
    print(f"{'client'.ljust(width)}  {'tag':8}  {'result':6}  failing tests / notes")
    print(f"{'-' * width}  {'-' * 8}  {'-' * 6}  {'-' * 40}")
    for r in rows:
        print(f"{r.client.ljust(width)}  {r.tag:8}  {r.status:6}  {r.note}")
        for name in r.failing:
            print(f"{' ' * width}  {'':8}  {'':6}    - {name}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--core", required=True, type=Path, help="the NEW core checkout (with a .venv)"
    )
    parser.add_argument(
        "--clients", type=int, default=3, help="how many released desktop tags to check (default 3)"
    )
    parser.add_argument("--ide-repo", type=Path, default=WORK_ROOT / "snowpea-ide")
    parser.add_argument(
        "--core-repo",
        type=Path,
        default=WORK_ROOT / "snowpea",
        help="main core checkout: tags, node_modules",
    )
    parser.add_argument(
        "--work-root", type=Path, default=JOB_ROOT, help="where the compat-* worktrees live"
    )
    parser.add_argument(
        "--skip-desktop", action="store_true", help="only the sdk/tui rows (minutes, not an hour)"
    )
    parser.add_argument("--desktop-timeout", type=int, default=3 * 3600)
    parser.add_argument("--sdk-timeout", type=int, default=900)
    args = parser.parse_args()

    new_core = args.core.resolve()
    core_python(new_core)  # fail early
    ide_tags = release_tags(args.ide_repo)[-args.clients :]
    core_tags_all = release_tags(args.core_repo)
    log(f"new core: {new_core} ({git(new_core, 'describe', '--tags', '--always')})")
    log(f"desktop tags: {', '.join(ide_tags)}")

    rows: list[Row] = []
    core_tags: list[str] = []
    pairs: dict[str, str] = {}
    for tag in ide_tags:
        match = matching_core_tag(args.core_repo, core_tags_all, tag_time(args.ide_repo, tag))
        if match is None:
            rows.append(Row("desktop", tag, "FAIL", [], "no matching core tag found"))
            continue
        pairs[tag] = match
        if match not in core_tags:
            core_tags.append(match)
    log("pairs: " + ", ".join(f"desktop {d} + core {c}" for d, c in pairs.items()))

    for core_tag in core_tags:
        rows.extend(run_core_clients(core_tag, args.core_repo, new_core, args))
    if not args.skip_desktop:
        for tag, core_tag in pairs.items():
            rows.append(run_desktop(tag, args.ide_repo, args.core_repo, new_core, core_tag, args))

    print_table(rows)
    broken = [r for r in rows if r.status == "FAIL"]
    pins = sum(r.status == "PIN" for r in rows)
    passed = sum(r.status == "PASS" for r in rows)
    print(f"{len(broken)} FAIL, {pins} PIN, {passed} PASS")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())

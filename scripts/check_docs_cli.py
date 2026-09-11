#!/usr/bin/env python3
"""Validate the CLI invocations and relative links in snowpea's documentation.

Two checks run over ``README*.md`` and ``docs/manual/**/*.md``:

1. **CLI check.**  Every ``snowpea …`` command line inside a fenced code block is
   parsed, and its subcommand chain and flags are validated against the CLI's own
   ``--help`` output (``snowpea --help``, ``snowpea <sub> --help``,
   ``snowpea <sub> <action> --help``).  A documented subcommand or flag that the
   installed CLI does not have is an error.
2. **Link check.**  Every relative Markdown link and image target in those files
   must resolve to a file that exists.

Exit code is 0 when everything holds, 1 otherwise, with one line per problem.

    uv run python scripts/check_docs_cli.py
    uv run python scripts/check_docs_cli.py --verbose

The docs deliberately describe a few subcommands that are still landing in other
stories.  Those live in ALLOWLIST below, each with the story that removes it.
Nothing else may be added: if the check rejects a command, either the docs or the
CLI is wrong.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Subcommand chains the documentation is allowed to mention before `--help`
# knows about them.  Each entry names the story that will land it, after which
# the entry must be deleted.
ALLOWLIST: dict[tuple[str, ...], str] = {
    ("team", "status"): "US-020 — team mode CLI; `snowpea team` is still a placeholder parser",
    ("service", "install"): (
        "US-022 — service registration; `snowpea service` is still a placeholder"
    ),
    ("service", "uninstall"): "US-022 — service registration",
    ("service", "status"): "US-022 — service registration",
}

# Shell words that may precede `snowpea` on a documented command line.
COMMAND_PREFIXES = {"uv", "run", "sudo", "exec", "env", "time", "npx"}

# Fenced-code info strings whose contents are shell commands.  Blocks tagged
# anything else (json, ts, yaml, mermaid, markdown, …) are skipped entirely, and
# `text` is the tag to reach for when a block is a schematic rather than
# something a reader could paste into a shell.
SHELL_LANGUAGES = {"", "bash", "sh", "shell", "console", "zsh", "powershell", "ps1"}

FENCE_RE = re.compile(r"^(?P<indent> *)(?P<fence>```+|~~~+)(?P<info>[^\n]*)$")
# Markdown inline links and images, plus HTML href/src attributes.
MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_ATTR_RE = re.compile(r"(?:href|src)\s*=\s*\"([^\"]+)\"")

# An option line in argparse help: two spaces, then a dash.
OPTION_LINE_RE = re.compile(r"^ {2}(-{1,2}[^\s,]+.*)$")
# A subcommand line in argparse help: four spaces, a name, then the description.
SUBCOMMAND_LINE_RE = re.compile(r"^ {4}([a-z][\w-]*)(?:\s{2,}.*)?$")
FLAG_RE = re.compile(r"^(-{1,2}[A-Za-z][\w-]*)")


class HelpNode:
    """One parser level of the CLI, as described by its own ``--help`` output."""

    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path
        self.flags: dict[str, bool] = {}  # flag -> takes a value
        self.subcommands: set[str] = set()
        self.children: dict[str, HelpNode] = {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HelpNode({' '.join(('snowpea', *self.path))!r})"


def run_help(path: tuple[str, ...], snowpea_cmd: list[str], env: dict[str, str]) -> str | None:
    """Return the ``--help`` text for one subcommand chain, or None if it has none."""
    try:
        proc = subprocess.run(
            [*snowpea_cmd, *path, "--help"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - environment
        print(f"error: could not run `{' '.join((*snowpea_cmd, *path))} --help`: {exc}")
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_help(text: str, node: HelpNode) -> None:
    """Fill a node's flags and subcommands from one argparse help page.

    Flags come from the usage line and from the indented option listing; a flag
    is recorded as taking a value when a metavar or a ``{a,b}`` choice set
    follows it.  Subcommands come from the four-space-indented entries argparse
    prints for a subparser action.
    """
    # Usage line(s): everything up to the first blank line.
    lines = text.splitlines()
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("usage:") or (stripped.startswith("[") and "]" in stripped):
            for token in re.findall(r"-{1,2}[A-Za-z][\w-]*", raw):
                node.flags.setdefault(token, False)

    in_options = False
    for raw in lines:
        header = raw.strip().lower()
        if header in {"options:", "optional arguments:"}:
            in_options = True
            continue
        if header.endswith(":") and header not in {"options:", "optional arguments:"}:
            in_options = False
        match = OPTION_LINE_RE.match(raw)
        if in_options and match:
            body = match.group(1)
            # `-c PROMPT, --prompt PROMPT   description` -> split off the description.
            spec = re.split(r"\s{2,}", body, maxsplit=1)[0]
            for part in spec.split(","):
                part = part.strip()
                flag_match = FLAG_RE.match(part)
                if not flag_match:
                    continue
                flag = flag_match.group(1)
                rest = part[len(flag) :].strip()
                takes_value = bool(rest) and not rest.startswith("-")
                node.flags[flag] = node.flags.get(flag, False) or takes_value
        sub_match = SUBCOMMAND_LINE_RE.match(raw)
        if sub_match and not in_options and not raw.strip().startswith("-"):
            node.subcommands.add(sub_match.group(1))


def build_cli_tree(snowpea_cmd: list[str], env: dict[str, str]) -> HelpNode | None:
    """Walk `--help` from the root down through every subcommand it advertises."""
    root = HelpNode(())
    text = run_help((), snowpea_cmd, env)
    if text is None:
        return None
    parse_help(text, root)

    def descend(node: HelpNode) -> None:
        for name in sorted(node.subcommands):
            child_path = (*node.path, name)
            child_text = run_help(child_path, snowpea_cmd, env)
            if child_text is None:
                continue
            child = HelpNode(child_path)
            parse_help(child_text, child)
            node.children[name] = child
            descend(child)

    descend(root)
    return root


def iter_code_blocks(text: str):
    """Yield (line_number, info_string, block_lines) for every fenced code block."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = FENCE_RE.match(lines[i])
        if not match:
            i += 1
            continue
        fence = match.group("fence")[0] * 3
        info = match.group("info").strip().split()
        info_word = info[0].lower() if info else ""
        start = i
        body: list[str] = []
        i += 1
        while i < len(lines):
            close = FENCE_RE.match(lines[i])
            if close and close.group("fence").startswith(fence) and not close.group("info").strip():
                break
            body.append(lines[i])
            i += 1
        yield start + 1, info_word, body
        i += 1


def split_shell_segments(line: str) -> list[str]:
    """Split one documented line into the pipeline/list segments it contains."""
    return [seg for seg in re.split(r"\|\||&&|;|\||\n", line) if seg.strip()]


def extract_invocations(body: list[str]) -> list[tuple[int, str]]:
    """Return (offset_in_block, segment) for every `snowpea …` segment in a block."""
    found: list[tuple[int, str]] = []
    buffer = ""
    for offset, raw in enumerate(body):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Shell prompts and YAML `- run:` wrappers around a real command.
        stripped = re.sub(r"^\$\s+", "", stripped)
        stripped = re.sub(r"^-\s*run:\s*", "", stripped)
        if buffer:
            stripped = buffer + " " + stripped
            buffer = ""
        if stripped.endswith("\\"):
            buffer = stripped[:-1].strip()
            continue
        for segment in split_shell_segments(stripped):
            segment = segment.strip()
            if not segment:
                continue
            if re.search(r"(?:^|[\s/])snowpea(?:\s|$)", segment):
                found.append((offset, segment))
    return found


def tokenize(segment: str) -> list[str] | None:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return None


def check_invocation(root: HelpNode, tokens: list[str]) -> list[str]:
    """Validate one tokenized `snowpea …` command; return a list of problems."""
    problems: list[str] = []

    i = 0
    # Strip environment assignments and harmless command prefixes.
    while i < len(tokens):
        token = tokens[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token) or token in COMMAND_PREFIXES:
            i += 1
            continue
        break
    if i >= len(tokens):
        return problems
    if Path(tokens[i]).name not in {"snowpea", "snowpea.exe"}:
        return problems
    i += 1

    node = root
    chain: list[HelpNode] = [root]
    allowlisted = False

    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            flag, _, inline_value = token.partition("=")
            known = None
            for level in reversed(chain):
                if flag in level.flags:
                    known = level
                    break
            if known is None:
                if not allowlisted:
                    where = " ".join(("snowpea", *node.path)).strip()
                    problems.append(f"unknown flag {flag!r} for `{where}`")
                i += 1
                continue
            if known.flags[flag] and not inline_value:
                i += 2
            else:
                i += 1
            continue

        # A bare word: either the next subcommand, or a positional value.
        if token in node.children:
            node = node.children[token]
            chain.append(node)
        elif token in node.subcommands:
            # Advertised by --help but its own help page was unavailable.
            node = HelpNode((*node.path, token))
            chain.append(node)
        else:
            candidate = (*node.path, token)
            if candidate in ALLOWLIST:
                allowlisted = True
                node = HelpNode(candidate)
                chain.append(node)
            elif not node.path and not node.children:
                pass  # positional value at the root, e.g. a prompt
            elif not node.path and node.subcommands and token not in node.subcommands:
                problems.append(f"unknown subcommand {token!r} for `snowpea`")
            elif node.subcommands and token not in node.subcommands:
                where = " ".join(("snowpea", *node.path))
                problems.append(f"unknown subcommand {token!r} for `{where}`")
        i += 1

    # A placeholder parser advertises no actions; `snowpea team status` reaching
    # here means the allowlist caught it, which is the intended outcome.
    if node.path and node.path in ALLOWLIST:
        allowlisted = True
    return [] if allowlisted and not problems else problems


def doc_files() -> list[Path]:
    files = sorted(REPO_ROOT.glob("README*.md"))
    manual = REPO_ROOT / "docs" / "manual"
    if manual.is_dir():
        files.extend(sorted(manual.rglob("*.md")))
    return files


def check_links(path: Path) -> list[str]:
    """Every relative link target in one file must exist on disk."""
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    targets = [m.group(1) for m in MD_LINK_RE.finditer(text)]
    targets += [m.group(1) for m in HTML_ATTR_RE.finditer(text)]
    for target in targets:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            continue  # http:, https:, mailto:, ws:, …
        if target.startswith("#") or target.startswith("//"):
            continue
        clean = target.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            continue
        resolved = (path.parent / clean).resolve()
        if not resolved.exists():
            rel = resolved.relative_to(REPO_ROOT) if REPO_ROOT in resolved.parents else resolved
            problems.append(f"broken link {target!r} -> {rel}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="list every command checked")
    parser.add_argument(
        "--skip-cli",
        action="store_true",
        help="check links only, without invoking the CLI",
    )
    args = parser.parse_args()

    files = doc_files()
    if not files:
        print("error: no documentation files found")
        return 1

    failures: list[str] = []

    # --- links -----------------------------------------------------------
    link_count = 0
    for path in files:
        for problem in check_links(path):
            failures.append(f"{path.relative_to(REPO_ROOT)}: {problem}")
        link_count += 1

    # --- CLI -------------------------------------------------------------
    checked = 0
    if not args.skip_cli:
        env = dict(os.environ)
        env.setdefault("SNOWPEA_HOME", "/tmp/snowpea-docs-check")
        env["COLUMNS"] = "200"
        from_checkout = (REPO_ROOT / "pyproject.toml").exists()
        snowpea_cmd = ["uv", "run", "snowpea"] if from_checkout else ["snowpea"]
        root = build_cli_tree(snowpea_cmd, env)
        if root is None:
            print("error: `snowpea --help` failed; cannot validate documented commands")
            return 1

        for path in files:
            text = path.read_text(encoding="utf-8")
            for start, info, body in iter_code_blocks(text):
                if info not in SHELL_LANGUAGES:
                    continue
                for offset, segment in extract_invocations(body):
                    tokens = tokenize(segment)
                    if tokens is None:
                        continue
                    checked += 1
                    if args.verbose:
                        print(f"  {path.relative_to(REPO_ROOT)}:{start + offset + 1}: {segment}")
                    for problem in check_invocation(root, tokens):
                        failures.append(
                            f"{path.relative_to(REPO_ROOT)}:{start + offset + 1}: "
                            f"{problem}\n    {segment}"
                        )

    if failures:
        print(f"check_docs_cli: {len(failures)} problem(s)\n")
        for failure in failures:
            print(f"  {failure}")
        print(
            "\nEither fix the documentation, or — if the command is genuinely still "
            "landing — add it to ALLOWLIST in scripts/check_docs_cli.py with the story."
        )
        return 1

    print(
        f"check_docs_cli: ok — {checked} snowpea invocation(s) and the links in "
        f"{link_count} file(s) all check out."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

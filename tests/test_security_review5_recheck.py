"""Security review 5: earlier items re-checked and closed (round-4 M3, R2-M2, the
CLI password variable, ``git apply``).  Nothing here starts a process: the
daemon spawn is a spy; command lines are only handed to the pure classifier."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.exec import local


def test_no_path_entry_inside_any_known_project(tmp_path: Path) -> None:
    here, other = tmp_path / "here", tmp_path / "other"
    (other / "bin").mkdir(parents=True)
    here.mkdir()
    value = os.pathsep.join([str(other / "bin"), "/usr/bin"])
    local.set_project_roots(lambda: [str(other)])
    try:
        assert local.sanitized_path(value, here) == "/usr/bin"
    finally:
        local.set_project_roots(None)
    assert local.sanitized_path(value, here) == value


def test_a_hidden_parent_hides_the_child_without_the_service(tmp_path: Path) -> None:
    from snowpea_core.server.jarvis_delegation_handlers import _exposed

    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".snowpea").mkdir()
    (parent / ".snowpea" / "settings.json").write_text(
        json.dumps({"jarvis": {"expose": False}}), encoding="utf-8")
    core = SimpleNamespace(jarvis=None, sessions=None, _security_projects=(0, (str(parent),)))
    assert _exposed(str(child), core) is False
    assert _exposed(str(child)) is True  # without a core: its own file only, as before


def test_the_daemon_never_inherits_the_web_password(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.cli import daemon_client

    seen: dict[str, Any] = {}

    def spy(command: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(pid=0)

    monkeypatch.setenv("SNOWPEA_WEB_PASSWORD", "a-password-for-the-test")
    monkeypatch.setattr(daemon_client.subprocess, "Popen", spy)
    daemon_client._spawn_daemon(tmp_path / "home")
    assert "SNOWPEA_WEB_PASSWORD" not in seen["env"]
    assert seen["env"].get("PATH") == os.environ.get("PATH")


def test_git_apply_is_not_safe(tmp_path: Path) -> None:
    from snowpea_core.jarvis.classify import ClassifyRequest, classify

    verdict = classify(ClassifyRequest(tool="shell", args={"command": "git apply fix.patch"},
                                       tags=("exec",), workdir=str(tmp_path),
                                       home=str(tmp_path / "home")))
    assert verdict.classification in ("guarded", "never"), verdict

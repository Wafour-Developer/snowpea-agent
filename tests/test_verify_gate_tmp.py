"""R5 (1.37.0): a deliverable named under the session's throwaway ``tmp/`` is sent back once."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from snowpea_core.agent import verify_gate

WORKSPACE = "/home/u/.snowpea/workspaces/s1"


def _session(workspace: str | None = WORKSPACE) -> Any:
    session = SimpleNamespace(
        workspace_dir=workspace, origin_surface="tui", verify_turn=verify_gate.TurnEvidence()
    )
    return session


def _core() -> Any:
    return SimpleNamespace(settings=SimpleNamespace(agent=SimpleNamespace(verifyOnStop="on")))


def test_the_path_is_found_whatever_surrounds_it() -> None:
    session = _session()
    text = f"Done: `{WORKSPACE}/tmp/post.html`."
    assert verify_gate.tmp_deliverable(session, text) == f"{WORKSPACE}/tmp/post.html"
    assert verify_gate.tmp_deliverable(session, f"see {WORKSPACE}/tmp/a b") == (
        f"{WORKSPACE}/tmp/a"
    )


def test_a_file_in_the_working_directory_is_fine() -> None:
    assert verify_gate.tmp_deliverable(_session(), "saved to /home/u/proj/out/post.html") is None
    assert verify_gate.tmp_deliverable(_session(None), f"{WORKSPACE}/tmp/x") is None


def test_a_tmp_deliverable_is_sent_back_once() -> None:
    session = _session()
    text = f"The page is at {WORKSPACE}/tmp/post.html"
    first = verify_gate.nudge(_core(), session, text)
    assert first and "scratch space" in first and f"{WORKSPACE}/tmp/post.html" in first
    assert verify_gate.nudge(_core(), session, text) is None  # asked once, not again


def test_an_answer_without_a_tmp_path_ends_the_turn() -> None:
    assert verify_gate.nudge(_core(), _session(), "all done") is None


def test_the_gate_off_lets_it_end() -> None:
    core = SimpleNamespace(settings=SimpleNamespace(agent=SimpleNamespace(verifyOnStop="off")))
    assert verify_gate.nudge(core, _session(), f"{WORKSPACE}/tmp/post.html") is None

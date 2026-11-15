"""Security review H4: Jarvis never hands work to an external session snowpea cannot gate.

``controlled: false`` sessions (agy with always-proceed, Cursor outside
readonly, …) run without snowpea's approvals, so Jarvis may read and
summarise them but neither send into them nor create one.  The fake Claude
adapter stands in for such a CLI by reporting itself uncontrolled.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from _support import env_vars
from test_jarvis_external import Person, _call, _daemon, _key, _project

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")


def _uncontrolled(core: Any) -> None:
    adapter = core.external.registry.get("claude")
    adapter.controlled = lambda mode, discovery: False  # like agy with always-proceed


async def test_jarvis_neither_sends_into_nor_creates_an_uncontrolled_session(
    tmp_path: Path,
) -> None:
    with env_vars(FAKE_CLI_SCENARIO="normal", FAKE_CLI_SCENARIO_FILE=None,
                  FAKE_CLI_LOG=str(tmp_path / "fake.log")):
        daemon = await _daemon(tmp_path)
        try:
            core = daemon.core
            root = await _project(core, tmp_path / "proj")
            _uncontrolled(core)
            # The person may open one; Jarvis lists and reads it…
            row = (await core.external.create("claude", root, conn=Person()))["session"]
            assert row["controlled"] is False and row["mode"] != "unrestricted"
            listed = (await _call(core, "session_list", {})).output
            assert _key(row) in listed and "UNCONTROLLED" in listed
            read = await _call(core, "external_read", {"sessionId": _key(row)})
            assert read.ok, read.error
            # …but never sends it work, even for the person's own request.
            refused = await _call(core, "external_send",
                                  {"sessionId": _key(row), "text": "push it"}, human=True)
            assert not refused.ok and "cannot gate" in refused.error
            # Nor opens one itself, in any mode it may choose.
            for mode in ("default", "readonly"):
                created = await _call(core, "session_create",
                                      {"project": root, "cli": "claude", "mode": mode,
                                       "prompt": "look around"}, human=True)
                assert not created.ok and "could not gate" in created.error, created
            assert len([r for r in core.external._records.values()]) == 1
        finally:
            await daemon.stop()


def test_the_prompt_tells_jarvis() -> None:
    from snowpea_core.jarvis.tools import JARVIS_PROMPT

    assert "UNCONTROLLED" in JARVIS_PROMPT and "only read and summarise" in JARVIS_PROMPT

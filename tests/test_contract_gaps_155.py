"""1.55.0 contract gaps for the IDE and TUI: enumerated reasons, structured problems.

Every pattern and path below is DATA for the pure validators; nothing runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.security.lists import from_raw, problems_in
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import SecurityGetResult, SecurityProblem, dump_schema
from snowpea_core.server.security_handlers import settings_problems


def test_schema_enumerates_detail_reasons_and_kinds() -> None:
    details = dump_schema()["errorDetails"]
    reasons = details["reasons"]
    for reason in ("jarvis_on", "mode_raise", "person_only", "not_bundled_plugin"):
        assert reasons[reason]["code"] == "jarvis_person_only"
    assert reasons["catch_all"]["code"] == "invalid_params"
    assert set(details["personOnlyKinds"]) >= {"person", "remote_login", "browser",
                                              "connector", "none", "agent"}
    for reason, row in reasons.items():
        assert row["code"] in errors.ERROR_CODES, reason


def test_generated_ts_has_the_reason_types() -> None:
    root = Path(__file__).resolve().parent.parent
    text = (root / "sdk" / "src" / "protocol.ts").read_text("utf-8")
    assert "export type ErrorDetailReason" in text and '| "catch_all"' in text
    assert "export type PersonOnlyKind" in text
    assert '  catch_all: "invalid_params",' in text


def test_problems_in_has_a_structured_twin() -> None:
    raw = {
        "blockedCommands": {"presets": ["nope"], "custom": [""]},
        "protectedPaths": {"custom": [{"path": "/", "level": "sometimes"}]},
        "other": 1,
    }
    problems = problems_in(raw)
    items = problems.structured  # type: ignore[attr-defined]
    assert len(items) == len(problems) >= 4
    for item in items:
        SecurityProblem(**item)
    lists = {item["list"] for item in items}
    assert lists == {None, "commands", "paths"}
    assert {"list": "commands", "entry": "nope", "reason": "unknown preset"} in items


def test_settings_set_refusal_carries_problem_items() -> None:
    with pytest.raises(RpcError) as caught:
        settings_problems({"security": {"blockedCommands": {"presets": ["nope"]}}})
    assert caught.value.code == errors.INVALID_PARAMS
    details = caught.value.data
    assert details["problems"] and details["problemItems"][0]["entry"] == "nope"


async def test_security_get_lists_problem_items(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from snowpea_core.server.protocol import SecurityGetParams
    from snowpea_core.server.security_handlers import get_handler

    lists = from_raw({"blockedCommands": {"presets": ["essential", "bogus"]}})
    assert lists.problem_items == ({"list": "commands", "entry": "bogus",
                                    "reason": "unknown preset"},)
    settings = Settings.model_validate(
        {"security": {"blockedCommands": {"presets": ["essential"]}}}
    )
    core = Core(settings=settings, paths=Paths.create(tmp_path / "home"), token="t")
    conn = SimpleNamespace(authenticated=True, closed=False, client_kind="ide",
                           remote_login=False)
    result = await get_handler(conn, SecurityGetParams(), core)
    assert isinstance(result, SecurityGetResult)
    assert result.problemItems == [] and result.problems == []

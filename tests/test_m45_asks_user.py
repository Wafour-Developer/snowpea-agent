"""Addendum 31: ``asksUser`` tag on tools (protocol 1.80.0, capability ``toolAsksUser``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.prompts import tool_descriptions
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION, HostToolSpec
from snowpea_core.session.session import Session
from snowpea_core.tools import asks_user, host_tools
from snowpea_core.tools.host_tools import HOST_TOOLS
from snowpea_core.tools.registry import ToolRegistry, register_builtin_tools
from snowpea_core.tools.tool_search import render


def _settings(**security: Any) -> Settings:
    return Settings.model_validate({"security": security})


def _session(tmp_path: Path, mode: str = "accept") -> Session:
    return Session(id="s", workdir=tmp_path, mode=mode)  # type: ignore[arg-type]


def _registry(settings: Settings | None = None) -> ToolRegistry:
    reg = register_builtin_tools(ToolRegistry())
    if settings is not None:
        reg.bind(settings)
    return reg


def _host_conn() -> Any:
    return SimpleNamespace(
        surface_id="host-1",
        client_id="browser-1",
        client_kind="browser",
        on_close=[],
    )


@pytest.fixture(autouse=True)
def _isolate_global_host_tools() -> Iterator[None]:
    saved_hosts = dict(HOST_TOOLS._hosts)
    saved_gone = dict(HOST_TOOLS._gone)
    yield
    HOST_TOOLS._hosts.clear()
    HOST_TOOLS._hosts.update(saved_hosts)
    HOST_TOOLS._gone.clear()
    HOST_TOOLS._gone.update(saved_gone)
    host_tools._invalidate_tool_prompt()


ALWAYS = asks_user.SUFFIX["always"]
FIRST = asks_user.SUFFIX["first_use"]
SOMETIMES = asks_user.SUFFIX["sometimes"]
ASK_USER_LINE = (
    "Don't use it to ask permission for a call whose description says it asks the user itself"
)


# -- protocol model -------------------------------------------------------------------------


def test_host_tool_spec_parsing() -> None:
    base = dict(name="use_tab", description="d", permission="read")
    assert HostToolSpec.model_validate(base).asksUser is None
    assert HostToolSpec.model_validate({**base, "asksUser": "always"}).asksUser == "always"
    assert HostToolSpec.model_validate({**base, "asksUser": "first_use"}).asksUser == "first_use"
    assert HostToolSpec.model_validate({**base, "asksUser": "sometimes"}).asksUser == "sometimes"
    assert HostToolSpec.model_validate({**base, "asksUser": "never"}).asksUser == "never"
    assert HostToolSpec.model_validate({**base, "asksUser": "bogus"}).asksUser == "never"


def test_parse_unknown_to_never() -> None:
    assert asks_user.parse(None) == "never"
    assert asks_user.parse("often") == "never"


def test_capability_and_protocol_version() -> None:
    assert PROTOCOL_VERSION == "1.80.0"
    assert "toolAsksUser" in CAPABILITIES


# -- built-in derivation --------------------------------------------------------------------


def _spec_named(
    reg: ToolRegistry, session: Session, tool: str, *, eager_only: bool = True
):
    specs = reg.specs(session) if eager_only else reg.all_specs(session)
    return next(item for item in specs if item.name == tool)


@pytest.mark.parametrize(
    ("mode", "tool", "enabled", "suffix", "eager_only"),
    [
        ("accept", "shell", True, ALWAYS, True),
        ("auto", "shell", True, "", True),
        ("plan", "shell", True, ALWAYS, True),
        ("accept", "shell", False, "", True),
        ("accept", "read_file", True, SOMETIMES, True),
        ("accept", "write_file", True, SOMETIMES, True),
        ("accept", "web_search", True, ALWAYS, False),
        ("auto", "settings_set", True, ALWAYS, False),
        ("plan", "settings_set", True, "", False),
    ],
)
def test_builtin_suffix_by_mode(
    tmp_path: Path,
    mode: str,
    tool: str,
    enabled: bool,
    suffix: str,
    eager_only: bool,
) -> None:
    settings = _settings(enabled=enabled)
    reg = _registry(settings)
    session = _session(tmp_path, mode)
    spec = _spec_named(reg, session, tool, eager_only=eager_only)
    if suffix:
        assert spec.description.endswith(suffix)
    else:
        for tag in asks_user.SUFFIX.values():
            assert tag not in spec.description


def test_eager_schema_shell_tag(tmp_path: Path) -> None:
    """The provider's eager tool list carries the tag on shell in accept mode."""
    settings = _settings(enabled=True)
    reg = _registry(settings)
    session = _session(tmp_path, "accept")
    shell = _spec_named(reg, session, "shell")
    assert shell.description.endswith(ALWAYS)


# -- host tools -----------------------------------------------------------------------------


def test_host_tool_suffix_in_eager_schema(tmp_path: Path) -> None:
    settings = _settings(enabled=True)
    reg = _registry(settings)
    core = SimpleNamespace(settings=settings, tools=reg)
    HOST_TOOLS.register(
        core,
        _host_conn(),
        [
            {
                "name": "use_tab",
                "description": "switch tab",
                "inputSchema": {"type": "object", "properties": {}},
                "permission": "read",
                "asksUser": "always",
            },
            {
                "name": "read_tab",
                "description": "read tab",
                "inputSchema": {"type": "object", "properties": {}},
                "permission": "read",
                "asksUser": "first_use",
            },
        ],
    )
    session = _session(tmp_path)
    session.host_tools_from = "host-1"
    use = next(s for s in reg.specs(session) if s.name == "use_tab")
    read = next(s for s in reg.specs(session) if s.name == "read_tab")
    assert use.description.endswith(ALWAYS)
    assert read.description.endswith(FIRST)


def test_host_unknown_asks_user_is_never(tmp_path: Path) -> None:
    settings = _settings(enabled=True)
    reg = _registry(settings)
    core = SimpleNamespace(settings=settings, tools=reg)
    HOST_TOOLS.register(
        core,
        _host_conn(),
        [
            {
                "name": "noop",
                "description": "noop",
                "inputSchema": {"type": "object", "properties": {}},
                "permission": "read",
                "asksUser": "maybe",
            }
        ],
    )
    session = _session(tmp_path)
    session.host_tools_from = "host-1"
    spec = next(s for s in reg.specs(session) if s.name == "noop")
    assert not any(tag in spec.description for tag in asks_user.SUFFIX.values())


# -- tool_search render ---------------------------------------------------------------------


def test_tool_search_output_carries_tag(tmp_path: Path) -> None:
    settings = _settings(enabled=True)
    reg = _registry(settings)
    session = _session(tmp_path, "accept")
    shell = next(s for s in reg.all_specs(session) if s.name == "shell")
    body = render([shell])
    assert ALWAYS in body
    assert "shell:" in body


# -- ASK_USER description -------------------------------------------------------------------


def test_ask_user_description_sentence() -> None:
    assert ASK_USER_LINE in tool_descriptions.ASK_USER


# -- stability within one mode --------------------------------------------------------------


def test_descriptions_stable_across_specs_calls(tmp_path: Path) -> None:
    settings = _settings(enabled=True)
    reg = _registry(settings)
    session = _session(tmp_path, "accept")
    once = {s.name: s.description for s in reg.specs(session)}
    twice = {s.name: s.description for s in reg.specs(session)}
    assert once == twice


def test_mode_change_changes_builtin_tag(tmp_path: Path) -> None:
    settings = _settings(enabled=True)
    reg = _registry(settings)
    accept = _session(tmp_path, "accept")
    auto = _session(tmp_path, "auto")
    shell_accept = next(s for s in reg.specs(accept) if s.name == "shell").description
    shell_auto = next(s for s in reg.specs(auto) if s.name == "shell").description
    assert ALWAYS in shell_accept
    assert ALWAYS not in shell_auto
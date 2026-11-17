# ruff: noqa: F811  (pytest fixtures imported from the sibling tmux test modules)
"""Tmux bridge follow-up: the person's slash commands and picker-driven mode switches.

Fake tmux only (``tests/fixtures/fake_tmux.py``): the picker screens are played
from a JSON world in which each key moves to another screen.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from test_external_claude import Conn
from test_external_tmux_bridge import FakeProbe, World, bridge_for, probe, world  # noqa: F401
from test_external_tmux_manager import SESSION, _remote, make  # noqa: F401

from snowpea_core.external.proc import ExternalError
from snowpea_core.external.tmux import check_text, looks_like_dialog
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no tmux on Windows")

IDLE = "› \n  ? for shortcuts\n"


def picker(selected: int) -> str:
    """codex 0.167.1's /permissions list (title and preset names from its binary)."""
    labels = ["Read Only", "Default", "Full Access"]
    rows = [f"{'›' if i == selected else ' '} {i + 1}. {label}  Codex can …"
            for i, label in enumerate(labels)]
    return "\n".join(["", "  Update Model Permissions", "", *rows, "",
                      "  Press enter to confirm or esc to go back", ""])


UPDATED = "• Permissions updated to Default\n› \n"


def keys(world: World) -> list[str]:
    """Each delivered key, '-l <text>' as the text."""
    out = []
    for argv in world.keys_sent():
        if argv[:1] == ["send-keys"]:
            out.append(argv[-1])
    return out


def play(world: World, screens: list[str], **on_key: int) -> None:
    world.data["screens"]["%3"] = screens
    world.data["at"] = {"%3": 0}
    world.data["on_key"] = {"literal": 0, **on_key}
    world.save()


# -- the picker ------------------------------------------------------------------------------


async def test_picker_happy_path(world: World, probe: FakeProbe) -> None:  # noqa: F811
    play(world, [IDLE, picker(0), picker(1), UPDATED], Enter=1, Down=1)
    bridge = await bridge_for(world, probe, "codex")
    assert await bridge.pick("Default", by="person", settle=0) is True
    assert keys(world) == ["/permissions", "Enter", "Down", "Enter"]


async def test_picker_up_presses_one_at_a_time(world: World, probe: FakeProbe) -> None:  # noqa: F811
    screens = [IDLE, picker(2), picker(1), picker(0), "• Permissions updated to Read Only\n"]
    play(world, screens, Enter=1, Up=1)
    bridge = await bridge_for(world, probe, "codex")
    assert await bridge.pick("Read Only", by="person", settle=0) is True
    assert keys(world) == ["/permissions", "Enter", "Up", "Up", "Enter"]


async def test_picker_not_recognised_changes_nothing_more(world: World,  # noqa: F811
                                                         probe: FakeProbe) -> None:  # noqa: F811
    play(world, [IDLE, "  Some other screen\n› 1. Something"], Enter=1)
    bridge = await bridge_for(world, probe, "codex")
    assert await bridge.pick("Default", by="person", settle=0) is False
    # No Escape: the picker header was never on screen.
    assert keys(world) == ["/permissions", "Enter"]


async def test_highlight_never_reaches_the_entry(world: World,  # noqa: F811
                                                 probe: FakeProbe) -> None:  # noqa: F811
    play(world, [IDLE, picker(0)], Enter=1, Down=0)
    bridge = await bridge_for(world, probe, "codex")
    assert await bridge.pick("Default", by="person", settle=0) is False
    sent = keys(world)
    assert sent[:2] == ["/permissions", "Enter"]
    assert sent[-1] == "Escape" and sent.count("Escape") == 1
    assert "Enter" not in sent[2:]  # never chose a line that was not the wanted one


async def test_label_missing_leaves_the_picker(world: World, probe: FakeProbe) -> None:  # noqa: F811
    renamed = picker(0).replace("Full Access", "Danger Zone")
    play(world, [IDLE, renamed], Enter=1)
    bridge = await bridge_for(world, probe, "codex")
    assert await bridge.pick("Full Access", by="person", settle=0) is False
    assert keys(world) == ["/permissions", "Enter", "Escape"]


async def test_codex_mode_switch_through_the_manager(make: Any) -> None:  # noqa: F811
    build, world, _probe = make
    play(world, [IDLE, picker(0), picker(1), UPDATED], Enter=1, Down=1)
    env = build("codex")
    await env.open(Conn("ide"))
    for conn in (None, _remote(), Conn("connector")):
        with pytest.raises(ExternalError) as caught:
            await env.manager.set_mode("codex", SESSION, native_mode="default", conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert keys(world) == []
    row = (await env.manager.set_mode("codex", SESSION, native_mode="default",
                                      conn=Conn("ide")))["session"]
    assert (row["mode"], row["nativeMode"]) == ("default", "default")
    assert row["terminal"]["modeState"] == "confirmed"
    # A person-only mode: the person's grant is in the ledger.
    ledger = (env.manager.home / "jarvis" / "ledger.jsonl").read_text()
    assert "Default" in ledger


# -- slash commands from the person ------------------------------------------------------------


def test_slash_text_rules() -> None:
    check_text("/model opus", slash_ok=True)
    check_text("/permissions", slash_ok=True)
    for bad in ("/model\nopus", "/model \x1b[2J", "!ls", "/", "/ model", "!/x"):
        with pytest.raises(ExternalError):
            check_text(bad, slash_ok=True)
    with pytest.raises(ExternalError):
        check_text("/model opus", slash_ok=False)
    assert looks_like_dialog(picker(0))
    assert looks_like_dialog("Do you want to proceed?\n❯ 1. Yes\n Esc to cancel")
    assert not looks_like_dialog(IDLE)


async def test_the_local_person_types_a_slash_command(make: Any) -> None:  # noqa: F811
    build, world, _probe = make
    play(world, [IDLE, picker(0)], Enter=1)
    env = build("codex")
    conn = Conn("ide")
    await env.open(conn)
    sent = await env.manager.send("codex", SESSION, "/permissions", conn=conn)
    assert sent["via"] == "tmux"
    assert keys(world) == ["/permissions", "Enter"]
    # It left a list open: the row says something waits there; no model turn hangs.
    from _claude_support import until

    done = await until(lambda: conn.turn_done(sent["turnId"]), 5)
    assert done["payload"]["reason"] == "complete"
    row = (await env.manager.get("codex", SESSION))["session"]
    assert row["terminal"]["prompt"] == {"recognised": False, "text": None}
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("codex", SESSION, "more", conn=conn)
    assert caught.value.details == {"reason": "terminal_prompt"}


async def test_slash_and_bang_refused_for_everyone_else(make: Any) -> None:  # noqa: F811
    build, world, _probe = make
    env = build()  # Claude, default mode confirmed: Jarvis may send plain text
    await env.open(Conn("ide"))
    for conn in (None, _remote()):
        with pytest.raises(ExternalError) as caught:
            await env.manager.send("claude", SESSION, "/model opus", conn=conn)
        assert caught.value.code == errors.INVALID_PARAMS
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("claude", SESSION, "/model opus", conn=Conn("connector"))
    assert caught.value.code in (errors.INVALID_PARAMS, errors.JARVIS_PERSON_ONLY)
    for text in ("!ls", "  !rm x", "/model\nopus"):
        with pytest.raises(ExternalError) as caught:
            await env.manager.send("claude", SESSION, text, conn=Conn("ide"))
        assert caught.value.code == errors.INVALID_PARAMS
    assert keys(world) == []

"""M37 §2.4–§2.5: the input gate (fake clock) and viewer flow control."""

from __future__ import annotations

import asyncio

import pytest

from snowpea_core.terminal.input import (
    HUMAN_QUIET,
    PASTE_END,
    PASTE_START,
    GateRefused,
    InputGate,
    encode_key,
)
from snowpea_core.terminal.viewers import ViewerSet


class Fake:
    """A terminal for the gate: a clock, written bytes and a status."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.out: list[bytes] = []
        self.state = "idle"
        self.turn = 0
        self.bracketed = False
        self.last_output = 0.0
        self.go_working_on_submit = True

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)

    def write(self, data: bytes) -> None:
        self.out.append(data)
        if data == b"\r" and self.go_working_on_submit:
            self.state = "working"

    def gate(self, **kwargs) -> InputGate:
        return InputGate(
            self.write,
            state=lambda: self.state,
            turn=lambda: self.turn,
            last_output_at=lambda: self.last_output,
            bracketed_paste=lambda: self.bracketed,
            clock=self.clock,
            sleep=self.sleep,
            launched_at=self.t - 60,
            **kwargs,
        )


async def test_plain_paste_then_submit_and_accepted() -> None:
    fake = Fake()
    gate = fake.gate()
    result = await gate.send("hello")
    assert fake.out == [b"hello", b"\r"]
    assert result.accepted


async def test_bracketed_paste_only_with_2004_and_end_marker_stripped() -> None:
    fake = Fake()
    fake.bracketed = True
    gate = fake.gate()
    await gate.send("a" + PASTE_END.decode() + "b")
    # Agent text loses its ESC (security review H1): what is left is plain text.
    assert fake.out[0] == PASTE_START + b"a[201~b" + PASTE_END
    # A split marker cannot be rebuilt by one stripping pass.
    from snowpea_core.terminal.input import strip_paste_end

    assert strip_paste_end(b"x\x1b[20\x1b[201~1~y") == b"xy"


async def test_not_accepted_when_it_never_goes_working() -> None:
    fake = Fake()
    fake.go_working_on_submit = False
    result = await fake.gate().send("x")
    assert not result.accepted


async def test_blocked_is_refused_unless_forced() -> None:
    fake = Fake()
    fake.state = "blocked"
    gate = fake.gate()
    with pytest.raises(GateRefused) as refused:
        await gate.send("y")
    assert refused.value.reason == "blocked"
    assert fake.out == []
    await gate.send("y", force=True)
    assert fake.out[0] == b"y"


async def test_waits_for_human_quiet_and_holds_their_keys() -> None:
    fake = Fake()
    gate = fake.gate()
    await gate.human(b"l")
    await gate.human(b"s\r")  # a finished line: no draft, but not quiet yet
    start = fake.t
    await gate.send("agent")
    assert fake.t - start >= HUMAN_QUIET - 0.01
    assert fake.out[:2] == [b"l", b"s\r"]
    assert fake.out[2:4] == [b"agent", b"\r"]


async def test_unfinished_draft_is_waited_on() -> None:
    fake = Fake()
    gate = fake.gate()
    await gate.human(b"git sta")
    assert gate.draft
    task = asyncio.ensure_future(gate.send("agent"))
    for _ in range(100):  # 5 s on the fake clock: still waiting on the draft
        await asyncio.sleep(0)
        fake.t += 0.05
    assert b"agent" not in fake.out
    await gate.human(b"tus\r")
    assert not gate.draft
    result = await task
    assert result.accepted
    assert fake.out.index(b"agent") > fake.out.index(b"tus\r")


async def test_keys_typed_during_agent_paste_are_held_then_flushed() -> None:
    fake = Fake()
    gate = fake.gate()
    original = fake.sleep

    async def sleep_and_type(seconds: float) -> None:
        if gate._agent_active and b"Z" not in b"".join(fake.out):
            await gate.human(b"Z")
        await original(seconds)

    gate._sleep = sleep_and_type
    await gate.send("agent")
    assert fake.out[:3] == [b"agent", b"\r", b"Z"]


async def test_startup_wait_for_quiet_output() -> None:
    fake = Fake()
    gate = fake.gate()
    gate.launched_at = fake.t  # just launched
    fake.last_output = fake.t  # and printing
    start = fake.t
    await gate.send("first")
    assert fake.t - start >= 0.8


async def test_submit_key_from_profile_and_kitty_mode() -> None:
    fake = Fake()
    fake.go_working_on_submit = False
    gate = fake.gate(submit_key="tab", kitty_flags=lambda: 8)
    await gate.send("x")
    assert fake.out[-1] == b"\x1b[9u"


async def test_named_keys_and_interrupt() -> None:
    fake = Fake()
    gate = fake.gate(reports_status=lambda: True)
    await gate.keys(["Down", "Enter", "ctrl+c", "y"])
    assert fake.out == [b"\x1b[B", b"\r", b"\x03", b"y"]
    with pytest.raises(GateRefused):
        await gate.keys(["nonsense-key"])
    await gate.interrupt()
    assert fake.out[-1] == b"\x1b"
    plain = Fake()
    await plain.gate().interrupt()
    assert plain.out == [b"\x03"]
    assert encode_key("up", app_cursor=True) == b"\x1bOA"


# -- flow control ---------------------------------------------------------------


def test_credit_limits_unacked_bytes_and_ack_releases_more() -> None:
    viewers = ViewerSet(lambda: b"REPAINT", credit=10, max_behind=100)
    conn = object()
    viewers.attach(conn, "interactive", 80, 24)
    sent = viewers.broadcast(b"0123456789abcdef")
    assert b"".join(d.data for d in sent) == b"0123456789"
    assert sent[-1].seq == 10
    assert viewers.broadcast(b"ghij") == []
    more = viewers.ack(conn, 10)
    assert b"".join(d.data for d in more) == b"abcdefghij"
    assert more[-1].seq == 20 == viewers.seq


def test_viewer_too_far_behind_gets_a_repaint() -> None:
    viewers = ViewerSet(lambda: b"REPAINT", credit=10, max_behind=50)
    slow, fast = object(), object()
    viewers.attach(slow, "observe")
    viewers.attach(fast, "interactive", 80, 24)
    for _ in range(10):
        viewers.broadcast(b"x" * 10)
        viewers.ack(fast, 10)
    assert viewers.get(slow).needs_repaint
    again = viewers.ack(slow, 10)
    assert len(again) == 1 and again[0].repaint and again[0].data == b"REPAINT"
    assert again[0].seq == viewers.seq


def test_size_owner_is_the_last_active_interactive_viewer() -> None:
    viewers = ViewerSet(lambda: b"")
    a, b, watcher = object(), object(), object()
    viewers.attach(a, "interactive", 80, 24)
    viewers.attach(b, "interactive", 120, 40)
    viewers.attach(watcher, "observe", 200, 60)
    assert viewers.size_owner().conn is b
    assert viewers.resize(a, 100, 30)
    assert viewers.size_owner().conn is a
    assert not viewers.resize(watcher, 10, 10)
    viewers.detach(a)
    assert viewers.size_owner().conn is b
    viewers.detach(b)
    assert viewers.size_owner() is None and not viewers.has_human

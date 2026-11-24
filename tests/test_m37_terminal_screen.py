"""M37 §2.2–§2.3: the pyte screen, its repaint, and the escape scanner (OSC 7501, queries)."""

from __future__ import annotations

from snowpea_core.terminal import osc
from snowpea_core.terminal.screen import Screen


def _same_screen(a: Screen, b: Screen) -> None:
    assert (a.cols, a.rows) == (b.cols, b.rows)
    for y in range(a.rows):
        for x in range(a.cols):
            assert a.cell(y, x) == b.cell(y, x), (y, x, a.cell(y, x), b.cell(y, x))
    assert a.cursor == b.cursor


def _round_trip(source: Screen) -> Screen:
    copy = Screen(source.cols, source.rows)
    copy.feed(source.repaint())
    return copy


def test_repaint_round_trip_rebuilds_cells_history_modes_and_title() -> None:
    screen = Screen(40, 6)
    for i in range(12):
        screen.feed(b"row %d \x1b[1;31mbold red\x1b[0m " % i
                    + b"\x1b[38;5;123mx\x1b[48;2;1;2;3my\x1b[0m\r\n")
    screen.feed(b"\x1b[4;7m\xe4\xb8\xad\xe6\x96\x87\x1b[0m wide \x1b]2;my title\x07")
    screen.feed(b"\x1b[?2004h\x1b[?1h\x1b[?1000h\x1b[3;5H")
    copy = _round_trip(screen)
    _same_screen(screen, copy)
    assert copy.text(4) == screen.text(4)
    assert copy.history_size == screen.history_size == 7
    assert copy.title == "my title"
    assert copy.bracketed_paste and copy.app_cursor and copy.mouse


def test_repaint_keeps_hidden_cursor_and_scroll_region() -> None:
    screen = Screen(20, 5)
    screen.feed(b"hello\x1b[2;4r\x1b[?25l\x1b[5;2H")
    copy = _round_trip(screen)
    assert not copy.cursor_visible
    _same_screen(screen, copy)
    assert copy._screen.margins == screen._screen.margins


def test_alt_screen_hides_and_restores_the_primary_screen() -> None:
    screen = Screen(30, 4)
    screen.feed(b"shell line 1\r\nshell line 2\x1b[?1049h\x1b[2J\x1b[Hfull screen app")
    assert screen.alt_screen
    assert screen.text() == "full screen app"
    copy = _round_trip(screen)
    assert copy.alt_screen
    assert copy.text() == "full screen app"
    for s in (screen, copy):
        s.feed(b"\x1b[?1049l")
        assert not s.alt_screen
        assert s.text() == "shell line 1\nshell line 2"
        assert s.cursor == (1, 12)


def test_alt_screen_output_never_reaches_history() -> None:
    screen = Screen(20, 3)
    screen.feed(b"\x1b[?1049h" + b"x\r\n" * 10 + b"\x1b[?1049l")
    assert screen.history_size == 0


def test_text_includes_history_lines_without_ansi() -> None:
    screen = Screen(20, 3)
    screen.feed(b"".join(b"\x1b[32mline %d\x1b[0m\r\n" % i for i in range(6)))
    assert screen.text() == "line 4\nline 5"
    assert screen.text(2) == "line 2\nline 3\nline 4\nline 5"
    assert "\x1b" not in screen.text(10)


def test_status_record_split_across_chunks() -> None:
    record = osc.encode_status("blocked", kind="permission", msg="allow rm?", id="", app="fake")
    for cut in range(1, len(record)):
        scanner = osc.EscapeScanner()
        events = scanner.feed(b"before " + record[:cut]) + scanner.feed(record[cut:] + b" after")
        statuses = [e for e in events if isinstance(e, osc.StatusRecord)]
        assert len(statuses) == 1, cut
        assert statuses[0].state == "blocked"
        assert statuses[0].kind == "permission"
        assert statuses[0].msg == "allow rm?"
        assert statuses[0].app == "fake"
        assert statuses[0].root


def test_status_record_bel_terminator_and_unknown_state() -> None:
    scanner = osc.EscapeScanner()
    events = scanner.feed(b"\x1b]7501;state=working:progress=140\x07\x1b]7501;state=bogus\x1b\\")
    assert events == [osc.StatusRecord(state="working", progress=100)]
    assert scanner.feed(b"\x1b]7501;state=clear:id=sub\x1b\\") == [
        osc.StatusRecord(state="clear", id="sub")
    ]


def test_queries_are_found_and_answered() -> None:
    scanner = osc.EscapeScanner()
    events = scanner.feed(b"\x1b[c\x1b[>c\x1b[6n\x1b]11;?\x07\x1b[?u\x1b]7501;?\x1b\\\x1b[5n")
    kinds = [e.kind for e in events if isinstance(e, osc.Query)]
    assert kinds == ["da1", "da2", "dsr-cursor", "color-bg", "kitty-keys", "status-query",
                     "dsr-status"]
    queries = [e for e in events if isinstance(e, osc.Query)]
    assert osc.answer(queries[2], cursor=(4, 9)) == b"\x1b[5;10R"
    assert osc.answer(queries[3]).endswith(b"\x07")  # same terminator as the query
    assert osc.answer(queries[5]) == b"\x1b]7501;?\x1b\\"


def test_kitty_keyboard_flags_push_pop_set() -> None:
    scanner = osc.EscapeScanner()
    scanner.feed(b"\x1b[>1u\x1b[>9u")
    assert scanner.kitty_keyboard == 9
    scanner.feed(b"\x1b[<u")
    assert scanner.kitty_keyboard == 1
    scanner.feed(b"\x1b[=8;2u")
    assert scanner.kitty_keyboard == 9
    scanner.feed(b"\x1b[<5u")
    assert scanner.kitty_keyboard == 0


def test_titles_from_osc_0_and_2_and_oversized_osc_dropped() -> None:
    screen = Screen(20, 3)
    screen.feed(b"\x1b]0;first\x07")
    assert screen.title == "first"
    screen.feed(b"\x1b]2;sec")
    screen.feed(b"ond\x1b\\")
    assert screen.title == "second"
    scanner = osc.EscapeScanner()
    assert scanner.feed(b"\x1b]2;" + b"x" * (osc.MAX_OSC + 10) + b"\x07") == []
    assert scanner.feed(b"\x1b]2;ok\x07") == [osc.TitleChange("ok")]

"""Windows CLI output encoding must handle Unicode when stdout is redirected."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap

import pytest

from snowpea_core.cli import main as cli_main


def _cp1252_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")


def test_windows_stdio_helper_reconfigures_redirected_cp1252_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main.sys, "platform", "win32")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    out_raw, out = _cp1252_stream()
    err_raw, err = _cp1252_stream()

    cli_main._configure_windows_stdio_encoding(out, err)

    assert out.encoding.lower().replace("_", "-") == "utf-8"
    assert err.encoding.lower().replace("_", "-") == "utf-8"
    out.write("▶ 도구(text=한글)\n")
    err.write("✗ 오류\n")
    out.flush()
    err.flush()
    assert "도구" in out_raw.getvalue().decode("utf-8")
    assert "오류" in err_raw.getvalue().decode("utf-8")


def test_windows_stdio_helper_respects_explicit_pythonioencoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main.sys, "platform", "win32")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    _raw, out = _cp1252_stream()

    cli_main._configure_windows_stdio_encoding(out, out)

    assert out.encoding.lower() == "cp1252"


def test_stdio_helper_leaves_non_windows_streams_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main.sys, "platform", "linux")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    _raw, out = _cp1252_stream()

    cli_main._configure_windows_stdio_encoding(out, out)

    assert out.encoding.lower() == "cp1252"


def test_stdio_helper_ignores_stringio_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main.sys, "platform", "win32")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    out = io.StringIO()

    cli_main._configure_windows_stdio_encoding(out, out)
    out.write("한글")

    assert out.getvalue() == "한글"


def test_windows_cp1252_subprocess_renders_plain_and_json_as_utf8() -> None:
    script = textwrap.dedent(
        """
        import io
        import os
        import sys
        from snowpea_core.cli import main as cli_main
        from snowpea_core.cli.render import JsonRenderer, PlainRenderer

        cli_main.sys.platform = "win32"
        os.environ.pop("PYTHONIOENCODING", None)
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
        cli_main._configure_windows_stdio_encoding(stream, stream)
        PlainRenderer(out=stream, err=stream).event({
            "kind": "tool.call",
            "payload": {"name": "도구", "args": {"text": "한글"}},
        })
        JsonRenderer(out=stream).event({"kind": "message.delta", "payload": {"text": "한글"}})
        stream.flush()
        sys.stdout.buffer.write(raw.getvalue())
        """
    )
    env = os.environ.copy()
    env.pop("PYTHONIOENCODING", None)

    done = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=env,
    )

    output = done.stdout.decode("utf-8")
    assert "▶ 도구(text=한글)" in output
    assert '"text": "한글"' in output

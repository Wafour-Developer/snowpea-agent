"""M0 smoke tests: the package imports and the console entry point answers."""

from __future__ import annotations

import os

import pytest

import snowpea_core
from snowpea_core.cli.main import main


def test_version() -> None:
    assert snowpea_core.__version__ == "0.1.0"


def test_snowpea_home_is_isolated() -> None:
    assert os.environ["SNOWPEA_HOME"]


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "snowpea 0.1.0"


def test_cli_placeholder_exits_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2

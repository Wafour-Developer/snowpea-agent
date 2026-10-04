"""Regression checks for cross-platform e2e smoke scripts.

The e2e scripts are shell/PowerShell mirrors. These lightweight tests catch
script drift that previously made remote CI fail without useful annotations.
"""

from __future__ import annotations

from pathlib import Path

import snowpea_core

REPO = Path(__file__).resolve().parents[1]
WINDOWS_SMOKE = REPO / "tests" / "e2e" / "v01_smoke.ps1"


def _windows_script() -> str:
    return WINDOWS_SMOKE.read_text(encoding="utf-8")


def test_windows_smoke_uses_checkout_version_not_old_minor_series() -> None:
    script = _windows_script()

    assert "$ExpectedVersion" in script
    assert "core\\snowpea_core\\__init__.py" in script
    assert "snowpea $ExpectedVersion" in script
    assert "^snowpea 0\\.1\\." not in script
    assert "expected 'snowpea 0.1.x'" not in script
    assert snowpea_core.__version__ not in script


def test_windows_smoke_plan_step_denies_source_not_document() -> None:
    script = _windows_script()

    assert "write foo.py" in script
    assert "foo.py" in script
    assert "write foo.txt" not in script
    assert "foo.txt" not in script
    assert "plan mode denied the write to source" in script


def test_windows_smoke_emits_github_actions_error_annotations() -> None:
    script = _windows_script()

    assert "function Escape-GitHubActionsAnnotation" in script
    assert "function Write-GitHubActionsError" in script
    assert "-replace '%', '%25'" in script
    assert "-replace \"`r\", '%0D'" in script
    assert "-replace \"`n\", '%0A'" in script
    assert "Write-GitHubActionsError \"Snowpea Windows smoke step $N failed\"" in script
    assert "Write-GitHubActionsError 'Snowpea Windows smoke install failed' $fatal" in script
    assert "$env:GITHUB_ACTIONS -eq 'true'" in script

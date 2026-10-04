"""Keep smoke failures observable without changing their exit status."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_shell_failure_is_a_public_escaped_ci_annotation() -> None:
    source = (ROOT / "tests/e2e/v01_smoke.sh").read_text()
    function = source.split("fail_step() {", 1)[1].split("\n}", 1)[0]
    result = subprocess.run(
        [
            "bash",
            "-c",
            "FAILED=0; fail_step() {" + function + '\n}; fail_step 7 "$1"; echo "FAILED=$FAILED"',
            "smoke",
            "bad 50%\nsecond line\r",
        ],
        env={**os.environ, "GITHUB_ACTIONS": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "::error::Smoke step 7: bad 50%25%0Asecond line%0D" in result.stdout
    assert "FAILED=1" in result.stdout


def test_all_ci_smokes_preserve_output_and_failure_status() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for name in ("ubuntu", "macos", "windows"):
        job = workflow.split(f"  e2e-{name}:", 1)[1].split("\n  e2e-", 1)[0]
        assert "if: always()" in job
        assert "actions/upload-artifact@v4" in job
        assert "path: smoke.log" in job
        if name == "windows":
            assert "Tee-Object -FilePath smoke.log" in job
            assert "exit $LASTEXITCODE" in job
        else:
            assert "set -o pipefail" in job
            assert "2>&1 | tee smoke.log" in job

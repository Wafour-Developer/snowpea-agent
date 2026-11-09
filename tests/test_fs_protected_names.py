"""More credential names are protected, and Windows spellings of them too."""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.agent.prompt_refs import is_secret_pattern
from snowpea_core.server.fs_handlers import _windows_clean


@pytest.mark.parametrize(
    "name",
    [
        ".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass",
        "id_dsa", "id_ecdsa.pub", "cert.p12", "cert.PFX", "store.jks", "app.keystore",
        "vault.kdbx", "terraform.tfstate", "terraform.tfstate.backup", "corp.ovpn",
        "putty.ppk", ".docker/config.json", ".kube/config", ".azure/accessTokens.json",
    ],
)
def test_these_names_are_protected(name: str) -> None:
    assert is_secret_pattern(Path("/work/app") / name)


@pytest.mark.parametrize("name", ["README.md", "notes.txt", "id_card.png", "main.py"])
def test_ordinary_files_are_not(name: str) -> None:
    assert not is_secret_pattern(Path("/work/app") / name)


@pytest.mark.parametrize(
    ("spelled", "plain"),
    [
        ("/w/secret.pem::$DATA", "/w/secret.pem"),
        ("/w/secret.pem.", "/w/secret.pem"),
        ("/w/secret.pem ", "/w/secret.pem"),
        ("/w/.env:Zone.Identifier", "/w/.env"),
        ("C:\\w\\.npmrc.", "C:\\w\\.npmrc"),
        ("/w/ok.txt", "/w/ok.txt"),
    ],
)
def test_windows_spellings_reach_the_plain_name(spelled: str, plain: str) -> None:
    assert _windows_clean(spelled) == plain
    assert is_secret_pattern(Path(_windows_clean(spelled))) == is_secret_pattern(Path(plain))

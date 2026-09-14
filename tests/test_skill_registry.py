"""``skills/registry_client.py``, ``skills/publish.py`` and ``skill publish|rate``
against a fake registry (no network) — CORE-registry-client.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.cli import commands as cli_commands
from snowpea_core.config.settings import Settings
from snowpea_core.skills import marketplace, publish, registry_client
from snowpea_core.skills.marketplace import SOURCE_REGISTRY, install
from snowpea_core.skills.publish import PublishError, build_zip, load_skill_dir
from snowpea_core.skills.registry_client import (
    HttpRegistryClient,
    RegistryError,
    resolve_token,
    resolve_url,
)
from snowpea_core.tools import http_util


def mock_transport(handler: Any) -> Any:
    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("guard_ssrf", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


# ---------------------------------------------------------------------------
# resolve_url / resolve_token precedence
# ---------------------------------------------------------------------------


def test_resolve_url_prefers_explicit_over_env_over_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SNOWPEA_REGISTRY_URL", raising=False)
    settings = Settings()
    settings.skills.registry.url = "https://settings.example/v1"
    assert resolve_url(settings=settings) == "https://settings.example/v1"

    monkeypatch.setenv("SNOWPEA_REGISTRY_URL", "https://env.example/v1")
    assert resolve_url(settings=settings) == "https://env.example/v1"

    assert resolve_url("https://explicit.example/v1", settings) == "https://explicit.example/v1"


def test_resolve_url_default_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNOWPEA_REGISTRY_URL", raising=False)
    assert resolve_url() == registry_client.REGISTRY_URL


def test_resolve_token_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNOWPEA_REGISTRY_TOKEN", raising=False)
    settings = Settings()
    settings.skills.registry.token = "settings-token"
    assert resolve_token(settings=settings) == "settings-token"

    monkeypatch.setenv("SNOWPEA_REGISTRY_TOKEN", "env-token")
    assert resolve_token(settings=settings) == "env-token"

    assert resolve_token("explicit-token", settings) == "explicit-token"

    monkeypatch.delenv("SNOWPEA_REGISTRY_TOKEN", raising=False)
    assert resolve_token(settings=None) is None


# ---------------------------------------------------------------------------
# HttpRegistryClient — search / download / publish / rate
# ---------------------------------------------------------------------------


async def test_search_returns_results_and_survives_a_dead_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=ralplan" in str(request.url)
        assert "sources=all" in str(request.url)
        assert "live=1" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "ralplan",
                        "name": "ralplan",
                        "description": "Consensus planning.",
                        "installSpec": "registry:ralplan",
                        "source": "local",
                        "sourceLabel": "snowpea registry",
                    }
                ],
                "total": 1,
                "page": 1,
                "perPage": 20,
                "nextPage": None,
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    hits = await client.search("ralplan")
    assert hits[0]["id"] == "ralplan"


async def test_search_with_sources_surfaces_per_hub_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "clawhub:@cua/driver",
                        "name": "driver",
                        "description": "CUA driver skill.",
                        "installSpec": "clawhub:@cua/driver",
                        "source": "clawhub",
                        "sourceLabel": "ClawHub",
                    }
                ],
                "unavailable": [
                    {
                        "id": "hermes",
                        "label": "Hermes Hub",
                        "reason": "hermes-hub.ai does not resolve (NXDOMAIN).",
                    }
                ],
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    results, hub_failures = await client.search_with_sources("driver")
    assert results[0]["id"] == "clawhub:@cua/driver"
    assert hub_failures == [
        {
            "id": "hermes",
            "label": "Hermes Hub",
            "reason": "hermes-hub.ai does not resolve (NXDOMAIN).",
        }
    ]


async def test_search_offline_returns_empty_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://dead.example/v1")
    assert await client.search("anything") == []
    assert await client.search_with_sources("anything") == ([], [])


async def test_resolve_returns_download_url() -> None:
    client = HttpRegistryClient("https://registry.example/v1")
    assert (
        await client.resolve("registry:ralplan")
        == "https://registry.example/v1/skills/ralplan/download"
    )
    assert (
        await client.resolve("ralplan") == "https://registry.example/v1/skills/ralplan/download"
    )
    assert await client.resolve("clawhub:@cua/driver") == (
        "https://registry.example/v1/skills/clawhub%3A%40cua%2Fdriver/download"
    )


async def test_sources_lists_hub_health(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/sources")
        return httpx.Response(
            200,
            json={
                "sources": [
                    {"id": "local", "label": "snowpea registry", "enabled": True, "count": 6},
                    {
                        "id": "hermes",
                        "label": "Hermes Hub",
                        "enabled": False,
                        "disabledReason": "hermes-hub.ai does not resolve (NXDOMAIN).",
                    },
                ]
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    sources = await client.sources()
    assert {row["id"] for row in sources} == {"local", "hermes"}


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


async def test_download_returns_content_and_rejects_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _zip_bytes({"ralplan/SKILL.md": "---\nname: ralplan\n---\nbody"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={
                "content-type": "application/zip",
                "x-snowpea-version": "1.0.0",
                "content-disposition": 'attachment; filename="ralplan-1.0.0.zip"',
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    downloaded = await client.download("registry:ralplan")
    assert downloaded.content == payload
    assert downloaded.version == "1.0.0"
    assert downloaded.filename == "ralplan-1.0.0.zip"

    monkeypatch.setattr(registry_client, "MAX_DOWNLOAD_BYTES", 4)
    with pytest.raises(RegistryError, match="byte cap"):
        await client.download("registry:ralplan")


async def test_download_404_raises_registry_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "nope"}})

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    with pytest.raises(RegistryError):
        await client.download("missing")


async def test_download_percent_encodes_a_federated_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"zip-bytes")

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    await client.download("clawhub:@cua/driver")
    assert seen["url"] == "https://registry.example/v1/skills/clawhub%3A%40cua%2Fdriver/download"


async def test_download_501_raises_not_fetchable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            501, json={"error": {"code": "not_fetchable", "message": "no archive for this hub"}}
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    with pytest.raises(registry_client.RegistryNotFetchable, match="no archive"):
        await client.download("clawhub:@cua/driver")


async def test_publish_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(
            201,
            json={
                "ok": True,
                "created": True,
                "skill": {"id": "ralplan", "installUrl": "https://registry.example/v1/x"},
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    result = await client.publish(b"zip-bytes", filename="ralplan.zip", token="spr_abc")
    assert result["created"] is True
    assert seen["auth"] == "Bearer spr_abc"
    assert seen["url"].endswith("/skills")


async def test_publish_without_token_raises() -> None:
    client = HttpRegistryClient("https://registry.example/v1")
    with pytest.raises(RegistryError, match="token"):
        await client.publish(b"zip-bytes")


async def test_publish_401_raises_registry_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "bad token"}}
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    with pytest.raises(RegistryError, match="bad token"):
        await client.publish(b"zip-bytes", token="wrong")


async def test_publish_validation_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"error": {"code": "invalid_package", "message": "no SKILL.md"}}
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    with pytest.raises(RegistryError, match="no SKILL.md"):
        await client.publish(b"zip-bytes", token="spr_abc")


async def test_rate_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"stars": 5, "comment": "great"}
        return httpx.Response(200, json={"ok": True, "rating": 4.8, "ratingCount": 4})

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    client = HttpRegistryClient("https://registry.example/v1")
    result = await client.rate("ralplan", 5, comment="great")
    assert result["ratingCount"] == 4


# ---------------------------------------------------------------------------
# skills/publish.py — local validation and zipping
# ---------------------------------------------------------------------------


def test_validate_frontmatter_rejects_bad_name() -> None:
    with pytest.raises(PublishError, match="name"):
        publish.validate_frontmatter("---\nname: Bad Name!\ndescription: ok ok ok ok\n---\n")


def test_validate_frontmatter_rejects_short_description() -> None:
    with pytest.raises(PublishError, match="description"):
        publish.validate_frontmatter("---\nname: ok\ndescription: hi\n---\n")


def test_load_skill_dir_requires_skill_md(tmp_path: Path) -> None:
    empty = tmp_path / "empty-skill"
    empty.mkdir()
    with pytest.raises(PublishError, match="SKILL.md"):
        load_skill_dir(empty)


def test_build_zip_excludes_junk(tmp_path: Path) -> None:
    root = tmp_path / "my-skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A properly sized description here.\n---\nbody",
        encoding="utf-8",
    )
    (root / "reference.md").write_text("notes", encoding="utf-8")
    junk = root / "__pycache__"
    junk.mkdir()
    (junk / "x.pyc").write_bytes(b"junk")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")

    package = load_skill_dir(root)
    data = build_zip(package)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
    assert names == {"my-skill/SKILL.md", "my-skill/reference.md"}


# ---------------------------------------------------------------------------
# marketplace.install("registry:<id>") — path-safety and extraction
# ---------------------------------------------------------------------------


class _FakeDownloadClient:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download(self, identifier: str, *, version: str | None = None) -> Any:
        return registry_client.DownloadedSkill(
            content=self.content, version="1.0.0", filename=None
        )


async def test_install_registry_spec_extracts_and_strips_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _zip_bytes(
        {
            "ralplan/SKILL.md": "---\nname: ralplan\n---\nbody",
            "ralplan/references/style.md": "notes",
        }
    )
    monkeypatch.setattr(registry_client, "CLIENT", _FakeDownloadClient(payload))
    plugins_dir = tmp_path / "plugins"
    target = await install("registry:ralplan", plugins_dir, tmp_path / "home")
    assert target == plugins_dir / "ralplan"
    assert (target / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (target / "references" / "style.md").exists()


async def test_install_registry_spec_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _zip_bytes({"../../evil.txt": "pwned"})
    monkeypatch.setattr(registry_client, "CLIENT", _FakeDownloadClient(payload))
    plugins_dir = tmp_path / "plugins"
    with pytest.raises(marketplace.InstallError):
        await install("registry:evil", plugins_dir, tmp_path / "home")
    assert not (tmp_path / "evil.txt").exists()


async def test_search_includes_registry_source_with_its_own_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A federated hit's ``sourceLabel`` becomes the hit's ``source``, not a
    fixed ``snowpea-registry`` constant — different hits from the same call
    can carry different hub labels."""

    class _Client:
        async def search_with_sources(
            self, query: str
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return (
                [
                    {
                        "id": "ralplan",
                        "name": "ralplan",
                        "description": "planning",
                        "installSpec": "registry:ralplan",
                        "source": "local",
                        "sourceLabel": "snowpea registry",
                    },
                    {
                        "id": "clawhub:@cua/driver",
                        "name": "driver",
                        "description": "driver skill",
                        "installSpec": "clawhub:@cua/driver",
                        "source": "clawhub",
                        "sourceLabel": "ClawHub",
                    },
                ],
                [],
            )

    monkeypatch.setattr(registry_client, "CLIENT", _Client())

    async def empty(*_args: Any, **_kwargs: Any) -> marketplace.SourceResult:
        return marketplace.SourceResult()

    monkeypatch.setattr(marketplace, "search_claude_marketplaces", empty)

    report = await marketplace.search("ralplan", "/tmp")
    assert {hit.source for hit in report.hits} == {"snowpea registry", "ClawHub"}


async def test_search_reports_per_hub_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        async def search_with_sources(
            self, query: str
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return [], [{"id": "hermes", "label": "Hermes Hub", "reason": "does not resolve"}]

    monkeypatch.setattr(registry_client, "CLIENT", _Client())

    async def empty(*_args: Any, **_kwargs: Any) -> marketplace.SourceResult:
        return marketplace.SourceResult()

    monkeypatch.setattr(marketplace, "search_claude_marketplaces", empty)

    report = await marketplace.search("x", "/tmp")
    assert any("Hermes Hub" in line and "does not resolve" in line for line in report.unavailable)


async def test_search_whole_registry_down_reports_generic_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DeadClient:
        async def search_with_sources(
            self, query: str
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            raise ConnectionError("refused")

    monkeypatch.setattr(registry_client, "CLIENT", _DeadClient())

    async def empty(*_args: Any, **_kwargs: Any) -> marketplace.SourceResult:
        return marketplace.SourceResult()

    monkeypatch.setattr(marketplace, "search_claude_marketplaces", empty)

    report = await marketplace.search("x", "/tmp")
    assert any(SOURCE_REGISTRY in line for line in report.unavailable)


# ---------------------------------------------------------------------------
# marketplace.install — federated install specs
# ---------------------------------------------------------------------------


async def test_install_clawhub_spec_downloads_via_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _zip_bytes({"driver/SKILL.md": "---\nname: driver\n---\nbody"})
    monkeypatch.setattr(registry_client, "CLIENT", _FakeDownloadClient(payload))
    plugins_dir = tmp_path / "plugins"
    target = await install("clawhub:@cua/driver", plugins_dir, tmp_path / "home")
    assert target == plugins_dir / "driver"
    assert (target / "SKILL.md").exists()


async def test_install_github_spec_never_asks_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``github:`` spec bypasses the registry entirely (a Claude-marketplace
    item's registry id is not its ``github:`` installSpec, so a registry
    lookup for it always 404s — the bug behind the desktop Skills screen
    failure installing ``github:anthropics/claude-code@frontend-design``)."""

    class _ExplodingClient:
        async def download(self, identifier: str, *, version: str | None = None) -> Any:
            raise AssertionError(f"the registry must not be asked for {identifier!r}")

    monkeypatch.setattr(registry_client, "CLIENT", _ExplodingClient())

    cloned: dict[str, Any] = {}

    async def fake_clone(url: str, target: Path) -> None:
        cloned["url"] = url
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text("---\nname: repo\n---\n", encoding="utf-8")

    monkeypatch.setattr(marketplace, "_clone", fake_clone)
    plugins_dir = tmp_path / "plugins"
    target = await install("github:owner/repo", plugins_dir, tmp_path / "home")
    assert cloned["url"] == "https://github.com/owner/repo.git"
    assert target == plugins_dir / "repo"


async def test_install_github_spec_with_plugin_installs_only_that_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``@plugin`` installs just that plugin's directory out of the monorepo,
    resolved from a locally registered marketplace pointed at the same repo —
    this is the exact case from the bug report:
    ``github:anthropics/claude-code@frontend-design``.
    """

    async def fake_clone(url: str, target: Path) -> None:
        (target / "frontend-design").mkdir(parents=True, exist_ok=True)
        (target / "frontend-design" / "SKILL.md").write_text(
            "---\nname: frontend-design\n---\n", encoding="utf-8"
        )
        (target / "other-plugin").mkdir(parents=True, exist_ok=True)
        (target / "other-plugin" / "SKILL.md").write_text(
            "---\nname: other-plugin\n---\n", encoding="utf-8"
        )

    monkeypatch.setattr(marketplace, "_clone", fake_clone)

    home = tmp_path / "home"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "frontend-design", "source": "frontend-design"},
                    {"name": "other-plugin", "source": "other-plugin"},
                ]
            }
        ),
        encoding="utf-8",
    )
    marketplace.save_marketplaces(
        home,
        [
            {
                "name": "claude-code",
                "url": str(manifest),
                "repo": "https://github.com/anthropics/claude-code",
            }
        ],
    )

    plugins_dir = tmp_path / "plugins"
    target = await install(
        "github:anthropics/claude-code@frontend-design", plugins_dir, home
    )
    assert target == plugins_dir / "frontend-design"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "---\nname: frontend-design\n---\n"
    # Only the resolved subdir is installed — not the whole monorepo.
    assert not (target / "other-plugin").exists()
    assert list(plugins_dir.iterdir()) == [target]


async def test_install_from_registry_404_falls_back_to_github_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain 404 (not just 501) is treated the same as "not fetchable" —
    this is the ``_install_from_registry`` safety net, exercised directly
    since ``install()`` no longer routes ``github:`` specs through it."""

    class _NotFoundClient:
        async def download(self, identifier: str, *, version: str | None = None) -> Any:
            raise registry_client.RegistryError(
                f"no skill named {identifier!r} on https://registry.snowpea.ai/v1"
            )

    monkeypatch.setattr(registry_client, "CLIENT", _NotFoundClient())

    async def fake_clone(url: str, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text("---\nname: repo\n---\n", encoding="utf-8")

    monkeypatch.setattr(marketplace, "_clone", fake_clone)
    plugins_dir = tmp_path / "plugins"
    target = await marketplace._install_from_registry(
        "github:owner/repo", plugins_dir, tmp_path / "home"
    )
    assert target == plugins_dir / "repo"
    assert (target / "SKILL.md").exists()


async def test_install_clawhub_spec_501_with_no_fallback_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NotFetchableClient:
        async def download(self, identifier: str, *, version: str | None = None) -> Any:
            raise registry_client.RegistryNotFetchable("no archive for this hub")

    monkeypatch.setattr(registry_client, "CLIENT", _NotFetchableClient())
    plugins_dir = tmp_path / "plugins"
    with pytest.raises(marketplace.InstallError, match="no archive"):
        await install("clawhub:@cua/driver", plugins_dir, tmp_path / "home")


async def test_install_clawhub_spec_404_message_names_what_was_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clawhub:``/``registry:`` specs have no git fallback, so the error is
    the registry's own reason — not a generic "not found"."""

    class _NotFoundClient:
        async def download(self, identifier: str, *, version: str | None = None) -> Any:
            raise registry_client.RegistryError(f"no skill named {identifier!r} on example")

    monkeypatch.setattr(registry_client, "CLIENT", _NotFoundClient())
    plugins_dir = tmp_path / "plugins"
    with pytest.raises(marketplace.InstallError, match="no skill named"):
        await install("clawhub:@cua/driver", plugins_dir, tmp_path / "home")


def test_has_external_scheme() -> None:
    assert marketplace._has_external_scheme("registry:ralplan")
    assert marketplace._has_external_scheme("clawhub:@cua/driver")
    assert marketplace._has_external_scheme("github:owner/repo@plugin")
    assert not marketplace._has_external_scheme("fixture-market/pdf-toolkit")
    assert not marketplace._has_external_scheme("https://example.com/repo.git")
    assert not marketplace._has_external_scheme("git@github.com:owner/repo.git")
    assert not marketplace._has_external_scheme("./local/dir")


def test_name_from_spec() -> None:
    assert marketplace._name_from_spec("registry:ralplan") == "ralplan"
    assert marketplace._name_from_spec("clawhub:@cua/driver") == "driver"
    assert marketplace._name_from_spec("github:owner/repo@plugin") == "plugin"
    assert marketplace._name_from_spec("github:owner/repo") == "repo"


# ---------------------------------------------------------------------------
# CLI: snowpea skill publish / rate against a fake registry
# ---------------------------------------------------------------------------


async def test_cli_skill_publish_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A properly sized description here.\n---\nbody",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer spr_test"
        return httpx.Response(
            201,
            json={
                "ok": True,
                "created": True,
                "skill": {"id": "my-skill", "installUrl": "https://registry.example/v1/x"},
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    home = tmp_path / "home"
    home.mkdir()
    code = await cli_commands.skill_publish_command(
        str(skill_dir),
        home,
        registry="https://registry.example/v1",
        token="spr_test",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "published my-skill" in out
    assert "registry:my-skill" in out


async def test_cli_skill_publish_rejects_bad_frontmatter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    code = await cli_commands.skill_publish_command(str(skill_dir), tmp_path / "home")
    assert code != 0


async def test_cli_skill_rate_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "rating": 5.0, "ratingCount": 1})

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    code = await cli_commands.skill_rate_command(
        "ralplan", "5", None, tmp_path / "home", registry="https://registry.example/v1"
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "rated ralplan" in out


async def test_cli_skill_rate_rejects_bad_stars(tmp_path: Path) -> None:
    code = await cli_commands.skill_rate_command("ralplan", "9", None, tmp_path / "home")
    assert code != 0


async def test_cli_skill_sources_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sources": [
                    {"id": "local", "label": "snowpea registry", "enabled": True, "count": 6},
                    {
                        "id": "hermes",
                        "label": "Hermes Hub",
                        "enabled": False,
                        "disabledReason": "does not resolve",
                    },
                ]
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    code = await cli_commands.skill_sources_command(
        tmp_path / "home", registry="https://registry.example/v1"
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "local" in out and "hermes" in out and "does not resolve" in out

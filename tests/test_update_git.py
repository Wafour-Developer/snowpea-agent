"""Git installs follow their branch, not an older release tag with the same version."""

import pytest
from test_update import FakeResponse, scripted

from snowpea_core import update as updates
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings

OLD = "a" * 40
NEW = "b" * 40


def provenance(monkeypatch, revision=OLD, ref=None):
    vcs = {"vcs": "git", "commit_id": revision}
    if ref is not None:
        vcs["requested_revision"] = ref
    monkeypatch.setattr(
        updates,
        "_read_direct_url_json",
        lambda: {
            "url": updates.REPO_URL,
            "vcs_info": vcs,
        },
    )


@pytest.mark.parametrize("ref", [None, "main"])
async def test_git_install_detects_new_commit_without_a_version_bump(tmp_path, monkeypatch, ref):
    paths = Paths.create(tmp_path)
    provenance(monkeypatch, ref=ref)
    scripted(
        monkeypatch,
        {
            f"{updates.COMMITS_URL}/main": FakeResponse(200, {"sha": NEW}),
            f"https://api.github.com/repos/{updates.REPO}/compare/{OLD}...{NEW}": FakeResponse(
                200, {"status": "ahead"}
            ),
        },
    )
    answer = await updates.check_update(paths, Settings())
    assert answer["available"] is True
    assert answer["latest"].endswith(NEW[:8])
    assert answer["source"] == f"git+{updates.REPO_URL}@{NEW}"
    assert answer["trackingSource"] == f"git+{updates.REPO_URL}@main"
    assert (await updates.check_update(paths, Settings()))["cached"] is True
    # After installation the previous available answer must not survive.
    provenance(monkeypatch, revision=NEW, ref="main")
    answer = await updates.check_update(paths, Settings())
    assert answer["available"] is False
    assert answer["cached"] is False


async def test_git_update_prompt_uses_the_new_release_version(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "__version__", "0.1.2")
    provenance(monkeypatch)
    scripted(
        monkeypatch,
        {
            f"{updates.COMMITS_URL}/main": FakeResponse(200, {"sha": NEW}),
            f"https://api.github.com/repos/{updates.REPO}/compare/{OLD}...{NEW}": FakeResponse(
                200, {"status": "ahead"}
            ),
            updates.TAGS_URL: FakeResponse(200, [{"name": "v0.1.4"}]),
        },
    )

    answer = await updates.check_update(Paths.create(tmp_path), Settings())

    assert answer["current"] == f"0.1.2+{OLD[:8]}"
    assert answer["latest"] == f"0.1.4+{NEW[:8]}"


@pytest.mark.parametrize("status", ["behind", "diverged"])
async def test_never_offers_a_non_descendant_commit(tmp_path, monkeypatch, status):
    provenance(monkeypatch)
    scripted(
        monkeypatch,
        {
            f"{updates.COMMITS_URL}/main": FakeResponse(200, {"sha": NEW}),
            f"https://api.github.com/repos/{updates.REPO}/compare/{OLD}...{NEW}": FakeResponse(
                200, {"status": status}
            ),
        },
    )
    assert (await updates.check_update(Paths.create(tmp_path), Settings()))["available"] is False


async def test_branch_network_failure_does_not_fall_back_to_old_tags(tmp_path, monkeypatch):
    provenance(monkeypatch)
    scripted(monkeypatch, {f"{updates.COMMITS_URL}/main": FakeResponse(403, {})})
    answer = await updates.check_update(Paths.create(tmp_path), Settings())
    assert answer["available"] is False
    assert answer["error"]
    assert answer["source"].endswith("@main")


def test_pinned_release_is_not_silently_changed_to_main(tmp_path, monkeypatch):
    provenance(monkeypatch, ref="v0.1.1")
    assert updates.git_install_provenance(Paths.create(tmp_path)) is None


def test_reinstalled_pinned_commit_keeps_recorded_branch(tmp_path, monkeypatch):
    paths = Paths.create(tmp_path)
    provenance(monkeypatch, revision=NEW, ref=NEW)
    updates.write_install_json(paths, "uv", f"git+{updates.REPO_URL}@main")
    install = updates.git_install_provenance(paths)
    assert install.branch == "main"
    assert install.installed_revision == NEW


async def test_successful_upgrade_preserves_branch_for_next_launch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    paths = Paths.create(tmp_path)
    core = SimpleNamespace(paths=paths, restart_required=False)
    monkeypatch.setattr(updates, "notify_progress", AsyncMock())
    await updates.watch_update(
        core, SimpleNamespace(poll=lambda: 0), "0.1.2+bbbbbbbb", f"git+{updates.REPO_URL}@main"
    )
    provenance(monkeypatch, revision=NEW, ref=NEW)
    assert updates.git_install_provenance(paths).branch == "main"
    assert core.restart_required is True


async def test_git_check_internal_metadata_stays_out_of_rpc(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from snowpea_core.server.protocol import CheckUpdateParams
    from snowpea_core.server.update_handlers import check_update_handler

    provenance(monkeypatch)
    scripted(monkeypatch, {f"{updates.COMMITS_URL}/main": FakeResponse(200, {"sha": OLD})})
    core = SimpleNamespace(paths=Paths.create(tmp_path), settings=Settings())
    result = await check_update_handler(None, CheckUpdateParams(force=True), core)
    assert result.available is False
    assert "installKey" not in result.model_dump()

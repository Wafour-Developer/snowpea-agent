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
    # A positive answer is re-verified rather than served from the 24h cache:
    # a force-push inside the window would otherwise keep offering a commit
    # whose ancestry was never re-checked (CORE-fixes-v017 R8).
    again = await updates.check_update(paths, Settings())
    assert again["available"] is True
    assert again["cached"] is False
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
            f"{updates.RAW_VERSION_URL}/{NEW}/core/snowpea_core/__init__.py": FakeResponse(
                200, '__version__ = "0.1.5"\n'
            ),
        },
    )

    answer = await updates.check_update(Paths.create(tmp_path), Settings())

    assert answer["current"] == f"0.1.2+{OLD[:8]}"
    assert answer["latest"] == f"0.1.5+{NEW[:8]}"


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
    monkeypatch.setattr(updates, "installed_cli_version", lambda: "0.1.5")
    await updates.watch_update(
        core, SimpleNamespace(poll=lambda: 0), "0.1.2+bbbbbbbb", f"git+{updates.REPO_URL}@main"
    )
    provenance(monkeypatch, revision=NEW, ref=NEW)
    assert updates.git_install_provenance(paths).branch == "main"
    assert core.restart_required is True
    updates.notify_progress.assert_awaited_with(core, "done", "updated to v0.1.5")


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


async def test_a_negative_git_answer_is_cached(tmp_path, monkeypatch):
    """The cheap half of R8: nothing is installed from "no update", so cache it."""
    paths = Paths.create(tmp_path)
    provenance(monkeypatch, ref="main")
    scripted(
        monkeypatch,
        {
            f"{updates.COMMITS_URL}/main": FakeResponse(200, {"sha": NEW}),
            f"https://api.github.com/repos/{updates.REPO}/compare/{OLD}...{NEW}": FakeResponse(
                200, {"status": "identical"}
            ),
        },
    )
    first = await updates.check_update(paths, Settings())
    assert first["available"] is False
    assert (await updates.check_update(paths, Settings()))["cached"] is True


async def test_a_short_sha_prefix_is_not_treated_as_the_same_revision():
    """R7: a 7-char abbreviation can collide, so it never skips the compare."""
    assert updates._same_revision(OLD, OLD) is True
    assert updates._same_revision(OLD[:7], OLD) is False
    assert updates._same_revision(OLD, OLD[:7]) is False
    assert updates._same_revision(OLD, NEW) is False

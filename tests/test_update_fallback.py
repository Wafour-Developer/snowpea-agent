"""The latest-release lookup survives the GitHub REST API's rate limit.

Nothing touches the network: ``update._new_client`` is a scripted stub and
``update._ls_remote_sync`` (the ``git ls-remote`` call) is replaced.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from test_update import FakeResponse, scripted

from snowpea_core import update as updates
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Tags from snowpea-agent</title>
  <entry>
    <id>tag:github.com,2008:Repository/1/v0.2.88</id>
    <link rel="alternate" type="text/html" href="https://github.com/Wafour-Developer/snowpea-agent/releases/tag/v0.2.88"/>
    <title>v0.2.88</title>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v0.2.89</id>
    <link rel="alternate" type="text/html" href="https://github.com/Wafour-Developer/snowpea-agent/releases/tag/v0.2.89"/>
    <title>v0.2.89</title>
  </entry>
  <entry>
    <title>not-a-version</title>
  </entry>
</feed>
"""

LS_REMOTE = (
    "1" * 40 + "\trefs/tags/v0.2.9\n"
    + "2" * 40 + "\trefs/tags/v0.2.89\n"
    + "3" * 40 + "\trefs/tags/v0.2.89^{}\n"
    + "4" * 40 + "\trefs/tags/v0.2.10\n"
    + "5" * 40 + "\trefs/tags/nightly\n"
)


class Limited(FakeResponse):
    def __init__(self, status: int = 403, reset: float | None = None) -> None:
        super().__init__(status, {"message": "API rate limit exceeded"})
        self.headers: dict[str, str] = {"X-RateLimit-Remaining": "0"}
        if reset is not None:
            self.headers["X-RateLimit-Reset"] = str(int(reset))


@pytest.fixture(autouse=True)
def git_channel(monkeypatch):
    monkeypatch.setattr(updates, "__version__", "0.2.88")
    monkeypatch.setattr(updates, "git_install_provenance", lambda _paths: None)


def route(monkeypatch, routes: dict[str, Any]):
    routes = {updates.PYPI_URL: FakeResponse(404, {}), **routes}
    return scripted(monkeypatch, routes)


def fake_git(monkeypatch, out: str | Exception):
    calls: list[tuple[str, ...]] = []

    def run(*patterns: str) -> str:
        calls.append(patterns)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(updates, "_ls_remote_sync", run)
    return calls


async def check(tmp_path):
    return await updates.check_update(Paths.create(tmp_path), Settings(), force=True)


async def test_api_200_is_unchanged(tmp_path, monkeypatch):
    client = route(
        monkeypatch,
        {updates.TAGS_URL: FakeResponse(200, [{"name": "v0.2.89"}, {"name": "v0.2.88"}])},
    )
    answer = await check(tmp_path)
    assert answer["latest"] == "0.2.89"
    assert answer["available"] is True
    assert answer["error"] is None
    assert updates.TAGS_ATOM_URL not in client.calls


async def test_api_rate_limited_falls_back_to_the_atom_feed(tmp_path, monkeypatch):
    client = route(
        monkeypatch,
        {updates.TAGS_URL: Limited(), updates.TAGS_ATOM_URL: FakeResponse(200, ATOM)},
    )
    answer = await check(tmp_path)
    assert answer["latest"] == "0.2.89"
    assert answer["available"] is True
    assert answer["error"] is None
    assert answer["source"].endswith("@v0.2.89")
    assert updates.TAGS_ATOM_URL in client.calls


async def test_api_and_atom_down_fall_back_to_git_ls_remote(tmp_path, monkeypatch):
    route(
        monkeypatch,
        {updates.TAGS_URL: Limited(), updates.TAGS_ATOM_URL: FakeResponse(500, "boom")},
    )
    calls = fake_git(monkeypatch, LS_REMOTE)
    answer = await check(tmp_path)
    assert answer["latest"] == "0.2.89"
    assert answer["error"] is None
    assert calls == [("--tags",)]


async def test_a_broken_feed_is_treated_as_a_failed_source(tmp_path, monkeypatch):
    route(
        monkeypatch,
        {updates.TAGS_URL: Limited(), updates.TAGS_ATOM_URL: FakeResponse(200, "<feed")},
    )
    fake_git(monkeypatch, LS_REMOTE)
    assert (await check(tmp_path))["latest"] == "0.2.89"


async def test_rate_limited_everywhere_names_the_reset_time(tmp_path, monkeypatch):
    reset = time.time() + 1800
    route(
        monkeypatch,
        {updates.TAGS_URL: Limited(reset=reset), updates.TAGS_ATOM_URL: Limited(429)},
    )
    fake_git(monkeypatch, OSError("no git"))
    answer = await check(tmp_path)
    clock = time.strftime("%H:%M", time.localtime(int(reset)))
    assert answer["error"] == (
        f"GitHub limited update checks from this network; try again after {clock}"
    )
    assert answer["available"] is False
    assert "no published release" not in answer["error"]


async def test_offline_says_could_not_reach_github(tmp_path, monkeypatch):
    route(
        monkeypatch,
        {
            updates.TAGS_URL: ConnectionError("dns"),
            updates.TAGS_ATOM_URL: TimeoutError("slow"),
        },
    )
    fake_git(monkeypatch, OSError("no git"))
    assert (await check(tmp_path))["error"] == "could not reach GitHub"


async def test_other_http_status_is_named(tmp_path, monkeypatch):
    route(
        monkeypatch,
        {updates.TAGS_URL: FakeResponse(502, {}), updates.TAGS_ATOM_URL: FakeResponse(502, "")},
    )
    fake_git(monkeypatch, OSError("no git"))
    assert (await check(tmp_path))["error"] == "GitHub answered 502"


async def test_no_release_wording_only_when_a_source_listed_no_tag(tmp_path, monkeypatch):
    route(monkeypatch, {updates.TAGS_URL: FakeResponse(200, [{"name": "nightly"}])})
    answer = await check(tmp_path)
    assert answer["error"] == "no published release could be found"


async def test_error_answers_are_not_cached(tmp_path, monkeypatch):
    paths = Paths.create(tmp_path)
    route(monkeypatch, {updates.TAGS_URL: Limited(), updates.TAGS_ATOM_URL: Limited()})
    fake_git(monkeypatch, OSError("no git"))
    failed = await updates.check_update(paths, Settings())
    assert failed["error"]
    assert not paths.update_check_json.exists()

    # A stale error written by an older build is not served either.
    updates.write_cache(paths, {**failed, "configured": "auto"})
    route(monkeypatch, {updates.TAGS_URL: FakeResponse(200, [{"name": "v0.2.89"}])})
    again = await updates.check_update(paths, Settings())
    assert again["cached"] is False
    assert again["latest"] == "0.2.89"


NEW = "b" * 40
OLD = "a" * 40


def branch_provenance(monkeypatch):
    monkeypatch.setattr(
        updates,
        "git_install_provenance",
        lambda _paths: updates.GitInstall(branch="main", installed_revision=OLD),
    )


async def test_branch_install_uses_ls_remote_when_the_commits_api_is_limited(
    tmp_path, monkeypatch
):
    branch_provenance(monkeypatch)
    route(
        monkeypatch,
        {
            f"{updates.COMMITS_URL}/main": Limited(),
            f"https://api.github.com/repos/{updates.REPO}/compare/{OLD}...{NEW}": FakeResponse(
                200, {"status": "ahead"}
            ),
        },
    )
    calls = fake_git(monkeypatch, f"{NEW}\trefs/heads/main\n")
    answer = await check(tmp_path)
    assert calls == [("refs/heads/main",)]
    assert answer["available"] is True
    assert answer["latest"].endswith(NEW[:8])


async def test_branch_compare_limited_degrades_with_plain_words(tmp_path, monkeypatch):
    branch_provenance(monkeypatch)
    route(
        monkeypatch,
        {
            f"{updates.COMMITS_URL}/main": FakeResponse(200, {"sha": NEW}),
            f"https://api.github.com/repos/{updates.REPO}/compare/{OLD}...{NEW}": Limited(),
        },
    )
    answer = await check(tmp_path)
    assert answer["available"] is False
    assert answer["error"].startswith("GitHub limited update checks from this network")


async def test_branch_everything_down_is_not_called_no_release(tmp_path, monkeypatch):
    branch_provenance(monkeypatch)
    route(monkeypatch, {f"{updates.COMMITS_URL}/main": ConnectionError("dns")})
    fake_git(monkeypatch, OSError("no git"))
    assert (await check(tmp_path))["error"] == "could not reach GitHub"


def test_ls_remote_argv_is_hardened(monkeypatch):
    seen: dict[str, Any] = {}

    class Done:
        returncode = 0
        stdout = "ok"

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return Done()

    monkeypatch.setattr(updates, "git_usable", lambda: True)
    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/evil")
    assert updates._ls_remote_sync("--tags") == "ok"
    assert seen["argv"][:4] == ["git", "-c", "credential.helper=", "ls-remote"]
    assert seen["argv"][4] == f"{updates.REPO_URL}.git"
    kw = seen["kwargs"]
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kw["env"]["GIT_ASKPASS"] == ""
    assert kw["timeout"] <= 15
    assert "shell" not in kw

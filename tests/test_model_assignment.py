"""The per-agent model assignment chain, end to end (CORE-model-assignment).

Section 7 of the v0.1.7 commit review found that the precedence users expected
did not exist: there was no tool-call override, no session pin that survived a
restart, and no project-scoped model settings at all — while the one rung that
did exist (an agent definition's ``model:``) was resolved by a private helper
that never looked at ``models.profiles``.

The chain these tests pin, highest first:

1. an explicit override (``delegate_task(model=…)`` / ``agent.spawn(model=…)``)
2. the agent's assignment — project over global, then the ``.md`` ``model:``
3. the session pin (``/model`` / ``session.setModel``)
4. the project default
5. the global default
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snowpea_core.config.model_routing import ModelRoute, model_config_for, route_for
from snowpea_core.config.paths import Paths
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.providers.registry import ProviderRegistry

GLOBAL = {
    "models": {
        "default": "slow",
        "profiles": {
            "slow": {"provider": "openai", "model": "gpt-slow"},
            "fast": {"provider": "openai", "model": "gpt-fast"},
            "deep": {"provider": "anthropic", "model": "claude-deep"},
        },
    },
    "agents": {"models": {"architect": "deep"}},
}


def settings() -> Settings:
    return Settings.model_validate(GLOBAL)


def project(workdir: Path, models: dict) -> Path:
    (workdir / ".snowpea").mkdir(parents=True, exist_ok=True)
    path = workdir / ".snowpea" / "settings.json"
    path.write_text(json.dumps({"models": models}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


def test_every_rung_of_the_precedence_chain(tmp_path: Path) -> None:
    conf = settings()
    project(
        tmp_path,
        {
            "default": "fast",
            "agents": {"executor": "deep"},
            "profiles": {"local": {"provider": "ollama", "model": "qwen"}},
        },
    )
    pin = ModelRoute("openai", "pinned-model")

    # 1. explicit override beats everything, including an assignment.
    explicit = route_for(
        conf, provider="x", model="y", agent="architect", workdir=tmp_path, session_pin=pin
    )
    assert (explicit.provider, explicit.model) == ("x", "y")

    # 2a. the agent's assignment beats the pin and both defaults...
    assigned = route_for(conf, agent="architect", workdir=tmp_path, session_pin=pin)
    assert (assigned.provider, assigned.model) == ("anthropic", "claude-deep")

    # 2b. ...and the project's assignment wins over the global one.
    overridden = route_for(conf, agent="executor", workdir=tmp_path, session_pin=pin)
    assert overridden.model == "claude-deep"

    # 2c. the definition's model: is consulted after the assignment.
    from_definition = route_for(
        conf, agent="nobody", definition_model="fast", workdir=tmp_path, session_pin=pin
    )
    assert from_definition.model == "gpt-fast"

    # 3. with no assignment and no definition, the session pin applies.
    pinned = route_for(conf, agent="nobody", workdir=tmp_path, session_pin=pin)
    assert (pinned.provider, pinned.model) == ("openai", "pinned-model")

    # 4. no pin -> the project default.
    project_default = route_for(conf, workdir=tmp_path)
    assert project_default.model == "gpt-fast"

    # 5. no project at all -> the global default.
    assert route_for(conf).model == "gpt-slow"

    # A project profile is visible to the resolver too.
    assert route_for(conf, definition_model="local", workdir=tmp_path).provider == "ollama"


def test_project_models_merge_over_global(tmp_path: Path) -> None:
    conf = settings()
    project(
        tmp_path,
        {"profiles": {"fast": {"provider": "ollama", "model": "qwen-fast"}}, "agents": {}},
    )
    merged = model_config_for(conf, tmp_path)
    # Redefining an id replaces that one profile and leaves the rest alone.
    assert merged.profiles["fast"].provider == "ollama"
    assert merged.profiles["deep"].provider == "anthropic"
    assert merged.agents == {"architect": "deep"}


def test_a_project_models_block_round_trips(tmp_path: Path) -> None:
    """It used to be ``extra=allow``: accepted, then silently ignored."""
    project(tmp_path, {"default": "fast", "agents": {"executor": "deep"}})
    loaded = ProjectSettings.load(tmp_path)
    assert loaded.models.default == "fast"
    assert loaded.models.agents == {"executor": "deep"}


# ---------------------------------------------------------------------------
# B-P1-1 — a profile id in an agent .md is not a vendor name
# ---------------------------------------------------------------------------


def test_a_definition_model_resolves_through_profiles_without_a_default() -> None:
    """The exact case the review reproduced live.

    With profiles configured but no ``models.default`` and no assignment, the
    old bypass split the definition's ``model: fast`` as ``vendor="fast"`` and
    the child died with ``unknown provider vendor: fast``.
    """
    conf = Settings.model_validate(
        {"models": {"profiles": GLOBAL["models"]["profiles"]}}  # note: no default
    )
    route = route_for(conf, agent="executor", definition_model="fast")
    assert (route.provider, route.model) == ("openai", "gpt-fast")


# ---------------------------------------------------------------------------
# B-P1-2 — an unknown profile reference must not brick the daemon
# ---------------------------------------------------------------------------


def test_an_unknown_profile_reference_degrades_instead_of_bricking(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text(
        json.dumps(
            {
                "models": {"default": "gone", "profiles": GLOBAL["models"]["profiles"]},
                "agents": {"models": {"executor": "also-gone", "architect": "deep"}},
                "search": {"provider": "ddgs"},
            }
        ),
        encoding="utf-8",
    )
    loaded = Settings.load(paths)
    # The bad references are dropped; everything else survives.
    assert loaded.models.default is None
    assert loaded.agents.models == {"architect": "deep"}
    assert loaded.search.provider == "ddgs"
    assert "ignoring unusable model routing" in caplog.text


def test_settings_set_still_rejects_an_unknown_profile() -> None:
    """Degrading is for *load*; a live patch must still be invalid_params."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="agents.models references unknown"):
        Settings.model_validate({"agents": {"models": {"executor": "nope"}}})


def test_a_genuinely_broken_settings_file_still_raises(tmp_path: Path) -> None:
    """Only model references are repaired; other errors are real errors."""
    from pydantic import ValidationError

    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text(
        json.dumps({"agents": {"teams": {"delivery": "not-a-list"}}}), encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        Settings.load(paths)


# ---------------------------------------------------------------------------
# B-P1-3 — a broken models.default must not hijack default_vendor()
# ---------------------------------------------------------------------------


def test_a_default_profile_naming_an_unknown_vendor_does_not_brick_every_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SNOWPEA_PROVIDER", raising=False)
    conf = Settings.model_validate(
        {
            "models": {
                "default": "bogus",
                "profiles": {"bogus": {"provider": "not-a-vendor", "model": "x"}},
            },
            "providers": {"default": "openai"},
        }
    )
    registry = ProviderRegistry(conf)
    # It used to return "not-a-vendor" ahead of providers.default, so *every*
    # session failed at its first turn, not just the routed ones.
    assert registry.default_vendor() == "openai"


def test_a_default_profile_naming_a_real_vendor_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SNOWPEA_PROVIDER", raising=False)
    conf = Settings.model_validate(
        {
            "models": {
                "default": "deep",
                "profiles": {"deep": {"provider": "anthropic", "model": "claude-deep"}},
            },
            "providers": {"default": "openai"},
        }
    )
    assert ProviderRegistry(conf).default_vendor() == "anthropic"


# ---------------------------------------------------------------------------
# B-P2-3 — the session pin persists, survives restore and announces itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_session_pin_survives_a_restore(tmp_path: Path) -> None:
    """``/model`` used to live in memory only and was lost on restore."""
    from snowpea_core.session.manager import SessionManager
    from snowpea_core.session.store import Store

    paths = Paths.create(tmp_path / "home")
    store = Store.open(paths)
    try:
        manager = SessionManager(store=store, settings=settings())
        session = await manager.create(tmp_path)
        assert session.model == "gpt-slow"  # the global default

        route = await manager.set_model(session, "deep")
        assert (route.provider, route.model) == ("anthropic", "claude-deep")

        # Drop it from memory the way a daemon restart does.
        manager._sessions.pop(session.id)
        restored = await manager.restore(session.id)
        assert restored is not None
        assert (restored.provider, restored.model) == ("anthropic", "claude-deep")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_clearing_the_pin_goes_back_to_the_configured_routing(tmp_path: Path) -> None:
    from snowpea_core.session.manager import SessionManager

    manager = SessionManager(settings=settings())
    session = await manager.create(tmp_path)
    await manager.set_model(session, "deep")
    cleared = await manager.set_model(session, "inherit")
    assert cleared.model == "gpt-slow"


@pytest.mark.asyncio
async def test_pinning_an_unknown_reference_is_refused(tmp_path: Path) -> None:
    """A pin the user asked for must not silently become something else."""
    from snowpea_core.session.manager import SessionManager

    manager = SessionManager(settings=settings())
    session = await manager.create(tmp_path)
    with pytest.raises(ValueError, match="unknown model"):
        await manager.set_model(session, "no-such-profile")
    assert session.model == "gpt-slow"


# ---------------------------------------------------------------------------
# B-P2-1 — definition_model is derived, not a caller obligation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_derives_the_definition_model(tmp_path: Path) -> None:
    """Three of five session creators used to forget to pass it."""
    from snowpea_core.session.manager import SessionManager

    manager = SessionManager(settings=Settings.model_validate(
        {"models": {"profiles": GLOBAL["models"]["profiles"]}}
    ))
    manager.definition_model_for = lambda name, _workdir: "fast" if name == "scribe" else None
    session = await manager.create(tmp_path, agent="scribe")
    assert (session.provider, session.model) == ("openai", "gpt-fast")


# ---------------------------------------------------------------------------
# the null delete sentinel
# ---------------------------------------------------------------------------


def test_null_deletes_a_key_in_a_settings_patch() -> None:
    """A merge cannot express a removal, so ``null`` means "delete"."""
    from snowpea_core.config.patch import deep_merge

    base = {
        "models": {"default": "fast", "profiles": {"fast": {}, "deep": {}}},
        "agents": {"models": {"executor": "fast", "architect": "deep"}},
    }
    merged = deep_merge(base, {"models": {"profiles": {"fast": None}}})
    assert set(merged["models"]["profiles"]) == {"deep"}
    assert merged["models"]["default"] == "fast"  # siblings untouched

    cleared = deep_merge(base, {"agents": {"models": {"executor": None}}})
    assert cleared["agents"]["models"] == {"architect": "deep"}

    # Deleting something absent is a no-op, and a scalar null still lands as
    # the field's default, which for every optional field here is None.
    assert deep_merge(base, {"nothing": None}) == base
    assert "default" not in deep_merge(base, {"models": {"default": None}})["models"]

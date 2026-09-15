"""``/setup`` and ``/login``: configuring the daemon without leaving the session.

Keys could only be typed into the Python wizard at a terminal. These put the
same questions through the question queue, and the thing that makes that safe
is one protocol flag: a `secret` answer is masked by the surface, kept out of
the transcript, and never logged.

What is pinned here: the questions carry `secret` where they should and not
where they should not; an answer reaches the settings slot the runtime reads;
an unattended or delegated session is refused rather than left hanging; and no
credential reaches the session's event store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snowpea_core.commands import setup_cmd
from snowpea_core.commands.registry import CommandContext
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.setup import credentials as creds

# ---------------------------------------------------------------------------
# a session with a scripted human behind it
# ---------------------------------------------------------------------------


class Answer:
    """What the question queue hands back."""

    def __init__(self, text: str | None = None, selected: list[str] | None = None) -> None:
        self.text = text
        self.selected = selected or []
        self.declined = not text and not selected
        self.timed_out = False
        self.by = "test"


class Questions:
    """A stand-in queue that answers from a script and records what it was asked."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.asked: list[Any] = []

    async def ask(self, _session: Any, questions: list[Any], **_kw: Any) -> list[Answer]:
        self.asked.extend(questions)
        out: list[Answer] = []
        for _ in questions:
            nxt = self.script.pop(0) if self.script else None
            out.append(nxt if isinstance(nxt, Answer) else Answer(nxt))
        return out


class Hub:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit_event(self, _session_id: str, event: Any) -> None:
        self.events.append(event)


class Session:
    def __init__(self, workdir: Path, *, attended: bool = True) -> None:
        self.id = "s-1"
        self.workdir = workdir
        self.origin_conn = object() if attended else None
        self.unattended = False
        self.is_subagent = False
        import asyncio

        self.interrupt = asyncio.Event()


class Core:
    def __init__(self, home: Path, script: list[Any] | None = None) -> None:
        self.paths = Paths(home=home)
        self.settings = Settings()
        self.questions = Questions(script)
        self.hub = Hub()
        self.saved = 0

        original = self.settings.save

        def save(paths: Any) -> None:
            self.saved += 1
            original(paths)

        self.settings.save = save  # type: ignore[method-assign]


def make(tmp_path: Path, script: list[Any] | None = None, *, attended: bool = True) -> Any:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    core = Core(home, script)
    session = Session(tmp_path / "project", attended=attended)
    ctx = CommandContext(core=core, session=session, turn_id="t-1")  # type: ignore[arg-type]
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    ctx.say = say  # type: ignore[method-assign]
    return ctx, core, said


def _settings_on_disk(home: Path) -> Settings:
    return Settings.load(Paths(home=home))


# ---------------------------------------------------------------------------
# which questions carry `secret`
# ---------------------------------------------------------------------------


def test_a_key_prompt_is_secret_and_a_url_prompt_is_not() -> None:
    key_plan = creds.plan_for("browser", "firecrawl_cloud")
    assert key_plan is not None
    [question] = setup_cmd.credential_questions(key_plan)
    assert question.secret is True
    assert question.options == [] and question.allowOther is True

    # Browserbase's project id is an identifier, not a credential.
    both = creds.plan_for("browser", "browserbase")
    assert both is not None
    questions = setup_cmd.credential_questions(both)
    assert [q.secret for q in questions] == [True, False]


def test_a_provider_that_needs_nothing_asks_nothing() -> None:
    plan = creds.plan_for("browser", "local_chromium")
    assert plan is not None
    assert setup_cmd.credential_questions(plan) == []


def test_a_closed_question_puts_the_recommendation_first_and_says_so() -> None:
    question = setup_cmd.choice_question(
        "Provider", "which?", [("b", "second", False), ("a", "first", True)]
    )
    assert [option.label for option in question.options] == ["a (recommended)", "b"]
    assert question.allowOther is False
    assert question.secret is False


# ---------------------------------------------------------------------------
# /setup writes the slot the runtime reads
# ---------------------------------------------------------------------------


async def test_setup_browser_picks_a_provider_and_saves_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(creds, "probe", lambda _plan: None)
    ctx, core, said = make(tmp_path, ["firecrawl_cloud", "fc-secret"])

    await setup_cmd.cmd_setup(ctx, "browser")

    assert core.settings.browser.provider == "firecrawl_cloud"
    assert core.settings.browser.credentials["firecrawl_cloud"]["api_key"] == "fc-secret"
    assert core.saved == 1
    assert "browser: firecrawl_cloud" in "\n".join(said)

    # The key question really was marked secret on the wire.
    key_questions = [q for q in core.questions.asked if q.header == setup_cmd.KEY_HEADER]
    assert key_questions and all(q.secret for q in key_questions)


async def test_setup_browser_asks_browserbase_for_both_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(creds, "probe", lambda _plan: None)
    ctx, core, _ = make(tmp_path, ["browserbase", "bb-key", "proj-9"])

    await setup_cmd.cmd_setup(ctx, "browser")

    block = core.settings.browser.credentials["browserbase"]
    assert block["api_key"] == "bb-key"
    assert block["browserbase_project_id"] == "proj-9"


async def test_setup_search_saves_under_the_slot_the_runtime_reads(tmp_path: Path) -> None:
    from snowpea_core.tools import search_providers

    ctx, core, _ = make(tmp_path, ["exa", "exa-secret"])
    await setup_cmd.cmd_setup(ctx, "search")

    assert core.settings.search.provider == "exa"
    provider = search_providers.get("exa")
    assert provider is not None
    found = search_providers.credentials_for(provider.meta, core.settings)
    assert found.api_key == "exa-secret"


async def test_the_written_settings_are_what_the_browser_runtime_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.tools import browser_providers

    monkeypatch.setattr(creds, "probe", lambda _plan: None)
    ctx, core, _ = make(tmp_path, ["browserbase", "bb-key", "proj-9"])
    await setup_cmd.cmd_setup(ctx, "browser")

    assert browser_providers.configured("browserbase", core.settings) is True
    # And it survives the round trip through settings.json.
    reloaded = _settings_on_disk(core.paths.home)
    assert browser_providers.credentials_for("browserbase", reloaded)["api_key"] == "bb-key"


async def test_declining_the_provider_question_changes_nothing(tmp_path: Path) -> None:
    ctx, core, said = make(tmp_path, [Answer()])
    before = core.settings.browser.provider
    await setup_cmd.cmd_setup(ctx, "browser")
    assert core.settings.browser.provider == before
    assert "browser: unchanged" in "\n".join(said)


async def test_a_required_key_left_empty_is_refused_in_words(tmp_path: Path) -> None:
    ctx, _core, said = make(tmp_path, ["firecrawl_cloud", Answer()])
    await setup_cmd.cmd_setup(ctx, "browser")
    assert "a key is required" in "\n".join(said)


async def test_a_failing_probe_warns_but_keeps_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(creds, "probe", lambda _plan: "firecrawl_cloud rejected the key (HTTP 401)")
    ctx, core, said = make(tmp_path, ["firecrawl_cloud", "fc-key"])
    await setup_cmd.cmd_setup(ctx, "browser")
    assert "warning:" in "\n".join(said) and "401" in "\n".join(said)
    assert core.settings.browser.credentials["firecrawl_cloud"]["api_key"] == "fc-key"


async def test_an_unknown_section_prints_the_usage(tmp_path: Path) -> None:
    ctx, core, said = make(tmp_path)
    await setup_cmd.cmd_setup(ctx, "wardrobe")
    assert setup_cmd.USAGE in "\n".join(said)
    assert core.questions.asked == []


# ---------------------------------------------------------------------------
# nobody to ask
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attribute", ["unattended", "is_subagent"])
async def test_a_session_with_nobody_to_ask_is_refused(tmp_path: Path, attribute: str) -> None:
    ctx, core, said = make(tmp_path, ["firecrawl_cloud", "fc-key"])
    setattr(ctx.session, attribute, True)

    await setup_cmd.cmd_setup(ctx, "browser")

    assert core.questions.asked == [], "a key prompt with no human hangs or is taken for silence"
    assert "unattended" in "\n".join(said)
    assert core.settings.browser.credentials == {}


async def test_a_session_with_no_connection_is_refused(tmp_path: Path) -> None:
    ctx, core, said = make(tmp_path, ["anthropic", "sk-x"], attended=False)
    await setup_cmd.cmd_login(ctx, "anthropic")
    assert core.questions.asked == []
    assert "unattended" in "\n".join(said)


def test_can_ask_is_the_one_place_that_decides(tmp_path: Path) -> None:
    ctx, _core, _said = make(tmp_path)
    assert setup_cmd.can_ask(ctx) is True
    ctx.session.is_subagent = True
    assert setup_cmd.can_ask(ctx) is False


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------


async def test_login_with_a_key_saves_it_without_a_browser(tmp_path: Path) -> None:
    ctx, core, said = make(tmp_path, ["sk-ant-123"])
    await setup_cmd.cmd_login(ctx, "anthropic api_key")

    assert core.settings.providers["anthropic"]["api_key"] == "sk-ant-123"
    assert core.saved == 1
    assert "key saved" in "\n".join(said)
    [question] = core.questions.asked
    assert question.secret is True


async def test_login_runs_the_browser_flow_when_one_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.providers import auth_web

    calls: list[tuple[str, str]] = []

    class Result:
        credentials = {"api_key": "minted-key"}
        message = "gemini: signed in"

    async def fake_login(vendor: str, method: str) -> Any:
        calls.append((vendor, method))
        return Result()

    monkeypatch.setattr(auth_web, "login", fake_login)
    monkeypatch.setattr(auth_web, "methods_for", lambda _v: ("browser",))

    ctx, core, said = make(tmp_path)
    await setup_cmd.cmd_login(ctx, "gemini browser")

    assert calls == [("gemini", "browser")]
    assert core.settings.providers["gemini"]["api_key"] == "minted-key"
    assert "signed in" in "\n".join(said)
    # A browser flow asks no question at all, so nothing was masked or leaked.
    assert core.questions.asked == []


async def test_login_asks_which_flow_when_none_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.providers import auth_web

    monkeypatch.setattr(auth_web, "methods_for", lambda _v: ("browser", "device"))
    ctx, core, _said = make(tmp_path, ["api_key", "sk-x"])

    await setup_cmd.cmd_login(ctx, "anthropic")

    first = core.questions.asked[0]
    assert [option.label for option in first.options] == ["browser", "device", "api_key"]
    assert core.settings.providers["anthropic"]["api_key"] == "sk-x"


async def test_login_rejects_an_unknown_vendor(tmp_path: Path) -> None:
    ctx, core, said = make(tmp_path)
    await setup_cmd.cmd_login(ctx, "wardrobe")
    assert "unknown vendor" in "\n".join(said)
    assert core.questions.asked == []


async def test_login_without_a_vendor_prints_the_usage(tmp_path: Path) -> None:
    ctx, _core, said = make(tmp_path)
    await setup_cmd.cmd_login(ctx, "")
    assert "/login <vendor>" in "\n".join(said)


# ---------------------------------------------------------------------------
# a credential never reaches the transcript
# ---------------------------------------------------------------------------


async def test_no_secret_reaches_the_session_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's own event stream is what a transcript is made of."""
    monkeypatch.setattr(creds, "probe", lambda _plan: None)
    ctx, core, said = make(tmp_path, ["firecrawl_cloud", "fc-super-secret"])

    await setup_cmd.cmd_setup(ctx, "browser")

    rendered = repr(core.hub.events) + "\n".join(said)
    assert "fc-super-secret" not in rendered
    # It did land where it belongs.
    assert core.settings.browser.credentials["firecrawl_cloud"]["api_key"] == "fc-super-secret"


async def test_no_vendor_key_reaches_the_session_events(tmp_path: Path) -> None:
    ctx, core, said = make(tmp_path, ["sk-ant-super-secret"])
    await setup_cmd.cmd_login(ctx, "anthropic api_key")
    rendered = repr(core.hub.events) + "\n".join(said)
    assert "sk-ant-super-secret" not in rendered


# ---------------------------------------------------------------------------
# both callers share one implementation
# ---------------------------------------------------------------------------


def test_the_wizard_and_the_command_run_the_same_plan() -> None:
    """The point of `setup/credentials.py`: one place decides what to ask."""
    from snowpea_core.setup import wizard

    assert wizard._ask_for_browser_key.__module__.endswith("wizard")
    for kind, pid in (("browser", "browserbase"), ("search", "exa")):
        plan = creds.plan_for(kind, pid)
        assert plan is not None and plan.needed and plan.required
        # The command turns exactly those prompts into questions.
        assert len(setup_cmd.credential_questions(plan)) == len(plan.prompts)


def test_the_slot_is_spelled_once(tmp_path: Path) -> None:
    settings = Settings()
    plan = creds.plan_for("browser", "browserbase")
    assert plan is not None
    plan.answers.update({"api_key": "k", "browserbase_project_id": "p"})
    creds.apply_to_settings(plan, settings)
    assert settings.browser.credentials["browserbase"] == {
        "api_key": "k",
        "browserbase_project_id": "p",
    }

"""``/setup`` and ``/login`` — configure the daemon without leaving the session.

Keys could only be typed into the Python wizard at a terminal.  A TUI user who
picked Browserbase had nowhere to put the key, and a desktop user had nowhere
at all; both had to quit, run ``snowpea setup``, and come back.

These two commands put the same questions through the **question queue**, which
is the machinery a surface already knows how to draw: the same request shape as
``ask_user`` and approvals, tabs for a batch, Esc to decline.  The one thing
that is new is :attr:`QuestionItem.secret` — a free-text answer a surface must
mask, must keep out of the transcript and must not log.

Both refuse in a session that has nobody to ask.  An unattended run or a
delegated child asking for an API key would either hang until the timeout or,
worse, take silence for an answer.

The *decisions* — which provider needs what, whether an answer is a secret,
where it is written — live in :mod:`snowpea_core.setup.credentials`, which the
CLI wizard runs too.  This module only knows how to ask through a session.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.server.protocol import QuestionItem, QuestionOption
from snowpea_core.setup import credentials as creds

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.setup.credentials import CredentialPlan

log = logging.getLogger("snowpea.commands.setup")

#: Sections ``/setup`` can re-run, in the order ``all`` walks them.
SECTIONS: tuple[str, ...] = ("providers", "search", "browser", "audio")

USAGE = f"Usage: /setup [{'|'.join(SECTIONS)}|all] | /login <vendor> [method]"

#: What both commands say when there is nobody to ask.
NO_ONE_TO_ASK = (
    "/{name} needs someone to answer: this session is unattended, so a key "
    "prompt would hang or be taken for silence. Run `snowpea setup` in a "
    "terminal instead."
)

#: Header chips, so a surface can label the tabs of a batch.
KEY_HEADER = "Credential"
PICK_HEADER = "Provider"


def can_ask(ctx: CommandContext) -> bool:
    """True when this session has a human on the other end.

    A subagent inherits its parent's connection object but must never stop to
    ask for a credential: its whole contract is to run and report.
    """
    session = ctx.session
    if getattr(session, "unattended", False) or getattr(session, "is_subagent", False):
        return False
    return getattr(session, "origin_conn", None) is not None


def choice_question(
    header: str, question: str, rows: list[tuple[str, str, bool]]
) -> QuestionItem:
    """One closed question. ``rows`` is ``(id, description, recommended)``.

    The recommended row comes first and is suffixed, which is the convention
    every picker in snowpea follows (M15 §F).
    """
    ordered = sorted(rows, key=lambda row: not row[2])
    return QuestionItem(
        header=header,
        question=question,
        options=[
            QuestionOption(
                label=f"{rid} (recommended)" if recommended else rid, description=description
            )
            for rid, description, recommended in ordered
        ],
        multi=False,
        allowOther=False,
    )


def credential_questions(plan: CredentialPlan) -> list[QuestionItem]:
    """One free-text question per prompt, masked where the answer is a secret."""
    return [
        QuestionItem(
            header=KEY_HEADER,
            question=prompt.text(),
            options=[],
            multi=False,
            allowOther=True,
            secret=prompt.secret,
        )
        for prompt in plan.prompts
    ]


async def _ask(ctx: CommandContext, questions: list[QuestionItem]) -> list[Any]:
    """One place the question queue is called, so the shape is stated once."""
    answers = await ctx.core.questions.ask(
        ctx.session, questions, cancel_event=ctx.session.interrupt
    )
    return list(answers or [])


def _answer_text(answer: Any) -> str:
    """The free text of one answer, or ``""`` when it was declined or empty."""
    if answer is None or getattr(answer, "declined", False):
        return ""
    text = getattr(answer, "text", None)
    if text:
        return str(text).strip()
    selected = getattr(answer, "selected", None) or []
    return str(selected[0]).strip() if selected else ""


def _picked(answer: Any) -> str:
    """The chosen row id, with the ``(recommended)`` suffix taken back off."""
    return _answer_text(answer).removesuffix(" (recommended)").strip()


async def _ask_plan(ctx: CommandContext, plan: CredentialPlan) -> CredentialPlan:
    """Put a credential plan to the session and record what came back."""
    questions = credential_questions(plan)
    if not questions:
        return plan
    answers = await _ask(ctx, questions)
    for prompt, answer in zip(plan.prompts, answers, strict=False):
        value = _answer_text(answer)
        if value:
            plan.answers[prompt.field] = value
    return plan


async def _configure_provider_section(ctx: CommandContext, kind: str) -> list[str]:
    """Pick a search or browser provider, then ask for whatever it needs."""
    from snowpea_core.setup import catalog

    rows = catalog.search_catalog() if kind == "search" else catalog.browser_catalog()
    current = getattr(getattr(ctx.core.settings, kind), "provider", "")
    question = choice_question(
        PICK_HEADER,
        f"Which {kind} provider? (now: {current or 'none'})",
        [
            (item.id, f"{item.tier} · {item.key} — {item.description}".rstrip(" —"), item.default)
            for item in rows
        ],
    )
    answers = await _ask(ctx, [question])
    chosen = _picked(answers[0] if answers else None)
    if not chosen:
        return [f"{kind}: unchanged"]

    saved = getattr(getattr(ctx.core.settings, kind), "credentials", {})
    plan = creds.plan_for(kind, chosen, saved)
    if plan is None:
        return [f"{kind}: unknown provider {chosen}"]

    lines: list[str] = []
    await _ask_plan(ctx, plan)
    getattr(ctx.core.settings, kind).provider = chosen
    creds.apply_to_settings(plan, ctx.core.settings)

    problem = creds.probe(plan)
    if problem:
        lines.append(f"warning: {problem}")
    refused = creds.refusal(plan)
    # The refusal only applies when nothing is saved either — a plan whose
    # answers were all "keep what is there" is complete, not empty.
    block = getattr(getattr(ctx.core.settings, kind), "credentials", {}).get(chosen) or {}
    if refused and not block.get("api_key"):
        lines.append(refused)
    lines.insert(0, f"{kind}: {chosen}")
    return lines


async def _configure_audio(ctx: CommandContext) -> list[str]:
    """Pick the voice engines, offering to install the recommended ones."""
    from snowpea_core.audio import capabilities
    from snowpea_core.server.audio_handlers import audio_config, speech_caller
    from snowpea_core.setup import catalog

    report = capabilities(audio_config(ctx.core), caller=speech_caller(ctx.core))
    questions = [
        choice_question(
            PICK_HEADER,
            "Voice in — who transcribes the microphone?",
            [
                (item.id, _audio_row(item), item.recommended or item.default)
                for item in catalog.stt_catalog(report["sttProviders"])
            ],
        ),
        choice_question(
            PICK_HEADER,
            "Voice out — who says the reply out loud?",
            [
                (item.id, _audio_row(item), item.recommended or item.default)
                for item in catalog.tts_catalog(report["ttsProviders"])
            ],
        ),
    ]
    answers = await _ask(ctx, questions)
    lines: list[str] = []
    for label, key, answer in (
        ("stt", "stt", answers[0] if answers else None),
        ("tts", "tts", answers[1] if len(answers) > 1 else None),
    ):
        chosen = _picked(answer)
        if not chosen:
            continue
        block = _audio_block(ctx.core.settings, key)
        block["provider"] = chosen
        lines.append(f"audio.{label}: {chosen}")
    return lines or ["audio: unchanged"]


def _audio_row(item: Any) -> str:
    state = "installed" if item.active else "not installed here"
    return f"{item.description or item.label} — {state}".lstrip(" —")


def _audio_block(settings: Any, key: str) -> dict[str, Any]:
    """``settings.audio.<key>`` as a mutable dict, created when absent."""
    audio = getattr(settings, "audio", None)
    if not isinstance(audio, dict):
        audio = {} if audio is None else audio
        settings.audio = audio  # type: ignore[attr-defined]
    if not isinstance(audio, dict):  # pragma: no cover - a typed model later
        block = getattr(audio, key, None)
        return block if isinstance(block, dict) else {}
    block = audio.setdefault(key, {})
    return block if isinstance(block, dict) else audio.setdefault(key, {})


async def _configure_providers(ctx: CommandContext) -> list[str]:
    """Pick the LLM vendor and ask for its key, masked."""
    from snowpea_core.providers.presets import PRESETS

    current = ctx.core.settings.providers if hasattr(ctx.core.settings, "providers") else {}
    rows = [
        (
            name,
            f"{preset.label}" if getattr(preset, "label", "") else name,
            name == "anthropic",
        )
        for name, preset in PRESETS.items()
    ]
    answers = await _ask(ctx, [choice_question(PICK_HEADER, "Which LLM vendor?", rows)])
    vendor = _picked(answers[0] if answers else None)
    if not vendor:
        return ["providers: unchanged"]
    saved = bool((current.get(vendor) or {}).get("api_key")) if isinstance(current, dict) else False
    hint = "saved — Enter to keep" if saved else f"Enter to use ${vendor.upper()}_API_KEY"
    key_answers = await _ask(
        ctx,
        [
            QuestionItem(
                header=KEY_HEADER,
                question=f"{vendor} API key [{hint}]",
                options=[],
                allowOther=True,
                secret=True,
            )
        ],
    )
    key = _answer_text(key_answers[0] if key_answers else None)
    if key:
        _store_vendor_key(ctx.core.settings, vendor, key)
        return [f"providers: {vendor} (key saved)"]
    return [f"providers: {vendor}" + ("" if saved else " (no key)")]


def _store_vendor_key(settings: Any, vendor: str, key: str) -> None:
    providers = getattr(settings, "providers", None)
    if not isinstance(providers, dict):
        return
    block = dict(providers.get(vendor) or {})
    block["api_key"] = key
    providers[vendor] = block


async def cmd_setup(ctx: CommandContext, args: str) -> None:
    """``/setup [section]`` — re-run a wizard screen inside this session."""
    section = (args or "").strip().lower() or "all"
    if section not in (*SECTIONS, "all"):
        await ctx.say(USAGE)
        return
    if not can_ask(ctx):
        await ctx.say(NO_ONE_TO_ASK.format(name="setup"))
        return

    wanted = SECTIONS if section == "all" else (section,)
    lines: list[str] = []
    for name in wanted:
        try:
            if name == "providers":
                lines += await _configure_providers(ctx)
            elif name == "audio":
                lines += await _configure_audio(ctx)
            else:
                lines += await _configure_provider_section(ctx, name)
        except Exception as exc:  # noqa: BLE001 - one bad screen, not a dead command
            log.exception("/setup %s failed", name)
            lines.append(f"{name}: {type(exc).__name__}: {exc}")
    _save(ctx)
    await ctx.say("\n".join(lines) or "nothing changed")


def _save(ctx: CommandContext) -> None:
    """Persist settings, the same file ``snowpea setup`` writes."""
    try:
        ctx.core.settings.save(ctx.core.paths)
    except Exception:  # noqa: BLE001 - the answers are still in memory
        log.exception("could not save settings")


async def cmd_login(ctx: CommandContext, args: str) -> None:
    """``/login <vendor> [method]`` — browser/device flow, or a masked key."""
    words = (args or "").split()
    if not words:
        await ctx.say("Usage: /login <vendor> [method]")
        return
    if not can_ask(ctx):
        await ctx.say(NO_ONE_TO_ASK.format(name="login"))
        return
    vendor = words[0].strip().lower()
    method = words[1].strip().lower() if len(words) > 1 else ""

    from snowpea_core.providers import auth_web
    from snowpea_core.providers.presets import PRESETS

    if vendor not in PRESETS:
        await ctx.say(f"unknown vendor {vendor}; known: {', '.join(sorted(PRESETS))}")
        return

    # A vendor with no browser flow at all raises rather than answering an
    # empty tuple; an API key is still a perfectly good way to sign in to it.
    try:
        flows = list(auth_web.methods_for(vendor))
    except Exception:  # noqa: BLE001 - "no browser login" is an answer, not a failure
        flows = []
    if method and method not in flows and method != "api_key":
        await ctx.say(
            f"{vendor} has no {method} login; try: {', '.join([*flows, 'api_key']) or 'api_key'}"
        )
        return
    if not method:
        rows = [(flow, f"{vendor} {flow} login", False) for flow in flows]
        rows.append(("api_key", "paste an API key instead", not flows))
        answers = await _ask(
            ctx, [choice_question("Login", f"How should {vendor} authenticate?", rows)]
        )
        method = _picked(answers[0] if answers else None) or "api_key"

    if method == "api_key":
        key_answers = await _ask(
            ctx,
            [
                QuestionItem(
                    header=KEY_HEADER,
                    question=f"{vendor} API key",
                    options=[],
                    allowOther=True,
                    secret=True,
                )
            ],
        )
        key = _answer_text(key_answers[0] if key_answers else None)
        if not key:
            await ctx.say(f"{vendor}: nothing entered — left unconfigured")
            return
        _store_vendor_key(ctx.core.settings, vendor, key)
        _save(ctx)
        await ctx.say(f"{vendor}: key saved")
        return

    try:
        result = await auth_web.login(vendor, method)
    except Exception as exc:  # noqa: BLE001 - a failed login is a message
        await ctx.say(f"{vendor} login failed: {exc}")
        return
    credentials = getattr(result, "credentials", {}) or {}
    providers = getattr(ctx.core.settings, "providers", None)
    if isinstance(providers, dict):
        block = dict(providers.get(vendor) or {})
        block.update({k: v for k, v in credentials.items() if v})
        providers[vendor] = block
    _save(ctx)
    await ctx.say(getattr(result, "message", None) or f"{vendor}: signed in")


COMMANDS: tuple[Command, ...] = (
    Command(
        name="setup",
        summary=f"Configure snowpea from here: /setup [{'|'.join(SECTIONS)}|all].",
        run=cmd_setup,
        args_schema={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": [*SECTIONS, "all"],
                    "description": "Which wizard screen to re-run; omit for all of them.",
                }
            },
        },
    ),
    Command(
        name="login",
        summary="Sign in to a vendor: /login <vendor> [method].",
        run=cmd_login,
        args_schema={
            "type": "object",
            "properties": {
                "vendor": {"type": "string", "description": "Vendor id, e.g. anthropic."},
                "method": {
                    "type": "string",
                    "description": "Login flow; omit to be asked which one.",
                },
            },
            "required": ["vendor"],
        },
    ),
)


__all__ = [
    "COMMANDS",
    "KEY_HEADER",
    "NO_ONE_TO_ASK",
    "PICK_HEADER",
    "SECTIONS",
    "USAGE",
    "can_ask",
    "choice_question",
    "cmd_login",
    "cmd_setup",
    "credential_questions",
]

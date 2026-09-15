"""Asking for a provider's credentials, once, for both places that ask.

``snowpea setup`` asks at a terminal and ``/setup`` asks through the session's
question queue.  They used to be able to disagree, and did: the CLI wizard grew
a key prompt for search and later for browser, and a surface that wanted the
same thing had to reimplement which providers need a key, which need two, where
the value is written and what a good hint says.

So the *decisions* live here — what to ask, whether it is a secret, what the
hint says, where the answer goes, whether the probe complained — and the
*asking* is a callback.  A terminal passes one that reads masked input; the
question queue passes one that puts the same request to a surface.  Neither
knows anything the other does not.

Nothing here prints, and nothing here logs a value.  A credential that reaches
a log is a credential that has leaked, however carefully the transcript is
handled elsewhere.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("snowpea.setup.credentials")

#: How long a credential probe may take.  It is a courtesy check; a slow
#: network must not hold up setup.
PROBE_TIMEOUT = 3.0

#: The cheapest authenticated call each provider offers, as ``(method, url)``.
BROWSER_PROBES: dict[str, tuple[str, str]] = {
    "browserbase": ("GET", "https://api.browserbase.com/v1/sessions"),
    "firecrawl_cloud": ("HEAD", "https://api.firecrawl.dev/"),
}

#: Providers that authenticate with their own header rather than a bearer token.
_HEADER_OVERRIDES: dict[str, str] = {"browserbase": "x-bb-api-key"}


@dataclass(frozen=True)
class Prompt:
    """One thing to ask for, and everything a surface needs to draw it."""

    #: Settings key inside the provider's credential block.
    field: str
    #: What the user reads.
    label: str
    #: Mask the input, keep it out of the transcript, never log it.
    secret: bool = False
    #: A value already saved; Enter keeps it.
    current: str = ""
    #: Environment variable that would serve instead, when there is one.
    env: str = ""

    @property
    def hint(self) -> str:
        """The bracketed hint after the label."""
        if self.current:
            return "saved — Enter to keep"
        return f"Enter to use ${self.env}" if self.env else "optional"

    def text(self) -> str:
        """``Firecrawl Cloud API key [Enter to use $FIRECRAWL_API_KEY]``."""
        return f"{self.label} [{self.hint}]"


@dataclass
class CredentialPlan:
    """What one provider needs before it can run."""

    kind: str  # "search" | "browser"
    provider_id: str
    label: str
    prompts: tuple[Prompt, ...] = ()
    #: True when the provider cannot work at all without these.
    required: bool = False
    #: Values collected so far, keyed by :attr:`Prompt.field`.
    answers: dict[str, str] = field(default_factory=dict)

    @property
    def needed(self) -> bool:
        return bool(self.prompts)

    def missing(self) -> list[str]:
        """Fields still without a value, saved or answered."""
        return [
            prompt.field
            for prompt in self.prompts
            if not (self.answers.get(prompt.field) or prompt.current)
        ]


def _block(store: Any, provider_id: str) -> dict[str, str]:
    block = (store or {}).get(provider_id) if isinstance(store, dict) else None
    return {str(k): str(v) for k, v in block.items()} if isinstance(block, dict) else {}


def plan_for_search(provider_id: str, saved: Any = None) -> CredentialPlan | None:
    """What ``search.credentials[<id>]`` still needs, or ``None`` for an unknown id."""
    from snowpea_core.tools import search_providers

    provider = search_providers.get(provider_id)
    if provider is None:
        return None
    meta = provider.meta
    current = _block(saved, provider_id)
    plan = CredentialPlan(
        kind="search",
        provider_id=provider_id,
        label=meta.label,
        required=meta.key == "key required",
    )
    if meta.key == "self-hosted":
        env = next((name for name in meta.env if name.endswith("_URL")), "")
        return _with(
            plan,
            (
                Prompt(
                    field="url",
                    label=f"{meta.label} base URL",
                    current=current.get("url", "") or current.get("base_url", ""),
                    env=env,
                ),
            ),
        )
    if meta.key == "no key":
        return plan
    return _with(
        plan,
        (
            Prompt(
                field="api_key",
                label=f"{meta.label} API key",
                secret=True,
                current=current.get("api_key", ""),
                env=search_providers.credential_env(provider_id),
            ),
        ),
    )


def plan_for_browser(provider_id: str, saved: Any = None) -> CredentialPlan | None:
    """What ``browser.credentials[<id>]`` still needs, or ``None`` for an unknown id."""
    from snowpea_core.tools import browser_providers

    provider = browser_providers.get(provider_id)
    if provider is None:
        return None
    meta = provider.meta
    current = _block(saved, provider_id)
    plan = CredentialPlan(
        kind="browser",
        provider_id=provider_id,
        label=meta.label,
        required=meta.key == "key required",
    )
    if meta.key == "self-hosted":
        env = next((name for name in meta.env if name.endswith("_URL")), "")
        return _with(
            plan,
            (
                Prompt(
                    field="base_url",
                    label=f"{meta.label} base URL",
                    current=current.get("base_url", ""),
                    env=env,
                ),
            ),
        )
    if meta.key == "no key":
        return plan
    prompts = [
        Prompt(
            field="api_key",
            label=f"{meta.label} API key",
            secret=True,
            current=current.get("api_key", ""),
            env=browser_providers.credential_env(provider_id),
        )
    ]
    # A provider that declared two variables needs both, and the second is an
    # identifier rather than a secret — masking a project id helps nobody.
    prompts += [
        Prompt(
            field=extra.lower(),
            label=f"{meta.label} {extra}",
            current=current.get(extra.lower(), ""),
            env=extra,
        )
        for extra in browser_providers.extra_envs(provider_id)
    ]
    return _with(plan, tuple(prompts))


def _with(plan: CredentialPlan, prompts: tuple[Prompt, ...]) -> CredentialPlan:
    return CredentialPlan(
        kind=plan.kind,
        provider_id=plan.provider_id,
        label=plan.label,
        prompts=prompts,
        required=plan.required,
    )


def plan_for(kind: str, provider_id: str, saved: Any = None) -> CredentialPlan | None:
    """:func:`plan_for_search` or :func:`plan_for_browser`, by kind."""
    if kind == "search":
        return plan_for_search(provider_id, saved)
    if kind == "browser":
        return plan_for_browser(provider_id, saved)
    return None


#: ``(prompt) -> the answer``, or ``""`` for "keep what is there".  Async so the
#: question-queue caller can await a surface; the terminal one returns at once.
Asker = Callable[[Prompt], Awaitable[str]]


async def collect(plan: CredentialPlan, ask: Asker) -> CredentialPlan:
    """Ask every prompt in turn and record the answers on ``plan``."""
    for prompt in plan.prompts:
        try:
            answer = (await ask(prompt) or "").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if answer:
            plan.answers[prompt.field] = answer
    return plan


def probe(plan: CredentialPlan) -> str | None:
    """One cheap call to see whether the credential works; a sentence or ``None``.

    Never blocks and never fails the save: an offline machine, a proxy, or a
    provider having a bad afternoon are not evidence that the key is wrong.
    """
    if plan.kind != "browser":
        return None
    target = BROWSER_PROBES.get(plan.provider_id)
    key = plan.answers.get("api_key") or next(
        (p.current for p in plan.prompts if p.field == "api_key" and p.current), ""
    )
    if target is None or not key:
        return None
    method, url = target
    header = _HEADER_OVERRIDES.get(plan.provider_id)
    headers = {header: key} if header else {"authorization": f"Bearer {key}"}
    try:
        import httpx

        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            response = client.request(method, url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - a probe never blocks setup
        return f"could not reach {plan.provider_id} to check the key ({type(exc).__name__})"
    status = int(getattr(response, "status_code", 0))
    if status in (401, 403):
        return f"{plan.provider_id} rejected the key (HTTP {status}); saving it anyway"
    if status >= 500:
        return f"{plan.provider_id} answered HTTP {status}; the key was not checked"
    return None


def refusal(plan: CredentialPlan) -> str | None:
    """What to say when a required credential was left empty."""
    if not plan.required or not plan.missing():
        return None
    where = "answer searches" if plan.kind == "search" else "drive a browser"
    return (
        f"a key is required — {plan.provider_id} cannot {where} until it has one; "
        f"choose another provider or set it later with `snowpea setup --section {plan.kind}`"
    )


def apply_to_settings(plan: CredentialPlan, settings: Any) -> None:
    """Write the collected answers into ``<kind>.credentials[<id>]``.

    The one place the slot is spelled, so the wizard, ``/setup`` and any future
    surface cannot drift apart. It is the slot the runtime already reads
    (``search_providers.credentials_for`` / ``browser_providers.credentials_for``).
    """
    if not plan.answers:
        return
    block_owner = getattr(settings, plan.kind, None)
    store = getattr(block_owner, "credentials", None)
    if not isinstance(store, dict):
        return
    existing = dict(store.get(plan.provider_id) or {})
    existing.update({k: v for k, v in plan.answers.items() if v})
    store[plan.provider_id] = existing


__all__ = [
    "BROWSER_PROBES",
    "PROBE_TIMEOUT",
    "Asker",
    "CredentialPlan",
    "Prompt",
    "apply_to_settings",
    "collect",
    "plan_for",
    "plan_for_browser",
    "plan_for_search",
    "probe",
    "refusal",
]

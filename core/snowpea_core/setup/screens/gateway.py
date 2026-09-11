"""Screen ⑤ — chat gateways, all off until a token is pasted.

Picking one here only flips it on; :mod:`snowpea_core.setup.wizard` then asks
for the bot token and for *your* account id on that platform.  The id is not
optional politeness: a messenger binding whose approver is unknown is
fail-closed and can approve nothing from chat.
"""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem, gateway_catalog
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import SKIP, WizardState

TITLE = "⑤ Messengers (chat gateways)"
HELP = (
    "Each one needs a bot token and your user id on that platform; "
    "pass them with `--gateway <id> --token <token> --user-id <id>`."
)


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    items = list(catalog) if catalog is not None else gateway_catalog()
    rows = [
        ScreenItem(
            id=item.id,
            label=item.label,
            tags=item.tags,
            selected=bool((state.gateways.get(item.id) or {}).get("enabled")),
            default=item.default,
            active=bool((state.gateways.get(item.id) or {}).get("token")),
            # NOTE: ``active`` means "already has a token"; the wizard asks for
            # the token and the approver user id right after this screen.
        )
        for item in items
    ]
    return Screen(title=TITLE, items=(*rows, skip_item()), multi=True, help=HELP)


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    chosen = {choice} if isinstance(choice, str) else set(choice)
    chosen.discard(SKIP)
    for item in gateway_catalog():
        if item.id in chosen:
            state.enable_gateway(item.id)
        elif item.id in state.gateways:
            state.gateways[item.id]["enabled"] = False
    return state


__all__ = ["HELP", "TITLE", "apply", "build"]

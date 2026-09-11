"""Screen ⑤ — chat gateways, all off until a token is pasted."""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem, gateway_catalog
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import SKIP, WizardState

TITLE = "⑤ Messengers (chat gateways)"
HELP = "Each one needs a bot token; pass it with `--gateway <id> --token <token>`."


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

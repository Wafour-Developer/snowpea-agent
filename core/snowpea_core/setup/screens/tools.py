"""Screen ④ — which tool categories are on."""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem, tools_catalog
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import SKIP, WizardState

TITLE = "④ Tool categories"
HELP = "Space toggles, Enter accepts. [inactive] categories need a provider first."


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    items = list(catalog) if catalog is not None else tools_catalog()
    rows = [
        ScreenItem(
            id=item.id,
            label=item.label,
            tags=item.tags,
            selected=state.tool_categories.get(item.id, item.default),
            default=item.default,
            active=item.active,
        )
        for item in items
    ]
    return Screen(title=TITLE, items=(*rows, skip_item()), multi=True, help=HELP)


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    """``choice`` is the full set of enabled ids, or ``SKIP``."""
    if isinstance(choice, str):
        if choice and choice != SKIP:
            state.set_categories({choice})
        return state
    state.set_categories({cid for cid in choice if cid != SKIP})
    return state


__all__ = ["HELP", "TITLE", "apply", "build"]

"""Screen ② — which web-search provider ``web_search`` prefers."""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem, search_catalog
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import SKIP, WizardState

TITLE = "② Web search provider"
HELP = (
    "Free, keyless providers are listed first; ★ is the default (AC-02b). "
    "A provider tagged \"key required\" asks for its key next and cannot answer without one."
)


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    items = list(catalog) if catalog is not None else search_catalog()
    rows = [
        ScreenItem(
            id=item.id,
            label=item.label,
            tags=item.tags,
            selected=item.id == state.search_provider,
            default=item.default,
            active=item.active,
        )
        for item in items
    ]
    return Screen(title=TITLE, items=(*rows, skip_item()), multi=False, help=HELP)


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if choice and choice != SKIP:
        state.search_provider = choice
    return state


__all__ = ["HELP", "TITLE", "apply", "build"]

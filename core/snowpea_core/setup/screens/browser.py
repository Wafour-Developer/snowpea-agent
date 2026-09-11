"""Screen ③ — which browser the ``browser_*`` tools drive."""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem, browser_catalog
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import SKIP, WizardState

TITLE = "③ Browser provider"
HELP = "Local headless Chromium needs no key; the cloud ones do."


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    items = list(catalog) if catalog is not None else browser_catalog()
    rows = [
        ScreenItem(
            id=item.id,
            label=item.label,
            tags=item.tags,
            selected=item.id == state.browser_provider,
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
        state.browser_provider = choice
    return state


__all__ = ["HELP", "TITLE", "apply", "build"]

"""Screen ① — which LLM vendor to use."""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem, vendor_auth_tags, vendor_catalog
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import SKIP, WizardState

TITLE = "① LLM provider"
HELP = "↑↓ to move, Enter to choose. A key is asked for after the list."


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """List the eleven vendors, marking the configured ones ``[active]``."""
    items = list(catalog) if catalog is not None else vendor_catalog()
    rows = [
        ScreenItem(
            id=item.id,
            label=item.label,
            tags=item.tags + vendor_auth_tags(item.id),
            selected=item.id == state.vendor,
            default=item.default,
            active=item.active,
        )
        for item in items
    ]
    help_text = HELP
    if state.hints:
        help_text = HELP + "\n" + "\n".join(state.hints)
    return Screen(title=TITLE, items=(*rows, skip_item()), multi=False, help=help_text)


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    """``choice`` is a vendor id, or ``SKIP`` to keep whatever is configured."""
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if choice and choice != SKIP:
        state.vendor = choice
    return state


__all__ = ["HELP", "TITLE", "apply", "build"]

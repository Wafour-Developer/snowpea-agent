"""Screen ⑥ — the summary; nothing left to answer."""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import WizardState

TITLE = "⑥ Done"
HELP = "Enter writes settings.json."


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """One read-only row per answer, then the usual Skip row."""
    rows = [
        ScreenItem(
            id=f"summary:{index}",
            label=line,
            tags=(),
            selected=False,
            default=index == 0,
            active=True,
        )
        for index, line in enumerate(state.summary())
    ]
    return Screen(title=TITLE, items=(*rows, skip_item()), multi=False, help=HELP)


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    """The summary screen collects nothing."""
    return state


__all__ = ["HELP", "TITLE", "apply", "build"]

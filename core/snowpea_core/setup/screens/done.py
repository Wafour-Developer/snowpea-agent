"""Screen ⑥ — the summary; nothing left to answer."""

from __future__ import annotations

from collections.abc import Sequence

from snowpea_core.setup.catalog import CatalogItem
from snowpea_core.setup.screens import Screen, ScreenItem
from snowpea_core.setup.state import WizardState

TITLE = "⑥ Done"
#: Summary rows that can be revisited by selecting them.
SECTION_ROWS = frozenset({"providers", "search", "browser", "tools", "gateway"})
HELP = "Enter on a row revisits that section · Save writes settings.json · Cancel discards."
SAVE = "action:save"
CANCEL = "action:cancel"


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """One read-only row per answer, then the usual Skip row."""
    rows = []
    for index, line in enumerate(state.summary()):
        section = line.split()[0] if line.split() else ""
        section = {"provider": "providers"}.get(section, section)
        row_id = f"section:{section}" if section in SECTION_ROWS else f"summary:{index}"
        rows.append(
            ScreenItem(
                id=row_id, label=line, tags=(), selected=False, default=False, active=True
            )
        )
    actions = (
        ScreenItem(
            id=SAVE, label="✓ Save — write settings.json", tags=(), selected=False, default=True
        ),
        ScreenItem(id=CANCEL, label="✕ Cancel — discard changes", tags=(), selected=False),
    )
    return Screen(title=TITLE, items=(*rows, *actions), multi=False, help=HELP)


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    """The summary screen collects nothing."""
    return state


__all__ = ["CANCEL", "HELP", "SAVE", "TITLE", "apply", "build"]

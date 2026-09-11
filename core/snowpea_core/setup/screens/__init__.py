"""Hermes-style setup screens (M3 contract §5, plan §4 M3 / AC-02b).

Each screen module is two pure functions and nothing else:

``build(state, catalog) -> Screen``   what to show, given the answers so far
``apply(state, choice) -> WizardState``  fold one answer back into the state

No screen touches the terminal; :mod:`snowpea_core.setup.ui` renders a
:class:`Screen` and :mod:`snowpea_core.setup.wizard` drives the order.  That
split is what makes the non-interactive flags and the arrow-key list the same
code path — and it is why every screen can be unit-tested without a TTY.
"""

from __future__ import annotations

from typing import NamedTuple

from snowpea_core.setup.state import SKIP, SKIP_LABEL


class ScreenItem(NamedTuple):
    """One row: ``(id, label, tags, selected, default, active)``."""

    id: str
    label: str
    tags: tuple[str, ...]
    selected: bool
    default: bool
    active: bool = True


class Screen(NamedTuple):
    """A whole screen: a title, rows, and how the rows are chosen."""

    title: str
    items: tuple[ScreenItem, ...]
    multi: bool
    help: str = ""

    @property
    def default_choice(self) -> str | set[str]:
        """What a non-TTY run (or an explicit Skip) picks."""
        if self.multi:
            return {item.id for item in self.items if item.selected and item.id != SKIP}
        for item in self.items:
            if item.selected and item.id != SKIP:
                return item.id
        for item in self.items:
            if item.default:
                return item.id
        return SKIP


def skip_item() -> ScreenItem:
    """The ``Skip — keep defaults`` row every screen ends with."""
    return ScreenItem(id=SKIP, label=SKIP_LABEL, tags=(), selected=False, default=False)


__all__ = ["SKIP", "SKIP_LABEL", "Screen", "ScreenItem", "skip_item"]

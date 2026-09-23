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
    """The last row of every screen: confirm what is selected, or skip.

    Its label is drawn from the screen's state (:func:`last_row_label`):
    "Done — keep X" when a row is selected, "Skip — decide later" when nothing
    is. The id is :data:`SKIP` either way, because both mean "leave the
    setting as the screen shows it". The static label is what a screen built
    without state reads; an install that came back to a screen still saying
    Skip is what this replaces.
    """
    return ScreenItem(id=SKIP, label=SKIP_LABEL, tags=(), selected=False, default=False)


DONE_LABEL = "Done — keep {label}"
DONE_MULTI_LABEL = "Done — keep these {count}"
SKIP_LATER_LABEL = "Skip — decide later"


def last_row_label(screen: Screen, chosen: set[str]) -> str:
    """What the SKIP row says given what is chosen right now."""
    picked = [item for item in screen.items if item.id in chosen and item.id != SKIP]
    if not picked:
        return SKIP_LATER_LABEL
    if screen.multi:
        return DONE_MULTI_LABEL.format(count=len(picked))
    return DONE_LABEL.format(label=picked[0].label)


__all__ = [
    "DONE_LABEL",
    "DONE_MULTI_LABEL",
    "SKIP",
    "SKIP_LABEL",
    "SKIP_LATER_LABEL",
    "Screen",
    "ScreenItem",
    "last_row_label",
    "skip_item",
]

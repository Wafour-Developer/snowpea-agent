"""finish-the-job: a turn_stopping listener.

When the model is about to end its turn with an answer that still holds a
placeholder ("TODO", "FIXME", "..."), the turn goes on with one more
instruction. At most twice per turn, so a model that cannot finish is never
kept in a loop.

Order 120: after the built-in verify gate (100), which asks for a passing
check first, and before unfinished-work (150) and the Stop hooks (200). The
first listener that returns a decision wins.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, StopContext, StopDecision

PLACEHOLDER = re.compile(r"\b(TODO|FIXME)\b|<placeholder>")
MAX_PER_TURN = 2


def apply(ctx: PluginContext) -> None:
    sent: dict[tuple[str, Any], int] = {}  # (session id, turn id) -> nudges so far

    def check(core: Any, session: Any, stop: StopContext) -> StopDecision | None:
        if stop.at_budget or not PLACEHOLDER.search(stop.last_message):
            return None
        key = (session.id, session.current_turn)
        count = sent.get(key, 0)
        if count >= MAX_PER_TURN:
            return None
        sent[key] = count + 1
        if len(sent) > 1000:  # forget old turns
            sent.pop(next(iter(sent)))
        return StopDecision(
            text=(
                "Your answer still has a TODO, FIXME or placeholder. Finish that part "
                "now, or say plainly why it cannot be done."
            ),
            producer="finish-the-job",
            shown="finish-the-job: the answer still had a placeholder",
            count=count + 1,
            limit=MAX_PER_TURN,
        )

    ctx.hooks.turn_stopping("placeholders", check, order=120)

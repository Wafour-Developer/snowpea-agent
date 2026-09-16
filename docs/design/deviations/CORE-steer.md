# Deviations — CORE-steer (busy-turn steering)

Busy follow-up prompts now steer the running turn by default (`agent.busy = "steer"`), instead of always waiting for a later FIFO turn.

1. **Default changed to steer.** Prompts submitted during an active turn are still accepted as queued first (`turn.queued`), but in steer mode they are folded back into the same turn before the next model call as user messages (`message.user` with `steered: true`), and retired with `turn.dequeued(reason="steered")`.
2. **Legacy behavior is preserved behind a setting.** `agent.busy = "queue"` keeps the previous separate-turn FIFO drain exactly as before.
3. **Operator control is explicit.** `/busy [steer|queue]` shows/sets the policy and persists it in daemon settings.

Inspiration: Hermes `/busy steer|queue|interrupt` semantics. Compatibility note: Snowpea currently implements `steer` and `queue`; `interrupt` remains `session.interrupt`/`Esc`.

License note: behavioral inspiration only; implementation is original Snowpea code. Hermes is MIT-licensed.

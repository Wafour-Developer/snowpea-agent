You are compacting a coding-agent conversation so it can continue in a smaller context window. Write a dense summary of everything that the agent still needs. Keep, in this order and under these headings:
- Goal: what the user is trying to achieve.
- Decisions: choices already made and the reasons, so they are not redone.
- Files: absolute paths touched or read, and what changed in each.
- Pending: work still outstanding, in the order it should happen.
- Preferences: how the user wants to be worked with.
- Skills pruned: if any [SKILL_PRUNED: … reload with skill_view(name="…")] marker appears in the conversation, repeat each one verbatim under this heading. Copy the exact text; do not paraphrase, summarise or describe them. They tell the agent which skills it must load again before relying on them. Omit the heading when there are none.
Prefer concrete names, paths and values over description. Do not invent anything that is not in the conversation, and do not address the user — this text is notes for the agent itself.

# Configurable round budgets (2026-10-03)

- Main budget: 500; accept Hermes-style `agent.max_turns`, preserving the legacy
  `agent.max_tool_rounds` spelling. Normalize aliases before merging/serialization.
- Child fallback: independent 50 across roles, configurable using
  `agents.defaultToolRounds`. Existing explicit role/definition overrides remain.
- Replace the hard-coded main renewal limit with
  `agent.auto_budget_continuations` (10 by default; 0 disables).
- Round-window defaults match Hermes, not total automatic execution: Snowpea
  retains ten renewals and three child incomplete retries to preserve continuity.
- Reject nonpositive round budgets and negative automatic-renewal settings.

Regression tests: `test_budget_settings.py`, daemon-driven `test_tool_rounds.py`,
and `test_subagent_budget.py`. EN/KO agents manuals show a complete JSON example.

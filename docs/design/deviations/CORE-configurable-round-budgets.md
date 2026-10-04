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

## Resolution order and removed role defaults

`tool_rounds_for` resolves the first configured positive budget in this order:
`agents.maxToolRoundsBy` (agent name, then `default`, then `*`), agent-definition
`max_tool_rounds` / `tool_rounds`, scalar `agents.maxToolRounds`, legacy
`agents.toolRounds` (agent name, then `default`, then `*`, or a scalar), then
`agents.defaultToolRounds` for children or `agent.max_turns` for the main agent.
Definition overrides therefore outrank the scalar global limit; the named/default
`maxToolRoundsBy` mapping outranks definitions. These settings apply to both main
and child sessions before their independent fallback budgets.

The previous built-in role-specific caps (including explore's 8 rounds) were
removed. Explore, architect, reviewer, executor and custom children now all fall
back to 50; use a definition or `maxToolRoundsBy` to restore a smaller role budget.
Regression evidence: `tests/test_round_budget_precedence.py`.

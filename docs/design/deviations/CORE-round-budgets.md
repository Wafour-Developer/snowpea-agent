# Deviations — CORE-round-budgets (per-role budgets, toolless grace call, stage handoffs)

Finishes the token-efficiency architecture across subagent delegation and the team pipeline,
drawing from Hermes Agent (MIT), Claude Code / claw-code, and oh-my-claudecode (OMC) exploration
patterns.

Recorded per the deviations-log convention (`docs/design/deviations/README.md`).

## D1 — Per-role tool-round budgets and precedence

Subagents previously defaulted to a large tool-round budget (or floor), leading to uncontrolled
token spend during exploration and review. Subagents and pipeline stages now have bounded budgets
tailored to their responsibilities.

### Role defaults
- `explore` / `explorer`: **8** rounds
- `reviewer` / `critic`: **16** rounds
- `test-engineer`: **15** rounds
- `verifier`: **14** rounds
- `architect`: **10** rounds
- `executor` and others: **32** rounds

### Precedence resolution
`tool_rounds_for(core, session)` (`agent/loop.py`) resolves round budgets with the following precedence:
1. `agents.maxToolRoundsBy[<role | name>]` in settings (`core/snowpea_core/config/settings.py`)
2. `agents.maxToolRounds` in settings
3. `session.tool_rounds` / `max_tool_rounds` from the agent definition (`AgentDefinition`)
4. Legacy `agents.toolRounds` mapping / scalar
5. Built-in role default (`DEFAULT_TOOL_ROUNDS`)
6. Fallback `SUBAGENT_TOOL_ROUNDS` (32) for subagents, or `agent.max_tool_rounds`

## D2 — Hermes toolless grace call on exhaustion

When a subagent exhausts its tool rounds (`rounds_left <= 0`), it must not crash or run an
interactive prompt. Modeled on Hermes, the agent loop bypasses tool execution and directly executes
one final toolless "grace" model turn (`_budget_report`).

The grace call forces the model to summarize:
- What was decided / completed
- Files touched or discovered
- What remains unfinished
- Key risks or reasons for stopping

This produces an actionable summary report rather than a broken or empty transcript, tagged with
status `done` and reason `budget`.

## D3 — Team pipeline stage hand-offs and diff management

The team pipeline runs stages sequentially (`explore` -> `plan` -> `implement` -> `test` ->
`verify` -> `review` -> `fix` -> `review`).

1. **Structured hand-offs**: Each stage extracts a 10–20 line structured block (Decided, Files touched,
   Findings, Remaining, Risks) from a fenced ````handoff ... ```` block, falling back to the first
   20 lines if no fence is found.
2. **Persistent audit trail**: Hand-offs are persisted on disk at:
   `<workdir>/.snowpea/handoffs/<team-run-id>/<stage>.md`
3. **Cumulative context with diffs**: Subsequent stage briefs carry all prior hand-offs verbatim,
   combined with the working-tree changes (`git diff --stat` and `git diff`), capped at 20,000
   characters. If a diff exceeds this ceiling, it is spilled to disk via `output_spill.py` and
   referenced by a concise pointer (`[… N lines omitted — read_file("…")]`).
4. **Summary report & telemetry**: The pipeline final report lists paths to all stage hand-off files
   and displays a per-stage telemetry table (Stage, Agent, Rounds/Budget, Input/Output Tokens).

## D4 — Exploration discipline and token preservation

Prompt fragments and role definitions (`prompts/fragments/execution.md`, `explore.md`, `explorer.md`,
`reviewer.md`, `critic.md`) adopt OMC exploration discipline:
- Files >200 lines: outline symbols first (`lsp_symbols`) and read targeted ranges.
- Files >500 lines: never read whole file; use windowed `read_file` with offset and limit.
- Maximum 5 parallel reads per round.
- Diminishing returns: stop enquiry after 2 rounds with no new findings.
- Universal execution discipline: do not re-read a file already read unless it changed; do not
  re-run a command whose result is already held.

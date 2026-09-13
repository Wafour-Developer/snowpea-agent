# Design contracts

Binding interface contracts for snowpea-agent, one file per milestone. Names, signatures and event
shapes written here are the API: change the contract first, then the code. Acceptance criteria
(`AC-NN`) are defined in `.omc/plans/snowpea-agent-consensus-plan.md` §5; the wire protocol itself is
generated, and [`../protocol.md`](../protocol.md) wins wherever a contract disagrees with it.

| contract | scope | acceptance criteria |
|---|---|---|
| [m1-core-contract.md](m1-core-contract.md) | Protocol SSOT, RPC transport, core singletons, sessions and events, providers, tools, permissions, agent loop, commands, CLI, SDK (US-004…US-008) | AC-13 (edit/shell clauses), AC-15a; partial AC-03, AC-12. Carries the AC-21 protocol freeze gate and, in §7, the permission matrix and allowlist rules that M4 implements |
| [m2-tools-contract.md](m2-tools-contract.md) | Execution backends (local/docker/ssh), tool catalog, web search and browser providers, MCP (US-009, US-010) | AC-06, AC-18 |
| [m3-providers-setup-contract.md](m3-providers-setup-contract.md) | Vendor presets, provider registry, web login, the setup wizard (US-011, US-012) | AC-02, AC-02b |
| [m5-memory-scheduler-gateway-contract.md](m5-memory-scheduler-gateway-contract.md) | Long-term memory, scheduler, messenger gateway, unattended approvals (US-014…US-016) | AC-07, AC-08, AC-09, AC-17, AC-20 |
| [m6-m7-skills-agents-contract.md](m6-m7-skills-agents-contract.md) | Plugins, skills, generators, subagents, team mode, named agents (US-017…US-021) | AC-03, AC-04, AC-10, AC-11, AC-14, AC-15b, AC-16, AC-17 |
| [m8-packaging-contract.md](m8-packaging-contract.md) | Installers, wheel bundling, npm publish, service install, docs, E2E (US-022) | AC-01, AC-19 |
| [m9-prompts-contract.md](m9-prompts-contract.md) | Prompt library and tier budgets, subagent prompts, denial handling, turn queueing (ships CORE-prompts) | AC-22 … AC-26 |
| [m10-multimodal-audio-contract.md](m10-multimodal-audio-contract.md) | Attachments, vision content parts, STT/TTS chains and audio RPCs (ships CORE-multimodal) | AC-27 … AC-31 |
| [m11-context-compaction-contract.md](m11-context-compaction-contract.md) | Context-window tracking, compaction, store lifetime and shutdown ordering (ships CORE-context, CORE-memory-race, CORE-session-race) | AC-32 … AC-36 |
| [m12-tui-contract.md](m12-tui-contract.md) | Terminal UI surface: layout, HUD, panels, slash registry, resume picker, exit codes | AC-37 … AC-42 |

**M4 has no standalone contract file.** Its subject — the plan/accept/auto permission matrix, the
allowlist promotion rules and the approval queue — lives in
[m1-core-contract.md §7](m1-core-contract.md) and is referenced from
[m5-memory-scheduler-gateway-contract.md](m5-memory-scheduler-gateway-contract.md). M4 satisfies
AC-05, AC-12 and the allowlist clause of AC-13.

## Deviations

[`deviations/`](deviations/README.md) is the per-story log: **one file per story, and never edit
another story's file.** Numbered user stories use `US-0NN.md`; work that is not a numbered story uses
`CORE-<topic>.md`, named after the area of the core it touched. The contract files above are shared,
and several stories appending to them concurrently was losing sections — hence the split. The lead
folds the important deviations back into these contracts at milestone commits, which is what the M9
through M12 files above are.

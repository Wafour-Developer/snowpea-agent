# Round evidence continuity (2026-10-03)

- Persist the conversation at safe model-round boundaries, never insert child
  evidence between a tool-call declaration and its results.
- Queue live child tool results for the lead’s next model request, including
  background children that have not finished. Queue an evidence handoff for
  every child attempt before retrying, including
  failures, terminal reason, agent/session identifiers, bounded report and tool
  checkpoint. Do not replace earlier failure evidence with a successful retry.
- Persist incremental `/ralph`, `/team`, `/workers` output before final command
  summaries. Retain provider exceptions and explicit interrupted/denied status
  for subsequent ordinary prompts.
- Sensitive messages remain redacted on disk. Handoffs are bounded and redacted;
  full child transcripts remain in their referenced sessions. Compaction must
  preserve relevant unresolved failures, verification evidence and trace refs.

Regression coverage: command history, subagent retries, actual model inputs and
per-round durable snapshots, provider failure redaction, and context compaction.

# Readable messenger progress (2026-10-03)

- Preserve one edited progress message per turn, with roughly one-second
  coalesced refreshes and a trailing refresh for fast calls/results.
- Show agent identity, tool arguments, result/error previews, terminal reason,
  round counts, and child reports. Keep multiline previews and final status.
- Retain at most eight recent detail blocks within a hard 1,800-character limit;
  truncate whole blocks without losing agent attribution. This is a progress
  summary, not a replacement for persisted session/tool transcripts.
- Watch child and nested-child tool events, including a first tool call without
  a usage event. One global hub subscription is internally ancestry-filtered
  while children run and restored to the parent at turn boundaries.
- Redact credentials and sensitive tool results, and never forward child final
  messages as separate human-facing answers.
- Cancel deferred updates at turn completion, session switches and shutdown.

Telegram recommends avoiding more than one message per second in one chat:
[official Bots FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
The local edit cadence is conservative coalescing, not a guarantee against all
platform limits. Telegram transport and actual daemon/child routing are covered
by `tests/test_gateway.py` and related gateway suites.

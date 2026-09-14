# Vendoring map — hermes-agent → snowpea

> **Generated file.** The entries table between the `BEGIN GENERATED` and
> `END GENERATED` markers is written by
> `python3 scripts/verify_vendor_integrity.py --render-md` from
> `docs/vendoring-map.json`. Edit the JSON, not the table. Prose outside the
> markers is hand-written and is preserved across renders.

## Upstream

| field | value |
|---|---|
| repository | https://github.com/NousResearch/hermes-agent |
| commit | `8d79c2ff57bba4b07e5b37ed90387b16541aef53` |
| license | MIT (Copyright 2025 Nous Research) |
| reference clone | `$HERMES_REF`, default `/tmp/hermes-ref` |

The reference clone is **read-only**. It is never committed to this repository
and never modified in place. Any machine that runs the integrity check clones
hermes-agent at the commit above and exports `HERMES_REF` to point at it:

```bash
git clone https://github.com/NousResearch/hermes-agent /tmp/hermes-ref
git -C /tmp/hermes-ref checkout 8d79c2ff57bba4b07e5b37ed90387b16541aef53
export HERMES_REF=/tmp/hermes-ref
```

## Rules

1. **Destination layout.** Vendored sources live under
   `core/snowpea_core/vendor/hermes/<module>/...`, mirroring the upstream
   module boundary rather than the upstream flat file names.
2. **Provenance header.** The first line of every vendored file is exactly:

   ```
   # Vendored from hermes-agent @ 8d79c2ff57bba4b07e5b37ed90387b16541aef53, MIT
   ```

3. **Modification is allowed.** Vendored files may be edited to fit snowpea.
4. **Every modification is committed as a patch.** A modified file keeps a
   unified diff at
   `core/snowpea_core/vendor/patches/<destination path relative to core/snowpea_core/vendor/hermes>.patch`.
   The diff goes from the *pristine upstream bytes* to the *working copy*, so
   the provenance header itself appears as an added line inside the patch.
5. **Integrity rule.** Verification is **not** byte-equality with upstream. It is:

   ```
   upstream_original + committed_patch == working_copy      (byte-exact)
   ```

   and, for a file with no patch:

   ```
   header_line + "\n" + upstream_original == working_copy   (byte-exact)
   ```

6. **No orphans.** Every file under `core/snowpea_core/vendor/hermes/`
   (excluding `__init__.py` and `README*`) must appear in the entries table.

## Tooling

```bash
# verify everything (CI job: vendor-integrity)
HERMES_REF=/tmp/hermes-ref python3 scripts/verify_vendor_integrity.py

# verify without a reference clone available (skips the sha256 check)
python3 scripts/verify_vendor_integrity.py --allow-missing-ref

# vendor a new file
python3 scripts/verify_vendor_integrity.py \
    --add tools/terminal_tool.py core/snowpea_core/vendor/hermes/tools/terminal_tool.py \
    --reason "Shell backend selection and timeout handling"

# after editing a vendored file, refresh its patch
python3 scripts/verify_vendor_integrity.py \
    --update-patch core/snowpea_core/vendor/hermes/tools/terminal_tool.py

# regenerate the table below from the JSON
python3 scripts/verify_vendor_integrity.py --render-md
```

## Candidates (not yet vendored)

The list below is the curated shortlist for the milestones that will actually
pull code across. Nothing here is vendored yet; each row becomes an entry in
the table further down only when the file is copied in.

### M2 — tools family

**Shell / terminal**

| upstream path | why |
|---|---|
| `tools/terminal_tool.py` | The whole shell tool: backend dispatch, timeouts, truncation, exit-code reporting. The expensive part to re-derive. |
| `tools/terminal_tool_backends.py` | Local / SSH / Docker backend builders and config-to-kwargs mapping, matching our three exec backends. |
| `tools/terminal_tool_guards.py` | Pre-execution guards: blocked commands, destructive-pattern detection, feeds the approval gate. |
| `tools/terminal_tool_background.py` | Background process launch and tracking, needed for long builds and servers. |
| `tools/terminal_tool_lifecycle.py` | Process lifecycle: reaping, orphan cleanup, shutdown of tracked children. |
| `tools/terminal_tool_result.py` | Result envelope shaping shared by every backend. |
| `tools/process_registry.py` | Registry of live background processes, the state behind `list`/`kill` of running commands. |
| `tools/ansi_strip.py` | Strips ANSI escapes so terminal output never corrupts the transcript. 83 lines, pure win. |
| `tools/shell_heredoc.py` | Heredoc-aware masking so command scanners do not false-positive on embedded text. |
| `tools/tool_output_limits.py` | Configurable output truncation limits shared by all tools. |

**File operations**

| upstream path | why |
|---|---|
| `tools/file_operations.py` | read / write / patch / search over any exec backend, which is exactly our backend-agnostic requirement. |
| `tools/file_operations_common.py` | Shared encoding sniffing, line-ending handling and size guards. |
| `tools/file_operations_search.py` | Content and filename search tier (ripgrep with a pure-Python fallback). |
| `tools/file_tools_paths.py` | Path resolution: task-aware base dir, `~` expansion, workspace-divergence warning. |
| `tools/file_tools_read_tracking.py` | Read-before-write tracking that makes edit tools safe against stale content. |
| `tools/file_tools_write_guards.py` | Write guards for protected paths and accidental overwrites. |
| `tools/path_security.py` | 41-line path-escape validator; reused by skills, cron and credential files. |
| `tools/patch_parser.py` | V4A `*** Begin Patch` parser/applier, the format several providers emit. |
| `tools/fuzzy_match.py` | Fuzzy find-and-replace for LLM edits whose whitespace drifts. The single highest-value trap-avoider in the edit path. |
| `tools/binary_extensions.py` | Binary-file detection table so the agent never reads a blob into context. |

**Browser**

| upstream path | why |
|---|---|
| `tools/browser_tool.py` | Browser automation surface: navigate, click, type, snapshot, over an external browser CLI. |
| `tools/browser_tool_session.py` | Daemon spawn and per-backend session creation, including reconnect. |
| `tools/browser_tool_snapshot.py` | Truncate-and-store of oversized accessibility snapshots. |
| `tools/browser_tool_lifecycle.py` | Browser process lifecycle and cleanup on agent shutdown. |
| `tools/browser_tool_eval_policy.py` | Policy gate for arbitrary JS evaluation, which is an approval-relevant capability. |

**Web search / extract**

| upstream path | why |
|---|---|
| `tools/web_tools.py` | `web_search` / `web_extract` over pluggable backends. |
| `tools/web_tools_extract.py` | URL validation, provider resolution and cache-aware dispatch. |
| `tools/web_tools_truncate.py` | Truncate-and-store pipeline that keeps large pages out of context. |
| `tools/web_result_cache.py` | On-disk result cache, avoids repeat fetches across turns. |
| `tools/url_safety.py` | SSRF guard blocking private and link-local addresses. Security-critical and tedious to get right. |
| `tools/website_policy.py` | Per-domain allow/deny policy for fetches. |

**Delegate**

| upstream path | why |
|---|---|
| `tools/delegate_tool.py` | The `delegate_task` tool itself: schema, validation, sub-agent spawn. Direct analogue of our `agent.spawn`. |
| `tools/delegate_tool_dispatch.py` | Batch and background dispatch, the parallel fan-out that `ultrawork` and `team` need. |
| `tools/delegate_tool_child_run.py` | Child agent run loop and its isolated tool environment. |
| `tools/delegate_tool_results.py` | Result post-processing: summary budgets, spill to disk, cost rollup. |
| `tools/delegate_tool_registry.py` | Registry of in-flight delegations, which backs progress reporting. |
| `tools/delegate_tool_progress.py` | Progress streaming from child to parent. |
| `tools/async_delegation.py` | Async delegation primitives shared by the dispatch paths. |

### M5 — scheduler, gateway platforms, memory

**Cron scheduler**

| upstream path | why |
|---|---|
| `cron/scheduler.py` | `tick()` loop that runs due jobs, plus catch-up after downtime. The core of restart recovery. |
| `cron/jobs.py` | Job storage, schema and validation for the persisted job file. |
| `cron/occurrences.py` | 36-line exact scheduled-identity computation, independent of mutable dispatch stamps. Prevents double-fire. |
| `cron/executions.py` | Durable audit ledger of execution attempts. |
| `cron/delivery_queue.py` | Durable handoff queue for delivering cron output through live gateway adapters. |
| `cron/ledger.py` | Shared SQLite connection and transaction helpers for both ledgers. |
| `cron/lifecycle_guard.py` | Guards against creating jobs that can never be delivered (dead channel, missing gateway). |
| `cron/monitor.py` | Health monitoring and stall detection for the scheduler thread. |

**Gateway platforms**

| upstream path | why |
|---|---|
| `gateway/platforms/base.py` | `BasePlatformAdapter`, the interface every platform implements. Defines the contract we adopt. |
| `gateway/platforms/event.py` | 109-line inbound event dataclasses shared by all adapters. |
| `gateway/platforms/helpers.py` | Message dedup, markdown conversion and chunking shared by adapters. |
| `gateway/platform_registry.py` | Adapter registry and lazy loading. |
| `plugins/platforms/telegram/adapter.py` | Telegram adapter over python-telegram-bot: media, commands, approval buttons. |
| `plugins/platforms/telegram/telegram_network.py` | Hostname-preserving fallback transport, a real-world connectivity fix. |
| `plugins/platforms/telegram/inline_picker.py` | Inline keyboard picker, our approval-button surface. |
| `plugins/platforms/discord/adapter.py` | Discord adapter: gateway connection, threads, attachments. |
| `plugins/platforms/discord/adapter_media.py` | Attachment download and upload handling. |
| `plugins/platforms/discord/recovery.py` | Reconnect and resume after gateway drops. |
| `plugins/platforms/slack/adapter.py` | Slack Socket Mode adapter: messages, slash commands, threads. |
| `plugins/platforms/slack/block_kit.py` | Agent markdown to Block Kit rendering, tedious and well-tested upstream. |

**Memory / full-text search**

| upstream path | why |
|---|---|
| `hermes_state_fts.py` | FTS5 index DDL including the CJK-bigram tokenizer. Korean search works only with this. |
| `hermes_state_search.py` | Full-text, trigram and CJK message search plus FTS maintenance and rebuild. |
| `hermes_state_schema.py` | Session/message table schema and migration ladder that FTS is built on. |
| `tools/memory_tool.py` | The curated-memory tool surface (`MEMORY.md` / `USER.md` equivalents). |
| `tools/memory_tool_store.py` | Bounded file-backed memory store with eviction. |
| `tools/session_search_tool.py` | Cross-session search tool exposed to the agent. |

## Entries

<!-- BEGIN GENERATED: entries -->
| upstream path | upstream commit | file sha256 | destination | patch path | reason |
|---|---|---|---|---|---|
| hermes_cli/mcp_security.py | 8d79c2ff57bb | `05b4dd0c04125671502a006c64700b627cd8af98f3cf8ab7f4635052396c034b` | core/snowpea_core/vendor/hermes/mcp/mcp_security.py | — | Security shapes an MCP server entry must not have: IOC substrings, a shell interpreter with network egress, and writes to OS persistence surfaces (M14 mcp.add). |
| tools/ansi_strip.py | 8d79c2ff57bb | `71f339d1be720c24f407565b4a90cb55164dead2e860cf438258104fd96c1922` | core/snowpea_core/vendor/hermes/tools/ansi_strip.py | — | Strip ANSI escapes from backend shell output before it reaches the transcript. |
| tools/binary_extensions.py | 8d79c2ff57bb | `9dc721f746754753ea71ba898360325bc0eae13ab501dad4381ecb09c9e6cb79` | core/snowpea_core/vendor/hermes/tools/binary_extensions.py | — | Binary-file extension table so read_file never pulls a blob into context. |
| tools/fuzzy_match.py | 8d79c2ff57bb | `151d7de4ec9af37cf79e6d7885f4ae5ff309194acfc316e2b75d1a3362eb253f` | core/snowpea_core/vendor/hermes/tools/fuzzy_match.py | — | Whitespace-tolerant find-and-replace fallback for edit_file when the exact match fails. |
| tools/path_security.py | 8d79c2ff57bb | `e7f2d8c382b0333d3b937f08e1f0ec4ca455926dea43f6c70ca83ea11d62eed0` | core/snowpea_core/vendor/hermes/tools/path_security.py | — | Path-escape validation for glob/grep roots and MCP config discovery. |
| tools/tool_output_limits.py | 8d79c2ff57bb | `cdd96188ee96d631d355a742f3844de364503b4787bc1476d448c89fc794f0da` | core/snowpea_core/vendor/hermes/tools/tool_output_limits.py | — | Shared truncation limits behind settings.tools.max_output_chars. |
| tools/url_safety.py | 8d79c2ff57bb | `e1fe8e1a4d1462c14db4f2760963c9015b821d18d257b17c4e53e2f3d60df9e0` | core/snowpea_core/vendor/hermes/tools/url_safety.py | core/snowpea_core/vendor/patches/tools/url_safety.py.patch | SSRF guard for web_extract: blocks private, loopback and link-local destinations. |
<!-- END GENERATED: entries -->

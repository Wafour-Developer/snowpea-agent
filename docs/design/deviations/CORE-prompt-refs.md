# Deviations — `@` file references and `file.complete`

Against the atref shared contract (TUI, desktop, core).

1. **One `prepare_prompt` entry point.** `agent/prompt_refs.py:prepare_prompt` runs beside `_accept_attachments` from `session.prompt`, gateway chat prompts (`gateway/router.py`), and any path that already called `start_turn` after wire attachments were accepted. Steered and queued turns inherit attachments captured when the prompt was first accepted.

2. **User text keeps `@` tokens.** Resolved files become attachments or appended `[file: path]` blocks; the typed `@ref` strings stay in the user message so history and events stay readable.

3. **`file.complete` lives in `agent/file_complete.py`.** The RPC handler (`server/file_handlers.py`) only resolves the workdir and maps errors; listing, fuzzy match, gitignore cache and escape refusal are pure functions testable without a daemon.

4. **Trailing `!?` stripped from unquoted paths.** The contract names `.,;:)`; `?` after `@shot.png` in a sentence is stripped the same way so prompts do not need a space before punctuation.

5. **`agent.autoAttachImages` defaults true.** Bare image paths (no `@`) attach once per distinct path; `@` refs always resolve regardless of the setting.

6. **MCP images use `ToolResult.meta["images"]`.** `render_content` returns `RenderedMcpContent`; the agent loop's image hook is tool-agnostic (`flush_tool_image_messages` in `tools/view_image.py`). Base64 never enters `tool.result` events or history text.

7. **Transcript vs model text (D2).** `prepare_prompt` returns `PreparedPrompt`: `text` is what the user typed (plus inline wire `kind="text"` attachments); `model_text` adds inlined file bodies for the provider; `refs` is a list of `{path, kind, lines?, truncated}` on the `message.user` event. History stores `model_text`; the TUI transcript shows `text` and `refs` only.

8. **Gateway scope (D4).** `prepare_prompt(..., scope="workdir")` is used for Telegram/Discord/Slack: paths outside the session workdir are refused. In every scope, secret paths (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `.ssh`/`.gnupg`/`.aws` components, anything under `$SNOWPEA_HOME` except `attachments/`) get a `refs` entry with `kind="refused"` and are never inlined or attached.

9. **Trailing trim (D6).** For `@` refs and bare image paths, a token is shortened from the end while the candidate does not exist: drop trailing ASCII punctuation (`.,;:)!?]}>'"`) or any non-ASCII letter (Korean particles). Line ranges (`:10-40`) are applied after each trim step. Bare images are found by whitespace tokenisation, not a monolithic regex.

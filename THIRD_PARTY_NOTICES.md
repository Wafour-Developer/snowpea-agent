# Third-party notices

Code and data ported from projects under licences other than MIT. Each derived
file also says so in a header comment. See `NOTICE` for MIT-licensed sources.

## herdr

- Source: https://github.com/herdrdev/herdr (reviewed at commit `e8d7d9705a7a2c89cef4dd42079379f05685af87`, 2026-11-20)
- Licence: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0)
- Portions derived from herdr, Apache-2.0:
  - `core/snowpea_core/terminal/integrations/` — the CLI hook install / uninstall rules (exact-command identity, atomic writes that refuse hard links, the Codex `[features] hooks` edit, Claude's SessionStart matcher, the per-CLI hook payload rules) from `src/integration/*.rs` and `src/integration/assets/*`; rewritten in Python.
  - `core/snowpea_core/terminal/rules/*.toml` and `core/snowpea_core/terminal/rules/engine.py` — the screen detection manifests (`src/detect/manifests/{claude,codex,cursor,antigravity}.toml`) and the rule engine semantics and regions (`src/detect/manifest.rs`); regexes translated from Rust to Python syntax, `kind` / `extract` fields added.

Changes from upstream: the files are rewritten or translated, not copied; the
hook scripts report to snowpea's terminal report socket instead of herdr's API.

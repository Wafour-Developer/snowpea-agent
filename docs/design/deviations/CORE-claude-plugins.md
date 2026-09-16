# Deviations — CORE-claude-plugins (Claude Code plugin scanning)

snowpea scans plugins installed in Claude Code (`~/.claude/plugins/installed_plugins.json`) as source `claude-plugin`, respecting `enabledPlugins` in `~/.claude/settings.json`.
Snowpea-installed plugins and skills take precedence over Claude copies, and Claude plugin hooks are not registered.

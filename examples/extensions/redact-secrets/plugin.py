"""redact-secrets: a post_execute hook with a validated config.

Every tool result passes through the hook before the model reads it. Text that
looks like a credential is replaced with [redacted]. Extra patterns come from
settings.extensions.redact-secrets.config.patterns, which plugin.json's
configSchema checks before apply() runs.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, PostDecision, ToolContext, ToolResult

BUILT_IN = [
    r"sk-[A-Za-z0-9_-]{20,}",  # OpenAI-style API keys
    r"ghp_[A-Za-z0-9]{36}",  # GitHub personal access tokens
    r"AKIA[0-9A-Z]{16}",  # AWS access key ids
]


def apply(ctx: PluginContext) -> None:
    # The schema guarantees a list of strings; compiling can still fail.
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]

    def redact(
        tool_ctx: ToolContext, name: str, args: dict[str, Any], result: ToolResult
    ) -> PostDecision | None:
        text = result.output if result.ok else (result.error or "")
        cleaned = text
        for pattern in patterns:
            cleaned = pattern.sub(marker, cleaned)
        if cleaned == text:
            return None  # no change: the next listener sees the result as it was
        ctx.log.info("redacted a secret from %s", name)
        if not result.ok:
            # accept() replaces the output only; for a failed call, block with the clean text.
            return PostDecision.block(cleaned)
        return PostDecision.accept(cleaned, "A credential in this result was redacted.")

    # Order -100: before listeners at the default 0, so they never see the secret.
    ctx.hooks.post_execute("redact", redact, order=-100)

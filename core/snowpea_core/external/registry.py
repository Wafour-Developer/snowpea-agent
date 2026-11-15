"""Which external CLI adapters this core has (M19 WP5).

The four CLIs are always reported by ``external.clis``, in this order; one
without a registered adapter is shown as not available.  The per-CLI adapters
(WP6a-d) add themselves to :data:`BUILTIN_ADAPTERS`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from snowpea_core.external.base import ExternalAdapter
from snowpea_core.external.codex import CodexAdapter

#: ``external.clis`` order.
CLI_ORDER: tuple[str, ...] = ("claude", "codex", "agy", "cursor")
#: Display names for CLIs that have no adapter in this core.
CLI_TITLES: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "agy": "Antigravity (agy)",
    "cursor": "Cursor agent",
}

#: Factories for the adapters shipped with core, filled in by WP6a-d.
BUILTIN_ADAPTERS: list[Callable[[], ExternalAdapter]] = []
BUILTIN_ADAPTERS.append(CodexAdapter)


def _claude() -> ExternalAdapter:
    from snowpea_core.external.claude import ClaudeAdapter

    return ClaudeAdapter()


BUILTIN_ADAPTERS.append(_claude)


def _agy() -> ExternalAdapter:
    from snowpea_core.external.agy import AgyAdapter

    return AgyAdapter()


BUILTIN_ADAPTERS.append(_agy)


class AdapterRegistry:
    """CLI id -> adapter."""

    def __init__(self, adapters: Iterable[ExternalAdapter] = ()) -> None:
        self._adapters: dict[str, ExternalAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    @classmethod
    def builtin(cls) -> AdapterRegistry:
        return cls(factory() for factory in BUILTIN_ADAPTERS)

    def register(self, adapter: ExternalAdapter) -> None:
        if adapter.id not in CLI_ORDER:
            raise ValueError(f"unknown external CLI {adapter.id!r}")
        self._adapters[adapter.id] = adapter

    def unregister(self, cli: str) -> None:
        self._adapters.pop(cli, None)

    def get(self, cli: str) -> ExternalAdapter | None:
        return self._adapters.get(cli)

    def ids(self) -> list[str]:
        return [cli for cli in CLI_ORDER if cli in self._adapters]

    def __len__(self) -> int:
        return len(self._adapters)


__all__ = ["BUILTIN_ADAPTERS", "CLI_ORDER", "CLI_TITLES", "AdapterRegistry"]

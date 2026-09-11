"""The answers the wizard collects, independent of how they were collected.

Screens mutate a :class:`WizardState`; :func:`WizardState.write` is the single
place that turns those answers into ``$SNOWPEA_HOME/settings.json``.  Keeping
the two apart is what lets the non-interactive flags and the interactive
screens share one code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.setup import catalog

#: The id every screen carries as its last row.
SKIP = "__skip__"
SKIP_LABEL = "Skip — keep defaults"


@dataclass
class WizardState:
    """Everything the wizard will write, seeded from the current settings."""

    vendor: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    search_provider: str = catalog.DEFAULT_SEARCH_PROVIDER
    browser_provider: str = catalog.DEFAULT_BROWSER_PROVIDER
    #: category id -> enabled.
    tool_categories: dict[str, bool] = field(default_factory=dict)
    #: gateway id -> its config block (``{"enabled": True, "token": "..."}``).
    gateways: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Lines the providers screen shows above the list (detected keys, imports).
    hints: list[str] = field(default_factory=list)
    #: Human-readable record of what the run did, printed as the summary.
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings) -> WizardState:
        """Start from what is already configured, not from a blank sheet."""
        defaults = {item.id: item.default for item in catalog.tools_catalog()}
        enabled = list(getattr(settings.tools, "enabled_categories", []) or [])
        if enabled:
            defaults = {cid: cid in enabled for cid in defaults}
        gateways = {
            str(gid): dict(block)
            for gid, block in (getattr(settings, "gateway", None) or {}).items()
            if isinstance(block, dict)
        }
        return cls(
            vendor=(
                str(settings.providers.get("default"))
                if isinstance(settings.providers.get("default"), str)
                else None
            ),
            search_provider=settings.search.provider or catalog.DEFAULT_SEARCH_PROVIDER,
            browser_provider=settings.browser.provider or catalog.DEFAULT_BROWSER_PROVIDER,
            tool_categories=defaults,
            gateways=gateways,
        )

    # -- mutation ------------------------------------------------------
    def enabled_categories(self) -> list[str]:
        """The enabled ids in catalog order."""
        return [cid for cid in catalog.known_categories() if self.tool_categories.get(cid)]

    def set_categories(self, selected: set[str]) -> None:
        """Replace the whole set (what the multi-select screen hands back)."""
        self.tool_categories = {cid: cid in selected for cid in catalog.known_categories()}

    def apply_tools_flag(self, spec: str) -> list[str]:
        """``"vision,-git"`` — enable, disable with a leading ``-``.

        Returns the ids it did not recognise so the caller can complain.
        """
        unknown: list[str] = []
        known = set(catalog.known_categories())
        for raw in spec.split(","):
            token = raw.strip()
            if not token:
                continue
            off = token.startswith("-")
            cid = token[1:] if off else token
            if cid not in known:
                unknown.append(cid)
                continue
            self.tool_categories[cid] = not off
        return unknown

    def enable_gateway(self, gateway_id: str, token: str | None = None) -> None:
        block = dict(self.gateways.get(gateway_id) or {})
        block["enabled"] = True
        if token:
            block["token"] = token
        self.gateways[gateway_id] = block

    # -- output --------------------------------------------------------
    def write(self, paths: Paths, settings: Settings) -> Settings:
        """Fold the answers into ``settings`` and persist them."""
        from snowpea_core.providers.registry import ProviderRegistry

        if self.vendor:
            registry = ProviderRegistry(settings)
            config: dict[str, Any] = {}
            if self.api_key:
                config["api_key"] = self.api_key
            if self.model:
                config["model"] = self.model
            if self.base_url:
                config["base_url"] = self.base_url
            registry.configure(self.vendor, config)
            settings.providers["default"] = self.vendor

        settings.search.provider = self.search_provider
        settings.browser.provider = self.browser_provider
        settings.tools.enabled_categories = self.enabled_categories()
        settings.gateway = {
            gid: block for gid, block in self.gateways.items() if block.get("enabled")
        }
        settings.save(paths)
        return settings

    def summary(self) -> list[str]:
        """The lines ``snowpea setup`` prints when it is done."""
        lines = [
            f"provider   {self.vendor or '(none configured)'}"
            + (f"  model {self.model}" if self.model else ""),
            f"search     {self.search_provider}",
            f"browser    {self.browser_provider}",
            f"tools      {len(self.enabled_categories())} categories on"
            f" ({', '.join(self.enabled_categories())})",
            "gateway    "
            + (
                ", ".join(sorted(g for g, b in self.gateways.items() if b.get("enabled")))
                or "(none)"
            ),
        ]
        return lines + list(self.notes)


__all__ = ["SKIP", "SKIP_LABEL", "WizardState"]

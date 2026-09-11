"""``snowpea setup`` — the Quick / Full / Blank wizard (M3 contract §5).

``catalog`` is the data (what can be chosen), ``screens`` are pure
build/apply pairs (what a screen shows and what an answer means), ``ui``
renders them, and ``wizard`` drives the order and writes ``settings.json``.
``detect`` supplies the import hints the providers screen prints.
"""

from __future__ import annotations

from snowpea_core.setup.catalog import (
    CatalogItem,
    browser_catalog,
    gateway_catalog,
    search_catalog,
    tools_catalog,
    vendor_catalog,
)
from snowpea_core.setup.detect import Detected
from snowpea_core.setup.state import WizardState
from snowpea_core.setup.wizard import SetupError, SetupResult, login, run

__all__ = [
    "CatalogItem",
    "Detected",
    "SetupError",
    "SetupResult",
    "WizardState",
    "browser_catalog",
    "gateway_catalog",
    "login",
    "run",
    "search_catalog",
    "tools_catalog",
    "vendor_catalog",
]

"""The scripted fake provider (M1 contract §5, M3 contract §4).

There is exactly one implementation — :class:`snowpea_core.providers.fake.FakeProvider`
— and this module is the name the contracts use for it.  Select it with
``SNOWPEA_PROVIDER=fake:<path-to-script.json>``.

Script steps support ``match`` / ``after_tool`` / ``tool_calls`` / ``text`` /
``repeat`` / ``delaySec``; see the adapter's docstring for the full format.
"""

from __future__ import annotations

from snowpea_core.providers.fake import FakeProvider

__all__ = ["FakeProvider"]

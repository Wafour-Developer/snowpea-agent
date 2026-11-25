"""``asksUser`` on tool descriptions (protocol 1.80.0, capability ``toolAsksUser``).

Host tools declare a fixed value on registration; built-ins derive it each round
from the permission tag and the mode matrix.  The suffix is appended only when
specs are built for the model, not stored on :class:`~snowpea_core.tools.registry.Tool`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from snowpea_core.permissions.policy import MODE_MATRIX, Mode
from snowpea_core.providers.base import ToolSpec
from snowpea_core.security import enforce

if TYPE_CHECKING:
    from snowpea_core.tools.registry import Tool

AsksUser = Literal["always", "first_use", "sometimes", "never"]

KNOWN: frozenset[str] = frozenset({"always", "first_use", "sometimes", "never"})

SUFFIX: dict[str, str] = {
    "always": " (asks the user itself — don't ask_user for permission first)",
    "first_use": " (asks the user itself the first time — don't ask_user for permission first)",
    "sometimes": " (some calls ask the user themselves — don't ask_user for permission first)",
}


def parse(raw: Any) -> AsksUser:
    """Registration and protocol parsing: unknown and absent → ``never``."""
    if raw is None:
        return "never"
    value = str(raw)
    return value if value in KNOWN else "never"  # type: ignore[return-value]


def suffix_for(value: AsksUser) -> str:
    return SUFFIX.get(value, "")


def host_value(tool: Tool) -> AsksUser:
    return parse(getattr(tool, "asks_user", None))


def derive_builtin(tool: Tool, mode: Mode, *, checks_off: bool) -> AsksUser:
    if checks_off:
        return "never"
    if tool.permission_for is not None:
        return "sometimes"
    verdict = MODE_MATRIX.get(mode, {}).get(tool.permission, "ask")
    if verdict == "ask":
        return "always"
    return "never"


def effective(tool: Tool, mode: Mode, *, checks_off: bool) -> AsksUser:
    source = str(getattr(tool, "source", "") or "")
    if source.startswith("host:"):
        return host_value(tool)
    return derive_builtin(tool, mode, checks_off=checks_off)


def stamp_spec(tool: Tool, spec: ToolSpec, mode: Mode, *, checks_off: bool) -> ToolSpec:
    suffix = suffix_for(effective(tool, mode, checks_off=checks_off))
    if suffix:
        spec.description = f"{tool.description}{suffix}"
    return spec


def stamp_tools(tools: list[Tool], session: Any, settings: Any) -> list[ToolSpec]:
    mode = getattr(session, "mode", "accept") or "accept"
    if mode not in ("plan", "accept", "auto"):
        mode = "accept"
    checks_off = not enforce.enabled(settings)
    return [stamp_spec(tool, tool.spec(), mode, checks_off=checks_off) for tool in tools]


__all__ = [
    "KNOWN",
    "SUFFIX",
    "AsksUser",
    "derive_builtin",
    "effective",
    "host_value",
    "parse",
    "stamp_spec",
    "stamp_tools",
    "suffix_for",
]
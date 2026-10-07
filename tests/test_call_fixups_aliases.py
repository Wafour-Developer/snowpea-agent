"""Tool names and arguments other agents use reach snowpea's tools."""

from snowpea_core.agent import call_fixups
from snowpea_core.tools.shell import DEFAULT_TIMEOUT, MAX_TIMEOUT, _fit_sleeps

KNOWN = ["memory_write", "memory_search", "plan_update_step", "shell"]


def test_memory_names_from_other_agents_resolve() -> None:
    for name in ("update_memory", "save_memory", "remember", "memory", "memory_add"):
        assert call_fixups.resolve_name(name, KNOWN) == "memory_write", name
    assert call_fixups.resolve_name("search_memory", KNOWN) == "memory_search"
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    fixed = call_fixups.coerce_arguments({"content": "the repo is llm_opt"}, schema)
    assert fixed.get("text") == "the repo is llm_opt"


def test_a_long_sleep_gets_a_timeout_that_covers_it() -> None:
    assert _fit_sleeps("sleep 240; ssh hon1 'tail log'", DEFAULT_TIMEOUT) == 240 + DEFAULT_TIMEOUT
    assert _fit_sleeps("sleep 5 && ls", DEFAULT_TIMEOUT) == DEFAULT_TIMEOUT
    assert _fit_sleeps("sleep 3m", DEFAULT_TIMEOUT) == 180 + DEFAULT_TIMEOUT
    assert _fit_sleeps("sleep 99999", DEFAULT_TIMEOUT) == MAX_TIMEOUT
    assert _fit_sleeps("echo nosleep", DEFAULT_TIMEOUT) == DEFAULT_TIMEOUT

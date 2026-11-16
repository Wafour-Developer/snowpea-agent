"""Security review 5, M1: deny by default, one verdict per method and connection kind.

Every registered RPC method has an explicit row here: whether the dispatcher's
gates (``person_gate.method_refusal``, ``settings_gate.browser_method_refusal``,
``settings_gate.kind_method_refusal``) let it through for each kind.  A method
that is registered but not listed fails :func:`test_every_method_has_a_verdict`,
so a new method is refused to everyone but a local person until it is placed.

Columns: local person, remote web login, connector, legacy browser (< 0.3.0),
browser 0.3.0 without the ``actor`` marker, browser 0.3.0 with it.  ``y`` passes
the dispatcher (the handler may still refuse), ``n`` is ``jarvis_person_only``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, settings_gate
from snowpea_core.server.app_server import Core, build_dispatcher
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import method_refusal

KINDS = ("person", "remote", "connector", "legacy", "unmarked", "marked")

#: verdict code -> methods.  Each method appears exactly once.
TABLE: dict[str, tuple[str, ...]] = {
    # Everyone: the handshake, liveness, reads and streams a connector's
    # watchers use, and the calls that only take authority away from Jarvis.
    "yyyyyy": (
        "system.hello", "system.info", "system.health",
        "session.list", "session.resume", "session.events", "session.trajectory",
        "job.list", "job.preview", "job.runs", "job.nextRunForClient",
        "external.clis", "external.list", "external.get", "external.read", "external.unwatch",
        "mcp.preset.list", "jarvis.takeover", "jarvis.delegation.revoke",
    ),
    # Every local connection (installers and launchers stop the daemon), never a
    # remote login.
    "ynyyyy": ("system.shutdown",),
    # Operating sessions: people (local and remote) and browsers; no connector.
    "yynyyy": (
        "system.checkUpdate",
        "session.create", "session.prompt", "session.steer", "session.interrupt",
        "session.close", "session.rename", "session.setMode", "session.setModel",
        "session.setEffort", "session.setAgent", "session.setActive", "session.pin",
        "session.archive", "session.deleteSaved", "session.fork", "session.lineage",
        "session.compact", "session.continue", "session.notice", "session.artifacts",
        "session.attachments",
        "goal.set", "goal.get", "goal.pause", "goal.resume", "goal.clear",
        "checkpoint.list", "checkpoint.diff", "checkpoint.restore", "checkpoint.delete",
        "command.list", "command.run", "tool.list", "file.complete", "usage.summary",
        "approval.list", "approval.respond", "question.list", "question.respond",
        "fs.list", "fs.glob", "fs.stat", "fs.read", "fs.hash", "fs.write",
        "workspace.list", "workspace.add", "workspace.remove", "workspace.rename",
        "workspace.reorder",
        "agent.list", "agent.spawn",
        "team.start", "team.status", "team.guide.list", "team.guide.get",
        "job.schedule", "job.cancel", "job.delete", "job.runNow", "job.update",
        "settings.get", "setup.catalog", "setup.status", "provider.list", "provider.models",
        "provider.test", "mcp.list", "mcp.catalog", "mcp.authorize.complete",
        "skill.list", "skill.read", "skill.search",
        "memory.list", "memory.search", "memory.write", "memory.delete", "memory.confirm",
        "lsp.status", "lsp.catalog", "lsp.restart",
        "audio.capabilities", "audio.voices", "audio.speak", "audio.transcribe",
        "audio.record.start", "audio.record.stop",
        "gateway.list", "gateway.unbind", "gateway.sync", "permission.allowlist.list",
        "external.open", "external.send", "external.setMode", "external.interrupt",
        "external.create", "external.close",
    ),
    # A browser host's own runtime: never a remote login (M3) or a connector.
    "ynnyyy": (
        "session.attach", "session.setBrowserProvider", "session.toolContent",
        "tool.register", "tool.unregister", "tool.progress", "turn.outcome",
        "approval.ask", "approval.evaluate", "llm.sample", "web.search", "memory.ingest",
        "site.get", "site.list", "site.stats", "site.put", "site.mark", "site.markPart",
        "site.delete", "site.playbook.get", "site.playbook.put", "site.playbook.edit",
        "site.playbook.delete", "site.playbook.mark",
    ),
    # Config a remote login may change; a browser only with the marker.
    "yynyny": (
        "settings.set", "system.reloadSettings",
        "provider.configure", "provider.loginWeb", "provider.remove", "provider.visionReset",
        "mcp.add", "mcp.update", "mcp.remove", "mcp.reload", "mcp.authorize",
        "mcp.deauthorize", "mcp.test",
        "skill.create", "skill.learn", "skill.reload", "skill.remove", "skill.write",
        "skill.install", "memory.update", "audio.install", "lsp.install",
    ),
    # Config a browser changes with the marker, but no remote login.
    # Security review 7, Low 8: team.guide.delete is the class of team.guide.set
    # (it was yynyyy: a remote login and an unmarked browser call could delete).
    "ynnyny": ("setup.applyDefaults", "permission.allowlist.remove", "team.guide.set",
               "team.guide.delete"),
    # People (local and remote): Jarvis, security lists, project trust status.
    "yynnnn": (
        # Security review 6 (the browser team's source check): no browser calls
        # these, marker or not, any version.
        "agent.delete",
        "jarvis.status", "jarvis.overview", "jarvis.disable", "jarvis.project.list",
        "jarvis.project.rename", "jarvis.project.alias", "jarvis.escalations.list",
        "jarvis.escalations.dismiss", "jarvis.ledger", "jarvis.delegation.list",
        "jarvis.delegation.set", "jarvis.handback", "jarvis.briefing.get",
        "jarvis.briefing.set", "mcp.preset.check",
        "security.get", "security.check", "security.presets.list", "project.trust.status",
    ),
    # A local person only.
    "ynnnnn": (
        "jarvis.enable", "mcp.preset.apply", "mcp.preset.remove", "external.clis.confirmPath",
        "permission.allowlist.add",
        # Security review 6, M-4: binding a messenger chat is a local person's
        # (never a browser since 1.55.0, no longer a remote login either).
        "gateway.bind",
        # Security review 7, M-1/Low 8: agent.create (was yynnnn: it writes a
        # project's agent definition, a control file, and a named agent runs
        # unattended) and agent.bindChannel (was yynyyy: a messenger binding,
        # judged by nobody) are a local person's.
        "agent.create", "agent.bindChannel",
        "web.remote.status", "web.remote.listen", "web.remote.stop", "web.remote.setPassword",
        "web.remote.clearPassword", "web.remote.rotateToken",
        "extension.enable", "extension.disable", "extension.configure", "extension.reload",
        "extension.list", "system.update", "system.restart",
        "project.trust.set", "project.trust.revoke",
        "system.echoRequest",  # registered only under SNOWPEA_TEST=1
    ),
}


def _conn(kind: str) -> Any:
    client_kind = {"person": "ide", "remote": "web", "connector": "connector"}.get(kind, "browser")
    return SimpleNamespace(
        authenticated=True, closed=False, client_kind=client_kind,
        remote_login=kind == "remote",
        client_version="0.2.0" if kind == "legacy" else "0.3.0",
        client_id=f"{kind}-1", surface_id=f"{kind}-s",
    )


def verdict(method: str, kind: str) -> str:
    """``y`` when the dispatcher's gates let ``method`` through for ``kind``."""
    conn = _conn(kind)
    params: dict[str, Any] = {"actor": "person"} if kind == "marked" else {}
    try:
        method_refusal(method, conn)
        settings_gate.browser_method_refusal(method, params, conn, None)
        settings_gate.kind_method_refusal(method, params, conn, None)
    except RpcError as exc:
        assert exc.code == errors.JARVIS_PERSON_ONLY, (method, kind, exc)
        return "n"
    return "y"


def _expected() -> dict[str, str]:
    out: dict[str, str] = {}
    for code, methods in TABLE.items():
        for method in methods:
            assert method not in out, f"{method} is listed twice"
            out[method] = code
    return out


@pytest.fixture
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("SNOWPEA_TEST", "1")
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    return build_dispatcher(core).method_names()


def test_every_method_has_a_verdict(registered: list[str]) -> None:
    expected = _expected()
    unlisted = sorted(set(registered) - set(expected))
    assert not unlisted, f"methods with no verdict (place them in TABLE): {unlisted}"
    wrong: list[str] = []
    for method in registered:
        got = "".join(verdict(method, kind) for kind in KINDS)
        if got != expected[method]:
            wrong.append(f"{method}: expected {expected[method]}, got {got}")
    assert not wrong, "\n".join(wrong)


def test_a_new_method_is_refused_to_every_kind_but_a_local_person() -> None:
    assert "".join(verdict("brand.new.method", kind) for kind in KINDS) == "ynnnnn"


def test_listed_methods_are_registered(registered: list[str]) -> None:
    stale = sorted(set(_expected()) - set(registered))
    assert not stale, f"listed but not registered: {stale}"


def test_the_tables_name_only_registered_methods(registered: list[str]) -> None:
    names = set(registered)
    for table in (settings_gate.CONNECTOR_ALLOWED, settings_gate.REMOTE_LOGIN_ALLOWED,
                  settings_gate.BROWSER_OPEN_METHODS):
        assert table <= names, sorted(table - names)

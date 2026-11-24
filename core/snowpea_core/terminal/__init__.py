"""Terminal sessions (M37): detached PTY holders, a pyte screen, status, an input gate
and CLI hook integrations.

The daemon keeps one :class:`~snowpea_core.terminal.manager.TerminalManager`; every
terminal is a separate holder process (``python -m snowpea_core.terminal.holder``)
that owns the PTY, so terminals survive a daemon restart.

Kept import-free: ``snowpea-core hook report`` imports a module below this
package on every CLI hook call, and that path must stay stdlib-only and fast.
"""

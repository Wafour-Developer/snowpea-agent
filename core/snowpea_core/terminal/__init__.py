"""Terminal sessions (M37): detached PTY holders, a pyte screen, status and an input gate.

The daemon keeps one :class:`~snowpea_core.terminal.manager.TerminalManager`; every
terminal is a separate holder process (``python -m snowpea_core.terminal.holder``)
that owns the PTY, so terminals survive a daemon restart.
"""

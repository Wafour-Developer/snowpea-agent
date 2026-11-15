"""Jarvis mode (M19, beta): one supervising session per daemon.

Modules: :mod:`~snowpea_core.jarvis.classify` (what an approval request is),
:mod:`~snowpea_core.jarvis.delegation` (who may decide it: grants, routing,
pauses, the server-side re-check) and :mod:`~snowpea_core.jarvis.ledger` (the
append-only record).  Design: docs/design/m19-jarvis-mode.md.
"""

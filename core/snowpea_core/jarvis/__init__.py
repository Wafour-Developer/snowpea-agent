"""Jarvis mode (M19, beta): one supervising session per daemon.

Design: ``docs/design/m19-jarvis-mode.md``.  The service lives in
:mod:`snowpea_core.jarvis.service`; the delegation engine plugs in through
:mod:`snowpea_core.jarvis.ports`.
"""

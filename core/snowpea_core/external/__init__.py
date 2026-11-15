"""External agent CLIs hosted by snowpea (M19, Jarvis mode beta).

``base`` is the adapter interface, ``proc`` the process-safety rules every
adapter goes through, ``timeline`` the record -> timeline conversion,
``registry`` the adapters this core has and ``manager`` the open-session table
behind the ``external.*`` RPCs.  Design: docs/design/m19-jarvis-mode.md.
"""

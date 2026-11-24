"""The wire protocol's version, apart from :mod:`snowpea_core.server.protocol`.

``snowpea-core --version`` prints it without importing pydantic and the whole
protocol (apps probe an installed core with it before they spawn one), so the
constant lives here; :mod:`snowpea_core.server.protocol` re-exports it.
"""

PROTOCOL_VERSION = "1.76.0"

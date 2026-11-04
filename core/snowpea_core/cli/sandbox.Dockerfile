# The image `snowpea --docker` builds on first use (snowpea_core/cli/sandbox.py).
#
# One image per snowpea version: the daemon in the container must match the
# terminal UI on the host, so the build installs exactly the client's version
# (SNOWPEA_REQUIREMENT, computed by the CLI).  The CLI starts it as the host
# user (`docker run --user UID:GID`), so nothing in here may need root at run
# time: /home/snowpea is world-writable and holds the sandbox's own ~/.snowpea.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# What the tools shell out to: git and ripgrep for code, ffmpeg for media,
# curl for fetching, node/npm for MCP servers and language servers started
# through npx, ps for process listings.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg git nodejs npm procps ripgrep \
 && rm -rf /var/lib/apt/lists/*

# `wheels/` holds a local wheel when SNOWPEA_SANDBOX_SOURCE names one; it is
# empty otherwise.
COPY wheels/ /tmp/wheels/
ARG SNOWPEA_REQUIREMENT
RUN test -n "$SNOWPEA_REQUIREMENT" \
 && pip install "$SNOWPEA_REQUIREMENT" \
 && snowpea-core --version \
 && rm -rf /tmp/wheels

RUN useradd --create-home --uid 1000 --shell /bin/bash snowpea \
 && install -d -m 0777 /home/snowpea /home/snowpea/.snowpea /home/snowpea/.local /home/snowpea/.cache

ENV HOME=/home/snowpea SNOWPEA_HOME=/home/snowpea/.snowpea \
    PATH=/home/snowpea/.local/bin:$PATH
USER snowpea
WORKDIR /home/snowpea

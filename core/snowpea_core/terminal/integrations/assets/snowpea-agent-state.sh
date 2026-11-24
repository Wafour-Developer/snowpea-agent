#!/bin/sh
# installed by snowpea; reinstalling or updating the integration overwrites this file.
# add your own hooks beside this file instead of editing it.
# SNOWPEA_INTEGRATION_ID=snowpea-agent-state
# SNOWPEA_INTEGRATION_VERSION=3
#
# Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0.
#
# Usage: snowpea-agent-state.sh <cli> <action>   (the hook's JSON arrives on stdin)
# Reports the CLI's session id and state to the snowpea terminal it runs in.
# Outside a snowpea terminal (SNOWPEA_TERMINAL_ID unset) it does nothing.

cli="${1:-}"
action="${2:-}"

# Antigravity CLI wants a JSON object on stdout from every hook; we never inject anything.
[ "$cli" = "agy" ] && printf '{}\n'

if [ -z "${SNOWPEA_TERMINAL_ID:-}" ] || [ -z "${SNOWPEA_TERMINAL_REPORT:-}" ]; then
  cat >/dev/null 2>&1
  exit 0
fi

# The snowpea-core that installed this hook, written in at install time; the
# environment ($SNOWPEA_CORE_BIN, PATH) is never trusted to choose it.
bin=@@SNOWPEA_CORE_BIN@@
if [ -z "$bin" ] || [ ! -x "$bin" ]; then
  cat >/dev/null 2>&1
  exit 0
fi

"$bin" hook report --cli "$cli" --action "$action" >/dev/null 2>&1
exit 0

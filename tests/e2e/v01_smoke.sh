#!/usr/bin/env bash
# v0.1 end-to-end smoke run for macOS and Linux (plan §7.9, M8 contract §6).
#
# Runs the fifteen steps of plan §7.9 in order and prints one line per step:
#
#   PASS n <what was checked>
#   FAIL n <why it failed>
#   SKIP n <why it was not run>
#
# Exit code is 0 when no step failed, 1 otherwise.  Steps 10 and 11 need real
# messenger credentials, so they only run with SNOWPEA_E2E_CREDENTIALED=1.
#
# Everything is self-contained: a throwaway git repo at /tmp/snowpea-fixture, a
# throwaway SNOWPEA_HOME, and a throwaway uv tool directory.  The user's own
# snowpea installation, daemon and shell rc files are never touched.  All LLM
# calls go to the scripted fake provider, so no API key is needed.
#
# Usage
#   tests/e2e/v01_smoke.sh [--from-checkout] [--from-url] [--keep]
#
#   --from-checkout  install the checkout this script lives in (default when a
#                    checkout is found; this is what CI uses)
#   --from-url       install from the published raw install.sh URL instead
#   --keep           leave the fixture repo and the home directory behind
#
# Environment
#   SNOWPEA_E2E_CREDENTIALED=1  also run steps 10 and 11
#   SNOWPEA_E2E_ROOT            scratch root (default /tmp)

set -u

# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
E2E_ROOT="${SNOWPEA_E2E_ROOT:-/tmp}"

FIXTURE_REPO="$E2E_ROOT/snowpea-fixture"
E2E_HOME="$E2E_ROOT/snowpea-e2e-home"
TOOL_ROOT="$E2E_ROOT/snowpea-e2e-tools"
FAKE_SCRIPT="$REPO_ROOT/tests/fixtures/providers/fake/e2e.json"
# The version the checkout declares; the installed CLI must report exactly this.
EXPECTED_VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO_ROOT/core/snowpea_core/__init__.py")"
SAMPLE_PLUGIN="$REPO_ROOT/tests/fixtures/plugins/sample-plugin"
INSTALL_URL="https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh"

FROM_CHECKOUT=1
KEEP=0
[ -f "$REPO_ROOT/installer/install.sh" ] || FROM_CHECKOUT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --from-checkout) FROM_CHECKOUT=1 ;;
    --from-url) FROM_CHECKOUT=0 ;;
    --keep) KEEP=1 ;;
    -h | --help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

PASSED=0
FAILED=0
SKIPPED=0

pass_step() {
  printf 'PASS %s %s\n' "$1" "$2"
  PASSED=$((PASSED + 1))
}
fail_step() {
  printf 'FAIL %s %s\n' "$1" "$2"
  FAILED=$((FAILED + 1))
}
skip_step() {
  printf 'SKIP %s %s\n' "$1" "$2"
  SKIPPED=$((SKIPPED + 1))
}
note() { printf '     %s\n' "$*"; }

# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

export SNOWPEA_HOME="$E2E_HOME"
export SNOWPEA_PROVIDER="fake:$FAKE_SCRIPT"
export UV_TOOL_DIR="$TOOL_ROOT/tools"
export UV_TOOL_BIN_DIR="$TOOL_ROOT/bin"
export SNOWPEA_BIN_DIR="$TOOL_ROOT/bin"
# Keep the installer out of the caller's shell rc: this run is throwaway.
export SNOWPEA_SKIP_PATH=1
export PATH="$TOOL_ROOT/bin:$PATH"
unset SNOWPEA_TUI_ENTRY 2>/dev/null || true

SNOWPEA="$TOOL_ROOT/bin/snowpea"

cleanup() {
  if [ -x "$SNOWPEA" ]; then
    "$SNOWPEA" daemon stop >/dev/null 2>&1 || true
  fi
  if [ "$KEEP" -eq 0 ]; then
    rm -rf "$FIXTURE_REPO" "$E2E_HOME" "$TOOL_ROOT"
  else
    note "kept $FIXTURE_REPO, $E2E_HOME and $TOOL_ROOT"
  fi
}
trap cleanup EXIT INT TERM

reset_fixture_repo() {
  rm -rf "$FIXTURE_REPO"
  mkdir -p "$FIXTURE_REPO"
  git -C "$FIXTURE_REPO" init -q .
  git -C "$FIXTURE_REPO" config user.email "e2e@snowpea.invalid"
  git -C "$FIXTURE_REPO" config user.name "snowpea e2e"
  printf '# snowpea e2e fixture\n' >"$FIXTURE_REPO/README.md"
  # The /ralph script patches these two; they must be tracked and unmodified.
  printf 'a\n' >"$FIXTURE_REPO/tracked_a.txt"
  printf 'b\n' >"$FIXTURE_REPO/tracked_b.txt"
  git -C "$FIXTURE_REPO" add -A
  git -C "$FIXTURE_REPO" commit -qm "e2e fixture"
}

sn() { "$SNOWPEA" "$@" </dev/null; }

dirty_files() { git -C "$FIXTURE_REPO" status --porcelain | wc -l | tr -d ' '; }

command_registered() {
  # Is <name> in the slash-command registry?  Used to tell "the feature has not
  # landed yet" (SKIP) apart from "the feature is there and broke" (FAIL).
  sn commands list --json 2>/dev/null | grep -q "\"$1\""
}

echo "snowpea v0.1 end-to-end smoke"
echo "  repo     $REPO_ROOT"
echo "  fixture  $FIXTURE_REPO"
echo "  home     $SNOWPEA_HOME"
echo

rm -rf "$E2E_HOME" "$TOOL_ROOT"
mkdir -p "$E2E_HOME" "$TOOL_ROOT/bin"
reset_fixture_repo

# ---------------------------------------------------------------------------
# 1 — install
# ---------------------------------------------------------------------------

if [ "$FROM_CHECKOUT" -eq 1 ]; then
  note "installing from the checkout at $REPO_ROOT"
  install_log="$("$REPO_ROOT/installer/install.sh" --from-checkout 2>&1)"
  install_rc=$?
else
  note "installing from $INSTALL_URL"
  install_log="$(curl -fsSL "$INSTALL_URL" | sh 2>&1)"
  install_rc=$?
fi
version_out="$(sn --version 2>&1)"
if [ "$install_rc" -ne 0 ]; then
  fail_step 1 "the installer exited $install_rc"
  note "$(printf '%s' "$install_log" | tail -5)"
elif [ "$version_out" = "snowpea $EXPECTED_VERSION" ]; then
  pass_step 1 "installed; $version_out"
else
  fail_step 1 "snowpea --version printed '$version_out', expected 'snowpea $EXPECTED_VERSION'"
fi

if [ ! -x "$SNOWPEA" ]; then
  echo
  echo "FAIL: no snowpea executable at $SNOWPEA; the remaining steps cannot run" >&2
  echo "PASS=$PASSED FAIL=$((FAILED + 14)) SKIP=$SKIPPED"
  exit 1
fi

# ---------------------------------------------------------------------------
# 2 — setup writes settings.json
# ---------------------------------------------------------------------------

if sn setup --quick --vendor deepseek --key "sk-e2e-fixture" >/dev/null 2>&1 &&
  [ -f "$E2E_HOME/settings.json" ] &&
  grep -q '"deepseek"' "$E2E_HOME/settings.json"; then
  pass_step 2 "snowpea setup --quick wrote settings.json"
else
  fail_step 2 "snowpea setup --quick did not record the vendor in $E2E_HOME/settings.json"
fi

# ---------------------------------------------------------------------------
# 3 — daemon status: running, port > 0, an exit reason
# ---------------------------------------------------------------------------

status_out="$(sn daemon status 2>&1)"
status_rc=$?
port="$(printf '%s\n' "$status_out" | awk '$1 == "port" { print $2 }')"
if [ "$status_rc" -ne 0 ]; then
  fail_step 3 "snowpea daemon status exited $status_rc"
elif [ -z "$port" ] || [ "$port" -le 0 ] 2>/dev/null; then
  fail_step 3 "daemon status reported no usable port"
elif printf '%s' "$status_out" | grep -q "will exit\|will not exit"; then
  pass_step 3 "daemon up on port $port, lifecycle reason printed"
else
  fail_step 3 "daemon status printed no keepalive reason"
fi

# ---------------------------------------------------------------------------
# 4 — headless edit in accept mode
# ---------------------------------------------------------------------------

sn -c "edit README.md: add one line" --mode accept --cwd "$FIXTURE_REPO" >/dev/null 2>&1
edit_rc=$?
changed="$(git -C "$FIXTURE_REPO" diff --name-only | wc -l | tr -d ' ')"
if [ "$edit_rc" -eq 0 ] && [ "$changed" = "1" ]; then
  pass_step 4 "one file edited in accept mode"
else
  fail_step 4 "exit $edit_rc, $changed file(s) changed (wanted exit 0 and 1 file)"
fi
git -C "$FIXTURE_REPO" checkout -q -- .

# ---------------------------------------------------------------------------
# 5 — /help lists the nine built-ins
# ---------------------------------------------------------------------------

help_out="$(sn -c "/help" --json --cwd "$FIXTURE_REPO" 2>&1)"
help_rc=$?
missing=""
for name in ralph ralplan ultrawork deepinit deep-research deep-interview plan accept auto; do
  printf '%s' "$help_out" | grep -q "/$name" || missing="$missing $name"
done
if [ "$help_rc" -ne 0 ]; then
  fail_step 5 "snowpea -c /help --json exited $help_rc"
elif [ -n "$missing" ]; then
  fail_step 5 "/help is missing:$missing"
else
  pass_step 5 "/help lists all nine built-in commands"
fi

# ---------------------------------------------------------------------------
# 6 — tools list shows the media tools with a state
# ---------------------------------------------------------------------------

tools_out="$(sn tools list --json 2>&1)"
tools_rc=$?
if [ "$tools_rc" -ne 0 ]; then
  fail_step 6 "snowpea tools list --json exited $tools_rc"
elif printf '%s' "$tools_out" | grep -q '"image_generate"' &&
  printf '%s' "$tools_out" | grep -q '"video_generate"' &&
  printf '%s' "$tools_out" | grep -q '"state"'; then
  pass_step 6 "media tools present with a state field"
else
  fail_step 6 "media tools missing from tools list --json"
fi

# ---------------------------------------------------------------------------
# 7 — /ralph drives the repo to a reviewed finish
# ---------------------------------------------------------------------------

if ! command_registered ralph; then
  skip_step 7 "/ralph is not registered yet (pending US-019)"
else
  sn -c "/ralph 'make tests pass'" --mode auto --cwd "$FIXTURE_REPO" >/dev/null 2>&1
  ralph_rc=$?
  ralph_changed="$(dirty_files)"
  if [ "$ralph_rc" -eq 0 ] && [ "$ralph_changed" != "0" ]; then
    pass_step 7 "/ralph finished, $ralph_changed file(s) changed"
  else
    fail_step 7 "exit $ralph_rc, $ralph_changed file(s) changed (wanted exit 0 and a non-empty diff)"
  fi
  git -C "$FIXTURE_REPO" checkout -q -- .
  git -C "$FIXTURE_REPO" clean -qfd
fi

# ---------------------------------------------------------------------------
# 8 — /team merges two tasks and cleans its worktrees up
# ---------------------------------------------------------------------------

if ! command_registered team; then
  skip_step 8 "/team is not registered yet (pending US-020)"
else
  sn -c "/workers 2 'add docstrings'" --mode auto --cwd "$FIXTURE_REPO" >/dev/null 2>&1
  team_rc=$?
  team_status="$(cd "$FIXTURE_REPO" && sn team status 2>&1)"
  merged="$(printf '%s\n' "$team_status" | grep -c "merged")"
  worktrees="$(git -C "$FIXTURE_REPO" worktree list | wc -l | tr -d ' ')"
  if [ "$team_rc" -eq 0 ] && [ "$merged" -eq 2 ] && [ "$worktrees" = "1" ]; then
    pass_step 8 "/team merged 2 tasks and left no worktree behind"
  else
    fail_step 8 "exit $team_rc, $merged merged task(s), $worktrees worktree(s) (wanted 0/2/1)"
  fi
fi

# ---------------------------------------------------------------------------
# 9 — plugin install: hook marker plus an MCP tool
# ---------------------------------------------------------------------------

marker="$E2E_HOME/fixture-hook.marker"
rm -f "$marker"
sn skill install "$SAMPLE_PLUGIN" >/dev/null 2>&1
skill_rc=$?
mcp_tool=0
sn tools list --json 2>/dev/null | grep -q 'mcp__fixture-echo__echo' && mcp_tool=1
sn -c "run ls" --mode auto --cwd "$FIXTURE_REPO" >/dev/null 2>&1
if [ "$skill_rc" -ne 0 ]; then
  fail_step 9 "snowpea skill install exited $skill_rc"
elif [ "$mcp_tool" -ne 1 ]; then
  fail_step 9 "the plugin's MCP tool (mcp__fixture-echo__echo) did not register"
elif [ -s "$marker" ]; then
  pass_step 9 "plugin installed: MCP tool registered, PreToolUse hook wrote $(cat "$marker")"
else
  fail_step 9 "the plugin's PreToolUse hook did not write $marker"
fi

# ---------------------------------------------------------------------------
# 10, 11 — messenger delivery and approval timeout (real credentials only)
# ---------------------------------------------------------------------------

if [ "${SNOWPEA_E2E_CREDENTIALED:-0}" != "1" ]; then
  skip_step 10 "messenger delivery needs SNOWPEA_E2E_CREDENTIALED=1 and a channel"
  skip_step 11 "approval timeout needs SNOWPEA_E2E_CREDENTIALED=1 and a channel"
else
  channel="${SNOWPEA_E2E_CHANNEL:-}"
  if [ -z "$channel" ]; then
    fail_step 10 "SNOWPEA_E2E_CREDENTIALED=1 but SNOWPEA_E2E_CHANNEL is unset"
    fail_step 11 "SNOWPEA_E2E_CREDENTIALED=1 but SNOWPEA_E2E_CHANNEL is unset"
  else
    if sn job schedule --in 60s --task "echo hi" --channel "$channel" >/dev/null 2>&1; then
      sleep 65
      if sn job list 2>/dev/null | grep -qi "done\|sent"; then
        pass_step 10 "the scheduled job fired and was delivered to $channel"
      else
        fail_step 10 "the job did not report delivery within 65s"
      fi
    else
      fail_step 10 "snowpea job schedule failed"
    fi
    if grep -q "denied_by_timeout" "$E2E_HOME/logs/approvals.jsonl" 2>/dev/null; then
      pass_step 11 "an approval timeout was recorded as denied_by_timeout"
    else
      fail_step 11 "no denied_by_timeout entry in logs/approvals.jsonl"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 12 — docker / ssh execution backend
# ---------------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  skip_step 12 "docker is not available on this machine"
else
  # --json, because the backend confirmation arrives as a backend.changed event.
  # That the container really is a different host is asserted by
  # tests/test_backends.py; here the point is that the switch happens at all.
  backend_out="$(sn -c "/backend docker" --json --mode auto --cwd "$FIXTURE_REPO" 2>&1)"
  backend_rc=$?
  if [ "$backend_rc" -eq 0 ] && printf '%s' "$backend_out" | grep -q '"backend": *"docker"'; then
    pass_step 12 "the session switched to the docker backend"
  else
    fail_step 12 "exit $backend_rc; /backend docker did not report a docker backend"
  fi
fi

# ---------------------------------------------------------------------------
# 13 — plan mode denies a write to CODE
#
# Plan mode may write documents (markdown, text, docs/, plan files — see
# permissions/plan_paths.py), so a .txt is allowed by design. What it must
# never do is touch source: that is what this step asserts.
# ---------------------------------------------------------------------------

rm -f "$FIXTURE_REPO/foo.py"
sn --mode plan -c "write foo.py" --cwd "$FIXTURE_REPO" >/dev/null 2>&1
plan_rc=$?
if [ "$plan_rc" -eq 4 ] && [ ! -e "$FIXTURE_REPO/foo.py" ]; then
  pass_step 13 "plan mode denied the write to source (exit 4, no file)"
else
  created="no"
  [ -e "$FIXTURE_REPO/foo.py" ] && created="yes"
  fail_step 13 "exit $plan_rc, file created: $created (wanted exit 4 and no file)"
fi

# ---------------------------------------------------------------------------
# 14 — generated protocol artifacts are current
# ---------------------------------------------------------------------------

if [ ! -f "$REPO_ROOT/scripts/gen_protocol.py" ]; then
  skip_step 14 "scripts/gen_protocol.py is not in this install"
else
  protocol_out="$(cd "$REPO_ROOT" && python3 scripts/gen_protocol.py --check 2>&1)"
  protocol_rc=$?
  if [ "$protocol_rc" -eq 0 ]; then
    pass_step 14 "gen_protocol.py --check reports no drift"
  else
    fail_step 14 "gen_protocol.py --check exited $protocol_rc"
    note "$(printf '%s' "$protocol_out" | tail -3)"
  fi
fi

# ---------------------------------------------------------------------------
# 15 — daemon stop leaves nothing behind
# ---------------------------------------------------------------------------

daemon_pid="$(python3 -c "
import json, sys
try:
    print(json.load(open('$E2E_HOME/daemon.json')).get('pid', ''))
except Exception:
    print('')
" 2>/dev/null)"
sn daemon stop >/dev/null 2>&1
stop_rc=$?
sleep 1
alive=0
if [ -n "$daemon_pid" ] && kill -0 "$daemon_pid" 2>/dev/null; then
  alive=1
fi
if [ "$stop_rc" -eq 0 ] && [ "$alive" -eq 0 ] && [ ! -f "$E2E_HOME/daemon.json" ]; then
  pass_step 15 "daemon stopped, pid gone, daemon.json cleaned up"
else
  fail_step 15 "exit $stop_rc, pid alive: $alive, daemon.json present: $([ -f "$E2E_HOME/daemon.json" ] && echo yes || echo no)"
fi

# ---------------------------------------------------------------------------

echo
echo "PASS=$PASSED FAIL=$FAILED SKIP=$SKIPPED"
[ "$FAILED" -eq 0 ] || exit 1
exit 0

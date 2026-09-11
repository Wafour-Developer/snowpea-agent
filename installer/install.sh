#!/usr/bin/env bash
# Snowpea installer for macOS and Linux (M8 contract §2, plan §3.4, AC-01).
#
#   curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
#
# What it does, in order: make sure `uv` is available, make sure Node >= 20 is
# available (the TUI bundle runs on node), install the `snowpea-agent` tool with
# `uv tool install`, put `~/.local/bin` on PATH, then print `snowpea --version`.
#
# It is idempotent: every step checks first and skips when it is already done.
#
# Flags
#   --dry-run         print the plan, change nothing, exit 0
#   --from-checkout   install the checkout this script lives in (CI and E2E)
#   --force           reinstall the tool even if `snowpea` is already there
#   --help
#
# Environment
#   SNOWPEA_INSTALL_SOURCE  what to hand `uv tool install`
#                           (default: git+https://github.com/Wafour-Developer/snowpea-agent,
#                           until the package is on PyPI)
#   SNOWPEA_WHEEL_URL       a wheel URL/path; wins over SNOWPEA_INSTALL_SOURCE
#   SNOWPEA_NODE_DIR        where a downloaded Node goes (default ~/.snowpea/node)
#   SNOWPEA_BIN_DIR         where the `snowpea` executable goes (default ~/.local/bin)
#   SNOWPEA_SKIP_NODE=1     do not install Node (it is still reported)
#   SNOWPEA_SKIP_PATH=1     do not touch any shell rc file (used by the E2E run)
#   SNOWPEA_HOME            where install.json is recorded (default ~/.snowpea)
#
# Every failure exits non-zero after printing the exact command to run by hand.

set -eu

REPO_URL="https://github.com/Wafour-Developer/snowpea-agent"
DEFAULT_SOURCE="git+${REPO_URL}"
MIN_NODE_MAJOR=20
NODE_VERSION="${SNOWPEA_NODE_VERSION:-22.11.0}"
RC_MARKER="# added by snowpea installer"

DRY_RUN=0
FROM_CHECKOUT=0
FORCE=0

BIN_DIR="${SNOWPEA_BIN_DIR:-$HOME/.local/bin}"
NODE_DIR="${SNOWPEA_NODE_DIR:-$HOME/.snowpea/node}"

# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

say() { printf 'snowpea: %s\n' "$*"; }
step() { printf '  -> %s\n' "$*"; }
plan() { printf '  [dry-run] %s\n' "$*"; }

die() {
  # die <message> <manual command to run instead>
  printf 'snowpea: error: %s\n' "$1" >&2
  if [ -n "${2:-}" ]; then
    printf 'snowpea: run this by hand, then re-run the installer:\n    %s\n' "$2" >&2
  fi
  exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'USAGE'
usage: install.sh [--dry-run] [--from-checkout] [--force]

  --dry-run         print the planned steps, change nothing, exit 0
  --from-checkout   install the checkout this script lives in (CI and E2E)
  --force           reinstall even when `snowpea` is already present

environment: SNOWPEA_INSTALL_SOURCE, SNOWPEA_WHEEL_URL, SNOWPEA_BIN_DIR,
             SNOWPEA_NODE_DIR, SNOWPEA_SKIP_NODE, SNOWPEA_SKIP_PATH
USAGE
  exit 0
}

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --from-checkout) FROM_CHECKOUT=1 ;;
    --force) FORCE=1 ;;
    -h | --help) usage ;;
    *) die "unknown option: $1" "$0 --help" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# platform
# ---------------------------------------------------------------------------

detect_platform() {
  OS="$(uname -s)"
  ARCH="$(uname -m)"
  case "$OS" in
    Darwin) PLATFORM="darwin" ;;
    Linux) PLATFORM="linux" ;;
    *) die "unsupported operating system: $OS (macOS and Linux only)" "" ;;
  esac
  case "$ARCH" in
    x86_64 | amd64) NODE_ARCH="x64" ;;
    arm64 | aarch64) NODE_ARCH="arm64" ;;
    *) die "unsupported architecture: $ARCH" "" ;;
  esac
}

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------

ensure_uv() {
  if have uv; then
    step "uv already installed ($(uv --version 2>/dev/null || echo unknown))"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    plan "install uv via https://astral.sh/uv/install.sh"
    return 0
  fi
  say "installing uv"
  if have curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh \
      || die "the uv installer failed" "curl -LsSf https://astral.sh/uv/install.sh | sh"
  elif have wget; then
    wget -qO- https://astral.sh/uv/install.sh | sh \
      || die "the uv installer failed" "wget -qO- https://astral.sh/uv/install.sh | sh"
  else
    die "neither curl nor wget is installed" "install curl, then re-run this script"
  fi
  # The uv installer drops it in one of these; pick it up for this shell.
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -x "$candidate/uv" ]; then
      PATH="$candidate:$PATH"
      export PATH
    fi
  done
  have uv || die "uv installed but is not on PATH" "export PATH=\"\$HOME/.local/bin:\$PATH\""
}

# ---------------------------------------------------------------------------
# node
# ---------------------------------------------------------------------------

node_major() {
  node --version 2>/dev/null | sed -e 's/^v//' -e 's/\..*$//'
}

node_ok() {
  have node || return 1
  major="$(node_major)"
  [ -n "$major" ] || return 1
  [ "$major" -ge "$MIN_NODE_MAJOR" ] 2>/dev/null || return 1
  return 0
}

install_node_tarball() {
  # No nvm, no fnm, no sudo: unpack the official tarball under ~/.snowpea/node
  # and symlink node/npm/npx into BIN_DIR.
  tarball="node-v${NODE_VERSION}-${PLATFORM}-${NODE_ARCH}.tar.gz"
  url="https://nodejs.org/dist/v${NODE_VERSION}/${tarball}"
  manual="mkdir -p $NODE_DIR && curl -fsSL $url | tar -xz -C $NODE_DIR --strip-components=1"
  say "installing Node ${NODE_VERSION} into ${NODE_DIR}"
  mkdir -p "$NODE_DIR" || die "could not create $NODE_DIR" "$manual"
  if have curl; then
    curl -fsSL "$url" | tar -xz -C "$NODE_DIR" --strip-components=1 || die "Node download failed" "$manual"
  elif have wget; then
    wget -qO- "$url" | tar -xz -C "$NODE_DIR" --strip-components=1 || die "Node download failed" "$manual"
  else
    die "neither curl nor wget is installed" "$manual"
  fi
  mkdir -p "$BIN_DIR"
  for exe in node npm npx; do
    if [ -x "$NODE_DIR/bin/$exe" ]; then
      ln -sf "$NODE_DIR/bin/$exe" "$BIN_DIR/$exe"
    fi
  done
  PATH="$BIN_DIR:$PATH"
  export PATH
}

ensure_node() {
  if node_ok; then
    step "node already installed ($(node --version))"
    return 0
  fi
  if [ "${SNOWPEA_SKIP_NODE:-0}" = "1" ]; then
    step "SNOWPEA_SKIP_NODE=1: skipping Node (the TUI will not start without it)"
    return 0
  fi
  if [ -x "$NODE_DIR/bin/node" ]; then
    step "using the Node in $NODE_DIR"
    PATH="$NODE_DIR/bin:$PATH"
    export PATH
    if node_ok; then
      return 0
    fi
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$PLATFORM" = "darwin" ] && have brew; then
      plan "brew install node@${MIN_NODE_MAJOR}"
    else
      plan "download Node ${NODE_VERSION} (${PLATFORM}-${NODE_ARCH}) into ${NODE_DIR}"
    fi
    return 0
  fi
  if [ "$PLATFORM" = "darwin" ] && have brew; then
    say "installing Node with Homebrew"
    brew install node || die "brew install node failed" "brew install node"
    return 0
  fi
  if [ "$PLATFORM" = "darwin" ]; then
    say "Homebrew is not installed; falling back to the official Node tarball"
  fi
  install_node_tarball
  node_ok || die "Node ${MIN_NODE_MAJOR}+ is still not on PATH" \
    "install Node ${MIN_NODE_MAJOR}+ from https://nodejs.org/en/download and re-run this script"
}

# ---------------------------------------------------------------------------
# snowpea
# ---------------------------------------------------------------------------

checkout_root() {
  # installer/install.sh -> repo root
  script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
  dirname "$script_dir"
}

install_source() {
  if [ "$FROM_CHECKOUT" -eq 1 ]; then
    printf '%s' "$(checkout_root)"
  elif [ -n "${SNOWPEA_WHEEL_URL:-}" ]; then
    printf '%s' "$SNOWPEA_WHEEL_URL"
  else
    printf '%s' "${SNOWPEA_INSTALL_SOURCE:-$DEFAULT_SOURCE}"
  fi
}

install_snowpea() {
  source_spec="$(install_source)"
  if [ "$FROM_CHECKOUT" -eq 1 ]; then
    # Editable, so the E2E run exercises the working tree (and picks the TUI up
    # from the checkout's tui/dist) without waiting on a wheel build.
    set -- tool install --force --editable "$source_spec"
  elif [ "$FORCE" -eq 1 ]; then
    set -- tool install --force "$source_spec"
  else
    set -- tool install "$source_spec"
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    plan "uv $*"
    record_install uv "$source_spec"
    return 0
  fi
  if [ "$FORCE" -eq 0 ] && [ "$FROM_CHECKOUT" -eq 0 ] && have snowpea; then
    step "snowpea already installed ($(snowpea --version 2>/dev/null || echo unknown)); \
re-run with --force to reinstall"
    record_install uv "$source_spec"
    return 0
  fi
  say "installing snowpea from ${source_spec}"
  UV_TOOL_BIN_DIR="${UV_TOOL_BIN_DIR:-$BIN_DIR}" uv "$@" \
    || die "uv tool install failed" "uv $*"
  record_install uv "$source_spec"
}

# ---------------------------------------------------------------------------
# install.json — how snowpea got here, so `snowpea update` can repeat it
# ---------------------------------------------------------------------------

record_install() {
  method="$1"
  source_spec="$2"
  home_dir="${SNOWPEA_HOME:-$HOME/.snowpea}"
  if [ "$DRY_RUN" -eq 1 ]; then
    plan "record {method: $method} in $home_dir/install.json"
    return 0
  fi
  mkdir -p "$home_dir" || return 0
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{\n  "method": "%s",\n  "source": "%s",\n  "time": "%s"\n}\n' \
    "$method" "$source_spec" "$now" >"$home_dir/install.json" 2>/dev/null || return 0
  step "recorded the install method in $home_dir/install.json"
}

# ---------------------------------------------------------------------------
# PATH
# ---------------------------------------------------------------------------

shell_rc() {
  case "${SHELL:-}" in
    */zsh) printf '%s' "$HOME/.zshrc" ;;
    */bash)
      if [ "$PLATFORM" = "darwin" ]; then printf '%s' "$HOME/.bash_profile"; else printf '%s' "$HOME/.bashrc"; fi
      ;;
    */fish) printf '%s' "$HOME/.config/fish/config.fish" ;;
    *) printf '%s' "$HOME/.profile" ;;
  esac
}

ensure_path() {
  if [ "${SNOWPEA_SKIP_PATH:-0}" = "1" ]; then
    step "SNOWPEA_SKIP_PATH=1: leaving the shell rc files alone"
    PATH="$BIN_DIR:$PATH"
    export PATH
    return 0
  fi
  rc="$(shell_rc)"
  case ":$PATH:" in
    *":$BIN_DIR:"*) on_path=1 ;;
    *) on_path=0 ;;
  esac
  if [ "$on_path" -eq 1 ] && [ -f "$rc" ] && grep -qF "$RC_MARKER" "$rc" 2>/dev/null; then
    step "$BIN_DIR is already on PATH"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    plan "append '$BIN_DIR' to PATH in $rc"
    return 0
  fi
  if [ -f "$rc" ] && grep -qF "$RC_MARKER" "$rc" 2>/dev/null; then
    step "$rc already carries the snowpea PATH line"
  else
    mkdir -p "$(dirname "$rc")"
    case "$rc" in
      *config.fish) printf '\n%s\nfish_add_path %s\n' "$RC_MARKER" "$BIN_DIR" >>"$rc" ;;
      *) printf '\n%s\nexport PATH="%s:$PATH"\n' "$RC_MARKER" "$BIN_DIR" >>"$rc" ;;
    esac
    step "added $BIN_DIR to PATH in $rc (open a new shell to pick it up)"
  fi
  PATH="$BIN_DIR:$PATH"
  export PATH
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
  detect_platform
  if [ "$DRY_RUN" -eq 1 ]; then
    say "dry run on ${PLATFORM}/${NODE_ARCH}: planned steps"
  else
    say "installing on ${PLATFORM}/${NODE_ARCH}"
  fi
  ensure_uv
  ensure_node
  install_snowpea
  ensure_path
  if [ "$DRY_RUN" -eq 1 ]; then
    plan "snowpea --version"
    say "dry run complete; nothing was changed"
    return 0
  fi
  if ! have snowpea; then
    die "snowpea is installed but not on PATH" "export PATH=\"$BIN_DIR:\$PATH\""
  fi
  printf '\n'
  snowpea --version || die "snowpea --version failed" "snowpea --version"
  cat <<EOF

Next:
  snowpea setup      configure a provider (API key or browser login)
  snowpea            start the agent
  snowpea --help     every subcommand

Docs: ${REPO_URL}
EOF
}

main

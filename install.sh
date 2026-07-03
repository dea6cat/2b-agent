#!/bin/sh
# 2B installer — https://github.com/dea6cat/2b-agent
#
#   curl -fsSL https://raw.githubusercontent.com/dea6cat/2b-agent/main/install.sh | sh
#
# This script only BOOTSTRAPS the tool: it installs uv (if needed) and the `2b`
# command, then hands off to `2b setup`, which does all the onboarding — machine
# grading, model download, self-test, and PATH — so the exact same setup runs whether
# you install this way or via `pip install 2b-agent` / `uv tool install 2b-agent`.
#
# Flags are passed straight through to `2b setup`:
#   --yes            accept defaults, no prompts
#   --clean / --no-clean       remove (or keep) other agentic tools
#   --models "a b"   pull these models, skip the menu
#   --no-models      skip local model setup
#   --no-benchmark   skip the tok/s + correctness self-test
#   --fix-path / --no-fix-path add (or don't) uv's tool dir to your PATH
set -u

REPO="git+https://github.com/dea6cat/2b-agent"

have() { command -v "$1" >/dev/null 2>&1; }
log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

# 1) uv
if ! have uv; then
  log "Installing uv…"
  if   have curl; then curl -LsSf https://astral.sh/uv/install.sh | sh
  elif have wget; then wget -qO- https://astral.sh/uv/install.sh | sh
  else echo "Need curl or wget to install uv. Install one and re-run." >&2; exit 1; fi
fi
# Remember the user's real (persistent) PATH so `2b setup`'s PATH check reflects future
# terminals, not the shims we prepend just for this run.
_2B_ORIG_PATH="$PATH"; export _2B_ORIG_PATH
# uv drops its shims here for this session even before shell PATH is updated.
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH

# 2) the 2b tool
log "Installing 2B…"
uv tool install --force "$REPO" || { echo "uv tool install failed." >&2; exit 1; }
# Make the freshly-installed `2b` resolvable for the setup call below.
PATH="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin"):$PATH"; export PATH

# 3) onboarding lives in the tool (single source of truth). Feed /dev/tty so prompts
#    work even when this script is piped from `curl | sh`; otherwise run non-interactive.
log "Running 2B setup…"
if [ -r /dev/tty ]; then
  2b setup "$@" < /dev/tty
else
  2b setup --yes "$@"
fi

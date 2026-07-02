#!/bin/sh
# 2B installer — https://github.com/dea6cat/2b-agent
#
#   curl -fsSL https://raw.githubusercontent.com/dea6cat/2b-agent/main/install.sh | sh
#
# Installs uv (if missing) and the `2b` command, then offers to set up local
# models: an optional clean-install of other agentic tools, a hardware grade to
# pick the right models for this machine, a multi-select menu, Ollama install +
# pull with progress, and a per-model self-test.
#
# Interactive when run on a terminal (prompts read from /dev/tty so the pipe
# above works). Non-interactive elsewhere; flags drive it in scripts:
#   -y, --yes           accept defaults, no prompts (pulls the default model)
#   --clean             clean-install rejected agentic tools without asking
#   --no-clean          skip the clean-install step
#   --models "a b c"    pull exactly these models (skips the menu)
#   --no-models         install only uv + 2b, no model setup
set -u

REPO="git+https://github.com/dea6cat/2b-agent"
OLLAMA_HOST="${OLLAMA_API_BASE:-http://localhost:11434}"

ASSUME_YES=0
WANT_CLEAN=""        # "", "yes", "no"
WANT_MODELS=""       # "", "no", or an explicit space list
MODELS_ARG=""
WANT_BENCH=1         # correctness self-test runs by default; --no-benchmark skips
WANT_FIXPATH=""      # "", "yes", "no" — put uv's tool dir on PATH (else just print how)
for a in "$@"; do
  case "$a" in
    -y|--yes)   ASSUME_YES=1 ;;
    --clean)    WANT_CLEAN="yes" ;;
    --no-clean) WANT_CLEAN="no" ;;
    --no-models) WANT_MODELS="no" ;;
    --no-benchmark) WANT_BENCH=0 ;;
    --fix-path) WANT_FIXPATH="yes" ;;
    --no-fix-path) WANT_FIXPATH="no" ;;
    --models)   WANT_MODELS="list"; MODELS_ARG="__next__" ;;
    *)          if [ "$MODELS_ARG" = "__next__" ]; then MODELS_ARG="$a"; fi ;;
  esac
done

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# Run a command with a hard time cap (macOS/BSD have no `timeout`).
run_with_timeout() {  # $1=seconds, rest=command
  rwt_secs="$1"; shift
  "$@" &
  rwt_pid=$!
  ( sleep "$rwt_secs"; kill -9 "$rwt_pid" 2>/dev/null ) &
  rwt_watch=$!
  wait "$rwt_pid" 2>/dev/null; rwt_status=$?
  kill "$rwt_watch" 2>/dev/null; wait "$rwt_watch" 2>/dev/null
  return "$rwt_status"
}

# Report-only correctness check: drive `2b` headlessly on a small two-change coding
# task (edit an existing method + add a new one) and verify the result. Prints a
# per-model line, records a row in GRADE_FILE (if set), and returns non-zero on failure.
correctness_test() {  # $1=model
  ct_model="$1"
  have 2b || { info "2b not on PATH — skipping correctness check for $ct_model"; return 0; }
  ct_dir=$(mktemp -d)
  cat > "$ct_dir/sample.dart" <<'DART'
/// A tiny greeter used only to check editing accuracy.
class Greeter {
  /// Returns a greeting for [name].
  String greet(String name) => 'Hello, $name!';
}
DART
  ct_task="In sample.dart, make exactly two changes to the Greeter class and nothing else: (1) change the greeting returned by greet() from 'Hello, \$name!' to 'Hi there, \$name!'; (2) add a new method to the class: String farewell(String name) => 'Bye, \$name!';"
  ct_start=$(date +%s 2>/dev/null || echo 0)
  ( cd "$ct_dir" && export OLLAMA_API_BASE="$OLLAMA_HOST" && \
    run_with_timeout 150 2b --classic --model "$ct_model" --yes "$ct_task" \
    >"$ct_dir/2b.log" 2>&1 </dev/null )
  ct_end=$(date +%s 2>/dev/null || echo 0)
  ct_wall=$((ct_end - ct_start))
  ct_content=$(cat "$ct_dir/sample.dart" 2>/dev/null)
  ct_new=0; ct_old=0; ct_fw=0; ct_bye=0
  case "$ct_content" in *'Hi there, $name!'*) ct_new=1 ;; esac
  case "$ct_content" in *'Hello, $name!'*) ct_old=1 ;; esac
  case "$ct_content" in *farewell*) ct_fw=1 ;; esac
  case "$ct_content" in *'Bye, $name!'*) ct_bye=1 ;; esac
  rm -rf "$ct_dir"
  ct_correct=no
  if [ "$ct_new" -eq 1 ] && [ "$ct_old" -eq 0 ] && [ "$ct_fw" -eq 1 ] && [ "$ct_bye" -eq 1 ]; then
    ct_correct=yes
  fi
  [ -n "${GRADE_FILE:-}" ] && printf '%s|%s|%s\n' "$ct_model" "$ct_correct" "$ct_wall" >> "$GRADE_FILE"
  if [ "$ct_correct" = yes ]; then
    printf '  %-20s ✓ correct   (%ss)\n' "$ct_model" "$ct_wall"
    return 0
  fi
  printf '  %-20s ✗ wrong     (%ss)\n' "$ct_model" "$ct_wall"
  return 1
}

# Interactive only when we can talk to the controlling terminal.
if [ -r /dev/tty ] && [ -w /dev/tty ] && [ "$ASSUME_YES" -eq 0 ]; then
  INTERACTIVE=1
else
  INTERACTIVE=0
fi

ask() {  # $1 prompt  $2 default  -> echoes the answer
  if [ "$INTERACTIVE" -ne 1 ]; then printf '%s' "$2"; return; fi
  printf '%s' "$1" > /dev/tty
  IFS= read -r _a < /dev/tty || _a=""
  [ -z "$_a" ] && _a="$2"
  printf '%s' "$_a"
}

confirm() {  # $1 prompt  $2 default(y/n)  -> 0 if yes
  if [ "$INTERACTIVE" -ne 1 ]; then [ "$2" = "y" ]; return; fi
  _hint=$([ "$2" = "y" ] && echo "Y/n" || echo "y/N")
  printf '%s [%s] ' "$1" "$_hint" > /dev/tty
  IFS= read -r _a < /dev/tty || _a=""
  [ -z "$_a" ] && _a="$2"
  case "$_a" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# Remember the PATH the user actually has, before we prepend the tool dirs for
# this run — so the closing note can tell whether `2b` will be found in *future*
# terminals, not just this one.
ORIG_PATH="$PATH"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# ---------------------------------------------------------------------------
# 1. uv + 2b
# ---------------------------------------------------------------------------
if ! have uv; then
  log "Installing uv…"
  if have curl; then curl -LsSf https://astral.sh/uv/install.sh | sh
  elif have wget; then wget -qO- https://astral.sh/uv/install.sh | sh
  else printf 'error: need curl or wget to install uv\n' >&2; exit 1
  fi
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

log "Installing 2B…"
uv tool install --force "$REPO"

# ---------------------------------------------------------------------------
# 2. Optional clean install of other agentic tools
# ---------------------------------------------------------------------------
clean_install() {
  log "Clean install: removing other agentic tools that were unreliable with local models…"
  if have opencode && have brew; then info "opencode";       brew uninstall opencode >/dev/null 2>&1 || true; fi
  rm -rf ~/.config/opencode ~/.cache/opencode ~/.local/state/opencode ~/.local/share/opencode 2>/dev/null || true
  if have npm && npm list -g --depth=0 2>/dev/null | grep -q "@continuedev/cli"; then
    info "Continue (cn)"; npm uninstall -g @continuedev/cli >/dev/null 2>&1 || true; fi
  rm -rf ~/.continue 2>/dev/null || true
  if have brew && brew list --formula 2>/dev/null | grep -qx "block-goose-cli"; then
    info "Goose"; brew uninstall block-goose-cli >/dev/null 2>&1 || true; fi
  rm -rf ~/.config/goose ~/.local/share/goose ~/.local/state/goose 2>/dev/null || true
  if have npm && npm list -g --depth=0 2>/dev/null | grep -q '^cline@'; then
    info "Cline"; npm uninstall -g cline >/dev/null 2>&1 || true; fi
  rm -rf ~/.cline 2>/dev/null || true
  if have uv && uv tool list 2>/dev/null | grep -q '^openhands '; then
    info "OpenHands"; uv tool uninstall openhands >/dev/null 2>&1 || true; fi
  rm -rf ~/.openhands 2>/dev/null || true
  info "done."
}

DO_CLEAN=0
if [ "$WANT_CLEAN" = "yes" ]; then DO_CLEAN=1
elif [ "$WANT_CLEAN" = "no" ]; then DO_CLEAN=0
elif confirm "Do a clean install? Uninstalls opencode, Continue, Goose, Cline, OpenHands and their configs." "n"; then
  DO_CLEAN=1
fi
[ "$DO_CLEAN" -eq 1 ] && clean_install

# ---------------------------------------------------------------------------
# 3. Local model setup (skip on --no-models, or if declined)
# ---------------------------------------------------------------------------
if [ "$WANT_MODELS" = "no" ]; then
  log "Skipping local model setup (--no-models)."
  SELECTED=""
elif [ "$INTERACTIVE" -ne 1 ] && [ "$WANT_MODELS" != "list" ] && [ "$ASSUME_YES" -ne 1 ]; then
  log "No terminal detected — installed uv + 2b only."
  info "For the guided model setup, run it interactively:"
  info "  sh -c \"\$(curl -fsSL https://raw.githubusercontent.com/dea6cat/2b-agent/main/install.sh)\""
  SELECTED=""
else
  # --- machine grade -------------------------------------------------------
  OS="$(uname)"; ARCH="$(uname -m)"
  if [ "$OS" = "Darwin" ]; then
    RAM_GB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
  elif [ -r /proc/meminfo ]; then
    RAM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1048576 ))
  else RAM_GB=0; fi
  APPLE_SILICON=0
  [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ] && APPLE_SILICON=1

  log "Grading this machine…"
  info "$OS $ARCH · ${RAM_GB}GB RAM$( [ "$APPLE_SILICON" -eq 1 ] && echo ' · Apple Silicon (Metal GPU)' )"
  [ "$APPLE_SILICON" -eq 0 ] && info "note: no Apple-Silicon GPU detected — local models will run much slower on CPU."

  # If Ollama is already here, bring its server up and see what's installed,
  # so we honor an existing setup instead of pulling models redundantly.
  EXISTING=""
  if have ollama; then
    if ! curl -s -o /dev/null "$OLLAMA_HOST/api/tags" 2>/dev/null; then
      (ollama serve >/tmp/ollama_serve.log 2>&1 &)
      i=0; while [ "$i" -lt 10 ]; do
        curl -s -o /dev/null "$OLLAMA_HOST/api/tags" 2>/dev/null && break
        i=$((i+1)); sleep 1
      done
    fi
    EXISTING=$(ollama list 2>/dev/null | awk 'NR>1{print $1}')
  fi

  USE_EXISTING=0
  if [ -n "$EXISTING" ] && [ "$WANT_MODELS" != "list" ]; then
    log "Ollama is already installed, with these models:"
    for m in $EXISTING; do info "$m"; done
    if ! confirm "Pull additional models? (Otherwise 2B just uses the ones you already have.)" "n"; then
      USE_EXISTING=1
      SELECTED=""
    fi
  fi

  if [ "$USE_EXISTING" -ne 1 ]; then
    # name | download size | min comfortable RAM (GB) | opt-in? | note
    CAT_FILE="$(mktemp)"
    cat > "$CAT_FILE" <<'EOF'
qwen3:4b|~2.6GB|6|0|small & fast — good for low-RAM machines
qwen3:8b|~5.2GB|10|0|solid all-rounder
qwen3.5:9b|~5.6GB|11|0|recommended — best balance in testing
gemma4:12b-mlx|~8GB|14|1|opt-in — some machines show a cold-reload slowdown
qwen2.5-coder:14b|~9GB|16|1|coder-focused — can slow on very large files
EOF
    N=$(awk 'END{print NR}' "$CAT_FILE")
    # default = the best-fitting *non-opt-in* model (the largest that fits
    # comfortably among the safe ones), else the smallest. Opt-in models
    # (gemma4, coder) are never auto-selected — they carry known caveats.
    DEFAULT_NUM=$(awk -F'|' -v ram="$RAM_GB" '
      { if ($4+0 == 0 && ram+0 >= $3+0 && $3+0 >= best+0) { best=$3; line=NR } }
      END { print (line ? line : 1) }' "$CAT_FILE")

    if [ "$WANT_MODELS" = "list" ] && [ -n "$MODELS_ARG" ] && [ "$MODELS_ARG" != "__next__" ]; then
      SELECTED="$MODELS_ARG"
    else
      log "Models — pick what fits (grade is based on your ${RAM_GB}GB of RAM):"
      n=0
      while IFS='|' read -r name size minram optin note; do
        n=$((n+1))
        if   [ "$RAM_GB" -ge "$minram" ]; then tag="✓ fits well"
        elif [ "$RAM_GB" -ge $((minram-3)) ]; then tag="~ tight"
        else tag="✗ needs ${minram}GB+"; fi
        case " $EXISTING " in *" $name "*) note="already installed" ;; esac
        star=$([ "$n" -eq "$DEFAULT_NUM" ] && echo '*' || echo ' ')
        printf '  %s%d) %-20s %-8s  %-14s %s\n' "$star" "$n" "$name" "$size" "$tag" "$note"
      done < "$CAT_FILE"
      sel=$(ask "Select by number (space-separated), 'all', or Enter for the default (#$DEFAULT_NUM): " "$DEFAULT_NUM")
      case "$sel" in all|ALL) sel=$(awk 'END{for(i=1;i<=NR;i++)printf i" "}' "$CAT_FILE") ;; esac
      sel=$(printf '%s' "$sel" | tr ',' ' ')
      SELECTED=""
      for num in $sel; do
        case "$num" in *[!0-9]*|"") continue ;; esac
        [ "$num" -ge 1 ] && [ "$num" -le "$N" ] || continue
        m=$(awk -F'|' -v k="$num" 'NR==k{print $1}' "$CAT_FILE")
        SELECTED="$SELECTED $m"
      done
      SELECTED=$(printf '%s' "$SELECTED" | sed 's/^ *//')
    fi
    rm -f "$CAT_FILE"
  fi

  if [ -z "$SELECTED" ]; then
    if [ "$USE_EXISTING" -eq 1 ]; then
      log "Using your existing models — nothing to pull."
    else
      log "No models selected — skipping Ollama."
    fi
  else
    # --- ensure Ollama + server -------------------------------------------
    if ! have ollama; then
      log "Installing Ollama…"
      if [ "$OS" = "Darwin" ] && have brew; then brew install ollama
      else curl -fsSL https://ollama.com/install.sh | sh; fi
    fi
    if ! curl -s -o /dev/null "$OLLAMA_HOST/api/tags" 2>/dev/null; then
      log "Starting the Ollama server…"
      (ollama serve >/tmp/ollama_serve.log 2>&1 &)
      i=0; while [ "$i" -lt 15 ]; do
        curl -s -o /dev/null "$OLLAMA_HOST/api/tags" 2>/dev/null && break
        i=$((i+1)); sleep 1
      done
    fi

    # --- pull with Ollama's native progress bar ---------------------------
    COUNT=$(printf '%s\n' $SELECTED | awk 'END{print NR}')
    IDX=0
    for m in $SELECTED; do
      IDX=$((IDX+1))
      log "[$IDX/$COUNT] Pulling $m …"
      ollama pull "$m" || info "warning: pull of $m failed — skipping it."
    done

    # --- self-test each pulled model --------------------------------------
    log "Self-testing (tok/s + GPU residency on a short prompt)…"
    PERF_FILE=$(mktemp)
    for m in $SELECTED; do
      ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$m" || continue
      ollama stop "$m" >/dev/null 2>&1 || true
      TOKS=$(OLLAMA_HOST="$OLLAMA_HOST" python3 - "$m" <<'PYEOF'
import json, os, sys, urllib.request
model = sys.argv[1]
host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
payload = json.dumps({"model": model, "messages":
    [{"role": "user", "content": "Write a one-sentence description of a binary search tree."}],
    "stream": False}).encode()
req = urllib.request.Request(host + "/api/chat", data=payload, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read())
    ec, ed = d.get("eval_count", 0), d.get("eval_duration", 0) / 1e9
    print(f"{ec/ed:.1f}" if ed > 0 else "0")
except Exception:
    print("0")
PYEOF
)
      PS=$(ollama ps 2>/dev/null | awk -v mm="$m" '$1==mm')
      MEM=$(printf '%s' "$PS" | awk '{print $3, $4}')
      GPU=no; printf '%s' "$PS" | grep -q "100% GPU" && GPU=yes
      CTX=$(2b --print-ctx "$m" 2>/dev/null | sed -E 's/.*: ([0-9]+) tokens.*/\1/')
      printf '  %-20s %6s tok/s   [%s]   100%%GPU=%s   ctx=%s\n' "$m" "$TOKS" "$MEM" "$GPU" "${CTX:-?}"
      printf '%s|%s|%s|%s|%s\n' "$m" "$TOKS" "$MEM" "$GPU" "${CTX:-?}" >> "$PERF_FILE"
    done
  fi

  # --- correctness self-test: does the model actually edit, run through 2B? ---
  if [ "$WANT_BENCH" -eq 1 ]; then
    BENCH_SET="$SELECTED"
    if [ -z "$BENCH_SET" ] && [ "${USE_EXISTING:-0}" -eq 1 ]; then
      BENCH_SET=$(printf '%s\n' $EXISTING | awk 'NR==1{print}')   # one representative
    fi
    if [ -n "$BENCH_SET" ]; then
      log "Correctness self-test — a real two-change coding task, run through 2B itself (up to ~2 min per model)…"
      FAILED_MODELS=""
      GRADE_FILE=$(mktemp)
      for m in $BENCH_SET; do
        correctness_test "$m" || FAILED_MODELS="$FAILED_MODELS $m"
      done
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 4. Done — how to use it
# ---------------------------------------------------------------------------
# Suggest a model that's actually present: prefer qwen3.5:9b, else the first
# one just pulled, else the first the user already had, else the default tag.
DEFAULT_MODEL=""
case " ${SELECTED:-} ${EXISTING:-} " in *" qwen3.5:9b "*) DEFAULT_MODEL="qwen3.5:9b" ;; esac
[ -z "$DEFAULT_MODEL" ] && DEFAULT_MODEL=$(printf '%s\n' ${SELECTED:-} | awk 'NR==1{print}')
[ -z "$DEFAULT_MODEL" ] && DEFAULT_MODEL=$(printf '%s\n' ${EXISTING:-} | awk 'NR==1{print}')
[ -z "$DEFAULT_MODEL" ] && DEFAULT_MODEL="qwen3.5:9b"

# Combined grade: join perf (tok/s/mem/GPU) with correctness into a KEEP/REMOVE
# table — all measured through 2B, never a generic harness that unfairly fails
# small models. Only when the benchmark actually ran and produced rows.
if [ "${WANT_BENCH:-0}" -eq 1 ] && [ -n "${GRADE_FILE:-}" ] && [ -s "$GRADE_FILE" ]; then
  log "Grade (measured through 2B — verdict per model)"
  printf '  %-20s %8s  %-12s %-8s %-7s %s\n' MODEL TOK/S MEMORY 100%GPU CODING VERDICT
  awk -F'|' -v pf="${PERF_FILE:-/dev/null}" -v def="$DEFAULT_MODEL" '
    BEGIN {
      while ((getline line < pf) > 0) {
        split(line, a, "|"); toks[a[1]]=a[2]; mem[a[1]]=a[3]; gpu[a[1]]=a[4];
      }
      best=""; bestv=-1;
    }
    {
      m=$1; ok=$2; wall=$3;
      verdict=(ok=="yes")?"KEEP":"REMOVE";
      t=(m in toks)?toks[m]:"?"; mm=(m in mem)?mem[m]:"?"; g=(m in gpu)?gpu[m]:"?";
      printf "  %-20s %8s  %-12s %-8s %-7s %s\n", m, t, mm, g, wall"s", verdict;
      if (ok=="yes") { keep[m]=1; if ((t+0)>bestv) { bestv=t+0; best=m; } }
    }
    END {
      if (best!="") {
        if (!(def in keep))
          printf "\n  note: %s was graded REMOVE — pick a KEEP model, e.g. /default %s (fastest that passed).\n", def, best;
        else
          printf "\n  suggested default: %s (fastest model that passed).\n", best;
      }
    }
  ' "$GRADE_FILE"
fi
rm -f "${PERF_FILE:-}" "${GRADE_FILE:-}"

log "2B is ready."
cat <<EOF

  Start it from any project directory:
    2b                                 # open the TUI
    2b "add a docstring to lib/main.dart"
    2b --model $DEFAULT_MODEL

  Inside 2B:
    /models          list every available model
    /model <name>    switch model mid-task (context is preserved)
    shift+tab        cycle mode: normal · accept edits · plan
    /theme, /copy, /context, /help

EOF

# Whether `2b` will resolve in a *new* terminal: the tool dir must be on the
# user's persistent PATH. We don't edit their shell profile — we hand them the
# command to do it. (uv installs tool executables here.)
BIN_DIR=$(uv tool dir --bin 2>/dev/null) || BIN_DIR=""
[ -z "$BIN_DIR" ] && BIN_DIR="$HOME/.local/bin"
case ":$ORIG_PATH:" in
  *":$BIN_DIR:"*) ;;   # already persistent — nothing to do
  *)
    # Decide whether to add it for the user: --fix-path forces it, --no-fix-path
    # opts out, otherwise ask (interactive only). We never edit a profile silently.
    DO_FIX=0
    case "$WANT_FIXPATH" in
      yes) DO_FIX=1 ;;
      no)  DO_FIX=0 ;;
      *)   [ "$INTERACTIVE" -eq 1 ] && confirm "Put 2B on your PATH now (runs 'uv tool update-shell')?" y && DO_FIX=1 ;;
    esac
    if [ "$DO_FIX" -eq 1 ] && have uv && uv tool update-shell >/dev/null 2>&1; then
      log "Added uv's tool directory to your PATH."
      info "Open a new terminal (or re-source your shell profile), then '2b' works everywhere."
    else
      log "One more step: put 2B on your PATH so '2b' works in new terminals."
      info "$BIN_DIR isn't on your PATH yet. Run this, then open a new terminal:"
      info "  uv tool update-shell"
      info "or add it to your shell profile yourself:"
      info "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc"
    fi ;;
esac

# 2B

A local-first coding agent for the terminal. It runs your own models over Ollama's
native protocol, keeps them focused instead of hallucinating, and gives you a full-screen
TUI — streaming replies, a live plan checklist, narrated tool actions — without ever routing
your local model through a translation layer that would confuse it.

I built **2B** to keep working when the power and the internet don't — I live somewhere the grid
isn't a guarantee, and I wanted a coding agent that doesn't fall apart the moment I'm offline.

> **macOS only.** 2B is built and tested for macOS — the installer is a shell script that leans on
> Homebrew, and the clipboard integration uses `pbcopy`. It hasn't been tested elsewhere.

---

## Why I built it

I kept being told that small models — Nemotron 3 Nano 4B, `gpt-oss:20b`, the Qwen family — were
"good enough" for agentic coding. On paper they were. In practice, every off-the-shelf harness I
tried broke them:

- **opencode** made `gpt-oss` invent tool names that didn't exist and emit fake `<command>` tags as
  plain text.
- **Cline**, **Goose**, **Continue**, **OpenHands** each failed in their own way — malformed tool
  schemas, reasoning collapse, the model narrating tool calls instead of making them.

The models weren't the problem. The harnesses were. Nearly all of them talk to a local model
through a generic **OpenAI-compatible `/v1` shim** and pile abstraction on top of it. That shim
measurably degrades a small model's tool selection, and the extra complexity buries whatever
capability the model actually has.

The one thing that worked cleanly was a ~350-line script I wrote that talked to Ollama's **native
`/api/chat`** endpoint with a tiny, fixed set of five tools. So I grew that prototype into a real,
shareable tool. That's 2B.

**The core rule, and the whole point:** all complexity lives on the *host* side. The model's world
never changes — the same five tools, the same native wire format for whatever provider is active, no
generic shim. Everything you see below — the TUI, the plan checklist, task management, model
switching, auto-compaction — is something 2B renders *around* the tool loop, never a new thing the
model has to understand.

---

## What it does

**The one idea that makes it work:** the model only ever sees **five tools** — `list_files`,
`read_file`, `search_files`, `edit_file`, `write_file` — over its provider's **native** wire
format, never a generic `/v1` shim. Every feature below is something 2B renders *around* that
tiny loop, so a small model's world stays dead simple while you get a full-screen coding agent.

**Reliable with small models — the host does the hard part:**

- **Edits that survive drift.** `edit_file` matches in tiers (exact → whitespace-tolerant →
  indentation-agnostic), so a near-miss still lands — never on an ambiguous match. The tool the
  model sees is unchanged; the tolerance is all host-side.
- **Catches its own mistakes.** After an edit, 2B runs the file's checker (`dart analyze`, `ruff`, …)
  and folds new errors into the *same* tool result — the model sees the break it just caused, no new
  tool to learn.
- **Finds definitions, not just matches.** `search_files` floats a symbol's *definition* to the top
  and `read_file` appends a symbol outline — resolved semantically over a language server (LSP) when
  one's installed, with a dependency-free regex fallback otherwise.
- **Rescues weak tool-calls.** Recovers calls a small model emits as plain text, and nudges one that
  narrates a plan but forgets to act — so weak models actually finish the job.

**A full-screen experience, not a log dump:**

- **Streaming TUI** with a live **plan checklist** (`□ ■ ✓`) and a status line showing a context
  meter (`ctx N%`) plus, for local models, a live RAM/GPU readout.
- **Narrated actions** — plain-language steps with a ✓/✗ tree, not raw `read_file {...}`:
  ```
  ├ ✓ Searching for "MemoryScopeLevel" in lib
  └ ✓ Editing lib/memory/memory_store.dart
  ```
- **Themes** (`/theme`), **copy that works** (Ctrl+Y, or drag + Ctrl+C via `pbcopy`), **keyboard
  scroll** (Shift+↑/↓), and **queued/background tasks** (Ctrl+B, `/fg`).

**Any model, one conversation.** Ollama (local + cloud), OpenAI, OpenRouter, Mistral, NVIDIA,
DeepSeek, Cerebras, Anthropic, and Google Gemini — each over its own native format, all streaming.
History is provider-agnostic, so you **switch models mid-task** with `/model` and keep every bit of
context: start on a local Qwen, hand it to Claude when it gets hard.

**Keeps the thread — or doesn't, your call.** Cloud sessions carry context across messages by
default; local models stay lean until you opt in with `/continuity`. `/new` starts a fresh thread,
and `/export` dumps the whole session — **tool calls included** — to a Markdown file.

**Knows your project.** `/init` writes a compact `2B.md` (stack, layout, ranked symbol map) that's
auto-loaded into context; `/map` shows an outline on demand — all bounded so it never floods a small
window.

**Runs things, safely.** Local models get `run_git` (git only — no raw shell); cloud models get a
full `run_command` whose **writes are sandbox-confined to the workspace** (macOS `sandbox-exec` /
Linux `bubblewrap`). Network and secret-path commands re-prompt even under "allow-all," your ambient
secrets (`*_API_KEY`, `AWS_*`) are scrubbed from the child env, and tool output is fenced as
*untrusted* so a poisoned file can't hijack the model.

**Scales without bloating the model.** Cloud models can `delegate` read-only investigations (and
isolated edit sub-tasks) in parallel so a big search never bloats the main thread; **MCP tools** are
opt-in *per tool*; **auto-compaction** folds old turns into a summary near the window limit; and
**sessions persist** to SQLite (`2b --continue` / `--resume`, `/sessions`). `/undo` reverts the last
write or edit.

**Three modes** (Shift+Tab): **normal** (confirm writes) · **accept edits** (auto-apply) · **plan**
(read-only — investigate and return a plan, touching nothing).

---

## Install

One line — paste it in your terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/dea6cat/2b-agent/main/install.sh | sh
```

It installs [`uv`](https://docs.astral.sh/uv/) if you don't have it, installs the `2b` command,
then — on an interactive terminal — walks you through local model setup:

1. **Optional clean install** — offers to remove other agentic tools that proved unreliable with
   local models (opencode, Continue, Goose, Cline, OpenHands) and their configs. Off by default;
   it asks first.
2. **Grades your machine** — reads your RAM and chip and rates each candidate model
   (`✓ fits well` / `~ tight` / `✗ needs NGB+`), defaulting to the best one your hardware can run.
3. **You pick** one or several from the menu.
4. **Installs Ollama and pulls** what you chose, with a live progress bar.
5. **Self-tests** each model — tok/s + GPU residency, then a **correctness check that runs a real
   one-line edit through 2B itself** and verifies the result (`✓ correct` / `✗ wrong`, ~20–90s per
   model). It only reports — it never removes a model — and `--no-benchmark` skips it. Then it prints
   how to launch 2B.

Already have Ollama and some models? It skips what you already have — it lists your installed
models, offers to just use them (pulling nothing), and marks anything in the menu you've already
got. Your existing setup is left untouched.

Prefer to do it by hand? Install the published package from
[PyPI](https://pypi.org/project/2b-agent/), then run the same onboarding the installer uses:

```bash
pip install 2b-agent      # or: uv tool install 2b-agent
2b setup                  # grades your machine, installs Ollama, pulls a model, self-tests, fixes PATH
```

On macOS/Linux you can also use **Homebrew** (it puts `2b` on your PATH automatically):

```bash
brew install dea6cat/2b/twob-agent   # the formula is twob-agent; it installs the `2b` command
2b setup
```

`2b setup` is the single source of truth for onboarding — the `curl … | sh` installer just installs
uv + the `2b` command and then runs it, so you get the exact same setup either way. (On first launch
with no model configured, `2b` offers to run it for you.)

The installer — and `2b setup` — are scriptable: `--yes` (accept defaults, no prompts), `--clean` / `--no-clean`,
`--models "qwen3.5:9b qwen3:8b"`, `--no-models`, `--no-benchmark` (skip the correctness check),
`--fix-path` / `--no-fix-path` (add uv's tool dir to your PATH for you via `uv tool update-shell`,
or leave it — otherwise it asks, and never edits a profile without consent). Pass them through the
pipe with `... | sh -s -- --yes --models "qwen3.5:9b"`.

---

## Use it

```bash
2b                       # start in the current directory, autodetects a local model
2b "add a docstring to lib/main.dart"   # run one task, then drop into the session
2b --model qwen3.5:9b    # pin a model
2b --continue            # resume your most recent session (--resume <id> for a specific one)
2b --list-sessions       # list saved sessions
2b --list-models         # what's available across configured providers
2b --doctor              # diagnose PATH, Ollama, and the default model, then exit
2b --test                # grade installed models + compare them to the latest coding models
2b --test auto           # auto-clean: remove failures, then pull/coding-test the best new one
2b --update              # upgrade to the latest release (uv tool upgrade)
2b --rm                  # uninstall 2B and delete its config (asks first); --rm --yes to skip
```

Then just type what you want done. Type `/` to see the commands.

### Testing your models

`2b --test` grades each installed model — tok/s, GPU residency, and a **real two-change edit run
through 2B** (`✓`/`✗`, up to ~2 min each) — then prints a KEEP/REMOVE table with a suggested
default. It also **compares your models to the latest tool-capable coding models on ollama.com**
that fit your RAM, surfacing families you don't have and larger variants worth upgrading to. Plain
`--test` only reports — it changes nothing (`2b --test <model>` grades just one).

`2b --test auto` is the hands-off cleanup:

- **Removes the failing models automatically** — no prompt (that's the point of `auto`); your
  current default is never removed.
- Then **offers to pull + coding-test the best new candidate**. It asks once before the multi-GB
  **download** (skip that with `--yes`), keeps the model only if it **passes** the coding test, and
  removes it if it fails — remembering a failed one so it isn't re-downloaded on the next run.

### Updating

One command, whatever you installed with — it detects the method and runs the right upgrade:

```bash
2b --update
```

That resolves to `uv tool upgrade 2b-agent` (the `curl … | sh` installer / `uv`),
`pipx upgrade 2b-agent` (pipx), or `pip install -U 2b-agent` (pip). You can of course
run the matching command yourself — e.g. **if you installed with pip**:

```bash
pip install -U 2b-agent
```

2B also checks for a newer release in the background (at most once a day, never blocking
startup) and prints a one-line notice on the next launch when one is available — set
`TWOB_NO_UPDATE_CHECK=1` to turn that off. Releases are tagged `vMAJOR.MINOR.PATCH`.

### Providers

Local Ollama needs nothing. For anything else, set the matching environment variable and it shows
up automatically in `/models`:

| Provider   | Environment variable                        |
| ---------- | ------------------------------------------- |
| Ollama     | `OLLAMA_API_BASE` (or `OLLAMA_HOST`)        |
| Ollama Cloud | `OLLAMA_API_KEY`                          |
| OpenAI     | `OPENAI_API_KEY`                            |
| OpenRouter | `OPENROUTER_API_KEY`                        |
| Mistral    | `MISTRAL_API_KEY`                           |
| NVIDIA     | `NVIDIA_API_KEY`                            |
| DeepSeek   | `DEEPSEEK_API_KEY`                          |
| Cerebras   | `CEREBRAS_API_KEY`                          |
| Anthropic  | `ANTHROPIC_API_KEY`                         |
| Google     | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)      |

Or connect one from **inside** 2B — `/connect <provider>` prompts for the key with a hidden field
and saves it to `~/.config/2b/keys.json` (`chmod 600`) so it's there next time; `/connect` on its own
shows what's connected, and `/disconnect <provider>` removes a saved key. A key exported in your
shell always takes precedence over a saved one.

Switch models anytime with `/model <name>`. A bare name works when it's unambiguous; otherwise use
`provider:model` (e.g. `/model anthropic:claude-sonnet-5`).

### Commands

| Command | What it does |
| --- | --- |
| `/help` | List commands |
| `/model [name]` | Show or switch model — context is preserved |
| `/models [filter]` | List available models, grouped by provider |
| `/default [name]` | Show or set the persisted default model (used when no `--model` is given) |
| `/connect [provider] [key]` | Connect a provider (hidden key prompt); bare shows status |
| `/disconnect <provider>` | Remove a saved provider key |
| `/init` | Scan the project → write `2B.md` (a compact map auto-loaded into context on new tasks) |
| `/map [subdir]` | Show a budget-bounded symbol outline of the project |
| `/mcp` | MCP servers/tools: status, `tools <server>`, `enable`/`disable <server> <tool…\|all>` |
| `/mode [normal\|accept\|plan]` | Set operating mode (or **Shift+Tab** to cycle) |
| `/theme [system\|light\|dark]` | Switch color theme |
| `/context` | Show estimated context usage (auto-compacts near the limit) |
| `/continuity [on\|off]` | Carry conversation context across messages — on by default for cloud, opt-in for local |
| `/new` | Start a fresh conversation thread (keeps the scrollback on screen) |
| `/export [path]` | Export the whole session — tool calls and errors included — to a Markdown file |
| `/copy` | Copy the last reply to the clipboard (**Ctrl+Y**) |
| `/task <desc>` | Queue a task |
| `/tasks` | List tasks and their status |
| `/fg <id>` | Foreground a backgrounded task |
| `/sessions` | List saved sessions (resume with `2b --continue` / `--resume <id>`) |
| `/tool <name> <args>` | Run a frozen tool directly, bypassing the model (e.g. `/tool read_file path=a.dart`) |
| `/history search <q>` | Search the scrollback; then `n` / `N` jump between matches |
| `/yes` | Toggle accept-edits mode |
| `/undo` | Revert the last write/edit |
| `/diff` | Re-show the last diff |
| `/add <file>` | Pre-load a file into the current task's context |
| `/fetch <url>` | Fetch a web page and pre-load its readable content into the current task's context |
| `/clear` | Reset the current task's history |
| `/quit` | Exit |

### Keyboard

| Key | Action |
| --- | --- |
| **Shift+Tab** | Cycle operating mode |
| **Shift+↑ / ↓** | Scroll the conversation log (a line); **PageUp / PageDown** by a page |
| **Ctrl+B** | Background the running task |
| **Ctrl+Y** | Copy the last reply |
| **Ctrl+C** | Copy the current mouse selection |
| **Esc** | Stop the current stream/task immediately — back to idle |
| **Ctrl+D** | Quit |
| **Tab** | Accept the top `/`-command suggestion |

### MCP servers (extra tools)

2B can pull in tools from [MCP](https://modelcontextprotocol.io) servers (stdio) like `dart` or
`mempalace`. But its whole reason for existing is that **small local models break when you flood them
with tools** — so MCP tools are opt-in and **curated per tool**: you enable a server and pick exactly
which of its tools reach the model. Nothing is exposed until you say so.

Declare servers the usual way — a Claude-Code-style `mcpServers` block in `~/.config/2b/mcp.json` (or
`./.mcp.json` in a project, which wins):

```json
{
  "mcpServers": {
    "dart": { "command": "dart", "args": ["mcp-server"] }
  }
}
```

Then curate from inside 2B:

```
/mcp                          # servers and how many tools each has enabled
/mcp tools dart               # list a server's tools ([x] = enabled)
/mcp enable dart hot_reload analyze_files
/mcp enable dart all          # expose everything (careful on small models)
/mcp disable dart hot_reload  # or /mcp disable dart  to turn the whole server off
```

Enabled tools appear to the model as `server__tool` (e.g. `dart__hot_reload`) and their results come
straight back into the loop. Only the tools you enable are ever sent — the model's tool list stays as
small as you keep it.

### Configuration

- **Context window (local) — sized to your machine.** For a local model 2B works out the largest
  window your box can run *comfortably* and pins `num_ctx` to it on every request (Ollama otherwise
  defaults to ~4k regardless of the model). It reads the model's architecture and trained max from
  `/api/show`, computes the KV-cache cost per token, and fits it into the RAM left after the model
  weights plus a headroom reserve — so a 16 GB laptop, an 18 GB one, and a 64 GB workstation each get
  a different, appropriate window (e.g. qwen3.5:9b ≈ 13k on 18 GB), never more than the model was
  trained for. That number drives auto-compaction (~75%) and the read-a-section threshold. Set
  `TWOB_CONTEXT_TOKENS` to override (higher if you want to spend more RAM, lower to save it).

- **Environment toggles.** Everything on-by-default can be turned off:
  `TWOB_CONTEXT_TOKENS` (override the local window) · `TWOB_NO_DIAGNOSTICS` (skip post-edit checks) ·
  `TWOB_NO_LSP` (regex symbol map instead of a language server) · `TWOB_NO_SEATBELT` /
  `TWOB_SEATBELT=strict` (relax / harden the `run_command` sandbox) · `TWOB_NO_TRIM` (keep bulky tool
  output in each request) · `TWOB_NO_HISTORY` (don't persist sessions) · `TWOB_SUBAGENT_MODEL` (run
  delegated sub-agents on a cheaper model) · `TWOB_NO_UPDATE_CHECK` (no background update check).

---

## Honest caveats

- **It reads and writes wherever you point it — with guardrails.** 2B resolves absolute paths and
  paths outside the working directory on purpose; it's a personal tool for your machine. Writes are
  still confirmed in normal mode, plan mode refuses them entirely, reads of known-secret paths
  (`~/.ssh`, `~/.aws`, …) prompt first, and on the cloud path `run_command`'s writes are
  sandbox-confined to the workspace by default (see the sandbox bullet above). The command classifier
  is defense-in-depth, not an unbypassable boundary — the sandbox is the boundary.
- **Switching to a stronger model mid-task hands it a tool-call history it didn't make.** For these
  five simple tools that's low-risk (the wire format is unambiguously the new provider's own; only
  the *choices* inside came from a weaker model), but you may see mild "why did I read that file"
  moments. It is *not* the shim-degradation failure that sank the other harnesses.
- **It's a full-screen TUI.** That means it lives in the terminal's alternate screen, so a single
  mouse drag selects what's on screen, not scrolled-off history. For a classic inline REPL, run
  `2b --classic`.

---

Built for local models, kept on task.

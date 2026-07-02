# 2B

A local-first coding agent for the terminal. It runs your own models over Ollama's
native protocol, keeps them focused instead of hallucinating, and gives you a full-screen
TUI — streaming replies, a live plan checklist, narrated tool actions — without ever routing
your local model through a translation layer that would confuse it.

I named it **2B**, after NieR: Automata. It's built to keep working when the power and the
internet don't — I live somewhere the grid isn't a guarantee, and I wanted a coding agent that
doesn't fall apart the moment I'm offline.

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

- **Five tools, and only five.** `list_files`, `read_file`, `search_files`, `edit_file`,
  `write_file`. That small, concrete surface is exactly what keeps a small model reliable. It
  explores before it edits — searching for where something lives instead of guessing paths — and
  prefers exact-snippet edits over rewriting whole files.
- **Edits that survive small-model drift.** `edit_file` resolves the target host-side in tiers —
  exact, then whitespace-tolerant, then indentation-agnostic (re-indenting your snippet to the
  file) — so a model that gets the whitespace slightly wrong still lands the edit instead of
  bouncing off an exact-match error. It never applies on an ambiguous match, and the tool the model
  sees is unchanged — all the tolerance lives on the host.
- **Catches its own mistakes.** After a successful edit, 2B runs the file's checker host-side
  (`dart analyze`, `ruff` or `py_compile`, …) and folds any new errors straight into the tool
  result — so a model that just broke the build sees it on the same turn, with no new tool to learn.
  Bounded so it can't flood a small window, skipped silently when there's no checker, and off with
  `TWOB_NO_DIAGNOSTICS`.
- **Finds definitions, not just matches.** `search_files` marks which hit is the *definition* of a
  symbol and floats it to the top, and `read_file` appends a compact symbol outline with line
  anchors — so "where is X defined?" is answered by the tools the model already calls, with no
  navigation tool to learn. When a language server is installed (`dart language-server`, `pyright`,
  `gopls`, …) it resolves symbols semantically over LSP, spoken as raw stdlib JSON-RPC; a curated MCP
  resolver is used if one's enabled; with neither, it falls back to a dependency-free regex map.
  Host-side, schema unchanged; off with `TWOB_NO_LSP`.
- **Native protocols, never a shim.** Local Ollama models get Ollama's own `/api/chat` with NDJSON
  streaming. Each cloud provider gets its own native format. Nothing is translated through a
  lowest-common-denominator layer.
- **Streaming, full-screen TUI.** A scrolling conversation, a framed input box, a live status line
  with a spinner, elapsed time, and — for local models — a RAM/GPU readout pulled from Ollama.
- **Narrated tool actions.** Instead of a wall of raw `read_file {...}`, you see what it's doing in
  plain language, tied together with a tree gutter and a ✓/✗ per step:
  ```
  ├ ✓ Searching for "MemoryScopeLevel" in lib
  ├ ✓ Reading lib/memory/memory_store.dart
  └ ✓ Editing lib/memory/memory_store.dart
  ```
- **A live plan checklist.** The model writes a short numbered plan before its first tool call; 2B
  parses it and renders it as a checklist that advances (`□` pending, `■` active, `✓` done) as the
  work progresses. Purely cosmetic — a wrong guess never breaks anything.
- **Many providers, one conversation.** Ollama (local and cloud), OpenAI, OpenRouter, Mistral,
  NVIDIA, Anthropic, and Google Gemini. 2B keeps history in a provider-agnostic form and
  re-serializes it fresh for whoever's active — so you can switch models *mid-task* with `/model`
  and keep every bit of context. Start a task on a local Qwen, hand it to Claude when it gets hard,
  keep going.
- **Knows the project.** `/init` scans the repo and writes a compact `2B.md` — stack, layout, and a
  ranked symbol map — that's auto-loaded into context, so the model starts knowing where things are
  instead of hunting for files. `/map` shows a budget-bounded outline on demand. All bounded, so it
  never floods a small local window.
- **Runs things — split by model.** Local models get `run_git` (git only, never a raw shell — no
  chaining/injection); cloud models get a full `run_command` shell (tests, build, git). Read-only git
  runs freely; anything that mutates is confirmation-gated and refused in plan mode.
- **Delegates read-only exploration (cloud).** On the cloud path the model can `delegate` one or
  more investigations to run in parallel, each in its own isolated context, and get back short
  findings — so a big search-and-read never bloats the main conversation. Each sub-agent can only
  `list_files`, `read_file`, and `search_files`; local models keep their frozen five tools
  untouched, and delegation is cloud-only for now.
- **Cheaper multi-turn cloud sessions.** Anthropic requests mark the system prompt and tool
  definitions as cacheable, so a long conversation pays full price for that stable prefix once
  instead of on every turn.
- **MCP tools, curated.** Pull in tools from MCP servers (dart, mempalace, …) — but **per tool**, not
  wholesale, because flooding a small model with tools is exactly what breaks it. You enable a server
  and pick which of its tools the model sees (`/mcp`); local models are capped to a few so their
  context stays lean. See [MCP servers](#mcp-servers-extra-tools).
- **Operating modes**, cycled with **Shift+Tab** or set with `/mode`:
  - **normal** — every write/edit asks first.
  - **accept edits** — writes apply automatically.
  - **plan mode** — read-only; `edit_file`/`write_file` *and* MCP tools are refused (they may change
    state), so the model investigates and returns a plan instead of touching anything.
- **Auto-compaction.** When a conversation nears the model's context window — which happens fast on
  small local windows — 2B folds the older turns into a summary and keeps going uninterrupted,
  instead of hitting the wall. It cuts on a safe boundary so nothing breaks, and shows you
  "Compacting conversation…" while it does it.
- **Themes.** `/theme system` (default — transparent, uses your terminal's own background),
  `/theme light` (a warm parchment palette), `/theme dark` (a dimmed version). Switches live.
- **Copy that actually works.** Drag to select any text and press **Ctrl+C**, or **Ctrl+Y** / `/copy`
  to grab the whole last reply. On macOS this goes through `pbcopy`, so it lands on your clipboard
  even in Terminal.app (which ignores the escape sequence most TUIs rely on).
- **Multiple tasks.** Queue tasks, background the running one with **Ctrl+B**, foreground it later
  with `/fg`. A backgrounded task pauses when it needs to write and waits for you.
- **Undo.** `/undo` reverts the last write or edit — one level, but it's there.

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
[PyPI](https://pypi.org/project/2b-agent/):

```bash
pip install 2b-agent                              # latest release from PyPI
ollama pull qwen3.5:9b        # my default — a good balance on an 18 GB machine
```

The installer is scriptable too: `--yes` (accept defaults, no prompts), `--clean` / `--no-clean`,
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
2b --list-models         # what's available across configured providers
2b --doctor              # diagnose PATH, Ollama, and the default model, then exit
2b --update              # upgrade to the latest release (uv tool upgrade)
2b --rm                  # uninstall 2B and delete its config (asks first); --rm --yes to skip
```

Then just type what you want done. Type `/` to see the commands.

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
| `/connect [provider] [key]` | Connect a provider (hidden key prompt); bare shows status |
| `/disconnect <provider>` | Remove a saved provider key |
| `/init` | Scan the project → write `2B.md` (a compact map auto-loaded into context on new tasks) |
| `/map [subdir]` | Show a budget-bounded symbol outline of the project |
| `/mcp` | MCP servers/tools: status, `tools <server>`, `enable`/`disable <server> <tool…\|all>` |
| `/mode [normal\|accept\|plan]` | Set operating mode (or **Shift+Tab** to cycle) |
| `/theme [system\|light\|dark]` | Switch color theme |
| `/context` | Show estimated context usage (auto-compacts near the limit) |
| `/copy` | Copy the last reply to the clipboard (**Ctrl+Y**) |
| `/task <desc>` | Queue a task |
| `/tasks` | List tasks and their status |
| `/fg <id>` | Foreground a backgrounded task |
| `/yes` | Toggle accept-edits mode |
| `/undo` | Revert the last write/edit |
| `/diff` | Re-show the last diff |
| `/add <file>` | Pre-load a file into the current task's context |
| `/clear` | Reset the current task's history |
| `/quit` | Exit |

### Keyboard

| Key | Action |
| --- | --- |
| **Shift+Tab** | Cycle operating mode |
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

---

## Honest caveats

- **It reads and writes wherever you point it.** 2B resolves absolute paths and paths outside the
  working directory on purpose — it's a personal tool for your machine. Writes are still confirmed
  in normal mode, and plan mode refuses them entirely.
- **Switching to a stronger model mid-task hands it a tool-call history it didn't make.** For these
  five simple tools that's low-risk (the wire format is unambiguously the new provider's own; only
  the *choices* inside came from a weaker model), but you may see mild "why did I read that file"
  moments. It is *not* the shim-degradation failure that sank the other harnesses.
- **It's a full-screen TUI.** That means it lives in the terminal's alternate screen, so a single
  mouse drag selects what's on screen, not scrolled-off history. For a classic inline REPL, run
  `2b --classic`.

---

## How it's built

- **Python, standard library first.** The only real dependencies are `rich`, `prompt_toolkit`, and
  `textual`. Every provider talks over `urllib` — no per-provider SDKs.
- **A canonical conversation model** that each provider serializes fresh on every request. That
  re-derivation is exactly what makes switching models mid-task safe.
- **One worker thread per task**, emitting events into a queue the UI thread drains and renders — so
  the tool code stays untouched off the main thread and one thread owns the terminal.
- The five-tool schema is **frozen**. It's what makes small models reliable, and it isn't up for
  redesign.

Built for local models, kept on task.

# Privacy

**Effective date: 2026-07-09**

2B is a local-first command-line coding agent that runs on your own machine. This document
describes, factually, what data 2B handles, what stays local, what leaves your machine, and the
controls you have. It reflects 2B's behavior as of the date above (see **Changes**).

**Plain-language summary.** 2B has **no telemetry**. The author operates **no servers** that
receive your data. Almost everything stays on your computer. The main way data leaves your
machine is when *you* choose a **cloud** model provider — then your prompts and the code/files
2B reads are sent to that provider under **their** terms. If you use only a **local** model
(Ollama on `localhost`), your code never leaves your machine.

## 1. No telemetry or analytics

2B contains no analytics, usage tracking, telemetry, or crash reporting. It generates and
transmits no install ID, device identifier, or usage statistics. There is no backend operated by
the author that collects data about you or your use of 2B.

## 2. What stays on your machine

2B stores everything under `~/.config/2b/` (unless you redirect it):

- **API keys** — `keys.json`, saved when you connect a provider, stored in plaintext with
  owner-only (`0600`) permissions. A key you export in your shell environment is used without
  being written to disk.
- **Preferences** — `prefs.json` (e.g. your default model and your license acknowledgment).
- **Session history** — `history.db` (SQLite): the full transcript of your sessions — your
  prompts, the model's replies, and the contents of files and command output that tools read
  during the session — so you can resume/continue past work. Disable with `TWOB_NO_HISTORY=1`;
  relocate with `TWOB_HISTORY_DB`.
- **Compaction archive** — older turns folded away to save context, kept (length-capped) inside
  `history.db` so they can be recalled. Same `TWOB_NO_HISTORY` control.
- **Undo snapshots** — `~/.config/2b/undo/`: pre-edit copies of files 2B changed, so edits can
  be undone. Auto-pruned after 30 days. Relocate with `TWOB_UNDO_DIR`; disable with
  `TWOB_NO_HISTORY=1`.
- **MCP config** — `mcp.json` / `mcp_enabled.json`: your MCP server definitions and per-tool
  opt-in state.
- **Update-check cache** — `update_check.json`: only a last-check timestamp and the latest known
  version string.

2B does not transmit any of this anywhere. **Note:** because session history and undo snapshots
store file and tool content verbatim, if you have 2B read a file containing secrets, that content
is written to your local history/undo store (on your machine, owner-readable). Remove it by
deleting `~/.config/2b/` or running `2b --rm`.

## 3. What leaves your machine

### 3a. Your model provider (the main one)

2B sends requests to whichever model provider you configure:

- **Local model (Ollama at `localhost`)** — requests stay on your machine; nothing goes to any
  third party.
- **Cloud provider** (Anthropic, OpenAI, Google Gemini, OpenRouter, Mistral, NVIDIA, DeepSeek,
  Cerebras, or Ollama Cloud) — 2B sends that provider the system prompt, the full conversation,
  and the outputs of the tools it runs, which include **the contents of files it reads and the
  output of commands it runs**. To do its job, a cloud model necessarily receives the code and
  context it is working on. 2B applies no redaction step — what the agent reads can be sent to the
  provider you chose. That data is then processed under **that provider's own privacy policy and
  terms**; review theirs, and use a local model if you do not want code leaving your machine. Your
  API key is sent to the provider only as the authentication header for these requests.

### 3b. 2B's own network calls (no personal data)

Independently of your model provider, 2B makes a few requests that carry no personal data:

- **Update check** — at most once per day, a background request to the GitHub API for this
  project's release tags, to tell you when a newer version exists. No data about you is sent.
  Disable with `TWOB_NO_UPDATE_CHECK=1`.
- **Model discovery** — during `2b setup` (and `2b --test`), a request to `ollama.com`'s public
  model search to list installable models. Sends only the search query. Disable with
  `TWOB_NO_MODEL_FETCH=1` or `--no-discover`.
- **Web fetch** — the `/web` command fetches a URL you explicitly provide, over HTTPS, and loads
  the page into the model's context. It sends only that URL (and is guarded against fetching
  internal/loopback addresses). Nothing is fetched unless you ask.
- **Setup / update** — `2b setup` may install `uv`/Ollama and pull model weights, and
  `2b --update` upgrades 2B; these delegate to those tools (Homebrew, `uv`, `pip`, the Ollama
  installer, and ollama.com's model registry), which make their own network requests per their own
  behavior.

## 4. Third-party integrations you add (MCP)

If you configure MCP servers, 2B launches them as local subprocesses and passes your tool calls to
them. Those servers are third-party software **you** chose; they may send data to external
services, and **2B has no visibility into or control over what they transmit.** Their results are
returned into your conversation and may therefore be sent to your active model provider (see 3a).
Review the privacy terms of any MCP server you enable.

## 5. Security safeguards (and their limits)

2B includes safety features that reduce accidental exposure — but these are safety mechanisms, not
guarantees of confidentiality:

- Credential-shaped environment variables are stripped from the environment given to commands 2B
  runs (opt out: `TWOB_NO_ENV_SCRUB`).
- Commands run inside a write-confinement sandbox by default; `TWOB_SEATBELT=strict` also blocks
  their network access and reads outside the workspace.
- Reads, searches, and edits touching well-known secret locations (`~/.ssh`, `.aws`, `.env`,
  etc.) and network-capable commands require your explicit confirmation.
- API keys are masked in the interface (though, as noted, stored in full on disk and sent in full
  to the provider).

These reduce accidents; they do not encrypt your stored data, nor prevent a cloud provider from
receiving content you direct the agent to read.

## 6. Your controls, at a glance

- Use a **local Ollama model** to keep all code on your machine.
- `TWOB_NO_HISTORY=1` — don't persist sessions/undo · `TWOB_NO_UPDATE_CHECK=1` — no update check ·
  `TWOB_NO_MODEL_FETCH=1` / `--no-discover` — no model discovery.
- Delete `~/.config/2b/` or run `2b --rm` to remove all stored data (keys, history, preferences).
- Choose which providers and MCP servers to connect, and review their terms.

## 7. Children

2B is a developer tool and is not directed at children.

## 8. Changes

This policy may change as 2B changes; the effective date above reflects the current version.
Material changes will be noted in the project's releases/changelog.

## 9. Contact

Questions about privacy: open an issue at https://github.com/dea6cat/2b-agent.

---

*This document is a factual description of 2B's behavior, not legal advice.*

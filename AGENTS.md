# AGENTS.md

Local-first coding agent (CLI + TUI) for small models over Ollama's native protocol.

## Stack & layout
- Python >=3.11, single package `two_b` under `src/` (hatchling build).
- Dependency manager is **uv** (`uv.lock` is committed). No pip/poetry.
- Entry point: `2b` → `src/two_b/cli.py:main`. TUI is Textual; there is also a classic REPL (`2b --classic`).
- Provider adapters live in `src/two_b/providers/` — each speaks one provider's own native wire format (Ollama, Anthropic, Google, OpenAI-compat), never a generic shim. This "no shim" rule is the core design constraint; preserve it when adding providers.
- The model's tool surface is a **small frozen set of five file-oriented tools** (`tools.py` / `toolspec.py`). Host-side complexity (edit drift tolerance, verify loop, retrieval, compaction) must stay out of the model's world.

## Commands
- Install/dev: `uv tool install .` or `uv run <cmd>`. There is no Makefile/Taskfile.
- Run a single test: `uv run python -m unittest tests.test_edit_file` (each test file documents its own run line at the top). Tests insert `src/` onto `sys.path` themselves, so run from repo root.
- The suite uses **unittest, not pytest**. Do not add a pytest config expecting fixtures/conftest.
- CLI smoke check: `2b --version` (reads `src/two_b/__init__.py:__version__`).

## Releases
- Version is the single source of truth in `src/two_b/__init__.py` (`__version__`); bump it there only.
- Publishing is tag-driven: `release.yml` builds and pushes to PyPI on `v*` tags and **fails if the tag does not equal `__version__`**. Tag exactly, e.g. `v2.4.7`.
- macOS-only in practice: `install.sh` assumes Homebrew + `pbcopy`; the README states it is untested elsewhere. Homebrew formula is `twob-agent` (not `2b-agent`) in `packaging/homebrew`.

## Conventions & gotchas
- License is **PolyForm Noncommercial 1.0.0** (source-available). Commercial use needs a separate license; the CLI gates first run on acceptance (`license.py`).
- No telemetry; provider keys go to `~/.config/2b/keys.json` (`chmod 600`). Env vars (e.g. `OPENAI_API_KEY`) override saved keys.
- `docs/` and `prepush.sh` are git-ignored — don't rely on them existing in a fresh checkout.
- Many `TWOB_*` env toggles exist (see README "Environment toggles"); tests for them live across `tests/test_*.py`.
- `tests/fixtures/` holds the only shared test fixture (`ollama_search_tools.html`).

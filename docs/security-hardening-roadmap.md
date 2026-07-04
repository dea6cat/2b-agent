# Security hardening → 2B: cross-source synthesis and roadmap

**Date:** 2026-07-04
**Sources analyzed (security lens):** Codex CLI (`/repo_apps/codex`, Rust), Crush (`/repo_apps/crush`, Go), Loom (`/repo_apps/loom`, Python), NatShell (`/repo_apps/natshell`, Python — NL→shell).
**Filter applied:** 2B's thesis — frozen 5-tool schema, all complexity host-side, macOS-first, small-model reliability, local *and* cloud. Cloud-benefiting items are in scope.
**Relationship to other docs:** the Codex secret-hygiene items (C22–C27 in `sandbox-capabilities-roadmap.md`) and the P12 seatbelt are folded in here as the authoritative security plan; this doc supersedes them for sequencing.

---

## The one-paragraph verdict

Reading four agents for security specifically turned up far more than the first Codex pass suggested — and the most important signal is **cross-corroboration**: independent codebases in three languages converge on the same defenses, which is strong evidence they're worth doing. 2B's single biggest gap, flagged by **both Crush and Loom**, is **workspace-escape containment**: 2B's file tools (`read_file`/`edit_file`/`write_file`/`list_files`) have no general path-traversal guard, so a poisoned file or instruction can get 2B to read `../../.ssh/id_rsa` or write outside the project — 2B only *plans* `.git`-write protection, which is a subset. Second, on subprocess env hygiene, **both Codex and Loom** use a *default-deny allowlist* (not a denylist), so 2B's planned `*KEY*/*SECRET*` scrubber (C22) should be flipped to allowlist-first. Third, **NatShell and Loom** both ship a curated destructive-command corpus with a **hard-BLOCK tier** distinct from confirm — which slots directly onto the risk-class label 2B already shipped, adding hard-refuse + chain/substitution-aware classification. The rest (exfil-command denylist, git subcommand allowlist, sensitive-path guard, secret-safe errors, redaction wired into all sinks, Keychain storage, bounded output, approval hardening) are each backed by one or more sources and are mostly small stdlib ports. **Honest caveats:** (1) regex command denylists are *defense-in-depth*, not containment — they feed a confirm/refuse decision and must be paired with the P12 seatbelt for a real boundary; (2) **prompt-injection / untrusted-content handling is a gap in all four sources** — none wraps tool output or file content as untrusted data, so it's an area 2B could actually lead rather than copy.

---

## A. TAKE — consolidated, deduped, cross-referenced

Value ★–★★★. Effort S <½day / M 1–2d / L larger. "Sources" shows corroboration — 2+ sources = higher confidence.

### Tier 1 — the gaps multiple sources agree on

| # | Item | Sources (file:line) | Why it fits 2B | Value | Effort |
|---|---|---|---|---|---|
| S1 | **Workspace-containment path guard on ALL file tools (read + write)** — `expanduser().resolve()` then `path.relative_to(root)`; if it escapes, refuse or force an explicit prompt. Separate read-roots vs write-scope. | Crush `view.go:116-145`, `ls.go:87-113`; Loom `registry.py:511,369,379` | 2B's biggest hole: no traversal guard today, only a planned `.git`-write subset. Resolving symlinks + `..` *before* the check defeats symlink escapes too. Python-native, directly portable. | ★★★ | S–M |
| S2 | **Default-deny allowlist env for subprocesses** — build the child env from `{}` + a fixed allowlist (PATH, SHELL, HOME, TMPDIR, LANG, LC_*, LOGNAME, USER, TERM), never inherit. | Codex `shell_environment.rs:56-116`; Loom `command_runner.py:28` (`constrained_env`) | **Flips 2B's planned C22 denylist to allowlist-first** — an allowlist can't be defeated by a secret named `KEY_MATERIAL` that a `*KEY*` glob misses. Both mature sources chose allowlist. Pass explicitly via `subprocess(env=…)`. | ★★★ | S |
| S3 | **Curated destructive-command corpus + hard-BLOCK tier** — a static regex set split into BLOCKED (never run: fork bomb, `rm -rf /`, `dd of=/dev/sd*`, `mkfs`, disk-wipe, `format C:`) and CONFIRM (rm/dd/chmod 777/chown/systemctl/pkg-install/docker/iptables/launchctl/…). BLOCKED is a hard refusal fed back to the model as "try an alternative." | NatShell `config.default.toml:74-130`, `classifier.py:14-17`; Loom `shell.py:18,42` | Slots onto **2B's already-shipped risk-class label** (P19/P11), adding (a) a hard-refuse tier 2B lacks and (b) a reusable corpus. See §C for the verbatim patterns. | ★★★ | S |
| S4 | **Chain/substitution-aware classification** — split the command on `&&`/`\|\|`/`;`/`\|`/`&`, classify each sub-command, take the worst; flag backticks/`$()` as CONFIRM; re-apply the check inside script sources & substitution. | NatShell `classifier.py:53-110`; Crush `dispatch.go:48-74`, `expand.go:113` (recursive block) | Hardens `run_command` against a benign-looking chain hiding a destructive tail, and against smuggling a banned command via `$(…)` or a wrapper script. Extends 2B's existing shell-chain rejection from "reject" to "classify each segment." | ★★★ | S–M |
| S5 | **git subcommand allowlist + destructive-flag denylist + flag-resistant verb parse** — whitelist subcommands, then regex-deny `push --force`, `reset --hard`, `clean -f`, `branch -D`, `checkout .`; pick the verb as the first non-flag arg so `git -c x=y push --force` still resolves to `push`. | NatShell `git_tool.py:56-268`; Loom `git.py:11,21,45` | Beyond 2B's shell-chain rejection: per-flag validation on the local-only `run_git`. Two sources, near-identical design. | ★★★ | S |
| S6 | **Secret redaction wired into ALL sinks (redact-at-persist)** — one redactor called before the SQLite history write, before the `TWOB_TRACE` JSONL write, and at the tool-output→model boundary; also scrub any outbound-request logging by header name. | Codex `sanitizer.rs:4-22` (applied at only ONE path); Loom `oauth.py:62` (redacts at persist time); Crush `http.go:101-117` (header-name redaction) | Codex's own weakness is the lesson: it redacts only its memory-write path. 2B should cover every sink. Merged regex set in §C. | ★★★ | M |

### Tier 2 — single-source, high value

| # | Item | Source (file:line) | Why it fits 2B | Value | Effort |
|---|---|---|---|---|---|
| S7 | **Sensitive-path confirm guard** — reads/writes touching `~/.ssh`, `id_rsa/id_ed25519`, `/etc/shadow`, `/etc/sudoers`, `.env`, `~/.aws/credentials`, `~/.kube/config`, `~/.docker/config.json` require confirmation even inside the workspace. | NatShell `classifier.py:22-34,150-155` | Direct exfil defense on 2B's file tools; complements S1 (escape) with a content-sensitivity layer. | ★★★ | S |
| S8 | **Exfil-command denylist for `run_command`** — flag/deny `curl/wget/aria2c/nc/ssh/scp/telnet/lynx/w3m` (and inline interpreters `python -c`/`perl -e`) as CONFIRM/BLOCK. | Crush `bash.go:75-146`; Loom `shell.py:18` | Concrete data-exfil / prompt-injection defense; **largest benefit in cloud mode** where a poisoned file could coax `run_command` into curling secrets out. Feeds the S3 classifier as a category. | ★★★ | S |
| S9 | **Keychain-backed key storage + graceful fallback + secret-ref resolver** — store the API key in the macOS Keychain (Python `keyring`, native backend); support `${ENV}`/`env://`/`keychain://` indirection and refuse to *persist* a secret into an env ref (writable target must be keychain). Fall back to a `0600`/encrypted file with a warning when no keychain (headless/CI). | Codex `keyring-store` + `secrets/local.rs`; Loom `auth/secrets.py:17,56` | Replaces plaintext-config keys. Codex fails closed with no keychain — Loom's write-target validation + a documented file fallback is the better shape. | ★★★ | M |
| S10 | **Secret-safe error rendering** — an error type that never carries the *resolved* (post-expansion) secret, only the user-typed template; truncate messages to ~512B and replace non-printables with `?`; keep `Unwrap()`/`__cause__` for matching. | Crush `resolve.go:129-174`, `expand.go:145-148` | Stops an API key surfacing in an exception string that lands in 2B's SQLite log or JSONL trace — a leak path the S6 redactor might miss. | ★★ | S |
| S11 | **Bounded subprocess output (OOM defense)** — read child stdout/stderr in chunks up to a cap (e.g. 1 MiB/stream); past the cap keep *draining* (so the pipe never blocks the process) but stop storing, mark truncated. | Loom `command_runner.py:60` (`_read_limited`) | Defends against `yes`/`cat /dev/urandom` blowing up memory. Pairs with 2B's existing process-group esc-kill (which Loom also has — take only the bounded-read delta). | ★★ | S |

### Tier 3 — approval-model upgrades (compose into one change)

| # | Item | Sources (file:line) | Why it fits 2B | Value | Effort |
|---|---|---|---|---|---|
| S12 | **Path-scoped grants** — grant key = `(session, tool, action, path)`, so "allow for session" remembers *this path* not the whole tool. | Crush `permission.go:88-93,248-259` | Upgrades 2B's coarse per-session y/n/allow-all to fine-grained, remembered-per-path grants. | ★★ | M |
| S13 | **High-risk re-prompt under allow-all + headless default-deny + approval bound to tool-call id** — a per-call "require explicit approval" flag re-prompts even when the tool is auto-approved (and is never persisted as always-approved); no prompt callback ⇒ DENY; a pre-approval only honored for the exact tool-call id. | Loom `approval.py:99,190`; NatShell `mcp_server.py:63-76`; Crush `permission.go:16-36,196-202` | Makes 2B's allow-all safe (destructive calls still stop), makes unattended/cloud runs fail-closed, and prevents approval replay. Three sources converge. | ★★ | S |

---

## B. DON'T-TAKE (security items that don't fit)

| Item | Source | Why rejected |
|---|---|---|
| SSRF / private-IP guard on fetch | NatShell `fetch_url.py`, Loom `web.py:229` | Excellent (DNS→IP check vs loopback/RFC1918/link-local incl. `169.254.169.254`, redirect re-validation), but **2B's frozen schema has no web-fetch tool**. Deferred/conditional — adopt only if 2B ever adds one. |
| OAuth device-code / PKCE / loopback flows | Crush `oauth/copilot`, Loom `oauth/engine.py` | 2B uses static API keys; no authorization-code surface. Keychain storage (S9) covers credential-at-rest. |
| `danger` mode / MCP `permissive` bypass switch | NatShell `classifier.py:117`, `mcp_server.py:70` | Downgrades CONFIRM→SAFE wholesale — a footgun. Keep the BLOCKED-still-blocks invariant, skip the bypass. |
| In-process sudo-password cache (5-min TTL) | NatShell `execute_shell.py:135` | Plaintext password in memory; 2B is macOS-first and shouldn't cache sudo passwords. |
| `_safe_eval` AST allowlist, calculator | Loom `calculator.py:56` | No expression-eval tool in the frozen schema; nothing to protect. |
| `fcntl.flock` token-store / cross-process OAuth refresh lock | Loom `oauth.py:92`, Crush `store.go` | 2B persists to SQLite (own locking) + append-only JSONL; the file-lock token-store machinery doesn't map. (Keep the `0600`+atomic-write recipe — already C4/C27.) |
| Shebang/`env -S` parsing, binary magic-byte probe, Windows drive/UNC handling | Crush `dispatch.go` | Go in-process shell-interpreter and Windows concerns; irrelevant to macOS-first subprocess model. |
| Weaker suffix-allowlist env scrubber | NatShell `execute_shell.py:113` | Superseded by the stronger allowlist approach (S2). |

---

## C. Reusable corpora & regexes (lift these verbatim)

### Destructive-command tiers (S3) — NatShell `config.default.toml:74-130`
**BLOCKED (never run):** `:(){ :|:& };:` (fork bomb) · `^rm\s+-[rR]f\s+/\s*$` · `^rm\s+-[rR]f\s+/\*` · `^mv\s+/\s` · `^dd\s+.*of=/dev/[sh]d[a-z]\s*$` · `^mkfs.*\s/dev/[sh]d[a-z][0-9]?\s*$` · `> /dev/[sh]d[a-z]` · `^diskutil\s+eraseDisk` · (Windows) `^format\s+C:`, `Remove-Item\s+-Recurse\s+-Force\s+C:\\`.
**CONFIRM:** `^rm\s` · `^sudo\s` · `^dd\s` · `^mkfs` · `^shutdown` · `^reboot` · `^systemctl\s+(stop|disable|mask|restart|enable|start)` · `^chmod\s+[0-7]*7` · `^chown` · `\|\s*tee\s` · `>\s*/etc/` · `^kill` · `^wipefs` · `^fdisk` · `^parted` · `^(apt|dnf|pacman|pip|brew)\s+(install|remove|…)` · `^docker\s+(rm|rmi|stop|kill|system\s+prune)` · `^iptables` · `^ufw` · `^crontab` · `^launchctl\s+(load|unload|bootout|…)`.
**Exfil (S8) add:** `curl|wget|aria2c|nc|ncat|ssh|scp|telnet|lynx|w3m`, inline interpreters `python\s+-c`, `perl\s+-e`, and `curl.*\|\s*(sh|bash)`.

### Classification logic (S4) — NatShell `classifier.py:53-110`
1. Match BLOCKED then CONFIRM against the *full* string first (catches fork bombs / pipe patterns spanning operators). 2. Flag `` `…` `` and `$(…)` as CONFIRM. 3. Split on `\s*(?:&&|\|\||[;&|])\s*`, classify each sub-command, **return the worst**. 4. Heuristics: `sudo ` prefix → CONFIRM; `>\s*/(?:etc|boot|usr|var/lib)/` → CONFIRM. *These are `re.search` over a bash string — bypassable (quoting/`$IFS`/env-indirection). Use as a confirm/refuse trigger, not a boundary; pair with P12.*

### Merged secret-redaction set (S6) — Codex `sanitizer.rs` + Loom `oauth.py:62`
- OpenAI: `sk-[A-Za-z0-9]{20,}` → `[REDACTED]`
- AWS: `\bAKIA[0-9A-Z]{16}\b` → `[REDACTED]`
- Bearer: `(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}` → `Bearer [REDACTED]`
- Token assignment: `(?i)\b(api[_-]?key|token|secret|password|access_token|refresh_token|id_token|client_secret)\b(\s*[:=]\s*)(["']?)[^\s"',;]{8,}` → keep name+sep+quote, redact value
- Auth headers: `(?i)\b(proxy-)?authorization\b\s*[:=]\s*\S+` → redact value

### Git allowlist/denylist (S5) — Loom `git.py`
Allow only known-safe subcommands; then deny-scan the joined args for `push\s+.*--force`, `reset\s+--hard`, `clean\s+.*-[a-z]*f`, `branch\s+.*-D`, `checkout\s+\.`. Resolve the verb as the first non-`-` arg.

---

## D. Proposed sequencing

These form a coherent **security release**, most host-side and unit-testable without a live model. Get an explicit go-ahead before starting (per the handoff process); branch off `main`.

1. **Containment phase (S1 + S7 + S2).** The biggest, best-corroborated gaps: workspace-escape guard on all five file tools (read + write), sensitive-path confirm, and the allowlist subprocess env. Pure Python, `pathlib`-native. *Recommended first — it closes 2B's largest hole and pairs conceptually with the planned P12 seatbelt (defense-in-depth + boundary).*
2. **Command risk-tiering phase (S3 + S4 + S5 + S8).** Extend 2B's existing risk-class label into a BLOCKED/CONFIRM classifier with the curated corpus, chain/substitution awareness, the exfil denylist, and git subcommand validation. One `cmdguard`-style pure module; heavily unit-testable.
3. **Secret-hygiene phase (S6 + S9 + S10 + the Codex C22 refinement).** Redactor wired into all sinks, Keychain key storage with fallback, secret-safe errors. This is the Codex C22–C27 cluster, now allowlist-first (S2) and multi-sink (S6).
4. **Robustness + approval phase (S11 + S12 + S13).** Bounded subprocess output, path-scoped grants, high-risk re-prompt / headless default-deny / call-id-bound approvals.
5. **P12 seatbelt** (from `sandbox-capabilities-roadmap.md` §C) as the containment *boundary* beneath all of the above — the one true sandbox; everything else is defense-in-depth feeding confirm/refuse decisions.

### The gap worth owning
**Prompt-injection / untrusted-content handling is absent in all four sources.** None wraps tool output, file content, or (in cloud mode) fetched content as untrusted data distinct from user instructions. The S1/S7/S8 guards are *partial* mitigations (they stop the exfil action, not the injection). A genuine 2B differentiator would be marking tool-result/file content as data in the prompt (e.g. a delimiter/role convention the small model is trained-by-prompt to not treat as instructions) — no source to copy, but the highest-leverage security research direction if 2B wants to lead rather than follow.

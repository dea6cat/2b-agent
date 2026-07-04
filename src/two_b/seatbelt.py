"""macOS write-confinement sandbox for run_command — a host-side backstop that
confines a command's *writes* to the workspace (plus temp/cache dirs), so a
misbehaving or prompt-injected cloud model can't clobber files outside the project.
Never a model-facing tool; dep-free; the policy/argv builders are pure.

Model: **permissive-base + deny-writes** SBPL, not deny-default. `(allow default)`
keeps reads / exec / network working (so `npm test`, `pytest`, compilers don't
break), then we close all writes and re-open only the workspace + temp/cache roots,
minus protected subtrees (.git, 2B's own config). This is a robust write boundary,
NOT an exfiltration boundary — reads and network stay open, so data-theft is caught
by the command-approval layer (cmdguard), not here. `strict` mode additionally denies
network for users who want that hard guarantee (at the cost of breaking npm/pip/fetch).

Posture (resolved by mode()): **on by default**; `TWOB_NO_SEATBELT` disables it;
`TWOB_SEATBELT=strict` adds the network deny. Non-macOS or a missing sandbox binary
degrades to off (we can't confine, so we don't pretend to). run_command is the only
sandboxed tool — run_git is git-only and left unconfined in v1.

Not an airtight jail (by design — it's a backstop, paired with cmdguard's command
approval): the permissive base leaves mach-lookup/IPC open, so a command that asks a
system service to write for it (launchctl/osascript, etc.) can step outside. Those
escalation commands are folded into cmdguard.is_high_risk so they re-prompt even under
a grant; the sandbox stops the common/accidental cases, and Layer 1 gates the rest.

Security notes carried from the reference implementation:
  * `/usr/bin/sandbox-exec` is HARD-PINNED (never PATH-resolved) — a tampered binary
    already on PATH would otherwise defeat the sandbox.
  * Writable/protected paths are passed OUT-OF-BAND as `-D KEY=VALUE` and referenced
    as `(param "KEY")`, never string-interpolated into the policy, so a path with SBPL
    metacharacters can't inject rules.
  * Protected dirs are denied via BOTH `(literal …)` and `(subpath …)` so the path is
    unwritable AND uncreatable (a plain subpath leaves a gap for first-time mkdir).
"""
import os
import sys

# Hard-pinned, never resolved via PATH — see module docstring.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Device writes many benign commands need even under (deny file-write*).
_DEV_WRITES = ("/dev/null", "/dev/zero", "/dev/dtracehelper", "/dev/tty", "/dev/stdout", "/dev/stderr")

# A non-zero command whose output carries one of these — while sandboxed — is very
# likely a sandbox denial rather than an ordinary failure (best-effort; feeds the
# "blocked from writing outside the workspace — re-run?" prompt, never an auto-action).
_DENIAL_MARKERS = ("operation not permitted", "sandbox_apply", "deny file-write")


def is_available() -> bool:
    """True only on macOS with the seatbelt binary present — otherwise we can't
    confine anything and mode() degrades to 'off'."""
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


def _env_disabled() -> bool:
    v = (os.environ.get("TWOB_NO_SEATBELT") or "").strip().lower()
    return v not in ("", "0", "no", "false", "off")


def mode() -> str:
    """Resolved posture: 'off' | 'on' | 'strict'. On by default where available;
    TWOB_NO_SEATBELT forces off; TWOB_SEATBELT=strict adds the network deny."""
    if not is_available():
        return "off"
    if _env_disabled():
        return "off"
    v = (os.environ.get("TWOB_SEATBELT") or "").strip().lower()
    if v == "strict":
        return "strict"
    if v in ("0", "off", "no", "false"):
        return "off"
    return "on"


def writable_roots() -> list[str]:
    """Absolute, symlink-resolved, deduped roots writes are allowed under: the
    workspace (cwd), $TMPDIR, /tmp, the user cache dirs, and the package-manager
    *download* caches — so default-on sandboxing doesn't break `npm install`/`pub get`/etc.

    These are scoped to the download subdirs on purpose. The package-manager ROOTS
    (~/.gradle, ~/.m2, ~/.cargo, ~/.pub-cache) hold auto-executed init scripts and
    stored credentials (~/.gradle/init.d, ~/.m2/settings.xml, ~/.cargo/config.toml,
    ~/.pub-cache/credentials.json) — making the whole root writable would let a
    prompt-injected command plant a machine-wide backdoor or hijack dependency
    resolution, so only the caches under them are opened. ~/.ssh, ~/.aws, ~/.gnupg are
    never writable (home itself isn't). Main false-denial knob; widen carefully."""
    candidates = [
        os.getcwd(),
        os.environ.get("TMPDIR") or "",
        "/tmp",
        *(os.path.expanduser(p) for p in (
            "~/Library/Caches", "~/.cache", "~/.npm",
            "~/.cargo/registry", "~/.cargo/git",
            "~/.gradle/caches",
            "~/.m2/repository",
            "~/.pub-cache/hosted", "~/.pub-cache/git",
        )),
    ]
    out, seen = [], set()
    for c in candidates:
        if not c:
            continue
        rp = os.path.realpath(c)
        if os.path.isabs(rp) and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def protected_paths() -> list[str]:
    """Paths that stay read-only even when nested inside a writable root: the repo's
    .git (VCS integrity) and 2B's own config/state dir (keys + history). realpath is
    used so a symlinked .git is still covered; the path need not exist yet."""
    out, seen = [], set()
    for p in (os.path.join(os.getcwd(), ".git"), os.path.expanduser("~/.config/2b")):
        rp = os.path.realpath(p)
        if os.path.isabs(rp) and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def build_policy(n_writable: int, n_protected: int, *, strict: bool = False) -> str:
    """The SBPL policy string. Writable/protected paths are referenced by param name
    (WRITABLE_ROOT_i / PROTECTED_i) supplied via -D — never interpolated here — so the
    policy is fixed text regardless of the paths. Later rules override earlier ones."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
    ]
    if n_writable:
        allow = " ".join(f'(subpath (param "WRITABLE_ROOT_{i}"))' for i in range(n_writable))
        lines.append(f"(allow file-write* {allow})")
    for i in range(n_protected):
        # Deny both the exact path and its subtree: unwritable AND uncreatable.
        lines.append(f'(deny file-write* (literal (param "PROTECTED_{i}")) (subpath (param "PROTECTED_{i}")))')
    dev = " ".join(f'(literal "{d}")' for d in _DEV_WRITES)
    lines.append(f"(allow file-write* {dev})")
    if strict:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def build_argv(command: str, roots: list[str], protected: list[str], *, strict: bool = False) -> list[str]:
    """Full argv for `sandbox-exec … -- /bin/sh -c <command>`. Paths must be absolute
    (writable_roots/protected_paths guarantee this); a relative path is a bug and
    raises rather than silently widening the sandbox."""
    for p in [*roots, *protected]:
        if not os.path.isabs(p):
            raise ValueError(f"seatbelt root must be absolute: {p!r}")
    policy = build_policy(len(roots), len(protected), strict=strict)
    argv = [SANDBOX_EXEC, "-p", policy]
    for i, r in enumerate(roots):
        argv += ["-D", f"WRITABLE_ROOT_{i}={r}"]
    for i, p in enumerate(protected):
        argv += ["-D", f"PROTECTED_{i}={p}"]
    argv += ["--", "/bin/sh", "-c", command]
    return argv


def wrap(command: str) -> tuple[list[str] | None, bool]:
    """(argv, strict) for running `command` under the sandbox, or (None, False) when
    the sandbox is off/unavailable (caller runs the command directly). `strict` is
    surfaced so the caller can tailor a denial message (network was blocked)."""
    m = mode()
    if m == "off":
        return (None, False)
    strict = m == "strict"
    return (build_argv(command, writable_roots(), protected_paths(), strict=strict), strict)


def looks_like_denial(returncode, output: str) -> bool:
    """Best-effort: True if a sandboxed command's failure looks like a write denial,
    so the caller can offer to re-run with the path allowed. Never triggers an action
    on its own — it only gates a prompt."""
    if not returncode or not output:
        return False
    low = output.lower()
    return any(m in low for m in _DENIAL_MARKERS)

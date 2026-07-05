"""OS write-confinement sandbox for run_command — a host-side backstop that confines a
command's *writes* to the workspace (plus temp/cache dirs), so a misbehaving or
prompt-injected cloud model can't clobber files outside the project. macOS backend =
`sandbox-exec` (seatbelt/SBPL); Linux backend = `bwrap` (bubblewrap). Never a model-facing
tool; dep-free; the policy/argv builders are pure. (The module keeps the "seatbelt" name
and the TWOB_SEATBELT env vars for historical continuity; it now covers both OSes.)

Model: **permissive-base + deny-writes** SBPL, not deny-default. `(allow default)`
keeps reads / exec / network working (so `npm test`, `pytest`, compilers don't
break), then we close all writes and re-open only the workspace + temp/cache roots,
minus protected subtrees (.git, 2B's own config). This is a robust write boundary,
NOT an exfiltration boundary — reads and network stay open, so data-theft is caught
by the command-approval layer (cmdguard), not here. `strict` mode additionally denies
network for users who want that hard guarantee (at the cost of breaking npm/pip/fetch).

Posture (resolved by mode()): **on by default** on macOS (sandbox-exec present) and Linux
(bwrap present) = WRITE-confinement, reads open; `TWOB_NO_SEATBELT` disables it;
`TWOB_SEATBELT=strict` = maximum confinement: also **denies network** (macOS `(deny network*)`
/ Linux `--unshare-net`) AND **confines reads** to the workspace/caches + a curated system-read
allowlist (so $HOME secrets like ~/.ssh/~/.aws become unreadable). A platform without its
sandbox binary degrades to off. run_command is the only sandboxed tool — run_git is git-only
and left unconfined. (Default keeps reads open because read-confinement breaks any command
that reads outside the workspace + system allowlist — notably $HOME-installed toolchains
(~/.pyenv, ~/.nvm, ~/.rustup, ~/.cargo/bin, ~/.local/bin, asdf/direnv shims) and per-user
config like ~/.gitconfig; strict is the opt-in hard-guarantee and expects such breakage.)

Not an airtight jail (by design — it's a backstop, paired with cmdguard's command
approval): the permissive base leaves mach-lookup/IPC open, so a command that asks a
system service to write for it (launchctl/osascript, etc.) can step outside. Those
escalation commands are folded into cmdguard.is_high_risk so they re-prompt even under
a grant; the sandbox stops the common/accidental cases, and Layer 1 gates the rest.

Linux (bwrap) caveats — parallels to the macOS disclosure above, pending on-Linux
validation (this module is developed/tested on macOS; the Linux behavioral tests skip
there): (1) only the mount namespace is unshared, so a same-UID sibling process's open
fd via /proc/<pid>/fd could be a write path out where Yama ptrace_scope permits it —
niche, and --unshare-pid is deferred until esc-kill interaction can be verified on Linux;
(2) is_available() checks that the bwrap binary is present, not that unprivileged user
namespaces are enabled — on a box where they're disabled, a run would fail at bwrap setup
(degrading like any command error). Both are Linux-validation items, not shipped guarantees.

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
import shutil
import sys

# Hard-pinned, never resolved via PATH — see module docstring.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Preferred absolute path for bubblewrap; falls back to PATH lookup if not there. Unlike
# sandbox-exec, bwrap's location varies by distro, so a hard pin alone would miss it — the
# fixed path is tried first (safer) before a PATH resolve (mild residual risk on Linux).
_BWRAP_PREFERRED = "/usr/bin/bwrap"

# Device writes many benign commands need even under (deny file-write*).
_DEV_WRITES = ("/dev/null", "/dev/zero", "/dev/dtracehelper", "/dev/tty", "/dev/stdout", "/dev/stderr")

# System dirs a process must READ to exec + load libraries + do basic lookups. Under
# read-confinement (strict) reads are denied except the workspace/caches and these — so
# $HOME's secrets (~/.ssh, ~/.aws, …) become unreadable while normal commands still run.
# macOS: SBPL subpaths. Curated to cover the shell, common tool prefixes (Homebrew at
# /opt/homebrew and /usr/local), the dyld cache, timezone/hosts/ssl, and /dev.
_MACOS_SYS_READ = (
    "/usr", "/bin", "/sbin", "/System", "/Library", "/opt", "/dev",
    "/private/etc", "/private/var/db", "/private/var/folders", "/private/tmp",
    "/private/var/select", "/Applications",
)
# Linux: dirs bind-mounted read-only under read-confinement (instead of all of /).
# /run covers systemd-resolved's resolv.conf symlink target and D-Bus user sockets.
_LINUX_SYS_READ = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/var", "/run")

# A non-zero command whose output carries one of these — while sandboxed — is very
# likely a sandbox denial rather than an ordinary failure (best-effort; feeds the
# "blocked from writing outside the workspace — re-run?" prompt, never an auto-action).
# "read-only file system" is the bwrap analog (a write to a ro-bound path yields EROFS).
_DENIAL_MARKERS = ("operation not permitted", "sandbox_apply", "deny file-write",
                   "read-only file system")


def _bwrap_path() -> str | None:
    """Resolved bubblewrap path (preferred fixed location, else PATH), or None."""
    if os.path.exists(_BWRAP_PREFERRED):
        return _BWRAP_PREFERRED
    return shutil.which("bwrap")


def _macos_available() -> bool:
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


def _linux_available() -> bool:
    return sys.platform.startswith("linux") and _bwrap_path() is not None


def is_available() -> bool:
    """True when the current OS has its write-confinement backend available (macOS
    sandbox-exec / Linux bwrap) — otherwise mode() degrades to 'off'."""
    return _macos_available() or _linux_available()


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
        # Never make "/" a writable root — it would re-expose the whole filesystem
        # (and /dev, /proc) read-write and defeat confinement (degenerate cwd=/ case).
        if os.path.isabs(rp) and rp != "/" and rp not in seen:
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
        # Read-confinement: deny reads, then re-allow the workspace/caches (the writable
        # roots are readable too) and the system dirs needed to exec/load libs — so a
        # command can't read secrets elsewhere in $HOME (~/.ssh, ~/.aws, …).
        lines.append("(deny file-read*)")
        read_roots = " ".join(f'(subpath (param "WRITABLE_ROOT_{i}"))' for i in range(n_writable))
        sys_read = " ".join(f'(subpath "{p}")' for p in _MACOS_SYS_READ)
        lines.append(f"(allow file-read* (literal \"/\") {read_roots} {sys_read})")
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


def build_bwrap_argv(command: str, roots: list[str], ro_protected: list[str],
                     tmpfs_protected: list[str], bwrap: str, *, strict: bool = False) -> list[str]:
    """Full argv for `bwrap … -- /bin/sh -c <command>` (Linux). The filesystem is bound
    read-only (whole `/` by default; under strict, only the system read dirs so $HOME's
    secrets aren't readable), then each writable root is bound read-write over it, then
    protected dirs are locked on top (bwrap applies binds in ARG ORDER; later overrides
    earlier):
      * `ro_protected` (EXISTING .git / 2B config) → `--ro-bind-try`: readable, unwritable.
      * `tmpfs_protected` (MISSING protected paths) → `--tmpfs` + `--remount-ro`: an empty
        READ-ONLY throwaway mount, so a write fails loudly (EROFS → a denial hint) rather
        than silently succeeding, AND the path can't be created on the real fs (e.g. a
        planted .git/hooks backdoor in a non-repo dir). The Linux stand-in for the macOS
        literal+subpath "uncreatable" guarantee.
    Paths must be absolute (writable_roots/protected_paths guarantee this)."""
    for p in [*roots, *ro_protected, *tmpfs_protected]:
        if not os.path.isabs(p):
            raise ValueError(f"seatbelt root must be absolute: {p!r}")
    argv = [bwrap]
    if strict:
        # Read-confinement: bind only the system read dirs (not all of /), so $HOME
        # (secrets) isn't readable. --ro-bind-try skips any that don't exist on this box.
        for d in _LINUX_SYS_READ:
            argv += ["--ro-bind-try", d, d]
    else:
        argv += ["--ro-bind", "/", "/"]  # everything readable, read-only by default
    argv += ["--dev", "/dev",            # fresh writable devtmpfs (/dev/null etc.)
             "--proc", "/proc",
             "--die-with-parent"]        # child dies if 2B does
    for r in roots:
        argv += ["--bind-try", r, r]     # re-open the writable roots for writing
    for p in ro_protected:
        argv += ["--ro-bind-try", p, p]  # existing .git / 2B config: readable, unwritable
    for p in tmpfs_protected:
        argv += ["--tmpfs", p, "--remount-ro", p]  # missing protected: uncreatable + read-only
    if strict:
        argv += ["--unshare-net"]        # network deny (parity with macOS strict)
    argv += ["--", "/bin/sh", "-c", command]
    return argv


def _ensure_writable_roots_exist(roots: list[str]) -> None:
    """Pre-create a missing writable root when its parent already exists (best-effort), so
    bwrap can bind it read-write. Without this, `--bind-try` silently skips a not-yet-created
    package cache (~/.cargo/registry, ~/.gradle/caches, …) and the first cargo/gradle/pub run
    fails with EROFS — a regression vs the macOS backend, whose subpath rule matches a path
    whether or not it exists. Guarded on parent-exists so we don't litter dirs for tools that
    aren't installed."""
    for r in roots:
        if not os.path.exists(r) and os.path.isdir(os.path.dirname(r)):
            try:
                os.makedirs(r, exist_ok=True)
            except OSError:
                pass


def wrap(command: str) -> tuple[list[str] | None, bool]:
    """(argv, strict) for running `command` under the OS sandbox, or (None, False) when
    the sandbox is off/unavailable (caller runs the command directly). `strict` is
    surfaced so the caller can tailor a denial message (network was blocked)."""
    m = mode()
    if m == "off":
        return (None, False)
    strict = m == "strict"
    roots, protected = writable_roots(), protected_paths()
    if sys.platform == "darwin":
        return (build_argv(command, roots, protected, strict=strict), strict)
    bwrap = _bwrap_path()
    if bwrap is None:                    # defensive: mode() is 'off' when unavailable
        return (None, False)
    _ensure_writable_roots_exist(roots)
    ro_protected = [p for p in protected if os.path.exists(p)]     # existing: readable+ro
    tmpfs_protected = [p for p in protected if not os.path.exists(p)]  # missing: uncreatable
    return (build_bwrap_argv(command, roots, ro_protected, tmpfs_protected, bwrap, strict=strict), strict)


def looks_like_denial(returncode, output: str) -> bool:
    """Best-effort: True if a sandboxed command's failure looks like a write denial,
    so the caller can offer to re-run with the path allowed. Never triggers an action
    on its own — it only gates a prompt."""
    if not returncode or not output:
        return False
    low = output.lower()
    return any(m in low for m in _DENIAL_MARKERS)

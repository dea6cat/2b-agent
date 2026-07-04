"""Command- and path-safety classifier — a host-side gate consulted before a shell
command runs or an unattended write lands. Never a model-facing tool; dep-free; pure.

Three tiers for a shell command (classify_command):
  block   — catastrophic / self-destructive; refused outright. A best-effort SAFETY
            FLOOR, not a perfect sandbox: it catches the obvious catastrophic forms
            (including simple grouping and `bash -c` wrapping) and refuses them, but the
            confirmation gate remains the primary control — exotic obfuscation (command
            substitution, base64, `eval`) falls through to `confirm`, not `allow`.
  allow   — a single, metacharacter-free, read-only probe (whoami, pwd, `--version`, …);
            runs without a confirmation prompt.
  confirm — everything else; goes through the normal confirmation/grant flow.

Plus:
  is_high_risk    — destructive-but-legitimate shell commands (rm -rf <dir>, git push
                    --force, reset --hard, clean -fx, branch -D, sudo) that must re-prompt
                    even if the tool was granted "allow for this session".
  git_is_high_risk— the git-only equivalent, for run_git.
  escapes_root    — path-jail helper: True if a path resolves outside the workspace root
                    (symlinks included), fail-closed.

The block corpus matches the COMMAND at the start of each shell segment (split on
; | && || & newline AND grouping ( ) { }), not anywhere in the string, so `echo reboot`
or a comment mentioning `rm -rf /` is not blocked; leading `sudo`, env assignments, and
wrappers are skipped, and `bash -c "<script>"` is classified recursively. Fail-closed:
anything uncertain is `confirm`, never `allow`.
"""
import os
import re
import shlex

# Split on shell separators. Grouping punctuation ( ) { } is handled by stripping it off
# tokens (see _norm / _skip_leading), NOT by splitting — splitting on { } would tear apart
# `${HOME}` parameter expansion and miss it as a delete target.
_SEP_RE = re.compile(r"&&|\|\||[;\n\r|&]")
# A metacharacter (incl. newline) makes a command ineligible for the no-prompt allow tier.
_META = set(";|&$`><(){}\n\r")
_WRAPPERS = {"sudo", "doas", "command", "nohup", "nice", "time", "env", "xargs", "then", "do", "exec"}
_INTERPRETERS = {"bash", "sh", "zsh", "dash", "ash", "ksh"}

# Read-only, side-effect-free commands safe to run without a prompt.
_SAFE = {
    "whoami", "id", "pwd", "hostname", "uname", "arch", "uptime", "date", "cal",
    "which", "type", "groups", "locale", "tty", "printenv", "echo", "printf",
    "true", "false", "clear",
}
_VERSION_FLAGS = {"--version", "-version", "-V", "-v", "--help", "-h", "help", "version"}

# System / home / whole-tree roots that must never be recursively deleted or chmod'd.
# Covers both Linux and macOS layouts (2B is macOS-first but may run on Linux).
_SYS_PATHS = {
    "/", "/usr", "/etc", "/var", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/root",
    "/home", "/boot", "/dev", "/sys", "/proc",
    "/Users", "/System", "/Library", "/Applications", "/private", "/Volumes", "/cores",
}


def _norm(t: str) -> str:
    """Strip surrounding quotes and grouping punctuation, and fold ${HOME}→$HOME, so
    `'rm'`, `(rm`, `/)`, and `${HOME}` all match their bare forms."""
    return t.strip("\"'`(){}").replace("${", "$").replace("}", "")


def _skip_leading(toks):
    """Drop leading env assignments (VAR=val) and wrappers (sudo/nohup/…) to reach the
    real command tokens. Also strips a leading grouping char left on the first token."""
    i = 0
    while i < len(toks):
        t = toks[i].lstrip("({")
        if not t:
            i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
            continue
        if t in _WRAPPERS:
            i += 1
            continue
        toks = toks[i:]
        toks[0] = toks[0].lstrip("({")
        return toks
    return []


def _segments(cmd):
    return [s for s in _SEP_RE.split(cmd) if s.strip()]


def _command_tokens(seg):
    """The command + args of a segment, past leading env assignments and wrappers."""
    return _skip_leading(seg.strip().split())


def _interpreter_script(seg):
    """If a segment is `bash -c <script>` (or sh/zsh/…), return the inner script string so
    it can be classified recursively; else None. shlex keeps the quoted script intact."""
    try:
        parts = _skip_leading(shlex.split(seg))
    except ValueError:
        return None
    if len(parts) >= 3 and _norm(parts[0]) in _INTERPRETERS and "-c" in parts[1:]:
        i = parts.index("-c")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _is_danger_target(t: str) -> bool:
    """A delete target that would wreck the system, the home dir, or the whole cwd."""
    t = _norm(t)
    if t in ("*", "~", "$HOME", ".", ".."):
        return True
    base = t[:-2] if t.endswith("/*") else t        # rm -rf /usr/* -> /usr
    base = base.rstrip("/") or "/"
    if base in ("~", "$HOME", "."):
        return True
    if base.startswith("/") and os.path.normpath(base) in _SYS_PATHS:   # normpath folds /var/../ -> /
        return True
    return False


def _is_system_path(t: str) -> bool:
    """An absolute system path (for chmod/chown) — NOT '.' or '~', which are routine to
    recurse over and shouldn't be un-bypassably blocked."""
    base = _norm(t).rstrip("/") or "/"
    return base.startswith("/") and os.path.normpath(base) in _SYS_PATHS


def _is_rm_bomb(toks) -> bool:
    """rm with recursive+force aimed at a system/home/wildcard/cwd target."""
    if not toks or _norm(toks[0]) != "rm":
        return False
    flags, targets = "", []
    for t in toks[1:]:
        n = _norm(t)
        if n.startswith("-") and not n.startswith("--"):
            flags += n[1:]
        elif n in ("--recursive", "--force"):
            flags += {"--recursive": "r", "--force": "f"}[n]
        else:
            targets.append(n)
    if not ("r" in flags and "f" in flags):
        return False
    return any(_is_danger_target(t) for t in targets)


_FORKBOMB_RE = re.compile(r":\s*\(\s*\)\s*\{.*\|.*&.*\}")   # :(){ :|:& };:
_DD_RE = re.compile(r"^dd\b.*\bof=/dev/", re.I)
_REDIR_DEV_RE = re.compile(r">\s*/dev/(sd|nvme|disk|hd|mapper)", re.I)
_OLLAMA_KILL_RE = re.compile(r"\bollama\s+(stop|rm|delete)\b", re.I)
_CURL_OLLAMA_RE = re.compile(r"\b(11434|/api/(delete|tags|ps|generate|chat))\b")


def _catastrophic(toks, seg) -> str:
    """A reason string if this segment's command is catastrophic/self-destructive, else ''."""
    if not toks:
        return ""
    c = _norm(toks[0])
    if _is_rm_bomb(toks):
        return "recursive force-delete of a system/home/wildcard path"
    if c == "mkfs" or c.startswith("mkfs."):
        return "formatting a filesystem"
    if c == "find" and any(_norm(a) in ("-delete", "-exec") for a in toks[1:]) \
            and any(_is_danger_target(a) for a in toks[1:]):
        return "find -delete/-exec over a system path"
    if _DD_RE.match(seg.strip()):
        return "dd writing to a raw device"
    if c in ("shutdown", "reboot", "halt", "poweroff"):
        return "shutting down the machine"
    if c == "init" and any(a in ("0", "6") for a in toks[1:]):
        return "changing runlevel (shutdown/reboot)"
    if _FORKBOMB_RE.search(seg):
        return "fork bomb"
    if _REDIR_DEV_RE.search(seg):
        return "writing to a raw disk device"
    if c in ("chmod", "chown", "chgrp") and any(_norm(a) in ("-R", "-r", "--recursive") for a in toks[1:]) \
            and any(_is_system_path(a) for a in toks[1:]):
        return "recursive permission/owner change on a system path"
    # Killing the model's own Ollama backend mid-task.
    if _OLLAMA_KILL_RE.search(seg):
        return "stopping/removing the Ollama backend"
    if c in ("pkill", "killall") and any("ollama" in _norm(a) for a in toks[1:]):
        return "killing the Ollama process"
    if c in ("systemctl", "service") and "ollama" in seg and re.search(r"\b(stop|kill|disable)\b", seg):
        return "stopping the Ollama service"
    if c == "curl" and _CURL_OLLAMA_RE.search(seg) and re.search(r"-X\s*DELETE|--request\s*DELETE", seg, re.I):
        return "deleting Ollama state over its API"
    return ""


def classify_command(cmd: str, _depth: int = 0):
    """(verdict, reason) where verdict is 'block' | 'allow' | 'confirm'. Fail-closed:
    anything not provably safe or catastrophic is 'confirm'."""
    if not cmd or not cmd.strip():
        return ("confirm", "")
    if _FORKBOMB_RE.search(cmd):        # spans separators, so match the whole string
        return ("block", "fork bomb")
    segs = _segments(cmd)
    for seg in segs:
        if _depth < 3:                  # `bash -c "<script>"` — classify the inner script too
            script = _interpreter_script(seg)
            if script:
                v, r = classify_command(script, _depth + 1)
                if v == "block":
                    return ("block", r)
        reason = _catastrophic(_command_tokens(seg), seg)
        if reason:
            return ("block", reason)
    # Safe-allow only a SINGLE, metachar-free, read-only probe (a newline or any separator
    # makes it multi-segment — never eligible, so a "pwd\nrm …" can't sneak through).
    if len(segs) == 1 and not any(ch in _META for ch in cmd):
        toks = _command_tokens(segs[0])
        if toks:
            c = _norm(toks[0])
            args = [_norm(a) for a in toks[1:]]
            if c in _SAFE or (args and all(a in _VERSION_FLAGS for a in args)):
                return ("allow", "")
    return ("confirm", "")


_HIGH_RISK_RE = re.compile(r"""
    \brm\s+(-\w*\s+)*-\w*[rf]      # any rm with -r or -f (destructive delete)
  | \bgit\s+push\b.*(--force|--force-with-lease|-f\b)
  | \bgit\s+reset\b.*--hard
  | \bgit\s+clean\b.*-\w*[fx]
  | \bgit\s+branch\b.*-D
  | \bsudo\b
""", re.I | re.X)


def is_high_risk(cmd: str) -> bool:
    """Destructive-but-legitimate: must re-prompt even under a session 'allow' grant."""
    return bool(cmd) and bool(_HIGH_RISK_RE.search(cmd))


_GIT_HIGH_RISK_RE = re.compile(r"""
    ^\s*(push\b.*(--force|--force-with-lease|-f\b)
       | reset\b.*--hard
       | clean\b.*-\w*[fx]
       | branch\b.*-D
       | filter-branch\b
       | update-ref\s+-d\b)
""", re.I | re.X)


def git_is_high_risk(args: str) -> bool:
    """run_git equivalent: force-push / hard-reset / clean -f / branch -D / history rewrite."""
    return bool(args) and bool(_GIT_HIGH_RISK_RE.search(args))


def escapes_root(path, root: str) -> bool:
    """True if `path` resolves (symlinks included) to a location outside `root`.
    Fail-closed: if it can't be proven inside, treat it as escaping."""
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        return os.path.commonpath([rp, rr]) != rr
    except (ValueError, OSError, TypeError):
        return True

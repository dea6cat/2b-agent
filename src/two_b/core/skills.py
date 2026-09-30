"""Skills for 2B — host-side prompt snippets with YAML frontmatter.

A skill is a directory under a skills search path containing ``SKILL.md`` — a
markdown file whose frontmatter describes the skill (name, description, when to
use, allowed-tools, arguments, etc.) and whose body is the prompt content. Skills
keep all orchestration complexity on the host: they expand to prompt text that
is injected into the conversation, never model-facing tools. This preserves the
frozen-tool-set invariant.

Load priority (mirrors Clawd-Code):
  1. $TWOB_SKILLS_DIR          (explicit override)
  2. ~/.clawd/skills           (user-level, current default)
  3. ~/.claude/skills          (TS-compatible fallback)
  4. <project>/.clawd/skills    (project-level)

Skills are also registered as /-prefixed commands so they appear in the command
palette and can be invoked directly (``/my-skill args…``), exactly like built-in
commands.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    loaded_from: str
    user_invocable: bool
    is_hidden: bool
    skill_root: Optional[str]
    allowed_tools: Sequence[str]
    arg_names: Sequence[str]
    markdown_content: str


@dataclass
class SkillRegistry:
    by_name: dict[str, Skill] = field(default_factory=dict)
    loaded_paths: list[str] = field(default_factory=list)

    def register(self, skill: Skill) -> None:
        self.by_name[skill.name] = skill

    def get(self, name: str) -> Optional[Skill]:
        return self.by_name.get(name)

    def visible(self) -> list[Skill]:
        return [s for s in self.by_name.values() if not s.is_hidden]

    def clear(self) -> None:
        self.by_name.clear()
        self.loaded_paths.clear()


_REGISTRY = SkillRegistry()


def _candidate_user_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("TWOB_SKILLS_DIR")
    if env:
        dirs.append(Path(env).expanduser().resolve())
    for name in (".clawd/skills", ".claude/skills"):
        d = Path.home() / name
        if d.exists():
            dirs.append(d.resolve())
    return dirs


def _as_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if str(x)]
    s = str(val).strip()
    if not s:
        return []
    if "," in s:
        return [x.strip() for x in s.split(",") if x.strip()]
    return [s]


def _coerce_scalar(value: str) -> Any:
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.isdigit():
        try:
            return int(value)
        except Exception:
            pass
    return value


def _split_kv(line: str) -> tuple[str, str]:
    idx = line.find(":")
    return line[:idx].strip(), line[idx + 1:].strip()


def _parse_inline_list(value: str) -> list[Any] | None:
    s = value.strip()
    if len(s) < 2 or not s.startswith("[") or not s.endswith("]"):
        return None
    inner = s[1:-1].strip()
    if not inner:
        return []
    return [_coerce_scalar(p.strip()) for p in inner.split(",") if p.strip()]


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML-like frontmatter parser.

    Supports: key: value, booleans, integers, inline [a, b] lists,
    hyphen lists, and comma-separated shorthand. Anything unrecognized
    falls back to a string.
    """
    lines = markdown.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}, markdown
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, markdown

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])
    fm: dict[str, Any] = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line.strip():
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, value = _split_kv(line)
        if value == "" and i + 1 < len(fm_lines) and fm_lines[i + 1].lstrip().startswith("- "):
            items: list[str] = []
            i += 1
            while i < len(fm_lines):
                item_line = fm_lines[i]
                if item_line.lstrip().startswith("- "):
                    items.append(item_line.lstrip()[2:].strip())
                    i += 1
                else:
                    break
            fm[key] = [_coerce_scalar(x) for x in items]
            continue
        inline = _parse_inline_list(value)
        if inline is not None:
            fm[key] = inline
            i += 1
            continue
        if "," in value:
            fm[key] = [_coerce_scalar(v.strip()) for v in value.split(",") if v.strip()]
        else:
            fm[key] = _coerce_scalar(value)
        i += 1
    return fm, body


def parse_argument_names(argument_names: Any) -> list[str]:
    if not argument_names:
        return []
    if isinstance(argument_names, list):
        names = [str(x).strip() for x in argument_names]
    else:
        names = [x.strip() for x in str(argument_names).split() if x.strip()]
    return [n for n in names if n and not re.fullmatch(r"\d+", n)]


def parse_arguments(args: str) -> list[str]:
    if not args or not args.strip():
        return []
    try:
        return shlex.split(args)
    except Exception:
        return [x for x in re.split(r"\s+", args.strip()) if x]


def substitute_arguments(
    content: str,
    args: str | None,
    *,
    argument_names: Sequence[str] = (),
) -> str:
    """Substitute named ($NAME) and positional ($N / $ARGUMENTS) placeholders."""
    if args is None:
        return content
    parsed = parse_arguments(args)
    original = content

    for idx, name in enumerate(argument_names):
        if not name:
            continue
        pattern = re.compile(rf"\${re.escape(name)}(?![\[\w])")
        content = pattern.sub(parsed[idx] if idx < len(parsed) else "", content)

    # $ARGUMENTS[N] indexed
    def _repl_indexed(m: re.Match[str]) -> str:
        i = int(m.group(1))
        return parsed[i] if i < len(parsed) else ""
    content = re.sub(r"\$ARGUMENTS\[(\d+)\]", _repl_indexed, content)

    # $N shorthand (1-indexed, not followed by a word char)
    def _repl_shorthand(m: re.Match[str]) -> str:
        i = int(m.group(1))
        return parsed[i - 1] if 0 < i <= len(parsed) else ""
    content = re.sub(r"\$(\d+)(?!\w)", _repl_shorthand, content)

    content = content.replace("$ARGUMENTS", args)

    if content == original and args:
        content = content + f"\n\nARGUMENTS: {args}"
    return content


def load_skill_from_dir(path: Path, *, loaded_from: str = "skills") -> Skill | None:
    """Load a single skill from a directory containing SKILL.md."""
    md_path = path / "SKILL.md"
    if not md_path.exists():
        return None
    try:
        raw = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = parse_frontmatter(raw)
    description = str(fm.get("description") or _extract_description(body) or f"Skill: {path.name}")
    user_invocable = bool(fm.get("user-invocable", True))
    arg_names = parse_argument_names(fm.get("arguments"))
    return Skill(
        name=path.name,
        description=description,
        loaded_from=loaded_from,
        user_invocable=user_invocable,
        is_hidden=not user_invocable,
        skill_root=str(path) if path.exists() else None,
        allowed_tools=_as_str_list(fm.get("allowed-tools")),
        arg_names=arg_names,
        markdown_content=body,
    )


def _extract_description(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        return stripped[:200]
    return None


def get_all_skills(*, project_root: str | Path | None = None) -> list[Skill]:
    """Load all visible skills from every search path, replacing the registry each call."""
    _REGISTRY.clear()
    seen_paths: set[str] = set()

    for d in _candidate_user_dirs():
        p = str(d)
        if p in seen_paths or not d.exists():
            continue
        seen_paths.add(p)
        _REGISTRY.loaded_paths.append(p)
        if d.is_dir():
            for entry in sorted(d.iterdir()):
                if entry.is_dir():
                    sk = load_skill_from_dir(entry, loaded_from="user")
                    if sk:
                        _REGISTRY.register(sk)

    if project_root is not None:
        pr = Path(project_root).expanduser().resolve()
        for sub in (".clawd/skills", ".claude/skills"):
            d = pr / sub
            p = str(d)
            if p in seen_paths or not d.exists():
                continue
            seen_paths.add(p)
            _REGISTRY.loaded_paths.append(p)
            if d.is_dir():
                for entry in sorted(d.iterdir()):
                    if entry.is_dir():
                        sk = load_skill_from_dir(entry, loaded_from="project")
                        if sk:
                            _REGISTRY.register(sk)

    return _REGISTRY.visible()


def get_skill(name: str) -> Skill | None:
    """Look up a skill by dotted/slash name (e.g. 'my-skill' or '/my-skill')."""
    if not name:
        return None
    key = name.lstrip("/")
    return _REGISTRY.get(key)



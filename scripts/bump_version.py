#!/usr/bin/env python3
"""Set or bump the 2b-agent version.

The single source of truth is ``src/two_b/__init__.py`` (``__version__``); the
release workflow (``.github/workflows/release.yml``) fails the publish if the
``v*`` git tag does NOT equal that string. This script keeps the two in sync.

Examples
--------
    python scripts/bump_version.py 2.5.0          # set an exact version + tag v2.5.0
    python scripts/bump_version.py --major        # 2.4.7 -> 2.5.0
    python scripts/bump_version.py --minor        # 2.4.7 -> 2.4.8
    python scripts/bump_version.py --patch        # 2.4.7 -> 2.4.8 (patch == minor here)
    python scripts/bump_version.py 2.5.0 --no-tag # update file only, skip the git tag

It never touches anything else (not the Homebrew formula — that is bumped
automatically by the tap-dispatch in release.yml).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "two_b" / "__init__.py"
VERSION_RE = re.compile(r'^__version__\s*=\s*"(?P<ver>\d+\.\d+\.\d+)"', re.M)


def read_version() -> str:
    text = INIT.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit(f"could not find __version__ in {INIT}")
    return m.group("ver")


def write_version(ver: str) -> None:
    text = INIT.read_text(encoding="utf-8")
    if not VERSION_RE.search(text):
        raise SystemExit(f"could not find __version__ in {INIT}")
    INIT.write_text(VERSION_RE.sub(f'__version__ = "{ver}"', text, count=1), encoding="utf-8")


def bump(ver: str, part: str) -> str:
    major, minor, patch = (int(x) for x in ver.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part in ("minor", "patch"):
        minor, patch = minor + 1, 0
    else:
        raise SystemExit(f"unknown part {part!r}")
    return f"{major}.{minor}.{patch}"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Set or bump the 2b-agent version.")
    ap.add_argument("version", nargs="?", help="exact version, e.g. 2.5.0")
    ap.add_argument("--major", action="store_true")
    ap.add_argument("--minor", action="store_true")
    ap.add_argument("--patch", action="store_true")
    ap.add_argument("--no-tag", action="store_true", help="update the file but do not git-tag")
    args = ap.parse_args()

    current = read_version()
    print(f"current version: {current}")

    how = sum(bool(x) for x in (args.version, args.major, args.minor, args.patch))
    if how == 0:
        ap.error("provide a version, or one of --major/--minor/--patch")
    if how > 1:
        ap.error("specify only one of: version, --major, --minor, --patch")

    if args.version:
        new = args.version
        if not re.fullmatch(r"\d+\.\d+\.\d+", new):
            ap.error(f"version must be MAJOR.MINOR.PATCH, got {new!r}")
    else:
        part = "major" if args.major else "minor"  # patch == minor increment
        new = bump(current, part)

    if new == current:
        print("already at that version — nothing to do.")
        return

    write_version(new)
    print(f"updated {INIT.name}: {current} -> {new}")

    if args.no_tag:
        print("skipped git tag (--no-tag).")
        return

    try:
        existing = git("tag", "--list", f"v{new}")
        if existing:
            print(f"tag v{new} already exists — leaving it as-is.")
        else:
            git("tag", f"v{new}")
            print(f"created git tag v{new}  (push with: git push origin v{new})")
    except subprocess.CalledProcessError as e:
        print(f"warning: could not create git tag: {e.stderr.strip()}", file=sys.stderr)


if __name__ == "__main__":
    main()

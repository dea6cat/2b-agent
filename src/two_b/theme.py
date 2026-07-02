"""Color themes for the full-screen TUI.

A theme is a flat map of semantic role -> color. The same map drives two paths:

  - Textual CSS, via `get_css_variables()` (each role exposed as `$tb-<role>`);
    this colors widget backgrounds/borders and the default text color.
  - Rich `Text`/markup built in Python (tool actions, plan, banner, palette),
    via `App.c(role)`.

`system` uses `transparent` backgrounds so the user's own terminal background
shows through (its text colors are tuned for the common dark terminal); `light`
is the original YoRHa parchment palette; `dark` is a dimmed olive-charcoal.
"""
from __future__ import annotations

# Semantic roles used across CSS and rich rendering.
ROLES = ("ground", "logbg", "panelbg", "ink", "accent", "dim", "faint", "ok", "err")

THEMES: dict[str, dict[str, str]] = {
    # Terminal-native: no background painting; colors chosen to read on a dark
    # terminal (the common case). Users on a light terminal should pick `light`.
    "system": {
        "ground": "transparent", "logbg": "transparent", "panelbg": "transparent",
        "ink": "#C8C3B4", "accent": "#B69A5E", "dim": "#8A8578",
        "faint": "#6E6A58", "ok": "#8FA06A", "err": "#C6704A",
    },
    # Original YoRHa menu palette: parchment ground, dark-olive ink, bronze accent.
    "light": {
        "ground": "#D3CDBB", "logbg": "#CDC7B4", "panelbg": "#C7C1AE",
        "ink": "#454235", "accent": "#8A7A45", "dim": "#6E6A58",
        "faint": "#928D79", "ok": "#6F7550", "err": "#9E5238",
    },
    # Dimmed, darker take on the same palette.
    "dark": {
        "ground": "#21201A", "logbg": "#1B1A15", "panelbg": "#2A281F",
        "ink": "#D3CDBB", "accent": "#B69A5E", "dim": "#8A8578",
        "faint": "#6E6A58", "ok": "#8FA06A", "err": "#C6704A",
    },
}

DEFAULT_THEME = "system"


def colors(name: str) -> dict[str, str]:
    """The role->color map for a theme, falling back to the default."""
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def css_variables(name: str) -> dict[str, str]:
    """Theme colors as Textual CSS variables (`tb-<role>`)."""
    return {f"tb-{role}": value for role, value in colors(name).items()}

"""First-run license acknowledgment gate.

2B is source-available under PolyForm Noncommercial 1.0.0 (noncommercial use only;
commercial use needs a separate license). This gate makes the user acknowledge that
once — brew, curl, and pip all funnel into running the tool, so a first-run gate in
the tool covers every channel. Acceptance is persisted in prefs.json and never
re-prompted unless the license id changes or config is wiped. This is assent + notice,
not technical prevention: the source is public and this code can be edited out.
"""
from __future__ import annotations

from typing import Callable

from . import config

LICENSE_ID = "PolyForm-Noncommercial-1.0.0"
LICENSE_URL = "https://polyformproject.org/licenses/noncommercial/1.0.0"
CONTACT_URL = "https://github.com/dea6cat/2b-agent"

_NOTICE = (
    "2B is licensed under the PolyForm Noncommercial License 1.0.0.\n"
    "Free for noncommercial use (personal, hobby, research, education, nonprofits, government).\n"
    f"Commercial use requires a separate license — open an issue at {CONTACT_URL} to arrange one.\n"
    f"Full terms: LICENSE / {LICENSE_URL}"
)
_PROMPT = "Use 2B under this license?  [y] accept · [n] uninstall 2B · [Enter] cancel: "


def accepted() -> bool:
    """True if this exact license version has already been acknowledged and persisted."""
    return config.get_prefs().get("license_accepted") == LICENSE_ID


def record() -> None:
    """Persist acceptance of the current license id."""
    config.set_pref("license_accepted", LICENSE_ID)


def ensure_accepted(*, assume_yes: bool, interactive: bool,
                    out: Callable[[str], None] = print,
                    on_decline: Callable[[], None] | None = None) -> bool:
    """Validate the license gate. Returns True if the user may proceed, False otherwise.

    Checked on every run: once accepted it returns True silently, so it never nags. When
    acceptance is missing it prompts:
      - 'y'/'yes'      -> record acceptance, proceed.
      - 'n'/'no'       -> explicit decline: call `on_decline` (which uninstalls 2B), stop.
      - Enter/anything -> cancel: do nothing destructive, stop, ask again next run.
    `assume_yes` (the --yes flag, which install.sh passes) counts as acceptance. A
    non-interactive run without --yes cannot consent, so it stops with a hint — and never
    uninstalls (a stray CI run must not remove the tool)."""
    if accepted():
        return True
    out(_NOTICE)
    if assume_yes:
        record()
        return True
    if interactive:
        try:
            ans = input(_PROMPT).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes"):
            record()
            return True
        if ans in ("n", "no"):
            out("License declined — removing 2B.")
            if on_decline is not None:
                on_decline()          # uninstall; may exit the process
            return False
        out("Not accepted — exiting without changes. You'll be asked again next run.")
        return False
    out("Non-interactive run: pass --yes to accept the license (see LICENSE), then re-run.")
    return False

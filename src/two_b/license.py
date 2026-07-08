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
_PROMPT = "Accept the PolyForm Noncommercial license and use 2B? [y/N] "


def accepted() -> bool:
    """True if this exact license version has already been acknowledged and persisted."""
    return config.get_prefs().get("license_accepted") == LICENSE_ID


def record() -> None:
    """Persist acceptance of the current license id."""
    config.set_pref("license_accepted", LICENSE_ID)


def ensure_accepted(*, assume_yes: bool, interactive: bool,
                    out: Callable[[str], None] = print) -> bool:
    """Return True if the user may proceed, False if they must be stopped.

    Prompts only on the first run per license version. `assume_yes` (the --yes flag,
    which install.sh also passes) counts as acceptance. Interactive runs prompt with a
    default of No — the user must type 'y'. A non-interactive run without --yes cannot
    consent, so it's blocked with a hint."""
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
        out("License not accepted — 2B will not run.")
        return False
    out("Non-interactive run: pass --yes to accept the license (see LICENSE), then re-run.")
    return False

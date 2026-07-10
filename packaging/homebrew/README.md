# Homebrew tap for 2B

`Formula/twob-agent.rb` is the Homebrew formula for the [`2b-agent`](https://pypi.org/project/2b-agent/)
PyPI package. It's kept here (versioned with the project); the live tap is a separate GitHub repo.

The formula is named **`twob-agent`**, not `2b-agent`: Homebrew derives a Ruby class name from the
formula name, and a name starting with a digit (`2b…`) yields an invalid class. `twob-agent` reads
as "2b-agent", is a valid class (`TwobAgent`), and still installs the **`2b`** command. Homebrew
puts `2b` on the user's PATH automatically (symlinked into `<brew-prefix>/bin`).

## Install (for users)

```bash
brew install dea6cat/2b/twob-agent   # installs the `2b` command
2b setup                             # first-time local-model onboarding
```

or tap first: `brew tap dea6cat/2b && brew install twob-agent`.

## Publishing the tap (one-time — DONE)

The tap is live at <https://github.com/dea6cat/homebrew-2b>, so
`brew install dea6cat/2b/twob-agent` works for anyone. Kept here for reference —
a Homebrew tap is just a GitHub repo named `homebrew-<name>`; for `dea6cat/2b` it was
stood up from this formula:

```bash
mkdir -p homebrew-2b/Formula
cp packaging/homebrew/Formula/twob-agent.rb homebrew-2b/Formula/
cd homebrew-2b
git init -b main && git add . && git commit -m "twob-agent <version>"
gh repo create dea6cat/homebrew-2b --public --source=. --remote=origin --push
```

## Updating on a new release

1. Bump `url` + `sha256` to the new PyPI sdist (`pip download 2b-agent==<v> --no-binary :all:`
   or the PyPI JSON gives the sha256).
2. Regenerate the resource stanzas so the dependency tree matches the new release. The canonical
   tool is `brew update-python-resources Formula/twob-agent.rb`; if it fails because the version
   was just published (its `--uploaded-prior-to` snapshot excludes brand-new uploads), regenerate
   from PyPI directly — resolve the tree with `pip install --dry-run --report` and emit one
   `resource` block (sdist url + sha256) per resolved package.
3. `brew style --fix` then `brew audit --formula --tap=dea6cat/2b twob-agent`, and test-install.

## Notes / caveats

- **Heavy build.** `mcp` (a hard dependency) pulls in `cryptography`, `pydantic-core`, and
  `rpds-py`, which compile from source — so the formula needs `rust` + `openssl@3` and drags in a
  Rust/LLVM toolchain (~2 GB) at install time (verified: ~4.5 min build). Making `mcp` an optional
  extra in `pyproject.toml` would drop the formula to three pure-Python resources and no toolchain.
- **Install-method detection.** 2B's `--update` / `--rm` / PATH-fix classify the install (uv / pipx
  / brew / pip). The `brew` case landed after `1.1.1`; now that the formula is pinned to `2.4.6`
  (which carries the `brew` case) a brew install correctly self-classifies as `brew` at runtime, so
  `--update` runs `brew upgrade`. Keep the formula at a release ≥ that commit on future bumps.
- Verified locally: `brew style` + `brew audit` clean, source build succeeds, `2b --version` runs
  on the brew-provided Python 3.14.

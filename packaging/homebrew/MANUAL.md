# 2B Homebrew Distribution — Manual

Operations manual for shipping 2B through Homebrew and keeping the tap in sync with PyPI.
For end-user install instructions see `README.md`; this file is the maintainer runbook.

---

## 1. Mental model

A 2B version lives in **three** places. Nothing propagates between them on its own except the
one automation this manual describes.

```
  ┌─────────────────────────┐   tag v2.4.7      ┌──────────┐
  │  dea6cat/2b-agent        │ ───────────────▶  │  PyPI    │
  │  (code + formula source) │   release.yml     │ 2b-agent │
  │  __version__, formula.rb │   → uv publish    └────┬─────┘
  └───────────┬─────────────┘                        │ sdist url + sha256,
              │ release.yml: dispatch-tap job          │ full dependency tree
              │ (repository_dispatch "formula-bump")   │
              ▼                                         │
  ┌─────────────────────────┐                          │
  │  dea6cat/homebrew-2b     │  bump.yml ── regen_formula.py reads ◀┘
  │  (the tap brew clones)   │  → brew style + audit → commit to main
  │  Formula/twob-agent.rb   │
  └───────────┬─────────────┘
              │ brew update / brew upgrade / `2b --update`
              ▼
          end users
```

- **Code repo** (`dea6cat/2b-agent`) — holds `__version__` and the *source-of-truth* copy of the
  formula at `packaging/homebrew/Formula/twob-agent.rb` (reference/versioning only; brew never
  reads this copy).
- **PyPI** (`2b-agent`) — the formula's `url`/`sha256` point at PyPI sdists, so a release must be
  published here first.
- **Tap repo** (`dea6cat/homebrew-2b`) — a *separate* GitHub repo that `brew` actually clones. Its
  `Formula/twob-agent.rb` is what users install. **This is the copy that must track releases.**

Once the tap repo is updated, the user side is automatic: `brew update` fetches the tap, `brew
upgrade` installs the newer version, and 2B's own `--update` runs `brew upgrade`.

---

## 2. Components

| File | Repo | Purpose |
|------|------|---------|
| `packaging/homebrew/Formula/twob-agent.rb` | 2b-agent | Source-of-truth formula (reference copy). |
| `.github/workflows/release.yml` → `dispatch-tap` job | 2b-agent | After PyPI publish, fires a `formula-bump` `repository_dispatch` at the tap with the version. |
| `Formula/twob-agent.rb` | homebrew-2b | The live formula `brew` installs. |
| `scripts/regen_formula.py` | homebrew-2b | Regenerates the formula for a version (top-level sdist + resource stanzas). |
| `.github/workflows/bump.yml` | homebrew-2b | Orchestrates regen → validate → commit. |

**Why the formula is named `twob-agent`, not `2b-agent`:** Homebrew derives a Ruby class name from
the formula name, and a name starting with a digit is invalid. `twob-agent` → class `TwobAgent`,
and it still installs the `2b` command (Homebrew symlinks it onto PATH).

### What `regen_formula.py` does

`python3 scripts/regen_formula.py <version> [formula_path]`

1. Fetches the sdist `url` + `sha256` for the exact version from the PyPI JSON API, retrying until
   the file is downloadable (publish → availability can lag a minute or two).
2. Resolves the full dependency tree as sdists via `pip install --dry-run --no-binary :all: --report`.
3. **Splices** only the top-level `url`/`sha256` and the `resource` blocks into the existing
   formula. Every other line (class, `desc`, `license`, `depends_on`, `install`, `test`) is left
   byte-identical. Existing resource display names and their order are preserved, so an unchanged
   dependency set reproduces the file exactly; new deps are appended, dropped deps removed.
4. Prints `CHANGED` or `UNCHANGED`; exit 0 on success.

### What `bump.yml` does

Triggers:
- `repository_dispatch` type `formula-bump` — **primary**, version from `client_payload.version`.
- `workflow_dispatch` — manual button; optional `version` input (blank = latest on PyPI).
- `schedule` (weekly, Mon 06:17 UTC) — **safety net** if a dispatch is ever dropped.

Job (ubuntu): resolve target version → if it differs from the formula's current version, run
`regen_formula.py` → if the file actually changed, symlink the workspace as the tap and run
`brew style` + `brew audit` → **on success** commit to `main` as `github-actions[bot]`; **on
failure** open an issue and commit nothing.

---

## 3. One-time setup

The tap is already published at <https://github.com/dea6cat/homebrew-2b>, so
`brew install dea6cat/2b/twob-agent` works today. The only remaining bootstrap is the cross-repo
token that lets `release.yml` reach the tap.

**`TAP_DISPATCH_TOKEN`** — the default `GITHUB_TOKEN` cannot dispatch to another repo, so store a
dedicated token as a secret on the code repo:

1. Create a fine-grained PAT: <https://github.com/settings/personal-access-tokens/new>
   - **Resource owner:** `dea6cat`
   - **Repository access:** Only select repositories → `homebrew-2b`
   - **Permissions:** Repository permissions → **Contents → Read and write** (Metadata read is
     included automatically).
2. Store it. `TAP_DISPATCH_TOKEN` is the secret **name** (must match `secrets.TAP_DISPATCH_TOKEN`
   in `release.yml`) — do not change it; only the token *value* differs.
   ```bash
   # Prompts for the value — keeps the token out of shell history / chat (preferred):
   gh secret set TAP_DISPATCH_TOKEN --repo dea6cat/2b-agent

   # One-liner (records the token in shell history — use only if that's acceptable):
   gh secret set TAP_DISPATCH_TOKEN --repo dea6cat/2b-agent --body "github_pat_xxx"
   ```

Until this is set, releases still publish fine — the `dispatch-tap` job just fails harmlessly and
the tap's weekly cron picks up the release within a week.

---

## 4. Normal release flow (fully automatic)

1. Bump `__version__` in `src/two_b/__init__.py`.
2. Tag and push: `git tag v2.4.7 && git push origin v2.4.7`.
3. `release.yml` guards that the tag matches `__version__`, then `uv build` + `uv publish` to PyPI.
4. `dispatch-tap` fires `formula-bump` at the tap with `version=2.4.7`.
5. The tap's `bump.yml` regenerates + validates the formula and commits it to the tap's `main`.
6. Users get it on their next `brew update` (or `2b --update` → `brew upgrade`).

**Keep `packaging/homebrew/Formula/twob-agent.rb` (the reference copy) in step** by regenerating it
in the code repo too, so the source-of-truth doesn't drift from what the tap ships. It doesn't
affect users, but it's what a fresh tap would be stood up from.

---

## 5. Manual operations

**Trigger a bump by hand** (e.g. token not yet set, or re-run a failed bump):
- GitHub UI: tap repo → **Actions → bump-formula → Run workflow** (optionally enter a version), or
  ```bash
  gh workflow run bump-formula --repo dea6cat/homebrew-2b -f version=2.4.7
  ```

**Regenerate the formula locally:**
```bash
cd <clone of homebrew-2b>
python3 scripts/regen_formula.py 2.4.7
```
> On macOS, if the PyPI fetch fails with `CERTIFICATE_VERIFY_FAILED`, point Python at a CA bundle:
> `export SSL_CERT_FILE="$(python3 -c 'import certifi;print(certifi.where())')"` (this does not
> affect the ubuntu CI runner).

**Validate:**
```bash
brew style dea6cat/2b/twob-agent
brew audit --tap=dea6cat/2b twob-agent
```

**Full test-install from source** (heavy: pulls `rust`, compiles native deps, ~5 min):
```bash
brew install --build-from-source dea6cat/2b/twob-agent
2b --version                       # expect the new version
brew uninstall twob-agent          # clean up
```

---

## 6. When the formula's *static* structure changes

`regen_formula.py` only rewrites the top-level `url`/`sha256` and the `resource` blocks. If a
release changes anything else — a new `depends_on`, different `install`/`test` logic, a new
build dependency — **edit `Formula/twob-agent.rb` in the tap by hand** (and mirror it in the code
repo's reference copy). The auto-bump will not touch those lines and would otherwise ship a stale
structure.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Release published but brew users still on the old version | Tap not bumped. Check the tap's **Actions** tab. If `dispatch-tap` failed, the token is likely missing/expired (§3); trigger a manual bump (§5) meanwhile. |
| `dispatch-tap` job red in `release.yml` | `TAP_DISPATCH_TOKEN` missing, expired, or lacks **Contents: write** on `homebrew-2b`. Re-mint and re-set (§3). Publish already succeeded; safe to re-run just this job. |
| Auto-bump opened an issue instead of committing | `brew style`/`audit` (or regen) failed. Read the linked run, fix `regen_formula.py` or hand-edit the formula, push. Nothing was committed. |
| `brew` reports the install as `pip` in `--update`/`--rm` | The formula predates the commit that added the `brew` case to `update._install_kind`. Pin the formula to a release ≥ that commit (2.4.6+). |
| Local regen: `CERTIFICATE_VERIFY_FAILED` | macOS Python cert path; set `SSL_CERT_FILE` (§5). Not a CI issue. |
| First `brew install` is very slow (~5 min) | Expected. `mcp` pulls `cryptography`/`pydantic-core`/`rpds-py`, which compile from source and need `rust` + `openssl@3`. Making `mcp` an optional extra would collapse this to pure-Python resources. |

---

## 8. Reference

- **Tap install:** `brew install dea6cat/2b/twob-agent` (installs the `2b` command).
- **Repos:** code `dea6cat/2b-agent` · tap `dea6cat/homebrew-2b` · package `pypi.org/project/2b-agent`.
- **Commit identity:** author commits as `notalexander24@gmail.com` (the `dea6cat` account);
  automated tap bumps commit as `github-actions[bot]`.
- **Formula source-of-truth:** `packaging/homebrew/Formula/twob-agent.rb` (this repo).
- **Bump script / workflow:** `scripts/regen_formula.py`, `.github/workflows/bump.yml` (tap repo).

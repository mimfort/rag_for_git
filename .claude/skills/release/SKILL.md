---
name: release
description: Prepare a release of rag_for_git — write human-readable Russian release notes into CHANGELOG.md, bump the version and rebuild plugin manifests. Use when the user asks to cut a release, bump the version, or prepare a dev → main merge ("подготовь релиз", "выпусти версию", "бампни версию", "release notes").
---

# Release

Reply in Russian. This skill is maintainer-only tooling for this repository — it is deliberately
NOT part of `plugin/`, so it never ships to plugin users and never changes codex manifests digests.

Run it on `dev`, before opening the `dev → main` PR. CI does the rest: on push to `main`
`.github/workflows/publish.yml` publishes to PyPI, then the `release` job cuts tag `vX.Y.Z` and
creates a GitHub Release whose body is the `CHANGELOG.md` section for that version.

## 1. Determine the range

```bash
git fetch --tags origin
git describe --tags --abbrev=0 --match 'v*'   # last released version
git log --no-merges --pretty='%h %s' v<last>..HEAD
git diff --stat v<last>..HEAD
```

If there are no commits since the last tag, say so and stop — there is nothing to release.

## 2. Gather what actually changed

Commit subjects alone are NOT enough. For each cluster of related commits, find out what the user
gains or stops suffering from:

- `gh pr list --state merged --base dev --limit 30 --json number,title,body,mergedAt` — PR bodies
  usually carry the "why".
- Task keys in commit messages (`PRI-NNN`) — read the task via `get_task` if the reviewer MCP
  server is available.
- `docs/superpowers/specs/` and `docs/plans/` — briefs written when the work started.
- Read the diff of anything you cannot explain. Never guess at a change's purpose.

## 3. Decide the version

Semver against the previous release: breaking change to a CLI flag, MCP tool contract, `.review.yml`
schema or stored data → minor while pre-1.0 (and call it out); new capability → minor; fixes and
internals only → patch. Propose the number and let the user override it.

## 4. Write the notes

Into `CHANGELOG.md`, directly under the intro block, in Russian, following the existing sections:

```
## X.Y.Z — YYYY-MM-DD

### Что нового
### Изменено
### Исправлено
### Ломающие изменения
```

Omit empty rubrics. Rules that make the notes worth reading:

- **Group by meaning, not by commit** — but grouping means one *heading* per feature, not one
  *sentence*. Fifteen commits that built one feature become one bullet with sub-bullets, never a
  single line that mentions the feature exists.
- **Never swallow an enumerable surface.** If the change introduces named things the user will
  choose between or type — modes, strategies, flags, commands, config keys, statuses — every name
  goes into the notes, each with what it does and when to pick it. "Added a start-up prompt for
  mode and strategy" is a failed note: the reader still does not know that `full-auto` and `lite`
  exist. This is the single most common way these notes go wrong; check for it before publishing.
- **Lead with the capability, not the module.** "Поднять инфраструктуру одной командой" beats
  "feat(cli): команды reviewer start и reviewer stop".
- **Say what it changes for the reader.** A fix names the symptom that disappears; a feature names
  the thing that was previously impossible or manual. State the cost or caveat where one exists —
  when a mode is a bad idea, say so in the note rather than letting the reader discover it.
- **Read the spec, not just the commits.** A feature built from a `docs/superpowers/specs/` design
  has its full surface listed there; commit subjects show only the construction order. If a spec
  exists and you did not open it, you are guessing at what shipped.
- **Skip pure noise** — manifest rebuilds, `uv.lock`, version bumps, internal test fixups — unless
  it changes something observable.
- **Breaking changes get their own rubric** with the exact migration step.
- Length follows the release: one bullet per user-visible change, sub-bullets for its named parts.
  A release with three features and a rich one among them runs long, and that is correct — the
  budget is "no filler", not "no detail". If there is genuinely little to say, say it plainly
  instead of padding.

Before showing the draft, re-read it against the diff and ask: could a reader who only saw these
notes use every new thing this release added? If a name appears in the code but not in the notes,
put it back.

## 5. Show the draft and STOP

Print the section in chat and wait for explicit approval. The user is the editor; this is the only
review the notes get before they are public.

## 6. Apply, after approval

1. `pyproject.toml` → `project.version`
2. `plugin/.claude-plugin/plugin.json` → `version` (must match)
3. `python scripts/update_codex_plugin_manifest.py` — required whenever the version or anything
   under `plugin/` changed, otherwise install tests go red
4. `CHANGELOG.md` — the approved section
5. Verify: `.venv/bin/pytest -q` and `.venv/bin/python scripts/changelog_section.py X.Y.Z`
   (it must print exactly the intended body)
6. Commit: `chore(release): X.Y.Z` — no self-attribution

Do not push tags or create Releases by hand: the tag is CI's job, and a hand-made tag makes the
`release` job skip the Release silently.

## 7. Ship it to main

The release commit must first reach `dev` (the default branch), then `main`:

1. If you are on a feature branch, open a PR into `dev`, wait for checks, merge it. If the release
   commit is already on `dev`, skip to the next step.
2. Open the release PR: `gh pr create --base main --head dev --title "release: X.Y.Z"` with a body
   that is the CHANGELOG section.
3. `gh pr checks <N> --watch` — merge only on green.
4. Merge it. **Use the keyring token for every write to a protected branch and for releases**:
   `env -u GITHUB_TOKEN -u GH_TOKEN gh pr merge <N> --merge`. The fine-grained PAT in `GITHUB_TOKEN`
   answers `403 Resource not accessible by personal access token`.
5. Watch the deploy: `gh run watch` on the `Publish to PyPI` run. The `release` job runs after
   `publish` and produces tag `vX.Y.Z` plus the GitHub Release.
6. Verify and report the real result: `gh release view vX.Y.Z` and the PyPI version. If the
   `release` job logged a `::warning::` about a missing CHANGELOG section, the Release body fell
   back to a commit list — fix the section and edit the Release.

Never claim the release shipped before step 6 shows it. `publish` is skipped without complaint when
the version already exists on PyPI (`skip-existing: true`), so a merge without a version bump looks
green and ships nothing.

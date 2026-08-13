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

- **Group by meaning, not by commit.** Fifteen commits that built one feature are one bullet.
- **Lead with the capability, not the module.** "Поднять инфраструктуру одной командой" beats
  "feat(cli): команды reviewer start и reviewer stop".
- **Say what it changes for the reader.** A fix names the symptom that disappears; a feature names
  the thing that was previously impossible or manual.
- **Skip pure noise** — manifest rebuilds, `uv.lock`, version bumps, internal test fixups — unless
  it changes something observable.
- **Breaking changes get their own rubric** with the exact migration step.
- 2–6 bullets for a normal release. If you cannot fill that, the release is small — say so plainly
  instead of padding.

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

Then tell the user to open the `dev → main` PR. Do not push tags or create Releases by hand: the
tag is CI's job, and a hand-made tag makes the `release` job skip the Release silently.

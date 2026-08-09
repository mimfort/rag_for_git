# Unified Update Command Design

## Goal

Make `reviewer update` the normal one-command lifecycle for an existing
rag-reviewer installation. One invocation updates the persistent CLI package when
applicable, refreshes every detected AI-client integration and its skills, and
synchronizes the canonical Compose file without overwriting user changes.

The same command must remain useful when no newer Python package exists: plugins,
file-based skills, and the Compose file can change independently on the canonical
`main` branch.

## Chosen Approach

Use a two-phase update driven by the latest available CLI process.

1. The outer `reviewer update` identifies the installation mode and updates a
   persistent `uv tool` installation when PyPI has a newer version.
2. After a successful package phase, a fresh CLI process performs artifact refresh.
   This prevents the already-running Python process from continuing with modules
   loaded from the pre-upgrade package.
3. The artifact phase reuses the existing `reviewer install --all` lifecycle for
   detected clients and synchronizes the Compose file from its canonical HTTPS
   source.

For the release that introduces this lifecycle, a user still running the old CLI
may need one bootstrap invocation through latest uvx:

```bash
uvx --refresh --from rag-reviewer@latest reviewer update --upgrade-tool
```

After that transition, the short command is sufficient:

```bash
reviewer update
```

### Alternatives Rejected

- Continue in the original process after `uv tool upgrade`: unsafe because imports
  and command behavior can remain from the old version while files on disk have
  already changed.
- Document `reviewer update` plus separate `install --all`, `install-skills`, and
  `curl` commands: preserves current implementation but does not meet the
  one-command goal.
- Always replace the local Compose file: simplest implementation, but destroys
  legitimate local port, image, profile, or service changes.

## Package and Process Lifecycle

`reviewer update` keeps the current installation-mode distinctions:

- `uv tool`: check PyPI, run `uv tool upgrade rag-reviewer` only when a newer valid
  version is known, then launch the artifact phase with a fresh executable;
- `uvx`: the package is temporary, so do not mutate an unrelated persistent tool;
  refresh artifacts with the currently invoked latest package and explain that
  future CLI invocations should use `@latest`. The explicit `--upgrade-tool`
  bootstrap flag is the sole exception: it upgrades an existing persistent
  rag-reviewer tool, then runs artifact refresh from that updated installation;
- editable checkout: never run `git pull` or replace developer source; refresh
  artifacts from the current checkout and print the existing source-update hint.

The internal artifact-phase switch is hidden from normal help and prevents
recursion. It is an implementation detail, not a second user-facing workflow.

The first 0.4.3-to-new-lifecycle transition cannot be retrofitted into the already
published 0.4.3 process. README therefore gives the latest-uvx bootstrap command
for that transition and documents plain `reviewer update` as the steady-state
command.

## Integration Refresh

Artifact refresh delegates to the existing installation contract rather than
creating a second installer:

- detect user-scope config clients through `CLIENTS` and public client CLI
  availability;
- run native Claude Code and Codex marketplace/plugin lifecycles;
- rewrite generic MCP entries idempotently while preserving unrelated config and
  existing backup behavior;
- download the skills archive once and refresh every detected file-skills client;
- skip the integration step successfully when no supported client is detected.

Current installer safety remains authoritative. Native lifecycle errors are
collected, generic config writes retain backups, and a failed skills download is
reported explicitly without undoing an already-correct MCP entry.

## Managed Compose File

The canonical target is
`$XDG_CONFIG_HOME/rag-reviewer/docker-compose.yml`, falling back to
`~/.config/rag-reviewer/docker-compose.yml`. The artifact phase downloads the
canonical file from the repository's `main` branch over HTTPS, so users no longer
need a manual `curl` step.

A sidecar state file in the same config directory stores the SHA-256 hash of the
last content written or adopted by reviewer. Synchronization follows these rules:

1. Missing target: write the downloaded file and its hash.
2. Target exactly matches downloaded content: leave it unchanged and write or
   repair the sidecar hash. This safely adopts an existing manual download.
3. Target hash matches the sidecar hash: reviewer owns an unchanged file, so
   atomically replace it with the new content and update the sidecar.
4. Target differs from both the downloaded content and the sidecar hash, or has no
   trustworthy sidecar: treat it as user-modified, do not overwrite it, and print
   an actionable warning.

The command does not run `docker compose down`, remove volumes, pull images, or
restart services. Compose applies image and service changes only when the operator
next runs the documented `docker compose ... up -d`; existing databases, indexes,
task data, and subsystem summaries remain untouched.

## Errors and Exit Status

- Package upgrade failure stops before artifact refresh and exits non-zero.
- Compose download/write failure does not prevent independent client refresh, but
  the final command exits non-zero and names the failed phase.
- One native client failure does not prevent other detected clients from being
  attempted; the final result remains non-zero.
- A deliberately preserved modified Compose file is a warning, not an execution
  failure.
- The command never prints a blanket success line when a required phase failed.

Every successful or skipped phase emits a concise status so users can see whether
the package, Compose file, MCP entries, and skills were changed or already current.

## Documentation

`README.md` and `README.ru.md` remain structurally parallel. Their quick-start and
team routes stop asking users to download Compose manually. The update section
documents:

- the one-time latest-uvx bootstrap for users upgrading from 0.4.3;
- steady-state `reviewer update` behavior;
- automatic detected-client/plugin/skills refresh;
- managed Compose ownership and the no-clobber warning;
- the required New Chat/new CLI session or IDE Reload Window;
- the fact that services and persistent Docker volumes are not restarted or
  deleted by update.

Launcher metadata must describe the new mutating lifecycle rather than the old
"check only" wording.

## Tests and Acceptance Criteria

Unit tests cover:

- package upgrade success/failure and fresh-process dispatch;
- uvx and editable behavior without unintended persistent-tool mutation;
- artifact refresh with no clients, generic clients, and native installer errors;
- Compose create, adopt, no-op, managed update, modified-file preservation,
  download failure, and sidecar consistency;
- README parity and absence of manual Compose download commands in onboarding;
- launcher metadata matching the command's effects.

Final verification runs the focused tests first, then the full non-integration
suite, Ruff, and package build/install smoke tests. The release is delivered
through `dev` and then `main`; a patch version is published so the new lifecycle is
actually available to the latest-uvx bootstrap command.

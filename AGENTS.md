# rag_for_git — Codex CLI context

See `CLAUDE.md` for the full architecture, commands, and invariants.

## Install or update rag-reviewer for Codex

```bash
uvx --from rag-reviewer@latest reviewer install codex
uvx --from rag-reviewer@latest reviewer install codex --dry-run
uvx --from rag-reviewer@latest reviewer install codex --no-skills
uvx --from rag-reviewer@latest reviewer install-skills codex
```

The first command manages one global `reviewer` MCP server and the namespaced
`rag-reviewer` plugin. Re-running it upgrades the marketplace/plugin. Dry-run is
read-only and offline; `install-skills codex` leaves MCP untouched.

Verify the result:

```bash
codex plugin list --json
codex mcp list
```

Success means `rag-reviewer` is installed and enabled and `codex mcp list` contains exactly one
`reviewer`. Identified legacy skills are moved to
`$CODEX_HOME/reviewer-legacy-backups/<timestamp>`; modified or ambiguous copies stay untouched.
Failures print the config backup path. Open a New Chat/new CLI session after installation; in an
IDE, also use Reload Window.

## Companion global install for Claude Code

```bash
uvx --from rag-reviewer@latest reviewer install claude-code
uvx --from rag-reviewer@latest reviewer install claude-code --no-skills
claude plugin list --json
claude plugin marketplace list --json
```

The default command manages the enabled user-scope `rag-reviewer` plugin from
`https://github.com/mimfort/rag_for_git.git`, so it works from every project and current
directory. `--no-skills` installs only the global MCP server. Verify that `plugin list` contains
the enabled `rag-reviewer@rag-reviewer-marketplace` entry with user scope; the optional
marketplace listing should show the exact HTTPS source. Open a New Chat/new CLI session after
installation; in an IDE, also use Reload Window.

## Skills

Every directory under `plugin/skills/` containing `SKILL.md` is registered in the
`rag-reviewer` namespace. `_common` and nested references are delivered as support files, not
registered skills.

Available skills include:

- `rag-reviewer:reviewer_review-pr`
- `rag-reviewer:reviewer_solve-task`
- `rag-reviewer:reviewer_sync-codebase`
- `rag-reviewer:reviewer_sync-tasks`
- `rag-reviewer:reviewer_performance-review`
- `rag-reviewer:reviewer_maintainability-review`
- `rag-reviewer:reviewer_ask`
- `rag-reviewer:reviewer_pr-walkthrough`
- `rag-reviewer:reviewer_configure-review`
- `rag-reviewer:reviewer_summarize-subsystems`
- `rag-reviewer:reviewer_finish-task`

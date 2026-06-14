# rag_for_git — Codex CLI context

See `CLAUDE.md` for full project documentation (architecture, commands, invariants).

## MCP server setup

### Global config (recommended)

Add the reviewer MCP server to your Codex CLI config at `~/.codex/config.toml`:

```toml
[mcp_servers.reviewer]
command = "/bin/bash"
args = ["-lc", "uvx --from rag-reviewer reviewer-mcp"]
```

### Project-level config

The `.codex-plugin/plugin.json` in this repo points to the `reviewer-mcp` command and
the skills in `plugin/skills/`. Codex CLI picks this up automatically when you open
this project.

## Skills

Codex CLI loads skills from `.codex-plugin/plugin.json` (project-level). To install globally —
no repo clone needed:

```bash
curl -sL https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz -o /tmp/rag-reviewer.tgz
mkdir -p ~/.codex/skills
tar xz -C ~/.codex/skills --strip-components=3 -f /tmp/rag-reviewer.tgz 'rag_for_git-main/plugin/skills'
rm /tmp/rag-reviewer.tgz
```

Available skills:

- `rag-reviewer:reviewer_review-pr` — review a GitHub PR end-to-end
- `rag-reviewer:reviewer_solve-task` — solve a task from the board
- `rag-reviewer:reviewer_sync-codebase` — build/update the vector store + code graph from a local repo clone
- `rag-reviewer:reviewer_sync-tasks` — index tasks into the vector store from a connected board
- `rag-reviewer:reviewer_performance-review` — performance-focused PR review
- `rag-reviewer:reviewer_maintainability-review` — maintainability-focused PR review

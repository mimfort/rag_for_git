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

Use the `skill` tool to load review workflows:

- `rag-reviewer:review-pr` — review a GitHub PR end-to-end
- `rag-reviewer:solve-task` — solve a task from the board
- `rag-reviewer:sync-tasks` — index tasks into the vector store
- `rag-reviewer:performance-review` — performance-focused PR review
- `rag-reviewer:maintainability-review` — maintainability-focused PR review

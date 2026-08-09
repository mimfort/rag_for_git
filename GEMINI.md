# rag_for_git — Gemini CLI context

See `CLAUDE.md` for full project documentation (architecture, commands, invariants).

## MCP server setup

### Quickest way

```bash
uvx --from rag-reviewer reviewer install gemini
```

This registers the MCP server in `~/.gemini/settings.json` automatically (cross-platform,
uses the absolute path to `uvx` — no `bash -lc` wrapper needed).

### Global config (manual alternative)

Add to `~/.gemini/settings.json` under `"mcpServers"`:

```json
{
  "mcpServers": {
    "reviewer": {
      "command": "/bin/bash",
      "args": ["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

The `bash -lc` wrapper loads your shell profile so `uvx` (in `~/.local/bin`) is found
by GUI tools that don't inherit the full shell PATH (macOS/Linux only; on Windows use
`reviewer install gemini` or set `command` to `uvx` directly).

### Project-level config

`.gemini/settings.json` in this repo already contains the MCP entry — Gemini CLI
picks it up automatically when you open this project.

## Skills

Gemini CLI discovers skills from `.gemini/skills/` (project) or `~/.gemini/skills/` (global).
Install globally — no repo clone needed:

```bash
curl -sL https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz -o /tmp/rag-reviewer.tgz
mkdir -p ~/.gemini/skills
tar xz -C ~/.gemini/skills --strip-components=3 -f /tmp/rag-reviewer.tgz 'rag_for_git-main/plugin/skills'
rm /tmp/rag-reviewer.tgz
```

Skills installed: `review-pr`, `solve-task`, `sync-codebase`, `sync-tasks`,
`performance-review`, `maintainability-review`.

Then use them: `use skill rag-reviewer:review-pr`

## Verify

After restarting Gemini CLI, run:
> "List available MCP tools"

You should see `mcp_reviewer_prepare_review`, `mcp_reviewer_publish_review`, etc.

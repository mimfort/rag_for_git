# rag_for_git — Gemini CLI context

See `CLAUDE.md` for full project documentation (architecture, commands, invariants).

## MCP server setup

### Global config (recommended)

Most AI coding tools (Mimo Code, OpenCode, Trae, Cursor, Gemini CLI) share
**`~/.factory/mcp.json`** as a global MCP registry. Add the reviewer there once:

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "stdio",
      "command": "/bin/bash",
      "args": ["-lc", "uvx --from rag-reviewer reviewer-mcp"],
      "disabled": false
    }
  }
}
```

The `bash -lc` wrapper loads your shell profile so `uvx` (in `~/.local/bin`) is found
by GUI tools that don't inherit the full shell PATH.

### Gemini CLI specifically

If Gemini CLI uses its own `~/.gemini/config/mcp_config.json` instead of the global config,
add the same entry there:

```json
{
  "mcpServers": {
    "reviewer": {
      "command": "/bin/bash",
      "args": ["-lc", "uvx --from rag-reviewer reviewer-mcp"]
    }
  }
}
```

## Skills

Gemini CLI has no built-in skill system. Use the MCP tools directly (`prepare_review`,
`publish_review`, etc.) or refer to `plugin/skills/review-pr/SKILL.md` for the review workflow.

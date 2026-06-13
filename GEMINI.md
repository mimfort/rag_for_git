# rag_for_git — Gemini CLI context

See `CLAUDE.md` for full project documentation (architecture, commands, invariants).

## MCP server setup (Gemini CLI)

Gemini CLI does not support path variables in MCP configs. Configure the reviewer MCP
server in your `settings.json` with an absolute path to this repo's venv:

```json
{
  "mcpServers": {
    "reviewer": {
      "command": "/absolute/path/to/rag_for_git/.venv/bin/python",
      "args": ["-m", "reviewer.entrypoints.mcp_server"],
      "cwd": "/absolute/path/to/rag_for_git"
    }
  }
}
```

Replace `/absolute/path/to/rag_for_git` with the actual path on your machine (e.g.
`~/PycharmProjects/rag_for_git` expanded). Run `pwd` in the repo root to get the path.

## Skills

The plugin skills live in `plugin/skills/`. Gemini CLI has no built-in skill system —
use the MCP tools directly (`prepare_review`, `publish_review`, etc.) or refer to the
skill prompts in `plugin/skills/review-pr/SKILL.md` for the review workflow.

# rag_for_git — Gemini CLI context

See `CLAUDE.md` for full project documentation (architecture, commands, invariants).

## MCP server setup (Gemini CLI)

Gemini CLI does not support path variable expansion. Use `uvx` so no absolute path is needed:

```json
{
  "mcpServers": {
    "reviewer": {
      "command": "uvx",
      "args": ["--from", "rag-reviewer", "reviewer-mcp"]
    }
  }
}
```

Add this to your Gemini CLI `settings.json`. `uvx` downloads and runs the published
`rag-reviewer` package from PyPI — no local clone required.

## Skills

Gemini CLI has no built-in skill system. Use the MCP tools directly (`prepare_review`,
`publish_review`, etc.) or refer to `plugin/skills/review-pr/SKILL.md` for the review workflow.

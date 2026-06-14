# Installing rag-reviewer for OpenCode

## Prerequisites

- [OpenCode](https://opencode.ai) installed
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Quick install (global)

Add to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer reviewer-mcp"]
    }
  }
}
```

Restart OpenCode. The `reviewer` MCP server will be available in all sessions.

## Project-level install

Copy `.opencode/opencode.json` from this repo into your target project root. The config is
already present here (see `opencode.json` in this directory).

## Verify

Open OpenCode and run:
> "List available MCP tools"

You should see `prepare_review`, `publish_review`, `search_code`, etc.

## Skills

OpenCode uses a JS plugin system — file-based skills are not supported.
Use the MCP tools directly (`prepare_review`, `publish_review`, etc.) or refer to
`plugin/skills/review-pr/SKILL.md` for the review workflow steps.

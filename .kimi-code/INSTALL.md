# Installing rag-reviewer for Kimi Code

## Prerequisites

- [Kimi Code](https://kimi.moonshot.cn) installed
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Quick install (global)

Create `~/.kimi-code/mcp.json` (or add to it if it already exists):

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

Restart Kimi Code. The `reviewer` MCP server will be available in all sessions.

## Project-level install

Copy `.kimi-code/mcp.json` from this repo into your target project. The config is
already present here (see `mcp.json` in this directory).

## Verify

Open Kimi Code and run:
> "List available MCP tools"

You should see `prepare_review`, `publish_review`, `search_code`, etc.

## Usage

See `plugin/skills/review-pr/SKILL.md` for the full review workflow.

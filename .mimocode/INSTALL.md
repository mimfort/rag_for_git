# Installing rag-reviewer for Mimo Code

## Prerequisites

- [Mimo Code](https://mimo.xiaomi.com) installed
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Quick install (global)

Add to `~/.config/mimocode/mimocode.json` (create if it doesn't exist):

```json
{
  "$schema": "https://mimo.xiaomi.com//config.json",
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer reviewer-mcp"],
      "enabled": true
    }
  }
}
```

Restart Mimo Code. The `reviewer` MCP server will appear in the tools list.

## Project-level install

Copy `.mimocode/mimocode.json` from this repo into your target project. The config is
already present here (see `mimocode.json` in this directory).

## Skills (optional)

Mimo Code discovers skills from `.mimocode/skills/` in your project or
`~/.config/mimocode/skills/` globally. To expose rag-reviewer skills globally:

```bash
mkdir -p ~/.config/mimocode/skills
cp -r plugin/skills/* ~/.config/mimocode/skills/
```

## Verify

Open Mimo Code, start a session, and ask:
> "List available MCP tools"

You should see `prepare_review`, `publish_review`, `search_code`, etc.

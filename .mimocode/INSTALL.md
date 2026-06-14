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

## Keys (.env)

The reviewer needs `VOYAGE_API_KEY` and `GITHUB_TOKEN`. Mimo launches the MCP
server with an arbitrary working directory, so a project-local `.env` is **not**
reliably found. Put the file in the fixed config location instead:

```bash
mkdir -p ~/.config/rag-reviewer
curl -fsSL https://raw.githubusercontent.com/mimfort/rag_for_git/main/.env.example \
  -o ~/.config/rag-reviewer/.env       # then edit it: fill in VOYAGE_API_KEY and GITHUB_TOKEN
uvx --from rag-reviewer reviewer check  # ✓/✗ for keys, Postgres, Neo4j, GitHub
```

Lookup order: `$REVIEWER_ENV_FILE` → `$XDG_CONFIG_HOME/rag-reviewer/.env`
(default `~/.config/rag-reviewer/.env`) → `./.env`. Alternatively pass keys via an
`"env"` block in the `reviewer` MCP entry above — real environment variables take
priority over the file.

## Project-level install

Copy `.mimocode/mimocode.json` from this repo into your target project. The config is
already present here (see `mimocode.json` in this directory).

## Skills (optional)

Mimo Code discovers skills from `.mimocode/skills/` in your project or
`~/.config/mimocode/skills/` globally. Install globally — no repo clone needed:

```bash
curl -sL https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz -o /tmp/rag-reviewer.tgz
mkdir -p ~/.config/mimocode/skills
tar xz -C ~/.config/mimocode/skills --strip-components=3 -f /tmp/rag-reviewer.tgz 'rag_for_git-main/plugin/skills'
rm /tmp/rag-reviewer.tgz
```

Skills installed: `reviewer_review-pr`, `reviewer_solve-task`, `reviewer_sync-codebase`, `reviewer_sync-tasks`, `reviewer_performance-review`, `reviewer_maintainability-review`.

## Verify

Open Mimo Code, start a session, and ask:
> "List available MCP tools"

You should see `prepare_review`, `publish_review`, `search_code`, etc.

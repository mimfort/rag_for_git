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

## Skills (optional)

Kimi Code loads skills from directories listed in `extra_skill_dirs` in `~/.kimi-code/config.toml`.

**Step 1** — add the skills directory to your config:

```toml
# ~/.kimi-code/config.toml
extra_skill_dirs = ["~/.kimi-code/skills"]
```

**Step 2** — download the skills (no repo clone needed):

```bash
curl -sL https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz -o /tmp/rag-reviewer.tgz
mkdir -p ~/.kimi-code/skills
tar xz -C ~/.kimi-code/skills --strip-components=3 -f /tmp/rag-reviewer.tgz 'rag_for_git-main/plugin/skills'
rm /tmp/rag-reviewer.tgz
```

Skills installed: `reviewer_review-pr`, `reviewer_solve-task`, `reviewer_sync-codebase`, `reviewer_sync-tasks`, `reviewer_performance-review`, `reviewer_maintainability-review`.

## Verify

Open Kimi Code and run:
> "List available MCP tools"

You should see `prepare_review`, `publish_review`, `search_code`, etc.

## Usage

See `plugin/skills/review-pr/SKILL.md` for the full review workflow.

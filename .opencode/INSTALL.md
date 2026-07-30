# Installing rag-reviewer for OpenCode

## Prerequisites

- [OpenCode](https://opencode.ai) installed
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Быстрая установка (рекомендуется)

```bash
uvx --from rag-reviewer reviewer install opencode
```

Пропишет MCP-сервер в `~/.config/opencode/opencode.json` автоматически (кроссплатформенно,
подставляет абсолютный путь к `uvx` — обёртка `bash -lc` не нужна).

## Ручная установка (альтернатива)

Добавьте в `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

Перезапустите OpenCode. MCP-сервер `reviewer` будет доступен во всех сессиях.

## Project-level install

Copy `.opencode/opencode.json` from this repo into your target project root. The config is
already present here (see `opencode.json` in this directory).

## Verify

Open OpenCode and run:
> "List available MCP tools"

You should see `prepare_review`, `publish_review`, `search_code`, etc.

## Skills

OpenCode loads file-based skills from `~/.config/opencode/skills/<name>/SKILL.md`. Install them:

```bash
uvx --from rag-reviewer reviewer install-skills opencode
```

(or `reviewer install opencode`, which sets up the MCP server and the skills together).
Then restart OpenCode and run `opencode debug skill` — short names such as `review-pr` and
`solve-task` should be listed.

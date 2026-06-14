# Installing rag-reviewer for Mimo Code

## Prerequisites

- [Mimo Code](https://mimo.xiaomi.com) installed
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Быстрая установка (рекомендуется)

```bash
uvx --from rag-reviewer reviewer install mimo
```

Пропишет MCP-сервер в `~/.config/mimocode/mimocode.json` автоматически (кроссплатформенно,
подставляет абсолютный путь к `uvx` — обёртка `bash -lc` не нужна).

## Ручная установка (альтернатива)

Добавьте в `~/.config/mimocode/mimocode.json` (создайте файл, если его нет):

```json
{
  "$schema": "https://mimo.xiaomi.com//config.json",
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer@latest reviewer-mcp"],
      "enabled": true
    }
  }
}
```

Перезапустите Mimo Code. MCP-сервер `reviewer` появится в списке инструментов.

## Ключи (.env)

Reviewer требует `VOYAGE_API_KEY` и `GITHUB_TOKEN`. Mimo запускает MCP-сервер из
произвольной директории, поэтому project-local `.env` **не гарантированно найдётся**.
Используйте фиксированное расположение:

```bash
uvx --from rag-reviewer reviewer init   # создаёт ~/.config/rag-reviewer/.env из шаблона
# заполните VOYAGE_API_KEY и GITHUB_TOKEN в ~/.config/rag-reviewer/.env
uvx --from rag-reviewer reviewer check  # ✓/✗ по ключам, Postgres, Neo4j, GitHub
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

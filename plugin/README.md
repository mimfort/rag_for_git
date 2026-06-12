# rag-reviewer — Claude Code plugin

Плагин = этот репозиторий. Требования: выполненная установка
`python -m venv .venv && .venv/bin/pip install -e ".[dev]"`, заполненный `.env`,
поднятые ParadeDB/Neo4j (`docker compose up -d`), построенный base-индекс
(`reviewer index /path/to/repo --ref main`).

MCP-сервер запускается из `.venv` репозитория с `cwd` в корне плагина —
`.env` подхватывается оттуда (диагностика при сбое старта: `reviewer check`).

Подключение: `claude --plugin-dir /path/to/rag_for_git` (или установка через
локальный marketplace). Скиллы: `/rag-reviewer:review-pr`,
`/rag-reviewer:performance-review`, `/rag-reviewer:maintainability-review`.

Headless:

```bash
claude --plugin-dir . -p "/rag-reviewer:review-pr owner/repo#123 --dry-run" --permission-mode bypassPermissions
```

# rag-reviewer — Claude Code plugin

Корень Claude Code-плагина для скиллов `/rag-reviewer:reviewer_*`. Полная документация
проекта — в корневых [README.md](../README.md) (англ.) и [README.ru.md](../README.ru.md) (рус.).

## Что внутри

- **MCP-сервер** `reviewer` (`reviewer-mcp`, 31 тул) — конфиг в `.mcp.json`.
- **10 скиллов** (`plugin/skills/*/SKILL.md`):
  `/rag-reviewer:reviewer_review-pr` · `/rag-reviewer:reviewer_solve-task` ·
  `/rag-reviewer:reviewer_sync-codebase` · `/rag-reviewer:reviewer_sync-tasks` ·
  `/rag-reviewer:reviewer_performance-review` · `/rag-reviewer:reviewer_maintainability-review` ·
  `/rag-reviewer:reviewer_ask` ·
  `/rag-reviewer:reviewer_pr-walkthrough` ·
  `/rag-reviewer:reviewer_configure-review` ·
  `/rag-reviewer:reviewer_summarize-subsystems`.

## Требования

- Поднятые ParadeDB/Neo4j (`docker compose up -d`), заполненный `.env`
  (`reviewer init`; обязателен `VOYAGE_API_KEY`, для ревью — `GITHUB_TOKEN`).
- Построенный base-индекс (`reviewer index /path/to/repo --ref main`) — для полного
  whole-repo контекста. Без него ревью работает «тонко» (только дифф + overlay).

`.env` резолвится из фиксированного места: `$REVIEWER_ENV_FILE` →
`~/.config/rag-reviewer/.env` → `./.env`. Диагностика старта: `reviewer check`.

## Установка плагина

Через marketplace (из любого проекта):

```text
/plugin marketplace add mimfort/rag_for_git
/plugin install rag-reviewer@rag-reviewer-marketplace
```

Или локально для разработки: `claude --plugin-dir /path/to/rag_for_git`.

## Грунтовка в план/ревью (опц.)

Reviewer-тулы доступны не только в ревью PR — их можно включить в фазах планирования/ревью
(writing-plans и т.п.), вставив opt-in блок в свой контекст-файл. См.
[README.md](../README.md#reviewer-grounding-in-planreview-phases-optional) (EN) /
[README.ru.md](../README.ru.md#грунтовка-reviewer-в-фазах-планревью-опционально) (RU).

## Headless

```bash
claude --plugin-dir . -p "/rag-reviewer:reviewer_review-pr owner/repo#123 --dry-run" --permission-mode bypassPermissions
```

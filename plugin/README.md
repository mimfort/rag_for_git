# rag-reviewer — Claude Code and Codex plugin payload

Корень Claude Code-плагина для скиллов `/rag-reviewer:<short-name>`. Полная документация
проекта — в корневых [README.md](../README.md) (англ.) и [README.ru.md](../README.ru.md) (рус.).

## Что внутри

- **MCP-сервер** `reviewer` (`reviewer-mcp`, 42 тул, включая generic `create_subtasks`) — конфиг в
  `.mcp.json` для Claude Code.
- Каждый каталог `plugin/skills/` с файлом `SKILL.md` регистрируется в namespace `rag-reviewer`.
  `_common` и вложенные references доставляются как вспомогательные файлы, но не регистрируются как
  скиллы.
- Скиллы (`plugin/skills/*/SKILL.md`):
  `/rag-reviewer:review-pr` · `/rag-reviewer:solve-task` ·
  `/rag-reviewer:sync-codebase` · `/rag-reviewer:sync-tasks` ·
  `/rag-reviewer:performance-review` · `/rag-reviewer:maintainability-review` ·
  `/rag-reviewer:ask` ·
  `/rag-reviewer:pr-walkthrough` ·
  `/rag-reviewer:configure-review` ·
  `/rag-reviewer:summarize-subsystems` ·
  `/rag-reviewer:create-task` ·
  `/rag-reviewer:decompose-task` ·
  `/rag-reviewer:finish-task`.

## Декомпозиция нативных подзадач

`/rag-reviewer:decompose-task <parent-key>` остаётся provider-neutral: skill читает сохранённого
parent и кодовый контекст, а поддержку определяет только по registry-owned capability
`native_subtasks` из generic board discovery. Затем он показывает полный batch дочерних задач и
idempotency key, запрашивает одно подтверждение и выполняет один generic `create_subtasks` call.

Неподдерживаемая capability останавливает flow без записи: fallback на отдельные `create_task`
calls запрещён. После любой начатой записи skill синхронизирует project и проверяет parent,
отношения и возвращённые children. Retry требует нового явного подтверждения и повторяет тот же
полный payload с тем же idempotency key; результат разделяет `created`, `attached`, `unattached`,
`pending` и `warnings`. Provider matrix, marker reconciliation и точный write-through contract
описаны в [docs/board-providers.md](../docs/board-providers.md#native-subtask-writes).

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

## Codex CLI

```bash
uvx --from rag-reviewer@latest reviewer install codex
uvx --from rag-reviewer@latest reviewer install codex --dry-run
uvx --from rag-reviewer@latest reviewer install codex --no-skills
uvx --from rag-reviewer@latest reviewer install-skills codex
```

Первая команда управляет одним глобальным MCP `reviewer` и namespaced plugin
`rag-reviewer`; повторный запуск обновляет marketplace/plugin. `--dry-run` ничего не пишет и не
ходит в сеть, а `install-skills codex` не трогает MCP.

```bash
codex plugin list --json
codex mcp list
```

Успех означает, что `rag-reviewer` установлен и включён, а `codex mcp list` содержит ровно один
`reviewer`. Идентифицированные legacy skills перемещаются в
`$CODEX_HOME/reviewer-legacy-backups/<timestamp>`; изменённые и неоднозначные копии остаются на
месте. При ошибке печатается путь к backup конфига. После установки откройте New Chat/new CLI
session; в IDE также выполните Reload Window.

## Грунтовка в план/ревью (опц.)

Reviewer-тулы доступны не только в ревью PR — их можно включить в фазах планирования/ревью
(writing-plans и т.п.), вставив opt-in блок в свой контекст-файл. См.
[README.md](../README.md#reviewer-grounding-in-planreview-phases-optional) (EN) /
[README.ru.md](../README.ru.md#грунтовка-reviewer-в-фазах-планревью-опционально) (RU).

## Headless

```bash
claude --plugin-dir . -p "/rag-reviewer:review-pr owner/repo#123 --dry-run" --permission-mode bypassPermissions
```

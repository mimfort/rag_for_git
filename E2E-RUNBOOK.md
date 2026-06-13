# E2E-ранбук: проверка RAG-ревью и /solve-task

> Транзиентный файл для ручного E2E-теста. Не трекается git; удалить после прогона и уборки фикстур.
> Создан 2026-06-13. Репозиторий: `mimfort/rag_for_git`.

## Что уже подготовлено

**Инфраструктура** (контейнеры `rag_for_git-*` — `docker ps`):
- ParadeDB :5433, Neo4j :7687, web :8000.
- Base-индекс собран по `origin/main` (`2f63c02`): **760 чанков, SCIP-граф 783 узла / 3209 рёбер**.
- `reviewer check` — все ✓ (Voyage, GitHub, Postgres, Neo4j, scip-python, auth `mimfort`).
- В worktree `.claude/worktrees/phase4-solve-task/.env` лежит копия корневого `.env` (gitignored).

**PR-ы (OPEN):**
| PR | Что | База |
|---|---|---|
| #7  | `chore(config)`: `.env.example` + `.review.yml` (блок `task_board` yougile) | `main` |
| #8  | `feat(index)`: jitter в `with_voyage_retry` — **PRI-35** | `chore/align-config` |
| #9  | `feat(vcs)`: jitter в `_RetryTransport` — **PRI-36**, ссылается на #8 | `chore/align-config` |

Тестовые PR нацелены на ветку `chore/align-config` (в ней есть `.review.yml`, который ревью читает из base-ветки PR), поэтому `main` не тронут.

**Задачи на доске «Сайт для зоомагазина» → колонка «Входящие задачи»:**
| Код | UUID | Задача | Связи |
|---|---|---|---|
| **PRI-35** / ID-35 | `58ec6398-05ba-4e06-99f1-bc5c41265f81` | Voyage retry jitter | ↔ PRI-36 + PR #8 |
| **PRI-36** / ID-36 | `10e56433-d122-4853-b90a-c00302f51adf` | GitHub retry jitter | ↔ PRI-35 + PR #9 |
| **PRI-37** / ID-37 | `11cb191a-0d5c-410e-b960-285840b7a08d` | Единая политика rate-limit | **без ссылок**, близка по смыслу → тест реранкера/вектора |

Цель связей: A↔B связаны (тест контекста задачи в ревью), C не связана ссылкой, но семантически рядом (тест `search_tasks` + реранкера).

## Предусловия запуска

Плагин `rag-reviewer` ставится через **локальный marketplace** (постоянно, без флага
при каждом запуске). В корне репо есть `.claude-plugin/marketplace.json` и
`.claude-plugin/plugin.json`. Установка — один раз, в Claude Code из корня репо:

```
/plugin marketplace add /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
/plugin install rag-reviewer@rag-reviewer-marketplace
```

Затем `/reload-plugins` или рестарт; подтвердить доверие MCP `reviewer`.

**Запускать Claude Code из КОРНЯ репо** — `plugin/.mcp.json` использует
`${CLAUDE_PROJECT_DIR}` (каталог запуска), оттуда берётся `.venv` и `.env`. Marketplace
копирует плагин в кэш, поэтому `${CLAUDE_PLUGIN_ROOT}` там не годится — отсюда
`${CLAUDE_PROJECT_DIR}`.

Альтернатива на разовый запуск (без установки): `claude --plugin-dir .` из корня репо.

После старта:
1. Подтвердить доверие MCP-серверу `reviewer`, если спросит.
2. Проверить: `/rag-reviewer:` автодополняет скиллы; `/mcp` показывает `reviewer` подключённым.
3. `/model sonnet`.
4. MCP `yougile` подключён (чтение доски скиллом).

**Сделано при подготовке (2026-06-13):** корневой `.venv` был устаревшим — без пакета
`mcp` (FastMCP); доустановлено `pip install -e ".[dev]"`, MCP-сервер импортируется. При
`--plugin-dir <корень>` сервер берёт `${CLAUDE_PLUGIN_ROOT}/.venv` (корневой) и читает
корневой `.env` (с ключами). Если всё же «No API key»/нет коннекта — пробрось
`VOYAGE_API_KEY`/`GITHUB_TOKEN`/`PG_DSN`/`NEO4J_*` в окружение MCP-сервера.

## Шаги прогона

### 1. Прогрев корпуса задач
```
/rag-reviewer:sync-tasks
```
Индексирует задачи доски (A/B/C) в Postgres-корпус (`tasks`) + граф (`:Task`). Без этого `search_tasks` не найдёт PRI-37.

Проверка: `docker exec rag_for_git-paradedb-1 psql -U reviewer -d reviewer -tA -c "SELECT count(*) FROM tasks;"` — должно вырасти (было 1).

### 2. Тест ревью (контекст связанной задачи)
```
/rag-reviewer:review-pr   → PR #9
```
Ожидаемо: скилл извлекает `PRI-36` (primary) и `PRI-35` (related) из PR, дочитывает задачу PRI-36 с доски, обогащает ревью требованиями задачи, ставит inline-комментарии на jitter в `reviewer/vcs/github.py` (напр. что `random.uniform` не инъектируется для тестов, или что cap `max_wait` превышается на jitter). Смотрим: подтянул ли связанную задачу/PR в контекст.

### 3. Тест /solve-task (поиск несвязанной задачи по смыслу)
```
/rag-reviewer:solve-task PRI-35
```
Ожидаемо: читает задачу A с доски → `get_task_context(ID-35)` → `search_tasks("Voyage retry jitter…")` → должен вытащить **PRI-37** (семантика, БЕЗ ссылки) и PRI-36; `search_codebase` → находит `reviewer/index/_retry.py`; сводит бриф → входит в `superpowers:brainstorming`. **Ключевая проверка** — найдёт ли несвязанную C через реранкер/вектор.

## Нюанс: графовая связь A↔B
Yougile MCP (`create_task`/`update_task`) не умеет создавать subtask/структурную связь, а граф строит рёбра `TASK_LINK` именно из `subtasks[]`. Сейчас связь A↔B выражена кросс-ссылками в описаниях (текст), графового ребра нет → `get_task_context` не покажет B как linked через граф (B всё равно всплывёт семантически).

Для полноценного графового теста: в UI Yougile сделать **PRI-36 подзадачей PRI-35**, затем повторить `/rag-reviewer:sync-tasks`. Семантический тест (C) работает и без этого.

## Уборка после теста
```bash
gh pr close 8 --delete-branch
gh pr close 9 --delete-branch
gh pr close 7 --delete-branch   # или: gh pr merge 7 --merge, если конфиг нужен на main
```
Задачи PRI-35/36/37 — в архив (update_task archived=true). Удалить этот файл.

## Полезное
- Состояние индекса: `docker exec rag_for_git-paradedb-1 psql -U reviewer -d reviewer -tA -c "SELECT count(*) FROM chunks; SELECT ref,substr(sha,1,7) FROM index_meta;"`
- Граф: `docker exec rag_for_git-neo4j-1 cypher-shell -u neo4j -p reviewerpass "MATCH (n) RETURN labels(n)[0], count(*);"`
- Пересобрать индекс (из worktree, где есть `.env`): `.venv/bin/reviewer index "$(pwd)" --ref origin/main`

# PRI-98 — Auto-конфигурация allowlist reviewer MCP «из коробки»

Дата: 2026-06-16 · Задача: PRI-98 / ID-98 (В работе) ·
[Доска](https://ru.yougile.com/team/686c049c8af8/#PRI-98)

## Проблема

В Claude Code режим разрешений `auto` зовёт safety-classifier на каждый вызов
`mcp__reviewer__*`. Если classifier недоступен — вызов отклоняется:

```
claude-sonnet-4-6 is temporarily unavailable, so auto mode cannot determine
the safety of mcp__reviewer__get_task_context right now.
```

Тогда `/solve-task`, `/review-pr`, `/sync-tasks` вырождаются: RAG-поиск, граф задач
и семантический поиск по коду не работают. Корневая причина — тулы reviewer не
прописаны в разрешениях, поэтому при каждом вызове идёт обращение к classifier.

## Поправка к постановке (сверено с docs Claude Code)

Постановка задачи предлагала top-level `{"allowedTools": [...]}` с явным перечнем 15
тулов. Это **некорректно** для `settings.json`. Авторитетно (docs.claude.com):

- Правильный ключ — **`permissions.allow: [...]`**. `allowedTools` — это CLI-флаг
  `--allowedTools` / SDK-опция, а **не** ключ `settings.json`.
- Формат правила — `mcp__<server>__<tool>`. Целый сервер разрешается **одним
  anchored-glob правилом `mcp__reviewer__*`** (документировано).
- Явное allow-правило **обходит auto-classifier** — это корректный фикс ошибки
  «classifier unavailable».
- **Плагины не могут раздавать разрешения автоматически.** `plugin/.claude/settings.json`
  применяется, только если открыть `plugin/` как проект. Поэтому основной механизм
  для пользователей — `reviewer install`, мёрджащий правило в `.claude/settings.json`
  их репозитория.

## Ключевое решение: wildcard вместо явного списка

Используем **`mcp__reviewer__*`** (одно правило), а не перечень 15 тулов. Обоснование:

- Корневая боль задачи — устаревание списка тулов: *«Нет гарантии, что после
  обновления списка тулов разработчики обновят свои конфиги»*. Wildcard **устраняет
  этот класс багов целиком**: новый тул (как `purge_orphaned_tasks`) покрывается
  автоматически, без правок конфигов/тестов. Это лучше закрывает критерий
  «автообновление при добавлении новых тулов», чем явный список.
- Wildcard `mcp__reviewer__*` — документированная, anchored-безопасная форма.
- `mcp__reviewer__*` и есть «единый источник правды» — синхронизировать нечего.

Перечень тулов в человекочитаемой документации (`plugin/GEMINI.md`) остаётся явным
(этого требует критерий «`plugin/GEMINI.md` содержит список pre-approved тулов»).

## Компоненты

### 1. Чек-инутые `settings.json` (committed allowlist)

Два файла, оба с содержимым `{"permissions":{"allow":["mcp__reviewer__*"]}}`:

- **`plugin/.claude/settings.json`** — критерий «открыть `plugin/` как проект →
  тулы работают без ручного редактирования settings».
- **`.claude/settings.json`** (корень репо) — для разработки самого `rag_for_git`
  (тут используется reviewer MCP; `.claude/settings.local.json` уже включает сервер).
  Shared `settings.json` и personal `settings.local.json` мёрджатся, allow-правила
  кумулятивны — конфликта нет.

### 2. `reviewer install` мёрджит allowlist (`reviewer/install.py`)

Новое:

- `REVIEWER_PERMISSION_RULE = "mcp__reviewer__*"` — единственная константа-правило.
- `claude_settings_path() -> Path` — `.claude/settings.json` (project scope, рядом с
  `.mcp.json`, который `claude-code` пишет в CWD).
- `AllowlistPlan(path, content, created, already)` — по образцу `InstallPlan`.
- `build_allowlist_plan(path=None, rule=REVIEWER_PERMISSION_RULE) -> AllowlistPlan` —
  читает существующий JSON (или `{}`), обеспечивает `permissions.allow` списком,
  добавляет правило при отсутствии (dedup, `already=True` если уже есть), сохраняет
  чужие ключи/правила. Возвращает план без записи.
- Рефактор: общая запись с бэкапом выделяется в `_write_with_backup(path, content)
  -> Path | None`; `apply_plan` и применение allowlist используют её (DRY). Поведение
  `apply_plan` не меняется (идемпотентность + `.bak`).

CLI (`reviewer/entrypoints/cli.py::install`): при установке клиента `claude-code`
после записи MCP-плана дополнительно строится и применяется allowlist-план в
`<dir>/.claude/settings.json` (dir = каталог MCP-конфига, по умолчанию CWD). При
`--dry-run` его содержимое печатается. Идемпотентно, с `.bak`.

### 3. `plugin/GEMINI.md`

Документирует тулы reviewer MCP как pre-approved для Gemini CLI: явный список 15
тулов + указание, что Gemini может доверять серверу `reviewer` целиком. Закрывает
критерий 5. Файловую запись в `~/.gemini/settings.json` не трогаем (вне scope).

### 4. Точечный фикс

`reviewer/entrypoints/mcp_server.py::create_server` — докстринг «14 тулов» → «15
тулов» (сейчас рассинхрон: тулов 15).

### 5. Документация

Краткая заметка в `README.md`: `reviewer install` теперь также прописывает allowlist
(`permissions.allow: mcp__reviewer__*`) в `.claude/settings.json`, тулы работают из
коробки без ручных шагов.

## Тесты (`tests/install/test_install.py`, стиль существующих)

- `build_allowlist_plan` на отсутствующем файле → создаёт
  `{"permissions":{"allow":["mcp__reviewer__*"]}}`, `created=True`, `already=False`.
- сохраняет чужие top-level ключи и существующие правила в `permissions.allow`.
- идемпотентность: повторный план → `already=True`, без дублей правила.
- merge в непустой `permissions.allow` с другими правилами.
- CLI-интеграция: `reviewer install claude-code` в tmp-CWD пишет и `.mcp.json`, и
  `.claude/settings.json` с правилом; повторный запуск не плодит дубли.

## Вне scope (YAGNI / не в критериях приёмки)

- Copilot CLI `references/copilot-tools.md` — не упомянут в критериях приёмки.
- Cursor / VS Code / Claude Desktop — у них иные модели разрешений; проблема
  classifier специфична для `auto`-режима Claude Code.
- Изменение формата Gemini-записи в `~/.gemini/settings.json` (`trust: true`) —
  риск задеть существующие тесты установки; документации в `GEMINI.md` достаточно.

## Критерии приёмки → как закрываются

1. Новый разработчик `reviewer install claude-code` → allow-правило в
   `.claude/settings.json` → `/solve-task` без ошибок classifier. ✓
2. Открыть `plugin/` как проект → `plugin/.claude/settings.json` применяется. ✓
3. `search_codebase`/`search_tasks`/`get_task_context` проходят (allow обходит
   classifier). ✓
4. `reviewer install` идемпотентен (план `already`, `_write_with_backup` не пишет при
   совпадении). ✓
5. `plugin/GEMINI.md` содержит список pre-approved тулов. ✓

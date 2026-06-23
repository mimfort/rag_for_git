# Дизайн — фолоу-апы PR #52 (PRI-159 / PRI-119)

**Дата:** 2026-06-23
**Ветка:** `feat/graphrag-summaries-walkthrough` (дослать в открытый PR #52 → dev)
**Контекст:** три опциональных фолоу-апа, отмеченных в теле PR #52 как «не блокеры мержа».
Решение по scope/подходу принято в брейншторме: делаем **все три**, #2 — серверным
re-derive (вариант «б»).

## Цель

Дошлифовать уже реализованную в PR #52 функциональность (GraphRAG community summaries +
PR walkthrough), не меняя существующих инвариантов и **не трогая публичные сигнатуры
MCP-тулов**. Три независимые правки в трёх слоях: skill / service / store. Каждая —
со своим тестом.

## Изменение #1 — pr-walkthrough берёт `prq.base_ref` (только скилл)

### Проблема
Единственный потребитель параметра `branch` в скилле pr-walkthrough — шаг 5
`get_subsystem_summaries(repo, branch)`. Сейчас `branch` приходит из include
`_common/branch-selection.md`, то есть это **локальная git-ветка ревьюера**
(`git branch --show-current`). Но summaries индексируются по **целевой ветке PR**
(`ref="base:<branch>"`), и локальная ветка может ей не соответствовать → walkthrough
тянет summaries не той ветки либо пустой результат. Остальные тулы шага 2–4
(`get_impact`, `get_changed_file_diff`, `find_callers`, `get_related_symbols`) —
PR-сессионные и параметр `branch` не принимают, поэтому include здесь — неверная
абстракция для PR-скоупного скилла.

### Решение
Файл: `plugin/skills/pr-walkthrough/SKILL.md`.
- Убрать include `<!-- include: _common/branch-selection.md -->` (≈стр. 28).
- Шаг 5: `get_subsystem_summaries(repo, pr.base_ref)` — передавать `base_ref` из ответа
  `prepare_review` (шаг 1; `_prepared_payload` возвращает `pr.base_ref`,
  `reviewer/mcp/service.py`). Добавить однострочное обоснование: «summaries индексируются
  по целевой ветке PR; локальная git-ветка может отличаться».

### Корректность
`base_ref` гарантированно входит в `REVIEW_BRANCHES` — иначе `prepare_review` вернул бы
`{"status":"skipped"}` и скилл остановился бы на шаге 1. Значит `_resolve_repo_branch`
внутри `get_subsystem_summaries` его пропустит без `note`.

### Тест
`tests/skills/test_pr_walkthrough_skill.py`:
- Существующие тесты остаются зелёными: после удаления остаются два include
  (`tool-usage.md`, `anti-hallucination.md`), а `test_skill_includes_resolve_to_existing_common_files`
  требует только `includes` непустым.
- Добавить ассерт: шаг 5 ссылается на `base_ref` (а не на локальную ветку из
  branch-selection).

## Изменение #2 — `member_node_ids` через server-side re-derive (вариант «б»)

### Проблема
`index_subsystem_summary` зашивает `[]` в `upsert_summary`
(`reviewer/mcp/service.py`), хотя стор и единичный `get_summary` поле `member_node_ids`
уже поддерживают (`reviewer/index/summary_store.py`), а `Cluster.member_node_ids`
считается в `build_clusters` (`reviewer/graph/summaries.py`). Поле — задел на будущий
drill-down summary→символы; сейчас оно теряется на пути индексации.

### Решение
Файл: `reviewer/mcp/service.py::index_subsystem_summary`. **Сигнатура MCP-тула
`index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash)`
не меняется** — LLM не передаёт `member_node_ids` (в этом и выигрыш варианта «б»).
Сервер выводит членов сам, переиспользуя чистые функции
`reviewer.graph.summaries.cluster_key()` и `compute_source_hash()`:

```python
raw = self.components.store.list_base_members(repo, resolved)   # один SQL-скан
depth = self.settings.summary_cluster_depth                    # тот же дефолт, что у list-шага
members = [(f"{p}#{s}", h) for p, s, h, _ in raw
           if cluster_key(p, depth) == cluster_key_arg]
member_node_ids = sorted(nid for nid, _ in members)
computed_hash = compute_source_hash(members)
```

Полный `build_clusters` не нужен — фильтр по `cluster_key` проще и дешевле.

### Инвариант консистентности (выбран как рекомендованный)
`member_node_ids` персистится **только если** `computed_hash == source_hash` (тот, что
LLM пробросил из list-шага). При расхождении (гонка — база изменилась между `list` и
`index`) или пустом матче — пишем `[]` и возвращаем `note` (fail-soft).

Свойство: stored `member_node_ids` **всегда** соответствует stored `source_hash` (и тексту
summary, написанному LLM по тому же составу). Транзиентный дрейф самозалечивается
существующим механизмом свежести: следующий проход `summarize-subsystems` видит
`stale` (новый hash ≠ stored) и пере-суммирует, восстанавливая консистентность по всем
полям. Стоимость guard'а ≈ ноль — `computed_hash` всё равно считается при выводе членов.

### Возврат тула
Обогатить: `{"cluster_key": ..., "stored": True, "members": <N>}` (+`"note"` при
mismatch). `upsert_summary` получает выведенный `member_node_ids` вместо `[]`.

### Тест
`tests/mcp/test_subsystem_summaries.py`:
- После `index_subsystem_summary` единичный `get_summary` возвращает непустой
  `member_node_ids`, совпадающий с составом кластера.
- Кейс mismatch (передан неактуальный `source_hash`) → `member_node_ids == []` + `note`.

Чистые `cluster_key`/`compute_source_hash` уже покрыты в `tests/graph/test_summaries.py`.

## Изменение #3 — `get_summaries` отдаёт `updated_at` (только стор)

### Проблема
Список `get_summaries` (`reviewer/index/summary_store.py`) возвращает
`cluster_key, title, summary` без `updated_at`; единичный `get_summary` уже отдаёт
`updated_at.isoformat()`.

### Решение
Файл: `reviewer/index/summary_store.py::get_summaries`.
- В SELECT добавить `updated_at`; в возвращаемый dict — `"updated_at": row[3].isoformat()`
  (зеркало единичного `get_summary`). `ORDER BY cluster_key` без изменений.
- `service.get_subsystem_summaries` пробрасывает результат as-is → список автоматически
  несёт `updated_at`, правок в service не требуется.
- `member_node_ids` в **список** не добавляем (объёмно; drill-down идёт через единичный
  `get_summary`, который его уже отдаёт).

### Корректность для потребителей
Потребители (`ask/SKILL.md`, `pr-walkthrough/SKILL.md`) читают summaries read-only и
fail-open — дополнительное поле их не ломает.

### Тест
`tests/index/test_summary_store.py`:
- `get_summaries` несёт `updated_at` (ISO-строка), совпадающий с upsert-нутой строкой.

## Тестовый прогон

- `pytest -q` — unit (быстрые, на фейках).
- `pytest -m integration` — стор против Postgres :5433 (#2 и #3 трогают SQL).

## Границы (что НЕ трогаем)

- Публичные сигнатуры MCP-тулов (`index_subsystem_summary`, `get_subsystem_summaries`,
  `list_subsystem_clusters`).
- `build_clusters`, `upsert_summary`, единичный `get_summary`.
- Схему БД: `subsystem_summaries` уже содержит колонки `member_node_ids` и `updated_at`.
- Логику свежести (`source_hash` / `stale`).

## Приземление

Все три правки дослать коммитами в открытый PR #52 (ветка
`feat/graphrag-summaries-walkthrough` → dev). Conventional Commits на русском, без
self-attribution.

# PRI-162 — solve-task: подмешивать существующие тесты как образец для TDD-хендоффа (include_tests)

**Задача:** [PRI-162](https://ru.yougile.com/team/686c049c8af8/#PRI-162) · Оценка: S · Слой: Плагин/агент (скилы) — качество.
**Бриф:** `docs/superpowers/briefs/2026-06-26-PRI-162-solve-task-include-tests.md`
**Дата:** 2026-06-26

## Проблема

`search_codebase` по умолчанию `include_tests=False`, поэтому бриф `solve-task` показывает целевой
код, но не показывает, **как** тестируются похожие фичи. Бриф уходит в хендофф
`brainstorming → writing-plans → subagent-driven-development/TDD`, где имплементер стартует с теста —
но без образца «как у нас принято тестировать это». Пробел бьёт по качеству TDD-старта.

## Ключевая предпосылка (уже реализовано в движке)

Возможность `include_tests=True` **уже проброшена end-to-end** — менять Python не нужно:
- `reviewer/mcp/service.py:396-417` — `search_codebase(..., include_tests: bool = False)`; докстринг
  (стр.405): «include_tests=True возвращает тест-чанки».
- `reviewer/retrieval/retriever.py:138-139` — `search_base`: `if not include_tests: items = [it ...
  if not _is_test_path(it.path)]`.

**Следствие:** скоуп задачи — чисто промптовый. Только два файла:
- `plugin/skills/solve-task/SKILL.md` — шаги 3–4.
- `tests/skills/test_solve_task_brief.py` — guard-тест.

Никаких новых MCP-параметров, никаких правок движка/ретривера.

## Дизайн

### 1. Шаг 3 — новый опциональный под-шаг «Test exemplars»

Вставляется в шаг 3 сразу **после** буллета код-ретрива (`SKILL.md:122-123`,
`search_codebase("<task description>")`) и **перед** «Deepen via the code graph» (`:124`). Логика
буллета: код-ретрив → тест-ретрив → углубление по графу.

Содержание под-шага:
- Один доп. вызов `search_codebase("<как тестируется область задачи — фикстуры/моки для <фичи>>",
  include_tests=True)` на том же `branch`, что и основной код-ретрив. **Целевой тест-запрос**
  (тематический «как тестируется область»), а не переиспользование код-запроса с флагом — точнее
  находит тест-паттерн.
- **Гейтинг — optional, when `search_codebase` surfaced concrete symbols** (зеркалит паттерн
  graph-deepen): если код-цели нет, под-шаг пропускается — не жжём Voyage (free tier 3 RPM / 10K TPM).
- Сниппеты построчно пронумерованы (тот же контракт, что у код-ретрива) → цитируем `path:line`
  напрямую, без повторного Read.
- Тот же rank-based relevance-фильтр шага 4 (см. §3), колпак **≤3** тест-файла/символа.
- **Fail-open:** тестов не нашлось / `(ничего не найдено)` / ошибка → секцию `## Test exemplars`
  в брифе опускаем; основной код-ретрив (`include_tests=False`) не меняется.

### 2. Шаг 4 — секция в скелете брифа

Новая строка в скелете (`SKILL.md:176-185`) **после** `## Relevant code` (`:183`) и **перед**
`## Constraints / open questions` (`:184`):

```
## Test exemplars — ≤3 test files/symbols, one line: «path:line — what's mocked / which pattern». (omit if none; dropped N: …)
```

Заголовок **английский** (`## Test exemplars`) — консистентно с остальными заголовками скелета
(`## Relevant code`, `## Constraints / open questions`). Русская формулировка задачи «Тесты для
образца» — это смысл секции, а не дословный заголовок.

### 3. Правки фильтра релевантности (шаг 4)

Чтобы новая секция жила по тем же правилам, что Related work / Relevant code:
- **Caps** (`SKILL.md:158-159`): добавить к перечню колпаков «· ≤3 test files/symbols in
  Test exemplars».
- **Report what you dropped** (`SKILL.md:168-169`): добавить секцию Test exemplars к перечню секций,
  завершаемых `(dropped N: reason)`.

### 4. Guard-тест

Новый тест в `tests/skills/test_solve_task_brief.py` (по конвенции файла — стабильные маркеры
наличия фичи, не дословные формулировки, чтобы будущая правка не удалила секцию молча):

```python
def test_solve_task_includes_test_exemplars():
    """PRI-162: solve-task подмешивает тест-образцы (include_tests) для TDD-хендоффа."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "include_tests=True" in text     # тест-ретрив в шаге 3
    assert "Test exemplars" in text         # секция скелета брифа
```

## Критерии приёмки (из задачи)

- На задаче с похожими протестированными фичами бриф содержит ≤3 тест-образца с `path:line`.
- Обычный код-ретрив (без тестов) не меняется (`include_tests=False` по умолчанию).
- Downstream TDD получает конкретный паттерн для mimic; fail-open при отсутствии тестов.
- Guard-тест `test_solve_task_includes_test_exemplars` зелёный; существующие тесты
  `test_solve_task_brief.py` не сломаны.

## Отклонённые альтернативы

- **Тест-ретрив всегда (не optional).** Лишний Voyage-запрос на задачах без протестированных
  аналогов + пустая секция. Гейтинг «when concrete symbols surfaced» зеркалит уже принятый
  паттерн graph-deepen.
- **Переиспользовать код-запрос с `include_tests=True`** (вместо целевого тест-запроса). Проще, но
  запрос «про фичу», а не «про её тестирование» → менее релевантные тесты. Отклонено в брейнсторме.

## Связанные задачи

- **PRI-161** — структурный прецедент: под-шаг ретрива в шаг 3 (subsystem prior) + секция в скелете
  (`## Subsystems`). PRI-162 повторяет ровно эту форму правки.
- **PRI-164** — свежайшая правка SKILL.md + guard-тест в `test_solve_task_brief.py` (образец, как
  дописать под-шаг и тест).
- **PRI-146** — ранговый relevance-фильтр (колпаки, `(dropped N: …)`), который переиспользуется.
- **PRI-138** — проброс `include_tests` в `search_codebase` (механизм; уже смержен).

# Дизайн — ID-158 (PRI-158): tree-sitter структурный diff

**Дата:** 2026-06-22
**Статус:** одобрен (брейншторм), готов к плану реализации
**Доска:** колонка «Движок (reviewer CLI/MCP)», `completed: false`
**Оценка:** M

## Проблема

Агенту PR-ревью подаётся **сырой unified-diff**. Для анализа контракта и blast-radius
(«изменена сигнатура `foo`», «добавлен метод `X` в класс `Y`», «удалён параметр») сырой diff
многословен и требует от агента самостоятельно восстанавливать символьную картину — это лишние
токены и навигационные round-trips (`read_file`/`get_definition`).

Структурное представление изменений символов точнее и компактнее. Сейчас в коде его **нет**:
есть лишь смежный on-demand тул `get_impact` (`reviewer/tools/impact.py`), который детектит смену
сигнатуры **парных** символов ради поиска внешних вызывающих, но не даёт общей структурной сводки
(add/remove) и не подмешивается в units.

## Цель и критерий приёмки

- Построить структурный diff на tree-sitter: сопоставить символы файла **до/после** (base vs head),
  классифицировать `add` / `remove` / `signature-change`.
- Подмешивать компактную структурную сводку в PR units рядом с raw-patch, чтобы её видел
  analyze-этап (субагенты), и научить промпт скилла её использовать.
- **Критерий приёмки:** на PR с изменением сигнатуры агент получает явную структурную сводку;
  качество blast-radius/correctness растёт, суммарных токенов на навигацию по diff меньше.

## Решения брейншторма

1. **Scope:** движок + промпт скилла (без обновления промпта токен-выигрыш не материализуется).
2. **Источник «до»-символов:** `chunk_python` по base-исходнику (симметрично head), не из стора —
   ловит add/remove/signature-change полным набором символов и не зависит от свежести индекса.
3. **Граница модуля:** новый `reviewer/index/struct_diff.py`; `extract_signature` переносится туда
   из `tools/impact.py` (чистые слои: `index/` ниже `tools/`).
4. **Политика файлов:** структурную сводку считаем только для изменённых на месте файлов
   (`status == "modified"`); added/renamed пропускаем (для нового файла «всё добавлено» — шум).

## Архитектура и компоненты

### 1. `reviewer/index/struct_diff.py` (новый)

Чистые функции, без сети/БД:

- `extract_signature(node_text: str) -> str | None` — **перенос** из `reviewer/tools/impact.py`.
  Поведение без изменений (заголовок def/async def/class, многострочные сигнатуры до `:` на нулевой
  глубине скобок, нормализация пробелов).
- `@dataclass SymbolChange`:
  - `kind: str` — `"signature_changed" | "added" | "removed"`
  - `fqn: str` — `Class.method` / `func` (как в `chunk_python`)
  - `symbol_kind: str` — `class | method | function`
  - `old_sig: str | None`, `new_sig: str | None`
  - `line: int | None` — head-строка для added/changed; base-строка для removed
- `diff_symbols(path: str, base_source: bytes | None, head_source: bytes) -> list[SymbolChange]`:
  - чанкает обе стороны через `chunk_python`, строит словари `fqn → Chunk`;
  - `added` = `fqn` в head, нет в base;
  - `removed` = `fqn` в base, нет в head;
  - `signature_changed` = `fqn` в обоих И `extract_signature(old.text) != extract_signature(new.text)`;
  - **чисто телесные правки (сигнатура та же) НЕ репортятся** — источник компактности;
  - `base_source is None` → все символы head как `added` (политику «пропускать added-файлы»
    реализует вызывающий, см. §3);
  - **fail-soft**: не бросает исключений (битый исходник → пустой/частичный результат).
- `format_struct_summary(changes: list[SymbolChange]) -> str`:
  - компактный текстовый блок (стиль PRI-126 — компактный вывод для агента);
  - `""`, если изменений нет;
  - порядок: `signature_changed` → `added` → `removed`;
  - кап ~40 строк, при превышении — хвостовая пометка «(…ещё N)».

Пример вывода:

```
Структурный diff:
  ~ сигнатура  ReviewService.prepare  было: def prepare(self, owner, name, pr_number)  стало: def prepare(self, owner, name, pr_number, vcs_provider=None)
  + добавлен   ReviewService._ensure_history  (method)
  - удалён     _legacy_helper  (function)
```

### 2. `reviewer/tools/impact.py` (правка — обратная совместимость)

`extract_signature` больше не определяется здесь, а ре-экспортируется:
`from reviewer.index.struct_diff import extract_signature`. Старые импорты
(`from reviewer.tools.impact import extract_signature`, в т.ч. в `tests/tools/test_impact.py`)
и `compute_impact` продолжают работать без изменений.

### 3. `reviewer/agent/state.py` (правка)

В `ReviewUnit` добавляется поле `structural_summary: str = ""` (дефолт сохраняет обратную
совместимость; `ReviewUnit` определён единожды — других определений нет).

### 4. `reviewer/services/review_service.py` (правка — вычисление в `prepare`)

В цикле сборки units (текущие строки 234–242):

- для файлов со `status == "modified"` догрузить base-исходник
  `vcs.get_file_at_ref(path, prq.base_sha)`;
- `diff_symbols(path, base_bytes, head_bytes)` → `format_struct_summary(...)` → положить в
  `ReviewUnit.structural_summary`;
- added/renamed файлы пропускаем (сводка остаётся `""`);
- весь блок под `try/except` с `log.warning` — **никогда не валит prepare** (сводка опциональна);
- цена: +1 GitHub-запрос на изменённый .py-файл (ограничено `review_max_files`);
- для eval-снапшотов (внешний `vcs_provider`) работает через тот же `get_file_at_ref(path, "base")`.

`changed_status` уже доступен в `prepare` (`{f.path: f.status}`), статус берём оттуда/из
`selected_files`.

### 5. `reviewer/mcp/service.py` (правка — `_prepared_payload`, строки 758–766)

В dict каждого юнита добавить `"structural_summary": u.structural_summary` **только когда непусто**
(не засорять payload и не тратить токены на пустые блоки).

### 6. `plugin/skills/review-pr/references/analyze-prompt.md` (правка — промпт)

Короткий блок-инструкция: если у юнита есть `structural_summary` — это компактная символьная сводка
(изменённые сигнатуры / добавленные / удалённые символы); использовать её для быстрой ориентации по
контракту и приоритизации blast-radius **до** чтения сырого diff; сырой patch остаётся источником
истины для номеров строк inline-комментариев. (При необходимости — короткая перекличка с
`blast-radius-prompt.md`, где живёт `get_impact`.)

## Поток данных

```
prepare (review_service)
  head_sources[path]                ← vcs.get_file_at_ref(path, head_sha)   (уже есть)
  base_source (только modified)     ← vcs.get_file_at_ref(path, base_sha)   (новое)
        │
        ▼
  diff_symbols(path, base, head) → format_struct_summary → ReviewUnit.structural_summary
        │
        ▼
  _prepared_payload → units[i]["structural_summary"]  (если непусто)
        │
        ▼
  analyze-этап (субагент) ← читает сводку по инструкции analyze-prompt.md
```

## Обработка ошибок (fail-soft)

- Base-исходник не дотянулся / tree-sitter упал / любой сбой → `structural_summary = ""`,
  ревью продолжается.
- Сводка строго **дополняет** raw-patch и никогда его не заменяет — номера строк для inline-
  комментариев не теряются.

## Тестирование

- `tests/index/test_struct_diff.py` (unit, без БД/сети):
  - `signature_changed` (добавлен параметр);
  - `added` символ, `removed` символ;
  - body-only правка (сигнатура та же) → пусто;
  - kinds: class / method / function;
  - многострочная сигнатура + декоратор (поведение `extract_signature`);
  - битый исходник → не падает;
  - `format_struct_summary`: формат, кап «(…ещё N)», пустой ввод → `""`;
  - `base_source is None` → все `added`.
- `tests/tools/test_impact.py`: существующие `test_extract_signature_*` остаются зелёными через
  ре-экспорт.
- `review_service` (на фейках): для modified-файла со сменой сигнатуры `unit.structural_summary`
  непуст; added-файл → пуст; сбой base-fetch → пусто и prepare не падает.
- `tests/skills/`: guard-тесты промптов не ломаются после правки `analyze-prompt.md`.

## Трейд-офф по токенам (честно)

Сводка **добавляет** небольшой блок в payload, но снижает **суммарные** токены сессии: агент реже
ходит в `read_file`/`get_definition` за ориентацией и быстрее видит контрактные изменения. Сырой
patch не урезаем (нужен для номеров строк inline).

## Вне scope (YAGNI)

- summary-режим в `get_changed_file_diff` (`reviewer/tools/code_tools.py`) — задача помечает
  низким приоритетом; не делаем в этой итерации.
- Не-Python языки (целевой язык анализа проекта — Python; не-py файлы уже отфильтрованы в
  `_select_changed_files`).
- Урезание/замена сырого patch структурной сводкой.

## Связи

- **PRI-126** (компактный вывод графовых тулов) — единый стиль компактного вывода для агента.
- Соседство с `get_impact`/`compute_impact` (blast-radius): структурный diff обобщает их гейт
  «сигнатура base != head» до add/remove и подаёт сводку проактивно в units (а не on-demand).

## Затрагиваемые файлы

| Файл | Изменение |
|---|---|
| `reviewer/index/struct_diff.py` | новый: `extract_signature` (перенос), `SymbolChange`, `diff_symbols`, `format_struct_summary` |
| `reviewer/tools/impact.py` | ре-экспорт `extract_signature` из `index.struct_diff` |
| `reviewer/agent/state.py` | поле `structural_summary: str = ""` в `ReviewUnit` |
| `reviewer/services/review_service.py` | вычисление сводки для modified-файлов в `prepare` (fail-soft) |
| `reviewer/mcp/service.py` | проброс `structural_summary` в `_prepared_payload` (если непусто) |
| `plugin/skills/review-pr/references/analyze-prompt.md` | инструкция использовать сводку |
| `tests/index/test_struct_diff.py` | новый: unit-тесты |
| `tests/services/test_review_service.py` | проверка установки `structural_summary` на фейках |
| `tests/mcp/test_service.py` | проверка проброса `structural_summary` в payload (по необходимости) |

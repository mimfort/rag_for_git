# Дизайн: расширение возможностей агента ревью (Тир 3)

Дата: 2026-06-07

## Цель

Поднять качество и глубину авто-ревью, дав агенту больше возможностей:
точные инструменты работы с кодом, контекст PR целиком, агентную верификацию
находок и кросс-файловый синтез. Бенчмарк-харнес сознательно **вне скоупа** —
проверка результата ручная, на уже открытых PR в `github.com/mimfort/rag-demo`
(где агент ранее ревьюил), сравнением «было/стало».

### Проблемы текущей системы (что чиним)

- Инструментов всего два: `search_code` (фаззи-чанки) и `get_related_symbols`
  (ненаправленный 1–2 хоп граф). Агент **не может прочитать конкретный файл/диапазон
  целиком** — чтобы проверить контракт функции, видит лишь похожий чанк.
- Нет **интента PR** (заголовок/описание) и нет взгляда на остальные изменённые
  файлы — каждый файл ревьюится в изоляции; кросс-файловые баги (поменяли сигнатуру
  тут — не обновили вызов там) невидимы.
- Граф используется грубо: `expand` ненаправленный, нет «кто вызывает».
- Верификация — one-shot классификация списка находок, без возможности проверить
  код.

## Не-цели (YAGNI)

- Никакого eval/benchmark-харнеса, размеченных датасетов, метрик precision/recall.
- Не трогаем индексацию, freshness, политику гейтинга, формат публикации.
- Не меняем модель-агностичный разбор JSON (`_extract_json`, fail-open) —
  см. CLAUDE.md, это намеренно.

## Архитектура изменений

Граф LangGraph меняется минимально по топологии — добавляется один узел:

```
plan → (fan-out per file) analyze → verify → synthesize → assemble → publish
```

Узлы `analyze`/`verify`/`synthesize` получают доступ к расширенному набору
инструментов через обогащённый `ToolContext`. Всё внешнее (VCS, граф, store)
остаётся за прежними интерфейсами.

## Компоненты

### A. Обогащённый `ToolContext` + новые инструменты — `reviewer/tools/code_tools.py`

`ToolContext` получает дополнительные поля:
- `read_file_fn: Callable[[str], str | None]` — привязка к `vcs.get_file_at_ref`
  на `head_sha` (читаем head-версию любого файла репо);
- `patches: dict[str, str | None]` — диффы всех изменённых файлов PR;
- `store: object` — индекс-стор (для `get_definition` через `fetch_nodes`).

Новые инструменты (существующие `search_code`, `get_related_symbols` сохраняются):

- `read_file(path: str, start: int = 1, end: int = 400) -> str`
  Точный исходник файла на head-ревизии, строки `[start..end]`, с номерами
  (`N|код`). Окно капится: если `end-start > 400` — берём первые 400 строк
  и помечаем усечение. Файл не найден → `"(файл не найден: ...)"`.

- `get_definition(symbol: str) -> str`
  Резолв имени символа в `node_id` через `graph.find_symbol(symbol)`; для лучшего
  совпадения берём узел(ы), затем исходник через `store.fetch_nodes`. Если граф
  пуст/нет совпадения — фолбэк на `search_code(symbol)`.

- `find_callers(node_id: str) -> str`
  Направленные входящие `CALLS` через `graph.callers([node_id])` — кто вызывает
  изменённую функцию (impact-анализ). Пусто → `"(вызовов не найдено)"`.

- `get_changed_file_diff(path: str) -> str`
  Возвращает `patches.get(path)` — дифф другого изменённого файла PR. Нет в PR →
  `"(файл не входит в изменения PR)"`.

Все инструменты — `StructuredTool.from_function`, докстринги на русском (стиль
проекта), отказоустойчивы (исключение не рвёт tool-loop — это уже гарантируется
обёрткой в `analyzer.py`, но сами функции тоже возвращают понятные строки).

### B. Расширение `GraphStore` — `reviewer/graph/store.py`

Два новых метода (Cypher), не ломающих существующие:

- `callers(node_ids: list[str]) -> set[str]`
  ```cypher
  UNWIND $ids AS sid MATCH (c:Symbol)-[:CALLS]->(s:Symbol {id: sid})
  RETURN DISTINCT c.id AS id
  ```
  Направленно: возвращает идентификаторы вызывающих.

- `find_symbol(name: str) -> list[str]`
  Совпадение по части `fqn` в `node_id` вида `path#fqn`. Реализация:
  ```cypher
  MATCH (s:Symbol) WHERE s.id ENDS WITH $suffix OR s.id CONTAINS $needle
  RETURN s.id AS id LIMIT 25
  ```
  где `suffix = "#" + name` (точное имя символа) приоритетнее `needle = name`.
  Возврат отсортирован: сначала точные `ENDS WITH`, потом `CONTAINS`.

### C. Контекст PR в анализе — `state.py`, `entrypoints/cli.py`, `agent/prompts.py`, `agent/analyzer.py`

- `Deps` получает поля `pr_title: str = ""`, `pr_body: str = ""`.
- `cli.py` (`review`) прокидывает `prq.title` и `prq.body` в `Deps`
  (поля у `PullRequest` уже есть — см. `vcs/base.py`).
- `LLMAnalyzer.analyze` строит human-промпт с **префиксом контекста PR**:
  - интент: `Заголовок PR: {title}` + `Описание PR: {body[:1500]}` (усечение);
  - манифест изменённых файлов: список `path (status)` по всем `changed_paths`
    (статус берём из `patches`/`ChangedFile`; если статуса нет — `modified`).
  Сами диффы других файлов **не дампим** — они доступны через
  `get_changed_file_diff` (экономия токенов).
- `ANALYZE_SYSTEM` дополняется: упоминание новых инструментов и явная инструкция
  при изменении сигнатуры/контракта проверять вызовы (`find_callers`) и читать
  смежный код (`read_file`, `get_changed_file_diff`).

Манифест прокидывается в `ToolContext`/промпт через уже существующие
`changed_paths` + новый `patches` в контексте; статусы — из `ChangedFile.status`,
для чего `Deps` уже хранит `patches`, а статусы добавим в отдельный
`changed_status: dict[str, str]` (заполняется в `cli.py`).

### D. Агентный верификатор — `agent/analyzer.py`, `agent/nodes.py`, `agent/prompts.py`

`LLMVerifier` переписывается с one-shot списка на **поштучную агентную проверку**:

- Конструктор получает `max_iterations` (бюджет tool-loop на находку, дефолт 3).
- `verify(findings, deps)`:
  - формируем `ToolContext` (как в `analyze`, инструменты `read_file`,
    `find_callers`, `get_definition`, `search_code`);
  - **фильтр кого вообще проверять агентно**: находка идёт в агентную проверку,
    если `severity ≥ review_verify_min_severity` ИЛИ `confidence < 0.5`
    (порог — константа/настройка). Остальные проходят как `is_real=True`
    (дёшево, не теряем).
  - для каждой проверяемой находки — короткий tool-loop с системным промптом
    `VERIFY_SYSTEM` в режиме «опровергни»: прочитай реальный код вокруг
    `file:line`, проверь вызовы, реши `is_real`. Парсинг — `_extract_json`,
    **fail-open**: не разобрали вердикт → оставляем находку (как сейчас).
  - возвращаем `[f for f in findings if kept(f)]`.
- `VERIFY_SYSTEM` дополняется инструкцией использовать инструменты для проверки
  факта (не угадывать), сохраняя recall-safe политику (оставлять при сомнении).
- Включение — флаг `review_agentic_verify` (см. ниже). Выключен → прежний
  one-shot путь (оставляем старую ветку как fallback для сравнения).

`make_verify_node` остаётся точкой вызова; меняется только реализация
верификатора + проброс `deps`/`ToolContext`.

### E. Узел кросс-файлового синтеза — `agent/nodes.py`, `agent/graph.py`, `agent/state.py`

Новый узел `synthesize` между `verify` и `assemble`.

- `ReviewState` получает поле для входа узла — переиспользуем `verified`
  (узел читает `state["verified"]`, пишет обновлённый список туда же либо в
  новое поле `synthesized`; для простоты пишем в `verified`, `assemble` читает
  `verified` как и раньше — тогда новое поле не нужно).
- `make_synthesize_node(deps)`:
  - вход: все `verified` находки + манифест изменённых файлов + доступ к диффам;
  - tool-enabled (`read_file`, `get_changed_file_diff`, `find_callers`);
  - LLM-задача (структурированный JSON, как в `analyze`):
    1. **добавить** кросс-файловые находки, не пойманные пофайлово
       (сигнатура↔вызов, переименование↔использование, новый контракт↔старые
       вызовы);
    2. **дедуп/слияние** близких дублей между файлами;
    3 вернуть итоговый список находок (того же формата `Finding`).
  - парсинг и сборка `Finding` — переиспользуем хелперы из `analyzer.py`
    (`_extract_json`, модели `_Findings`); fail-open: не разобрали → возвращаем
    вход без изменений (не теряем находки).
- Топология `graph.py`: `add_node("synthesize", ...)`,
  `add_edge("verify", "synthesize")`, `add_edge("synthesize", "assemble")`.
- Включение — флаг `review_synthesis`. Выключен → прямое ребро `verify→assemble`
  (как сейчас).

### F. Настройки — `reviewer/config/settings.py`

Новые поля `Settings` (env-дефолты, чтобы можно было откатиться и сравнить):
- `review_agentic_verify: bool = True` — включает D;
- `review_synthesis: bool = True` — включает E;
- `review_verify_min_severity: str = "medium"` — порог severity для агентной
  проверки в D;
- `review_verify_max_iterations: int = 3` — бюджет tool-loop верификатора.

При `review_agentic_verify=False` и `review_synthesis=False` поведение совпадает
с текущим (модуль остаётся обратносовместимым).

## Поток данных (после изменений)

1. CLI: PR + title/body + changed files (path/status/patch) → `Deps`.
2. `analyze` (fan-out per file): промпт с интентом PR + манифестом; tool-loop
   с расширенным набором (`read_file`, `get_definition`, `find_callers`,
   `get_changed_file_diff`, прежние два) → findings.
3. `verify` (fan-in): агентная поштучная проверка (фильтр по severity/confidence)
   с инструментами; recall-safe fail-open → отфильтрованные findings.
4. `synthesize`: добавление кросс-файловых находок + дедуп по всему PR.
5. `assemble`/`publish`: без изменений.

## Обработка ошибок

- Любой инструмент при исключении возвращает понятную строку (tool-loop не рвётся);
  обёртка в `analyzer.py` уже ловит исключения инструментов.
- Парсинг LLM-вывода везде через `_extract_json` + fail-open: при неразборе
  верификации/синтеза находки **не теряются**.
- Граф/стор недоступны → инструменты возвращают «не найдено», агент деградирует
  до анализа по диффу (не падает).
- Флаги `review_agentic_verify`/`review_synthesis` позволяют откатить новые ветки
  целиком при регрессе.

## Тестирование

Unit (на фейках, внешние API не дёргаем — стиль проекта):
- `tools/test_code_tools.py`: новые инструменты на фейковых `read_file_fn`/
  graph/store/patches — корректные строки, обработка «не найдено», капы окна.
- `graph/test_store_*` (или фейк-граф): `callers` направленность, `find_symbol`
  приоритет `ENDS WITH` над `CONTAINS`.
- `agent/test_analyzer.py`: агентный верификатор — fail-open при неразборе,
  фильтр по severity/confidence (низкие проходят без tool-loop), отсев явного
  ложного срабатывания на фейковом LLM.
- `agent/test_synthesize` (или в существующем наборе нод): дедуп дублей,
  добавление кросс-файловой находки на фейковом LLM; fail-open.
- `agent/test_graph` / `test_app_wiring`: граф собирается с узлом `synthesize`;
  при выключенных флагах — прежняя топология/поведение.

Ручная проверка (как раньше): прогон `reviewer review mimfort/rag-demo <N>` на
уже открытых PR, сравнение находок «было/стало» (флаги вкл/выкл).

## Последовательность реализации

1. `GraphStore.callers` + `find_symbol` (+ тесты).
2. Обогащённый `ToolContext` + 4 инструмента (+ тесты).
3. Проброс PR-контекста (Deps/cli/prompts/analyze human-промпт) (+ тест).
4. Флаги в `Settings`.
5. Агентный верификатор (+ тесты), за флагом.
6. Узел `synthesize` + топология графа (+ тесты), за флагом.
7. Прогон на rag-demo, сравнение было/стало.

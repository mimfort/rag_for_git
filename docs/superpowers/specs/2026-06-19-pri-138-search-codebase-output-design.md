# PRI-138 — Оптимизация выдачи `search_codebase` (дедуп / номера строк / подрезка тестов)

- **Задача:** PRI-138 / ID-138 «Оптимизировать выдачу search_codebase (дедуп/номера строк/headers-only) — общий фикс для ask и solve-task»
- **Доска:** https://ru.yougile.com/team/686c049c8af8/#PRI-138 (статус: Бэклог)
- **Репозиторий / ветка:** `mimfort/rag_for_git` / `dev`
- **Дата:** 2026-06-19

## Контекст и проблема

Скиллы `/rag-reviewer:ask` и `/rag-reviewer:solve-task` ходят через один session-less тул
`search_codebase` (= метод `Retriever.search_base`). Прогон `ask` показал: гибрид-поиск поднимает
ядро с заголовками `path#fqn (path:start-end)` (recall и точность строк выросли), но по токенам
прогон нейтрален/в минус — выдача избыточна, а grounding всё равно требует повторного `Read`.

Три источника избыточности:

1. **Дубли вложенных чанков.** chunker (`reviewer/index/chunker.py`) эмитит чанк класса (полный
   диапазон) **и** вложенные чанки методов. `search_base` дедуплицирует `merged` **только по
   `node_id`** → класс `path#Foo` `[10-50]` и метод `path#Foo.bar` `[20-30]` остаются оба, текст
   метода идёт дважды (отдельно и внутри класса).
2. **Нет построчных номеров в теле сниппета.** Заголовок с диапазоном есть, но тело без номеров →
   модель не может цитировать `path:line` по выдаче и перечитывает тот же код через `Read` =
   двойной расход.
3. **Шум из тестов.** В выдаче `tests/.../test_*.py`, для вопросов «как работает» / контекста
   задачи это чаще балласт.

Движковый фикс делается **один раз** в `search_base` / `ContextPack.as_context` и автоматически
чинит оба скилла; различие — только guidance в SKILL.md.

## Решения (зафиксированы при brainstorming)

1. **Объём:** дедуп + построчные номера + `include_tests=False` по умолчанию + правки SKILL.md.
   Режим `headers-only` (bodies=False) **не делаем** — критерий №2 («номера строк ИЛИ headers-only»)
   закрыт номерами строк.
2. **Широта:** изменения только на **session-less пути** (`search_base` → тулы
   `search_codebase`/`definition`). PR-ревью (`retrieve` / `code_tools.py`) **не трогаем**.
   `as_context` получает opt-in флаг номеров с дефолтом «старое поведение».
3. **Замер (критерий №4):** юнит-тесты на детерминированную механику + ручной before/after длины
   выдачи на 1–2 типовых запросах (числа — в спек/PR). Отдельный session-less eval-харнесс не
   строим (готовый `eval/run_eval.py` заточен под PR-ревью).

## Дизайн

### 1. Дедуп вложенных чанков

Чистый module-level хелпер в `reviewer/retrieval/retriever.py`:

```
_dedupe_overlapping(items) -> list
```

- группировка по `path`;
- чанк дропается, **если его `[start_line, end_line]` полностью вложен** в диапазон другого
  удержанного чанка того же `path` (правило «оставить самый широкий»);
- порядок выживших стабилен (сохраняет входной RRF/score-порядок).

Корректность: чанк удаляется только когда присутствует содержащий его удержанный чанк (класс уже
включает текст метода) → редундантность всегда уменьшается, потери информации нет. Если матчнулся
только метод (без класса) — метод остаётся.

Применяется в `search_base` к `merged.values()` **до** rerank/`top_k` (реранкер не тратится на
дубли, исчезает `[...truncated]`-артефакт от повторов). `retrieve` (PR-путь) не меняется.

### 2. Построчные номера в `as_context` (opt-in)

`ContextPack.as_context` (`reviewer/retrieval/retriever.py:16`):

```
def as_context(self, line_numbers: bool = False) -> str:
```

- при `line_numbers=True` тело рендерится с **абсолютными** номерами строк (`start_line + i`),
  формат `{n:>5} | {code}`; заголовок `// {node_id} ({path}:{start}-{end})` сохраняется;
- дефолт `False` → все прочие вызовы (в т.ч. `code_tools.py` PR-пути) остаются байт-в-байт.

**Единый источник формата заголовка.** `definition` (session-less, `reviewer/mcp/service.py:400`)
сейчас дублирует формат заголовка вручную (`service.py:418`). Переводим graph-ветку на
`ContextPack(items=nodes).as_context(line_numbers=True)` — дублирование уходит.

Вызовы:
- `service.py:364` `search_codebase` → `pack.as_context(line_numbers=True)`;
- `service.py` `definition` (обе ветки: graph-hit и фолбэк) → `as_context(line_numbers=True)`.

### 3. Подрезка тестов (`include_tests=False` по умолчанию)

Предикат `_is_test_path(path)` (репо-конвенция): путь содержит `tests/` **или** basename
соответствует `test_*.py` / `*_test.py`.

- фильтр применяется один раз к `merged` в `search_base`, когда `not include_tests`;
- проброс флага: MCP-тул `search_codebase` (`reviewer/entrypoints/mcp_server.py:110`,
  добавить `include_tests: bool = False`) → `MCPReviewService.search_codebase` → `search_base`;
- **`definition` тесты НЕ фильтрует** — запрашиваемый символ сам может быть тестом; зовёт
  `search_base(include_tests=True)`.

### 4. Guidance в SKILL.md

- **`plugin/skills/ask/SKILL.md`:** по умолчанию **пропускать** граф-тулы
  (`related_symbols`/`callers`/`definition`) — архитектурный вопрос их редко требует; упомянуть
  CLAUDE.md/README как дешёвый приор. Раз `search_codebase` теперь даёт дедуплицированный сниппет
  **с номерами строк** — цитировать `path:line` можно прямо по выдаче тула, отдельный `Read` для
  grounding не обязателен (Read — только при усечении сниппета или нужде в окружающем контексте).
  Это смягчение «hard rule» grounding-контракта.
- **`plugin/skills/solve-task/SKILL.md`:** граф-тулы **оставить** (blast radius), но раскрывать
  только символы, центральные для задачи; пользоваться номерами строк для точных ссылок в брифе
  без повторного `Read`.

## Контракты / инварианты

- `as_context()` без аргументов (дефолт) рендерит ровно как раньше — PR-путь (`code_tools.py`) не
  затрагивается.
- `search_codebase` по умолчанию: без вложенных дублей одного символа, с номерами строк, без
  тест-чанков. Опт-ин `include_tests=True` возвращает тесты.
- `definition` остаётся консистентным по формату с `search_codebase` (номера строк) и продолжает
  находить символы-тесты.
- `retrieve` (PR-ревью) и его потребители (`code_tools.py`) — без изменений поведения.

## Тестирование

Юнит:

- `_dedupe_overlapping`: вложенные диапазоны дропаются (остаётся самый широкий); непересекающиеся и
  частично пересекающиеся остаются; порядок стабилен; разные `path` независимы.
- `as_context(line_numbers=True)`: номера абсолютны и совпадают со `start_line`; заголовок цел;
  дефолт (`False`) идентичен прежнему выводу.
- `_is_test_path`: ловит `tests/...`, `test_*.py`, `*_test.py`; не ловит обычный код.
- `search_base`: по умолчанию фильтрует тест-чанки; `include_tests=True` их возвращает; дедуп
  применён до rerank/top_k.
- `definition`: тесты не режет; рендерит с номерами строк; дублирование формата `:418` убрано.

Обновить затронутые: `tests/retrieval/test_search_base.py`, `tests/retrieval/test_retriever_branch.py`,
`tests/mcp/test_service.py`. `tests/retrieval/test_store_hybrid.py` менять не нужно —
`store.hybrid_search` не трогаем (дедуп/фильтр в `search_base`); прогнать как sanity-check.

Замер (критерий №4): ручной before/after длины выдачи `search_codebase` на 1–2 типовых запросах,
числа зафиксировать в PR.

## Затронутые файлы

| Файл | Изменение |
|---|---|
| `reviewer/retrieval/retriever.py` | `_dedupe_overlapping`, `_is_test_path`; `search_base` (дедуп + фильтр тестов + флаг `include_tests`); `as_context(line_numbers=…)` |
| `reviewer/mcp/service.py` | `search_codebase` (проброс `include_tests`, `as_context(line_numbers=True)`); `definition` (через `as_context`, без ручного формата, `include_tests=True`) |
| `reviewer/entrypoints/mcp_server.py` | параметр `include_tests: bool = False` в MCP-туле `search_codebase` |
| `plugin/skills/ask/SKILL.md` | guidance: пропуск графа по умолчанию; цитирование `path:line` по выдаче |
| `plugin/skills/solve-task/SKILL.md` | guidance: граф оставить, раскрывать центральные символы, номера строк |
| `tests/...` | новые юниты + обновление затронутых |

## Вне объёма (YAGNI)

- Режим `headers-only` (`bodies=False`).
- Изменения PR-ревью пути (`retrieve` / `code_tools.py`).
- Отдельный session-less eval/token-харнесс.
- Дедуп/фильтр в `hybrid_search` (store) — делаем в `search_base`, чтобы не задеть PR-путь.

## Открытые вопросы / координация

- Согласовать с **PRI-113** (та же поверхность `search_codebase`: авто-derive repo + recovery-hint)
  при мерже, чтобы не конфликтовать по сигнатуре тула.

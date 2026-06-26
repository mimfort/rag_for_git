# PRI-164 — solve-task brief hygiene: резолв subtask-criteria + дедуп related-источников

**Задача:** https://ru.yougile.com/team/686c049c8af8/#PRI-164
**Бриф:** `docs/superpowers/briefs/2026-06-26-PRI-164-solve-task-brief-hygiene.md`
**Размер:** S (низкий приоритет). **Слой:** плагин/скил `solve-task`.

## Проблема

Скил `solve-task` собирает бриф для последующей разработки. Две гигиенические дыры:

1. **criteria всегда пусты при тонком description.** Store-first `get_task` (`reviewer/tasks/service.py:259-267`) и server-side `normalize_yougile` (`reviewer/tasks/boards/yougile.py:64`) оба возвращают `criteria=[]` — критерии живут в `description`. Это осознанный YAGNI, пока приёмку пишут инлайн. Но для задач, где приёмка вынесена в **подзадачи**, бриф теряет критерии.
2. **Нет дедупа related-источников.** Секция «Related work» собирается из двух источников — `get_task_context` (граф-связанные) ∪ `search_tasks` (семантически похожие) — без указания дедупа; одна задача может попасть в бриф дважды.

## Решение (обзор)

Обе части — **изменения только в `plugin/skills/solve-task/SKILL.md`** + guard-тесты в `tests/skills/`. Без правок движка, БД и миграций.

**Серверный путь явно отложен (out of scope).** `TaskRow` (`reviewer/tasks/service.py:52-55`) не имеет колонки `criteria` — поле используется лишь для текста эмбеддинга (`build_task_text`, `service.py:41`). Поэтому server-side резолв criteria — это DB-миграция (новая колонка + протяжка через `index_task`/`index_batch` + чтение в `get_task`), что несоразмерно размеру S. Если criteria из подзадач понадобятся и в `review-pr` без LLM-токенов — отдельная задача.

## Часть (a): резолв subtask-criteria при тонком description (client-side)

**Где:** шаги 2–3 `SKILL.md`.

**Механика (board-MCP-фолбэк):**

1. После идентификации задачи (store-first hit отдаёт `criteria=[]`), оценить «тонкий ли `description` на критерии».
2. **Детектор «тонкого» description:** в `description` **нет** секции-заголовка, матчащего `(?i)(критери|приёмк|acceptance)`. Если такой заголовок есть → критерии инлайн → ничего не делаем (`criteria` остаётся `[]`; `description` и так несёт требования — порт ноты плейбука `task-context-yougile.md:44-47`).
3. Если description тонкий **И доска подключена** (`task_board` резолвлен, board-MCP доступен):
   - один board-MCP `get_task(key)` → прочитать `subtasks[]`;
   - если `subtasks[]` непуст — резолвить титул каждой подзадачи в `criteria[]` **ровно по плейбуку** `../review-pr/references/task-context-<task_board.type>.md` (механика резолва subtasks→criteria уже описана там, строки 35, 44–47). Не дублировать механику в `solve-task`, а сослаться на плейбук (как это уже делает шаг 2 для miss-ветки).
4. Если подзадач нет, доска не подключена, или резолв упал → `criteria` остаётся `[]` (fail-open, как остальной шаг 3).
5. **Обогащённые `criteria` идут только в бриф** (контекст + файл) — НЕ ре-индексировать задачу (никакого `index_task`; синк уже персистил задачу). Дёшево, фолбэк-only.

**Зачем гейт «тонкий description»:** не тратить board-вызовы и LLM-токены на обычном инлайн-кейсе (большинство задач проекта PRI пишут критерии инлайн).

**В скелете брифа (шаг 4):** секция `## Task` уже включает `criteria` — резолвнутые подзадачи попадают туда.

## Часть (b): дедуп related-источников

**Где:** шаги 3 и 4 `SKILL.md`.

«Related work» собирается из `get_task_context` (linked) ∪ `search_tasks` (similar). Добавить явную инструкцию:

- **дедупить по каноническому ключу задачи** до применения капа ≤3;
- учитывать `PRI-N` ↔ `ID-N`: одна задача имеет канонический `key` (`ID-N`) и alias (`PRI-N`) — сопоставлять с учётом `aliases`, чтобы один и тот же таск из двух источников не задвоился;
- при коллизии оставлять **linked**-запись (богаче: несёт PR/граф-контекст из `get_task_context`), similar-дубль отбрасывать.

Прецедент формулировки уже есть в плейбуке: `task-context-yougile.md:37` дедупит `links[]` «merged and deduplicated by key».

## Тесты

Guard-тесты в `tests/skills/test_solve_task_brief.py` (репо-конвенция: `tests/skills/` стережёт инварианты `SKILL.md` grep'ом стабильных маркеров, не пиня формулировки). Добавить:

- **(a)** ассерт, что `SKILL.md` несёт инструкцию резолва subtask→criteria при тонком description (маркеры: упоминание `subtask`/«подзадач», детектор-критериев, ссылка на плейбук `task-context-`).
- **(b)** ассерт, что `SKILL.md` несёт дедуп related-источников (маркеры: «dedup»/«deduplicate», `linked`, `similar`).

Стиль — как существующие `test_solve_task_*` (grep по стабильным подстрокам). Прогон: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`.

## Инварианты / ограничения

- **Сохранить YAGNI инлайн-критериев:** при наличии секции критериев в `description` поведение не меняется (`criteria=[]`).
- **Fail-open везде:** нет доски / нет подзадач / сбой board-MCP / Neo4j down → бриф собирается без criteria-обогащения и без дедупа-падений; скил никогда не абортит (политика шага 3).
- **Никаких записей в движок:** часть (a) не вызывает `index_task` и не трогает стор/доску на запись.
- **Server-side criteria-колонка — вне скоупа** (см. «Решение»).

## Файлы

- `plugin/skills/solve-task/SKILL.md` — шаги 2–4 (обе части).
- `tests/skills/test_solve_task_brief.py` — guard-ассерты (a) и (b).
- (без изменений: `reviewer/tasks/*`, плейбук `task-context-yougile.md` уже содержит механику резолва subtasks).

# Brief — PRI-164 solve-task brief hygiene: резолв subtask-criteria при тонком description + дедуп linked vs similar
url: https://ru.yougile.com/team/686c049c8af8/#PRI-164

## Task
- **Слой:** плагин/скил `solve-task` (+ опц. движок `reviewer/tasks`). Качество, низкий приоритет (S).
- **(a) criteria из подзадач.** Store-first `get_task` и server-side `normalize` оба ставят `criteria=[]` (осознанный YAGNI: критерии живут в `description`). Но если приёмка вынесена в **подзадачи**, бриф её теряет. Надо: когда `description` тонкий на критерии (нет секции «Критерии приёмки/Acceptance») и у задачи есть subtask-links — дорезолвить их в `criteria[]` (board-MCP-фолбэк уже описан в плейбуке; опц. server-side в `yougile.py::normalize`).
- **(b) дедуп related-источников.** «Related work» собирается из двух источников — `get_task_context` (linked) + `search_tasks` (similar) — без дедупа; задача может попасть в бриф дважды. Шаг 4 должен явно требовать дедуп `linked ∪ similar` по ключу задачи.
- **Где:** `plugin/skills/solve-task/SKILL.md` (шаги 2–4); опц. `reviewer/tasks/boards/yougile.py`.
- **Критерии приёмки:** (1) задача с критериями-подзадачами и тонким description → непустые `criteria` в брифе; (2) «Related work» без дублей linked/similar.
- _Данные задачи — из стора reviewer (после preflight sync). Статус доски: «Плагин/агент (скилы)»._

## Related work
- **PRI-160 (ID-160)** — store-first `get_task`: тот самый путь, где `criteria=[]` зашит (`service.py:264`). Менять резолв criteria надо согласованно с ним.
- **PRI-153 (ID-153)** — `search_tasks`/`get_task_context`: ровно два источника related-work, которые (b) требует дедупить; несут relevance-score/усечение.
- **PRI-146 (ID-146)** — спека brief + relevance-фильтр (лимиты top-N, drop): задаёт скелет брифа и шаг 4, куда вписывается дедуп.
- (dropped 4: PRI-139 авто-линковка PR — другой механизм; PRI-161 приор сводок — другая фича; PRI-96 batch embeddings — Готово; PRI-162 include_tests для TDD — другая фича.)

## Subsystems
- **reviewer/tasks** — жизненный цикл задач (enumerate→normalize→index, store-first `get_task`); здесь живёт server-side опция (a) и инвариант идемпотентности по content_hash.
- **reviewer/mcp** — MCPReviewService экспонирует `get_task`/`get_task_context`/`search_tasks` как тулы (контракт, который потребляет скил).

## Relevant code
- `plugin/skills/solve-task/SKILL.md` — **главная цель правки** (шаги 2–4): нет ни резолва subtask-criteria при тонком description, ни дедупа linked∪similar. Подтверждено grep'ом — обе фичи отсутствуют.
- `reviewer/tasks/service.py:259-267` — store-first `get_task` жёстко возвращает `criteria: []` (строка 264); комментарий 249-250 фиксирует YAGNI «требования несёт description».
- `reviewer/tasks/boards/yougile.py:59-67` — `normalize_yougile` ставит `criteria: []` (строка 64), хотя `subtask_titles` уже инжектятся в `links` с `title` (строки 38-44) → server-side опция (a) почти бесплатна на уровне normalize.
- `reviewer/tasks/boards/yougile.py:134-146` — `YougileBoard.normalize` уже REST-резолвит титулы подзадач (`subtask_titles[sid]=f"{code}:{title}"`). Blast radius server-side пути: данные есть, но чтобы они дошли до брифа, нужно протащить criteria через `index_task` (`service.py:35`) + TaskStore-персист + снять хардкод `[]` в `get_task` (`service.py:264`).
- `plugin/skills/review-pr/references/task-context-yougile.md:35,44-47` — плейбук **уже** описывает резолв subtask-titles → `criteria[]` «только когда description тонкий», а строка 37 уже дедупит `links[]` по ключу (готовый прецедент формулировки для (b) и client-side пути (a)).
- (dropped 1: `reviewer/tasks/boards/base.py#TaskBoardProvider` — протокол, фон, не правим.)

## Constraints / open questions
- **Сохранить YAGNI инлайн-критериев.** Менять только ветку «description тонкий на критерии»; при инлайн-секции «Критерии приёмки» оставлять `criteria=[]` (плейбук:44-47). Нужно определить детектор «тонкого» description (отсутствие заголовка «Критерии приёмки/Acceptance»?) — дизайн-решение для brainstorming.
- **Две поверхности для (a):** client-side (скил резолвит подзадачи через board-MCP-фолбэк, когда description тонкий — плейбук review-pr это уже умеет) vs server-side (`normalize`→`criteria`, дёшево т.к. титулы уже фетчатся, но требует персиста через TaskStore + чтения в `get_task:264`). Выбор/оба — открытый вопрос.
- **(b) — чистая doc-правка** `SKILL.md` (шаги 3–4): добавить «дедуп linked ∪ similar по ключу задачи»; прецедент формулировки — плейбук:37.
- **PRI-164 ещё не реализована** — нет коммитов/PR, в `SKILL.md` нет обеих фич (проверено git log + grep).
- `get_task_context(PRI-164)` вернул только саму задачу (граф-связанных linked-задач/PR нет) — `get_pr_diff` тянуть не из чего; PRI-160/153/146 в description — «related»-ссылки без PR.
- Индекс свежий: drift=0, переиндексирован в `ca51509`, граф через SCIP (1888 узлов). Корпус задач прогрет (62 задачи). Доска yougile подключена, project=PRI, store-first hit.

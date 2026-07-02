# Brief — Post-PR task closeout: плагин закрывает задачу на доске после создания PR

## Task
Формулировка пользователя (board-less, ключа нет): после того как задача решена и **создан PR**, плагин
сам **предлагает обновить задачу на доске**:
- добавить в задачу **ссылку на PR** (и опционально — детали/заметку под задачу, если нужно);
- **пометить задачу выполненной**;
- сделать это так, чтобы **`updated_at`/last-modified задачи вырос** — тогда инкрементальный синк
  reviewer заново переиндексирует задачу (с новым статусом + PR-ссылкой).
- YouGile: «мб пробел в название добавить» — как надёжно двинуть last-modified; **узнать как лучше**.
- YouTrack: «уточнить как лучше, может редактирование описания снимает вотермарку» — **узнать**.

## Related work
(нет прямо информирующих задач на доске — фича новая, board-less)
(dropped 1: ID-203 «reviewer RAG за пределами брифа: грунтовка в фазах план/ревью» — другой механизм,
не про запись в доску; score 0.033, не информирует реализацию)

## Subsystems
- `reviewer/tasks` — пайплайн «доска → стор → граф»; `SyncService` с watermark-курсором, провайдеры
  YouGile/YouTrack (сейчас **read-only**). Ядро для watermark и любого write-пути.
- `reviewer/config` — `Settings.board_creds`/`task_board_api_base_for`: креды досок только в env,
  `board_config()` их не отдаёт. Важно, если write делать server-side.
- `reviewer/entrypoints` — MCP-сервер (FastMCP, 32 тула) + CLI; место регистрации нового write-тула.
- `reviewer/mcp` — `MCPReviewService` (сервисный слой); сюда ляжет метод нового write-тула.
- `plugin/hooks` — PostToolUse-хук (`brief_cost.py`) — готовый паттерн авто-триггера после действия.

## Relevant code
- `reviewer/tasks/sync.py:26` — `SyncService._cursor_ref` → курсор `tasks:<board_type>:<board>` в `index_meta`.
- `reviewer/tasks/sync.py:45` — watermark-гейт: переиндекс только если `raw.timestamp > cursor`; `:68` продвижение курсора. **Это и есть «вотермарка».**
- `reviewer/tasks/boards/yougile.py:175` — `timestamp = int(t["timestamp"])` (YouGile last-modified); поле, которое write обязан двинуть. Провайдер только GET (`_get_all`) — **write-метода нет**.
- `reviewer/tasks/boards/youtrack.py:82` — `timestamp = int(issue["updated"])` (YouTrack watermark = `updated`); правка описания/State двигает `updated` → «снимает вотермарку». `:152` `iter_raw` — только GET `/issues`, **write-метода нет**.
- `reviewer/entrypoints/mcp_server.py:104` — `sync_board`-тул: образец регистрации/делегации для нового `complete_task`/`update_task`-тула.
- `reviewer/config/settings.py` — `board_creds`/`task_board_api_base_for` (env-only креды по типу доски) — если write server-side.
- `plugin/skills/solve-task/SKILL.md` — старт трейса задача→бриф→…→PR; хэндофф кончается на brainstorming (PR создаётся позже).
- `plugin/skills/review-pr/references/task-context-yougile.md:34` — client-модель YouGile: `status = title колонки` (`get_column`), `completed`-флаг, коды `ID-N`/`PRI-N`. **`task-context-youtrack.md` отсутствует** (есть только jira+yougile).
- `plugin/hooks/hooks.json` + `plugin/hooks/brief_cost.py` — PostToolUse-матчер на тул (сейчас `Write`); паттерн для триггера после `gh pr create`.
- (внешний, superpowers) `finishing-a-development-branch` SKILL.md, Option 2 «Push and Create PR» — фактическая точка создания PR, но это superpowers-скилл, **плагин им не владеет** → интеграция обязана быть на стороне плагина (новый скилл или хук).
(dropped 0)

## Test exemplars
(на уровне каталогов из архитектурного приора; отдельный include_tests-поиск не гоняли — экономия Voyage 3 RPM)
- `tests/tasks` — watermark-идемпотентность синка, нормализация YouGile/YouTrack, purge; сюда лягут тесты write-метода провайдера.
- `tests/mcp` — тесты тулов (`sync_board` и пр.); сюда — тест нового write-тула.
- `tests/skills` — guard-тесты инвариантов SKILL.md (токены, include-маркеры); сюда — guard нового скилла/шага.
- `tests/config` — `board_creds`/`configured_board_types`; релевантно при server-side write.

## Constraints / open questions
- **Watermark-бамп бесплатен и уже решается PR-ссылкой.** Переиндекс гейтится `raw.timestamp > cursor`
  (YouGile `timestamp`, YouTrack `updated`). Любая мутация двигает last-modified → **запись PR-ссылки
  в описание САМА двигает вотермарку**; отдельный «пробел в названии» (YouGile) / «правка описания»
  (YouTrack) как самоцель не нужны — это и есть побочный эффект нужной записи. Open Q: подтвердить, что
  YouGile `completed:true`/перенос колонки **без** правки описания тоже двигает `timestamp` (если нет —
  всегда трогать description/title).
- **Write-путь асимметричен по доскам** (главная развилка для brainstorming):
  - YouGile: есть client-side board-MCP `mcp__yougile__update_task` → плагин пишет на стороне LLM (быстро, но только YouGile);
  - YouTrack: board-MCP в деплое **нет** и плейбука `task-context-youtrack.md` нет; провайдеры `reviewer/tasks/boards/` **read-only** (grep: write-методов нет).
  - Варианты: (a) client-side board-MCP (YouGile-only, YouTrack без решения); (b) **server-side reviewer MCP write-тул** + метод `write/complete` на `TaskBoardProvider` по каждой доске (единообразно, креды в env, можно инлайн-переиндексить) — но это **ещё раз разворачивает** инвариант «reviewer Python не трогает доску, кроме болк-синка».
- **Кросс-клиентность (жёсткое требование пользователя).** Должно работать не только в Claude Code, но и в Cursor / Codex / VS Code и т.п. `install.py` ставит reviewer-**MCP** во все эти клиенты единообразно → **портируемый механизм = MCP-тул сервера** (вариант (b) выше). Наоборот: **`hooks.json` (PostToolUse) — механизм Claude Code**, в Cursor/Codex не сработает → хук-триггер ломает кросс-клиентность. Скиллы устанавливаются во все клиенты (`install_skills`), но читаются по-разному → инструкция-в-скилле переносима «best-effort», сама запись — только через MCP-тул. **Вывод для brainstorming: write-способность держать в MCP-туле (портируемо); триггер — портируемым способом (скилл/инструкция), не CC-хуком.**
- **Где живёт триггер?** solve-task кончается на brainstorming; PR создаётся позже в `finishing-a-development-branch` (superpowers, не плагин), возможно в другой сессии. Варианты: (a) новый плагин-скилл `/reviewer_finish-task` (закрыть задачу после PR) — переносим; (b) PostToolUse-хук (паттерн есть — `brief_cost.py`) на `Bash gh pr create`/`git push` — **только Claude Code, не Cursor/Codex** (см. кросс-клиентность); (c) ключ задачи протащить через бриф (файл брифа уже фиксирует трейс) — скилл восстановит ключ из брифа.
- **Семантика «выполнено» различается.** YouGile: `completed:true` ИЛИ перенос в Done-колонку (status = имя колонки); что канонично — вопрос конфига. YouTrack: `State` (напр. команда `State Fixed` или PATCH customFields). Нужен per-board маппинг.
- **Опциональные «детали под задачу»** — писать только по confirm пользователя (запись в доску — outward-facing side effect → подтверждать до записи; что именно дописать — спросить).
- **Идемпотентность.** Повторный запуск не должен плодить дубли PR-ссылок (аналог фингерпринта `<!-- ai-review:hash -->` в комментариях).
- **board-less / без ключа.** Если задача решалась свободным текстом (как ЭТОТ прогон) — закрывать нечего → шаг обязан gracefully no-op.
- **Подтверждать запись.** Доска — внешний side effect; предлагать (offer), не писать молча.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 47.9K · out 58.7K · cache-write 474.4K · cache-read 2M
Всего: 2.6M токенов

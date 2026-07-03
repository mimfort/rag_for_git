# Brief — PRI-206 Ревью-дисциплина: blast-radius общих интерфейсов через reviewer (callers/search_codebase)
url: https://ru.yougile.com/team/686c049c8af8/#PRI-206
> Источник данных задачи — стор reviewer (после преполётного `sync_board`), не board-MCP.

## Task
- **Проблема:** когда дифф добавляет метод в общий `Protocol`/ABC/базовый класс (напр. `TaskBoardProvider`), каждый конформер (yougile, youtrack) обязан его реализовать; сейчас конформность держится на ручной внимательности ревьюера → пропущенная реализация в одном бэкенде = типовой баг, который граф кода ловит, а глаза — нет.
- **Мотивирующий кейс:** PRI-205 (`get_board_targets`/`list_done_targets`) и фикс `finish_task` write-through (`fetch_one`) — оба добавляли методы в Protocol сразу в оба провайдера вручную.
- **Предложение:** зашить в ревью-шаг обязательную проверку blast-radius интерфейс-правок — если дифф трогает Protocol/ABC/базовый класс, прогнать серверные тулы reviewer (`callers`/`search_codebase`/`related_symbols`) по символу и подтвердить, что все реализации/вызовы обновлены. Это форсированная версия «грунтовки reviewer» (PRI-203) для интерфейс-правок.
- **Scope:** skill/process, **НЕ движок** (кандидаты: `review-pr` скилл / reference-блок `_common/tool-usage.md` / раздел «Грунтовка reviewer» в CLAUDE.md).
- **Acceptance:** (1) при ревью диффа, добавляющего метод в Protocol с несколькими реализациями, ревьюер явно перечисляет конформеры и подтверждает покрытие через reviewer-тулы, а не «на глаз»; (2) fail-open: reviewer недоступен / индекс устарел → откат на grep/ручную проверку (как PRI-203).

## Related work
- **ID-203 [done]** — фундамент: «грунтовка reviewer в фазах план/ревью» (PR #90). **PR #90 = точный шаблон реализации**: новый `_common/*.md` блок + guard-тест в `test_common_blocks.py`; include-маркеры в SKILL.md + `test_assembled_prompts.py`; README EN/RU + CLAUDE.md-блок. PRI-206 — надстройка над этим паттерном.
- **ID-142 [done]** — механизм общих reference-блоков `_common/` (`<!-- include: -->`, нерекурсивный резолвер) + guard-тесты; определяет, куда физически ложится правка/новый блок.
- **ID-158 [done]** — tree-sitter структурный diff (`structural_summary`): `analyze-prompt.md:9-14` уже использует его «to prioritise blast-radius checks» → готовый хук триггер-детекции интерфейс-правки.
- **ID-145 [done]** — «неполнота графа → confidence/severity»: обосновывает fail-open-фрейминг находок при неполных IMPLEMENTS-рёбрах (не заявлять «безопасно» по короткому списку).
- **ID-155 [Движок]** — движковый blast-radius (`impact.py`: фильтр external-callers) — **граница, НЕ эта задача**: PRI-206 — процесс/скилл, движок не трогаем.
- (dropped 2: ID-144 общая калибровка confidence — покрыта findings-schema; ID-148 формат вывода графовых тулов — уже понятен из blast-radius-prompt.)

## Subsystems
> Правки — в markdown-скиллах (`plugin/skills/`), их нет в code-сводках; ниже — код-подсистемы, информирующие дизайн.
- `reviewer/tools` — `get_impact`/`compute_impact`: существующий blast-radius ловит **изменённую сигнатуру → ломает вызывающих**, но НЕ «новый метод в Protocol → неполные реализации» (иное направление).
- `reviewer/tasks` — `TaskBoardProvider` (Protocol) + конформеры Yougile/YouTrack: мотивирующий пример проверки.
- `reviewer/graph` — рёбра IMPLEMENTS даёт только SCIP; live-ревью инкрементально синкает граф tree-sitter'ом (CALLS-only) → IMPLEMENTS может отсутствовать (ключевой конструктивный риск).

## Relevant code
- `plugin/skills/review-pr/references/blast-radius-prompt.md` — **главный кандидат правки**: существующее blast-radius-измерение, `get_impact`-центричное (callers изменённой сигнатуры); НЕ покрывает «Protocol +метод → конформеры». Расширить триггером интерфейс-конформности либо вынести в новое измерение.
- `plugin/skills/review-pr/SKILL.md:98-102` — диспетч blast-radius-субагента (payload: диффы юнитов, `commentable_*`, repo/pr, тулы вкл. `get_impact`); правка payload/инструкции здесь.
- `plugin/skills/review-pr/references/analyze-prompt.md:9-14` — ориентация по `structural_summary` («prioritise blast-radius checks») — точка детекции интерфейс-правки в диффе.
- `plugin/skills/_common/tool-usage.md:11-22` — общий список PR-session тулов (`get_related_symbols`/`find_callers`/`search_code`); кандидат для конвенции «интерфейс → перечисли конформеров».
- `plugin/skills/_common/reviewer-grounding.md` — session-less блок грунтовки (для не-PR-session ревью-скиллов), эталон стиля fail-open.
- `reviewer/tasks/boards/base.py:43-89` — `TaskBoardProvider` (5 методов: iter_raw/normalize/finish/fetch_one/list_done_targets) — конкретный Protocol для примера/теста.
- `reviewer/tasks/boards/__init__.py:10-46` — `make_board_provider`: фабрика, перечисляющая конформеры (yougile/youtrack) — как enumerate реализации.
- `tests/skills/test_common_blocks.py`, `tests/skills/test_assembled_prompts.py` — guard-тесты сборки промптов; сюда ложатся новые ассерты (паттерн PR #90).

## Constraints / open questions
- **[дизайн для brainstorming]** расширить существующий `blast-radius-prompt.md` триггером интерфейс-конформности vs. отдельное измерение. Механизм иной: `get_impact` = callers изменённой сигнатуры; здесь нужны **реализации** интерфейса (IMPLEMENTS), не вызывающие.
- **[ключевой риск] IMPLEMENTS неполны в live-ревью:** рёбра IMPLEMENTS даёт SCIP; live-синк графа — tree-sitter (CALLS-only) → `get_related_symbols` может не вернуть конформеров. Обязателен фолбэк `search_code`(имя интерфейса) для поиска `class X(Protocol)`/подклассов (именно поэтому задача перечисляет `search_codebase` рядом с `related_symbols`). Python duck-typing: конформность структурная, не по ключевому слову → enumerate по имени + графу.
- **Anchoring:** пропущенная реализация живёт ВНЕ диффа (неизменённый `youtrack.py`), где GitHub запрещает inline → якорить на изменённой строке метода Protocol (`side: RIGHT`), конформеры перечислять в `message` (паттерн уже в `blast-radius-prompt.md:47-54`).
- **Триггер:** дифф модифицирует `Protocol`/`ABC`/базовый класс или сигнатуру с >1 реализации; детекция — через `structural_summary` (added/changed symbols) + распознавание `class X(Protocol)`/`(ABC)`/`@abstractmethod`.
- **Fail-open:** reviewer недоступен / индекс устарел → grep/ручная проверка (инвариант PRI-203); standalone-baseline и прочие скиллы не ломать.
- **Движок не трогаем:** никаких правок `impact.py`/`get_impact` (территория ID-155).
- **Реализация:** TDD-паттерн PR #90 — guard-тесты в `tests/skills/` первыми, затем правка скиллов; ветка off `dev`; коммиты Conventional Commits на русском без self-attribution.
- **Модель субагентов:** код через superpowers → Sonnet-субагенты (стоячее предпочтение); Fable не применять.
- Существующих артефактов PRI-206 (briefs/specs/plans) нет.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 49.2K · out 70.9K · cache-write 377.8K · cache-read 2.7M
Всего: 3.2M токенов

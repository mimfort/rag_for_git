# Brief — PRI-177 solve-task: трассируемость brief → spec (линк + Constraints)

https://ru.yougile.com/team/686c049c8af8/#PRI-177

## Task

- **Как поставлена:** SKILL.md-only правка шага 5 solve-task — инструктировать `superpowers:brainstorming`
  вставлять в спеку (а) секцию `## Brief` со ссылкой на файл брифа, (б) полную копию
  `## Constraints / open questions` из брифа **verbatim**.
- **Обоснование в задаче:** «3 из 56 спек имеют brief link, 0 из 56 копируют Constraints, 0 из 55 планов
  ссылаются на бриф/спеку» + пример потери на `2026-06-27-solve-task-brief-token-cost`.
- **Тесты по задаче:** guard-assertions в `tests/skills/test_solve_task_brief.py` на `"## Brief"`,
  `"Constraints"`, `"copy verbatim"`.
- `criteria=[]` в сторе; требования извлечены из description (раздел «Решение»).
- **Запрос пользователя поверх задачи:** оценить нужность; допустимо видоизменить или не делать;
  не должно сломать плагин (глобальный, мульти-CLI), только улучшить.

## Оценка (главный вывод брифа)

**Посылка задачи не подтверждается данными. Ценно ~20% скоупа (линк), 80% (verbatim-копия Constraints) — вредно.**

1. **Собственный пример задачи опровергает её тезис.** Бриф `2026-06-27-solve-task-brief-token-cost.md`
   содержит 6 constraints; в спеке `2026-06-27-solve-task-brief-token-cost-design.md` **все 6 присутствуют**,
   каждый — отдельной секцией: атрибуция окна → `§5.1`, дистрибуция хука → `§4.1`, цена/кэш → `§1.1` + `§5.2`,
   идемпотентность → `§7.1`, контракт stdin → `§8`, семантика «без плагина» → `§1` + `§2 Не-цели`.
   Задача измерила **наличие заголовка** `## Constraints`, а не наличие содержания.
2. **То же на втором проверенном кейсе.** Бриф `2026-07-20-PRI-212-session-keepalive.md` → спека
   `2026-07-20-pri-212-session-keepalive-design.md`: инвариант GC «не знаю живых ≠ живых нет» → `## Цели` п.3,
   единая семантика TTL → `## Цели` п.4, отказ от бампа `created_at` → `## Решение`, YAGNI по троттлингу → `## Не-цели`.
3. **Метрики устарели (задача написана до PRI-163/176-эффекта).** Сейчас: 29 брифов, 83 спеки, 83 плана.
   Спек со ссылкой на бриф — **26 из 83**, за июль **20 из 25 (80 %)**. Планов со ссылкой на бриф/спеку — **28 из 83**.
   Спек с секцией ограничений/рисков по широкому паттерну (`Риски`, `Открытые вопросы`,
   `Инварианты / ограничения`, `Constraints`) — **32 из 83**, за июль **9 из 25**. Не «3 из 56 / 0 из 56».
4. **Verbatim-копия ухудшает спеку.** Constraints брифа — это *открытые вопросы до дизайна*; спека — документ,
   который их *закрывает*. Копия verbatim положит «открытый вопрос: рефакторить ли 3 старых адаптера?»
   рядом с разделом, где ответ уже дан → документ противоречит сам себе.
5. **Бюджет токенов занижен ×5–10.** Задача оценивает +100–200 токенов. Фактические секции Constraints:
   `2026-07-25-PRI-217-…` ≈ 1200 токенов, `2026-07-20-PRI-212-…` ≈ 700.
6. **Конфликт с PRI-175.** PRI-175 вводит теги `[index_stale]`/`[boardless]`/`[summaries_missing]` в Constraints
   брифа. Это гэпы **этапа сбора контекста**, а не свойства дизайна; verbatim-копия унесёт их в спеку, где они
   бессмысленны. Две задачи семантически конфликтуют.
7. **Риск для глобального плагина — не «поломка», а углубление внешней связности.** `plugin/skills/`
   уже 7 раз хардкодит `docs/superpowers/…`, а Step 5 напрямую вызывает `superpowers:brainstorming`
   (README.md:104,691,707; README.ru.md:48,611,626). PRI-177 добавляет к этому диктовку **структуры выходного
   документа** сторонней, независимо версионируемой скилл-библиотеке, которая намеренно не задаёт шаблон спеки.
   Сломать не сломает (это текст промпта, fail-open по природе), но создаёт хрупкую точку вне нашего контроля.

**Рекомендация — урезать до одной строки:** в Step 5 просить brainstorming записать в спеку только
provenance-строку `Бриф: docs/superpowers/briefs/….md` + явно запретить копировать Constraints verbatim
(они — вход, который brainstorming обязан **разрешить**). Плюс один guard-тест. ~30 токенов вместо 700–1200,
закрывает реально недоделанные 20 % (5 из 25 июльских спек без ссылки), не может ухудшить спеку,
не диктует третьей стороне структуру документа.
Вариант «не делать вовсе» тоже защитим: 80 % адопшна достигнуто без вмешательства.

## Related work

- **PRI-175** (Плагин/агент, открыта) — стандартизованные теги рисков в Constraints брифа. Прямой семантический
  конфликт с verbatim-копированием; решать порядок/совместимость до реализации любой из двух.
- **PRI-163** (done) — персистентность брифа в файл; создала артефакт, на который ссылается `## Brief`. Именно она
  дала фактический рост brief-link до 80 %, без правки Step 5.
- **PRI-146** (done) — brief skeleton, ввёл саму секцию `## Constraints / open questions`.
- **PRI-176** (done) — проверка существующих briefs/plans/specs по ключу; ближайший прецедент правки
  «solve-task пишет/читает артефакты», тот же файл SKILL.md и тот же тест-файл.
- (dropped 4: PRI-187 «верификация brief перед hand-off», PRI-189 «sanity gate перед hand-off»,
  PRI-182 «флаг отсутствия тестовых примеров», ID-146-адъяцентные — тот же скилл, но иной механизм
  (качество брифа до handoff), реализацию PRI-177 не информируют.)

## Subsystems

- `tests/skills` — статические guard-тесты текстов `plugin/skills/*/SKILL.md` (regex/подстроки, без MCP и LLM);
  сюда ложится любой тест на Step 5.
- `plugin/hooks` — PostToolUse-хуки `brief_cost`/`brief_guard`, работают по пути `docs/superpowers/briefs/`,
  всегда fail-open; прецедент детерминированной (не промптовой) альтернативы, если линк захочется гарантировать.
- (остальные 4 из top-6 (`tests/hooks`, `tests/tasks`, `reviewer/tasks`, `tests/install`) — dropped:
  задача не трогает Python-ядро.)

## Relevant code

- `plugin/skills/solve-task/SKILL.md:265-270` — текущий Step 5 (handoff): передаёт путь брифа как seed,
  никаких инструкций про структуру спеки. **Единственный файл под правку.**
- `plugin/skills/solve-task/SKILL.md:239-262` — Step 4 persist: каталог, имя `ГГГГ-ММ-ДД-<KEY>-<slug>.md`,
  идемпотентный overwrite-glob. Источник пути, который должен попасть в спеку.
- `plugin/skills/solve-task/SKILL.md:10` — шапка скилла, уже фиксирует передачу в `superpowers:brainstorming`.
- `tests/skills/test_solve_task_brief.py` (140 строк, 14 тестов) — целевой файл для guard-теста; сейчас
  **ни один тест не покрывает Step 5/handoff** (grep по `brainstorming|handoff` пуст).
- `tests/skills/test_assembled_prompts.py:90-93` — `test_solve_task_assembled_has_branch_and_tools`, образец
  проверки собранного промпта solve-task.
- `tests/skills/test_readme_grounding_block.py:45-52` — образец assert-подстрок по тексту
  `plugin/skills/solve-task/SKILL.md`; ближайший шаблон для нового guard-теста.
- (dropped ~10: `reviewer/*` целиком, board-провайдеры, install-тесты — задача SKILL.md-only.)

## Test exemplars

- `tests/skills/test_readme_grounding_block.py:45-52` — читает SKILL.md через `_read(...)` и ассертит
  наличие/отсутствие подстрок; ровно нужная форма для «Step 5 упоминает `Бриф:` и запрещает verbatim».
- `tests/skills/test_configure_review_skill.py:97-101` — образец пары ассертов «инструкция есть» + «анти-инструкция
  есть» (`do NOT run`), точный прецедент для «`copy verbatim` не должен требоваться».
- `tests/skills/test_assembled_prompts.py:12-25` — `assemble()`, резолвер include-маркеров; нужен, только если
  правка попадёт в `_common/` (по рекомендации — не попадёт).
- `tests/skills/test_pr_walkthrough_skill.py:16-22` — проверка, что include-маркеры резолвятся в существующие файлы;
  сработает автоматически, если Step 5 обзаведётся новым include.
- (dropped 3: `test_summarize_subsystems.py`, `test_sync_tasks_guardrail.py`, `test_codex_plugin_payload.py` —
  другие скиллы/манифесты, паттерн тот же, ничего нового не дают.)

## Constraints / open questions

- **Скоуп РЕШЁН пользователем 2026-07-28: вариант (б)** — «только provenance-линк + явный запрет
  копировать Constraints verbatim». Варианты (а) не делать и (в) полный скоуп задачи — отклонены.
  Утверждённый текст правки Step 5 (дословно согласован):

  ```
  5. **Hand off to development.** Show the brief, state the saved
     file path, then invoke `superpowers:brainstorming` with the
     brief **file path** as the seed/context.
     **Ask brainstorming to record provenance in the spec:** a
     `Бриф: docs/superpowers/briefs/….md` line under the heading,
     so задача→бриф→спека→PR stays greppable.
     Do NOT ask it to copy `## Constraints` verbatim — those are
     open questions brainstorming is meant to RESOLVE, and a
     verbatim copy contradicts the spec that answers them.
  ```

  Существующие хвосты Step 5 (блоки «After the PR is created» про `/reviewer_finish-task` и
  «Board-less mode» про `/reviewer_create-task`) — **сохранить без изменений**.
- **Задачу PRI-177 на доске нужно переформулировать** под сокращённый скоуп (или закрыть с
  комментарием о ложной посылке) — текущее описание требует verbatim-копии и guard-ассерта
  `"copy verbatim"`, что прямо противоречит принятому решению.
- **Провенанс цифр.** Счётчики артефактов, содержимое `SKILL.md`, спек и брифов получены прямым
  `Bash`/`grep`/`sed` по рабочему дереву на `b9e1c8e`, не через retrieval-тулы — hook `brief_guard`
  пометит эти пути маркером «не в результатах поиска»; это ожидаемый false positive, не ошибка.
  Цитаты `tests/skills/*` — из `search_codebase` (номера строк оттуда).
- **Индекс актуален:** `reviewer index` прогнан в этой сессии, `drift = 0` @ `b9e1c8e`
  (383 файла, граф SCIP 4762 узла / 19087 рёбер).
- **`criteria=[]`** — в description нет раздела «Критерии приёмки»; формальных критериев приёмки у задачи нет.
- **Порядок с PRI-175 не определён.** Если делать (в), PRI-175 надо либо закрыть, либо переопределить.
  Для (б) конфликта нет.
- **Правка `plugin/` → пересборка манифестов.** Любое изменение контента под `plugin/` меняет codex
  payload-digest → обязателен прогон `update_codex_plugin_manifest.py`, иначе красные install-тесты.
- **Открытый вопрос при (б):** промпт-инструкция vs детерминированный PostToolUse-хук на запись
  `docs/superpowers/specs/*-design.md`. Хук надёжнее и бесплатен по токенам, но работает только в Claude Code
  (Codex/Cursor/Gemini хуки не исполняют) → для глобального плагина промпт покрывает больше хостов.
- **Что задача сама объявляет вне скоупа** (и это корректно): carry-forward в writing-plans и правка
  `brainstorming` SKILL.md — обе вне нашего репо.

Собран на: claude-opus-5 (session model), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 82 · out 41.1K · cache-write 237.1K · cache-read 3.2M
Всего: 3.5M токенов

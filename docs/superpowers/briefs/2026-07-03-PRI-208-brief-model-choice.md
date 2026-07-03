# Brief — PRI-208 [solve-task] Выбор модели для сборки брифа (спрашивать у юзера, рекомендовать дешевле, кросс-CLI)
https://ru.yougile.com/team/686c049c8af8/#PRI-208

## Task
Сейчас сборка брифа в `solve-task` (шаги 3–4: gather context + distill) идёт на дефолтной/сессионной
модели оркестратора. Для брифа топ-модель избыточна — это лёгкий reasoning поверх session-less MCP-тулов.
Нужно: **перед** сборкой брифа спрашивать у юзера, на какой из доступных моделей запустить, с рекомендацией
более дешёвого tier'а. Должно работать в **разных CLI** → спрашивать по tier'ам (cheap/mid/premium), не
хардкодя Claude-имена. Дефолт-рекомендация (решение юзера) — **Sonnet-класс (mid)**; Fable не рекомендовать.
Критерии: шаг выбора модели перед брифом · рекомендация дефолта · где харнесс умеет per-subagent override —
диспатч сабагента на выбранной модели, где нет — inline-фолбэк + пометка · fail-open · guard-тест в
`tests/skills/` · синк README EN+RU. (данные задачи — из стора reviewer после sync_board)

## Related work
- **ID-162** (done) [solve-task] include_tests для TDD-хендоффа — прецедент *формы* этой правки: новый шаг в
  `solve-task` + guard-тест в `tests/skills/` + синк README. Мимикрировать структуру.
- **ID-183** [solve-task] Параллельный wave-1 retrieval в Step 3 — Step 3 (gather) ровно та фаза, что оборачиваем
  в сабагента; правки пересекаются по региону → учесть взаимодействие при проектировании границы.
- Граница оркестратор↔сабагент: **ID-187** (верификация brief перед hand-off) и **ID-189** (sanity gate перед
  hand-off) остаются на оркестраторе (сессионная модель), а не на дешёвом сабагенте — держать hand-off вне
  «удешевлённого» участка.
- (dropped 3: ID-184 freshness-guard сводок, ID-181 детекция блокеров, ID-153 relevance-score — другой механизм;
  + необследованный хвост рельсы 8/30, не был нужен для реализации)

## Subsystems
- `reviewer` — `reviewer/install.py` перечисляет целевые CLI-клиенты (Cursor, VS Code, Claude Code, Gemini,
  Codex, Mimo, OpenCode, Kimi, Windsurf, Trae) — это и есть «разные CLI»; tier-абстракция должна их покрыть.
- `plugin/hooks` — хук `brief_cost` детерминированно считает LLM-токены брифа **по модели** и пишет блок в бриф;
  пропуск sidechain'ов — прямой конфликт с переносом сборки в сабагента (см. Constraints).

## Relevant code
- `plugin/skills/solve-task/SKILL.md` — **файл правки**: добавить шаг выбора модели ПЕРЕД шагами 3–4; шаги 3–4
  (gather+distill, стр. ~106–204) — то, что оборачиваем в сабагента/inline.
- `plugin/skills/summarize-subsystems/SKILL.md:48-65` — **образец паттерна для порта**: шаг 4 «Choose the summary
  model» (спроси tier, дефолт дешёвый) + шаг 5 «dispatch a subagent on the chosen model … where override is
  unavailable, write inline … and note this». Копировать структуру, поменять «summaries»→«brief».
- `plugin/skills/solve-task/SKILL.md:55-58` — существующий эскейп-хатч «Прогрею сам» (preflight шаг 0.4):
  прецедент кросс-CLI-варианта «запущу на своём CLI/дешёвой модели» — переиспользовать как фолбэк.
- `plugin/hooks/brief_cost.py:100-115` (`aggregate_usage`), `:88` (`find_window_start`) — ⚠️ считает токены по
  `message.get("model")` для assistant-ходов главного цикла, **пропуская sidechain** → сабагент-бриф обнулит/
  исказит блок «Модель: …». Blast radius.
- `plugin/skills/_common/` (anti-hallucination.md, branch-selection.md, tool-usage.md, …) — кандидат-локация
  для общего `model-selection.md` include (DRY с summarize-subsystems) — см. открытый вопрос.
- `reviewer/install.py` — `Client`/`build_plan`/`install_skills`: список поддерживаемых CLI (не редактируем, но
  это источник истины про «разные CLI» для формулировки tier-нейтральности).
- (dropped 0)

## Test exemplars
- `tests/skills/test_summarize_subsystems.py:35-53` — **образец guard-теста**: `test_skill_asks_model_choice`
  (assert `"Ask the user which model tier to use for writing summaries" in text` + дешёвый дефолт) и
  `test_skill_dispatches_subagent_on_chosen_model`. Для solve-task ввести уникальную фразу («… for building the
  brief») и зеркальные ассерты.
- `tests/skills/test_solve_task_brief.py:15-70` — **куда добавлять**: существующие guard-тесты solve-task читают
  `SKILL.md` и проверяют уникальные фразы шагов; добавить сюда ассерт нового маркера выбора модели.
- `tests/hooks/` (тесты `brief_cost`, `aggregate_usage`/`find_window_start` с пропуском sidechain) — если тронем
  хук ради корректного учёта subagent-токенов, расширить эти тесты.
- (dropped 0)

## Constraints / open questions
- ⚠️ **Хук `brief_cost` vs сабагент.** `aggregate_usage` пропускает sidechain-ходы; сабагент (Task) — sidechain →
  перенос сборки брифа в сабагента, скорее всего, обнулит по-модельный блок токенов. Развилка: (a) держать
  оркестрацию inline, а на сабагента выносить только распил/дистилляцию; (b) научить хук читать sidechain-
  транскрипт сабагента; (c) принять деградацию учёта и пометить. Решить в brainstorming.
- **Кросс-CLI без per-subagent override.** Харнессы без override (platform-refs superpowers: codex/pi/antigravity)
  → inline-фолбэк ИЛИ попросить юзера переключить модель/запустить самому (зеркало «Прогрею сам»). Спрашивать по
  tier'ам, не по именам моделей.
- **Открытый вопрос (DRY).** Общий `plugin/skills/_common/model-selection.md` include vs solve-task-local —
  первое даёт единый источник с summarize-subsystems, но требует и его миграции на include. Решить при проектировании.
- **Дефолт-tier = Sonnet-класс** (решение юзера). Память проекта: код через superpowers — Sonnet; Fable не применять.
- **Индекс dev отстаёт на 14 коммитов** (`drift=14`) — не реиндексировал: задача правит markdown-скиллы, code-индекс
  для неё нерелевантен. Якоря брифа — из grep/Read по markdown/hook-файлам, не из RAG.
- **Догфуд.** Этот бриф собран на сессионной модели (Opus); внедряемая фича рекомендовала бы Sonnet-класс — в
  следующий прогон запускать через новый шаг выбора модели.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 48.8K · out 92.8K · cache-write 444.3K · cache-read 3.4M
Всего: 4M токенов

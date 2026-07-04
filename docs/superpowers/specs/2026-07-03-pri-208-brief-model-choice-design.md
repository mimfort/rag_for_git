# PRI-208 — Выбор модели для сборки брифа в solve-task

**Задача:** https://ru.yougile.com/team/686c049c8af8/#PRI-208
**Бриф:** `docs/superpowers/briefs/2026-07-03-PRI-208-brief-model-choice.md`
**Тип:** правка Claude-Code-скилла (markdown) + guard-тест + доки. Python-код (в т.ч. хук
`brief_cost.py`) **не трогаем**.

## Проблема

Этап сборки брифа в `plugin/skills/solve-task/SKILL.md` (шаги 2–4: identify → gather → distill →
persist) выполняется на дефолтной/сессионной модели оркестратора. Для брифа топ-модель избыточна —
это лёгкий reasoning поверх session-less MCP-тулов ретрива. Нужно давать юзеру выбрать более дешёвую
модель под сборку брифа, с рекомендацией, и чтобы это работало в разных CLI (клиенты из
`reviewer/install.py`: Claude Code, Codex, Gemini, Cursor, Mimo, OpenCode, Kimi, Windsurf, Trae …).

## Решение (обзор)

Один новый шаг в `solve-task/SKILL.md` + переформулировка шагов 2–4 как «brief-building unit»,
диспатчащегося на выбранной модели, с inline-фолбэком. Образец паттерна — `summarize-subsystems`
SKILL.md, шаги 4–5. Текст шага — по-английски (тело скилла), с инструкцией общаться с юзером
по-русски (как в остальном скилле).

### Принятые решения (из brainstorming)

| Развилка | Решение |
|---|---|
| Что уезжает на выбранную модель | **Шаги 2–4** (gather + distill). Оркестратор держит интерактивный preflight (шаг 0) + hand-off (шаг 5). |
| Хук `brief_cost` (пропускает sidechain) | **Best-effort:** хук не трогаем; скилл дописывает в бриф строку «Собран на: <tier/модель>». Точные subagent-токены могут не попасть — помечаем. |
| DRY текста шага | **solve-task-local** (как summarize держит свой шаг инлайн; общий include не делаем). |
| Дефолт-рекомендация tier | **mid / Sonnet-класс.** Fable не рекомендовать (память проекта). |

## Обновлённый поток solve-task

- **Step 0 — Preflight** (оркестратор). Интерактивные гейты (реиндекс, прогрев сводок). Без изменений.
- **Step 1 — Config** (оркестратор). Резолв `task_board`. Без изменений.
- **NEW Step 1.5 — Choose the brief model** (оркестратор):
  - Спросить у юзера, на какой из доступных **в текущем CLI** моделей собрать бриф.
  - Спрашивать по **tier'ам (cheap / mid / premium)**, НЕ по конкретным Claude-именам → кросс-CLI.
  - Рекомендация-дефолт — **mid / Sonnet-класс**; пояснить, что топ-модель для брифа избыточна.
  - Запомнить выбор на прогон. Fail-open: отказ/молчание → дефолт-tier (или сессионная модель inline).
- **Steps 2–4 — Brief-building unit** (identify → gather → distill → persist):
  - **Путь A (харнесс умеет per-subagent model override):** диспатч сабагента на выбранной модели.
    Сабагент получает: reviewer session-less тулы (`get_task`, `search_codebase`,
    `get_subsystem_summaries`, graph-тулы, `get_task_context`, `search_tasks`, `get_pr_diff`) +
    harness `Read`/`Bash`/`Glob`/`Write` (для персиста брифа и idempotency-glob). Возвращает путь к
    файлу брифа + краткое резюме (что нашёл, что дропнул).
  - **Путь B (override недоступен — codex / pi / antigravity и др.):** inline-фолбэк — оркестратор
    выполняет 2–4 сам на сессионной модели. Дополнительно допускается эскейп-хатч «переключи
    модель / запусти сам» в духе preflight-опции «Прогрею сам» (шаг 0.4). Пометить, что сборка inline.
  - После сборки оркестратор дописывает в бриф строку-маркер:
    **`Собран на: <tier/модель>, режим: subagent | inline`** (наблюдаемость выбора).
  - **User-facing warn про существующие артефакты** (текущий шаг 4, «warn, don't block»): пред-скан и
    warn выполняет **оркестратор ДО диспатча** (сабагент неинтерактивен и не должен спрашивать юзера).
    Idempotency-glob (перезапись совпавшего брифа) остаётся в persist сабагента. Правило «don't block —
    continue unless user says no» сохраняется.
- **Step 5 — Hand-off** (оркестратор). Показать бриф, путь к файлу, invoke `superpowers:brainstorming`.
  Без изменений.

## Fail-open (инварианты устойчивости)

- Юзер не выбрал / отказался от выбора → дефолт-tier (Sonnet-класс) или inline на сессионной модели.
- Per-subagent override недоступен → путь B (inline). Никогда не блокируем.
- Ошибка/пустой возврат сабагента → оркестратор досбирает бриф inline на сессионной модели.
- Выбор модели НИКОГДА не должен ронять пайплайн — бриф всё равно собирается и передаётся в hand-off.

## Кросс-CLI

- Спрашиваем по **абстрактным tier'ам**, не по именам моделей → формулировка не зависит от конкретного
  CLI. Клиент сам мапит tier на доступную у него модель.
- Наличие per-subagent model override — свойство харнесса. Скилл описывает ОБА пути (A и B) и предписывает
  автодетект: где override есть — путь A, где нет — путь B. Референсы харнессов без override:
  superpowers platform-refs (codex / pi / antigravity).

## Guard-тест

Файл: `tests/skills/test_solve_task_brief.py` (туда же, где остальные guard-тесты solve-task; читают
текст `SKILL.md` и проверяют уникальные фразы шагов). Образец — `tests/skills/test_summarize_subsystems.py:35-53`
(`test_skill_asks_model_choice`, `test_skill_dispatches_subagent_on_chosen_model`).

Новые ассерты (уникальные фразы — их же вписываем в SKILL.md):
- Шаг выбора модели присутствует: assert фразы вида `"which model tier to use for building the brief"`.
- Рекомендация дефолта присутствует (mid / Sonnet-класс) — assert соответствующего маркера.
- Диспатч сабагента на выбранной модели + inline-фолбэк — assert фразы про subagent-on-chosen-model и
  про inline fallback (зеркало summarize-теста).

## Docs

- `README.md` (EN) + `README.ru.md` (RU): если solve-task описан — добавить строку про выбор модели для
  брифа (кросс-CLI, рекомендация дешевле). Если не упоминается — пропустить, отметив в отчёте
  (память проекта: README EN+RU держим синхронно).

## Вне скоупа (YAGNI)

- `plugin/hooks/brief_cost.py` и `tests/hooks/*` — не трогаем (best-effort учёт). Точный sidechain-учёт
  subagent-токенов — потенциально отдельная PRI-задача, не здесь.
- `summarize-subsystems` и общий `_common/model-selection.md` include — не делаем (local).
- Реиндекс base-индекса (dev drift=14) — нерелевантен для markdown-правки.

## Файлы, которые тронем

| Файл | Изменение |
|---|---|
| `plugin/skills/solve-task/SKILL.md` | Новый Step 1.5 (выбор модели) + переформулировка шагов 2–4 как brief-building unit (пути A/B) + строка-маркер «Собран на: …». |
| `tests/skills/test_solve_task_brief.py` | Новые guard-ассерты (выбор модели, дефолт, диспатч+фолбэк). |
| `README.md`, `README.ru.md` | Строка про выбор модели брифа (если solve-task описан). |

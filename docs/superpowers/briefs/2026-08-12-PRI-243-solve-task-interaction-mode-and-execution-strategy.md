# Brief — PRI-243 solve-task: выбор режима взаимодействия и стратегии исполнения на старте
https://ru.yougile.com/team/686c049c8af8/#PRI-243

## Task
solve-task сейчас безусловно передаёт бриф в `superpowers:brainstorming`
(`plugin/skills/solve-task/SKILL.md:282-292`), у которого зашит `<HARD-GATE>` на апрув дизайна
(`brainstorming/SKILL.md:12-14`) и отдельный «User Review Gate» на апрув спеки
(`brainstorming/SKILL.md:118-127`), а `writing-plans` в конце предлагает ровно два варианта
исполнения — subagent-driven / inline (`writing-plans/SKILL.md:150-168`). Нужно: (1) один опрос на
старте (AskUserQuestion) — тир модели брифа (существующий, `SKILL.md:91-94`) + режим взаимодействия
(обычный/auto/full-auto) + стратегия исполнения (inline/subagent/lite/auto); (2) в full-auto
записывать раздел «Решения, принятые за пользователя» в спеку; (3) новый файл-профиль `lite` в
`plugin/skills/` как дельты к `subagent-driven-development`; (4) персист режима+стратегии в файл
брифа; (5) явные подтверждения перед git push/PR/записью в доску даже в full-auto; (6) директива
Task Right-Sizing (`writing-plans/SKILL.md:36-40`) на handoff; (7) guard-тесты + README×2 + пересборка
codex-манифестов. Полные критерии приёмки (12 шт.) — в описании задачи в сторе; ключевое сжато выше.

## Related work
- PRI-208 (ID-208, done) — существующий паттерн опроса тира модели (Step 1.5, `SKILL.md:91-94`):
  новый опрос должен слиться с этим же AskUserQuestion, а не дублировать вызов.
- PRI-163 (ID-163, done) — персист брифа в артефакт для выживания через компакцию
  (`SKILL.md:255-280`): тот же приём («сохранить блок в файл брифа») нужно применить к
  режиму/стратегии (п.6 задачи).
- PRI-146 (ID-146, done) — спека брифа + relevance-фильтр (Step 4 skeleton, `SKILL.md:211-251`):
  формат секций брифа, которому эта задача не следует менять, но новый опрос логически идёт перед
  Step 4/5.
(dropped 4: ID-141 «preflight индекс freshness» и ID-161 «subsystem summaries prior» — другой
участок пайплайна (Step 0), не про режим/стратегию исполнения; ID-194 «лексический fallback» —
не в скоупе; ID-162 «test exemplars» — не про handoff-гейты)

## Subsystems
- tests/skills — контрактные тесты assembled skill prompts, common blocks, store-first workflows,
  solve-task/preflight guardrails; новые guard-тесты этой задачи ложатся сюда же, рядом с
  `test_solve_task_brief.py`/`test_preflight_guardrail.py`.
- plugin/hooks — brief_cost/brief_guard/brief_post_write оркеструют пост-обработку файла брифа;
  релевантно, если новый блок режима/стратегии в брифе должен пройти те же хуки (проверить, не
  сломает ли добавление раздела guard на "observed paths"/маркеры).
(остальные найденные сводки — reviewer/tasks, reviewer/mcp, reviewer/services, reviewer/agent,
tests/entrypoints, tests/tasks — не относятся к промпт-слою плагина; опущены)

## Relevant code
- plugin/skills/solve-task/SKILL.md:91-94 — Step 1.5, существующий опрос тира модели («cheap /
  mid / premium»); AskUserQuestion-панель нового опроса должна объединить этот вопрос с двумя
  новыми (режим, стратегия), не задавая тир отдельно.
- plugin/skills/solve-task/SKILL.md:96-116 — «Brief-building unit», Path A/B диспатч Steps 2-4 на
  выбранной модели; fail-open паттерн (нет ответа → дефолт) — образец для дефолтов режима/стратегии
  (критерий приёмки №10: обычный режим, subagent стратегия).
- plugin/skills/solve-task/SKILL.md:255-280 — «Persist the brief»: точный механизм записи файла
  брифа (директория, имя файла, идемпотентность через glob-overwrite, warn on existing artifacts) —
  сюда добавляется блок с режимом/стратегией (п.6 задачи); маркер модели уже пишется по этому же
  паттерну на строке 112-114 («Собран на: …»).
- plugin/skills/solve-task/SKILL.md:282-292 — «Hand off to development»: точка, где сейчас
  безусловно вызывается `superpowers:brainstorming`; здесь нужно передавать выбранный режим как
  явную волю пользователя (п.7 задачи) и директиву Task Right-Sizing (п.8).
- plugin/skills/_common/ (anti-hallucination.md, tool-usage.md, branch-selection.md,
  findings-schema.md, bug-reporting.md, reviewer-grounding.md, dimension-scope.md,
  dimension-output-tail.md) — существующий механизм общих reference-блоков, подключаемых маркером
  `<!-- include: _common/<file>.md -->`; новый lite-профиль логично оформить похожим отдельным
  файлом в `plugin/skills/`, но НЕ через `_common/`-include (он не общий текст, а профиль-дельта,
  подключаемый директивой в handoff, не в самом solve-task).
- ~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/brainstorming/SKILL.md:12-14
  — `<HARD-GATE>` на апрув дизайна.
- ~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/brainstorming/SKILL.md:118-127
  — «User Review Gate» после спека-self-review; в auto/full-auto handoff должен явно отменять этот
  gate словами «апрув не требуется» как волю пользователя, а не обход инструкции.
- ~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/writing-plans/SKILL.md:36-40
  — «Task Right-Sizing» рубрика (задача = наименьшая единица со своим тест-циклом, которую ревьюер
  мог бы осмысленно отклонить) — точный текст для п.8 задачи (handoff-директива на укрупнение задач
  плана).
- ~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/writing-plans/SKILL.md:150-168
  — «Execution Handoff»: ровно два варианта (subagent-driven / inline execution); здесь нужно
  внедрить выбор стратегии (сделанный на старте solve-task) вместо повторного вопроса — п.1 задачи
  («стратегия применяется позже — после написания плана»).
- ~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/subagent-driven-development/SKILL.md:53-105
  — граф диспатча: implementer per task → review after each task → fix-loop до 5 раундов (эскалация
  модели на раундах 4-5, строка 174) → final whole-branch review (строки 74-105); lite-профиль
  (п.5 задачи) описывает дельты именно к этому графу — ревью после группы задач вместо каждой,
  сниженный потолок fix-раундов, финальное ревью остаётся обязательным.
- ~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/executing-plans/SKILL.md:1-29
  — стратегия `inline` — существующий целевой скилл, без изменений; используется как есть при
  выборе inline.
- scripts/update_codex_plugin_manifest.py — скрипт пересборки codex-манифестов; прогнать после
  любой правки под `plugin/` (version-bump или контент skills/prompts/references меняет
  payload-digest) — см. память «version-bump-requires-manifest-sync».
(dropped 0 — все найденные code-элементы напрямую информируют реализацию)

## Test exemplars
- tests/skills/test_solve_task_brief.py:20-162 — образец assembled-prompt тестов для solve-task:
  `test_solve_task_brief_spec_present` (наличие раздела в SKILL.md), `test_solve_task_persists_brief`
  (проверка персиста в файл), `test_solve_task_records_brief_model_marker` (проверка маркера «Собран
  на: …») — прямой шаблон для новых тестов «наличие обоих опросов / персист режима+стратегии в
  бриф / маркер full-auto решений».
- tests/skills/test_preflight_guardrail.py — образец guardrail-теста (по названию задачи —
  проверка, что пайплайн не проскакивает обязательный шаг); шаблон для теста «full-auto перед
  git push/PR/записью в доску запрашивает подтверждение» (граница full-auto, критерий №5).
- tests/skills/test_common_blocks.py — вероятно проверяет корректность `<!-- include: … -->`
  разворачивания; сверить перед добавлением нового профильного файла — если lite подключается
  похожим маркером, этот тест может быть шаблоном или потребовать расширения allowlist общих
  блоков.
(dropped 1: tests/skills/test_kimi_install_doc.py — не относится к промптам solve-task/lite)

## Constraints / open questions
- Индекс собирался по ветке `dev` (drift 0, 40 сводок); `primary=main` неактуален (drift `null`,
  индекс от 2026-08-08) — граундинг в этом брифе
  выполнен по `dev`, что и является текущей рабочей веткой задачи (`feat/pri-242-...` не
  отслеживается индексом).
- В сторе `criteria` для ID-297 пусты — все требования и критерии приёмки взяты из `description`
  задачи (12 пунктов приёмки), а не из поля `criteria`.
- `superpowers` — внешний плагин версии 6.2.0 (кэш
  `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/`); менять его нельзя. Все
  рычаги этой задачи — директивы на handoff и отдельный профильный файл поверх SDD; при обновлении
  superpowers процитированные строки и профиль могут разъехаться с его текстом — guard-тесты
  проверяют только наши файлы (`plugin/`, `tests/skills/`), не содержимое кэша superpowers.
- `get_task_context("PRI-243")` вернул только сам таск без связанных задач/PR — граф-связи по этой
  задаче пока пусты (задача ещё не имеет PR); related work собран через `search_tasks`.
- Признанный компромисс из описания задачи: full-auto подавляет канал уточняющих вопросов на
  брейншторме — уместен только для задач с полным описанием и критериями, а не для расплывчатых
  формулировок; это ограничение стоит явно зафиксировать в тексте вопроса о режиме (п.2 задачи
  требует пояснение при каждом значении).
- Скоуп по формулировке задачи: `plugin/skills/solve-task/SKILL.md`, новый файл-профиль в
  `plugin/skills/`, guard-тесты в `tests/skills/`, оба README, манифесты. Сервер, `Settings`,
  `.review.yml` и разбор `$ARGUMENTS` не трогаются.

Собран на: mid (Sonnet), режим: subagent

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 31 · out 18.7K · cache-write 148.8K · cache-read 1M
Всего: 1.2M токенов

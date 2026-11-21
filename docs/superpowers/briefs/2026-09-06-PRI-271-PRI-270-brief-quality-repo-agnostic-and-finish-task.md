# Brief — PRI-271 + PRI-270 — репо-агностичное ядро метрики брифа + съём без ревью PR
https://ru.yougile.com/team/686c049c8af8/#PRI-271
https://ru.yougile.com/team/686c049c8af8/#PRI-270

## Task

Задачи парные, решаются одной веткой; порядок не критичен (независимы), но PRI-271 делает знаменатель метрики корректным вне rag_for_git, а PRI-270 даёт сами наблюдения (сейчас `brief_quality` — 0 строк при 22 реальных прогонах ревью).

### PRI-271 — Репо-агностичное ядро метрики качества брифа solve-task

Три тихих хардкода под rag_for_git:
1. `_KEY_RE = re.compile(r"(PRI-\d+)")` (`reviewer/metrics/brief_quality/briefs.py:20`) — бриф с ключом `RON-55` не распознаётся, молча выпадает из корпуса.
2. `is_core_production_path` (`reviewer/metrics/brief_quality/classify.py:7-23`) — ядро = `reviewer/**/*.py` + `plugin/**` (кроме `.md`) + корневые `*.py`; для чужого репо (например rondo: `app/**/*.py` + `frontend/src/**`) знаменатель пуст, и задача получает `empty_core_denominator`, неотличимый от честного «в диффе только тесты/доки».
3. `BRIEFS_DIR = REPO_ROOT / "docs/superpowers/briefs"` (`eval/solve_task_metrics/__main__.py:29`) — офлайн-харнесс не может посчитать чужой клон.

Что сделать:
1. Ключ задачи брать из уже существующего `task_board.key_pattern` в `.review.yml` (в этом репо — `PRI-\d+`, см. `.review.yml:9`), а не из константы модуля.
2. Ввести в `.review.yml` список glob-паттернов ядра (например `metrics.brief_quality.core_paths`); дефолт без ключа = нынешнее поведение rag_for_git.
3. `categorize_miss` сделать производной от того же списка.
4. Офлайн-харнессу дать `--repo-path`/`--briefs-dir`.
5. Развести статусы «ядро пустое» (в диффе нет файлов ядра) vs «ядро не сконфигурировано» (репо не настроено).

Критерии приёмки:
1. Клон rondo, ядро `app/**/*.py` + `frontend/src/**` → распознаёт `RON-55`, core-recall 85% (11/13) — совпадает с ручным замером 26.08.2026.
2. rag_for_git на дефолтном конфиге: `core_recall_median 0.5714`, `bulk_core_recall_median 0.3571` на корпусе 75 брифов — без изменений.
3. Репо без секции ядра → отдельный статус «не сконфигурировано», не ноль и не `empty_core_denominator`.
4. Офлайн-харнесс считает корпус rondo (28 брифов) штатной командой, без ручного скрипта.
5. Guard-тест на чтение конфига проверен мутационно (снять чтение ядра из конфига на копии вне рабочего дерева → тест обязан покраснеть).

### PRI-270 — Снимать качество брифа solve-task без ревью PR: finish_task + пересчёт из git

Онлайн-метрика снимается только в `publish_review` под гейтом `if not dry_run and posted and run_id is not None` (`reviewer/mcp/service.py:3199-3200`). Ревью PR запускается редко (22 прогона за всю историю), `brief_quality` пуста. `brief_quality.measure` требует ровно 4 вещи (`task_key`, `clone_path`, `changed_paths`, `changed_status`) — все доступны в `finish_task(key, pr_url, ...)`: ключ — аргумент, клон — из `repo_clone`, repo/PR резолвятся из `pr_url` тем же кодом, что дописывает бэклинк.

Что сделать:
1. Вынести съём из `publish_review` в сервисный путь, принимающий `(task_key, repo, pr, changed_paths, changed_status, clone_path)`; `publish_review` остаётся одним из вызывающих, поведение не меняется.
2. Снимать метрику в `finish_task`: repo/PR — тем же кодом, что бэклинк; дифф и статусы файлов — через VCS-провайдера; путь полностью fail-soft (как PRI-249).
3. Идемпотентность по `(repo, pr, task_key)`: повторный `finish_task` и последующий `publish_review` по тому же PR не создают вторую строку.
4. CLI `reviewer measure-briefs [--repo] [--path]` — пересчёт по PR-мержам клона (логика `ground_truth.collect` офлайн-харнесса), запись в ту же таблицу, без Voyage.
5. Развязать `brief_quality.run_id` с `review_runs` (nullable).

Критерии приёмки:
1. После `finish_task` на задаче с брифом — строка `status='measured'` с непустым `core_recall`, `review_runs` не пополняется.
2. Повторный `finish_task` той же задачи и последующий `publish_review` по тому же PR второй строки не создают.
3. `reviewer measure-briefs` на клоне rag_for_git заполняет исторические задачи, числа тождественны `python -m eval.solve_task_metrics snapshot` по совпадающим ключам.
4. Отсутствие брифа/недоступность VCS/неизвестный клон → поле `status`, не исключение наружу; `finish_task` возвращает прежние поля.
5. Строка пишется при `run_id IS NULL` — без единой строки в `review_runs`.

## Related work
- ID-303 [done] «Постоянная метрика качества брифа solve-task: попадание ретрива в реально изменённые файлы PR» — исходная online-метрика (PRI-249), которую PRI-270 расширяет второй точкой съёма; `brief_quality.measure`/схема таблицы — её наследие.
- ID-304 [done] «Офлайн-харнесс метрик solve-task: цена, качество ретрива, тренд и прогноз» — исходный харнесс (`eval/solve_task_metrics/`), который PRI-271 п.4 (`--repo-path`/`--briefs-dir`) и PRI-270 п.4 (`measure-briefs`, логика `ground_truth.collect`) расширяют напрямую.
- ID-308 [done] «Replay-режим офлайн-харнесса: прогон ретрива по корпусу против ground truth» — построил `eval/solve_task_metrics/replay.py`, три собственных вызова `is_core_production_path` (76, 153, 239) — тот же предикат, который PRI-271 обязана сделать конфигурируемым, паттерн переиспользования `ground_truth.collect` для `measure-briefs`.
- ID-315 [done] «Знаменатель контекста для метрики брифа: контекстное ядро из графа рядом с core-recall» — построил `reviewer/metrics/brief_quality/context_core.py`, который импортирует и вызывает `is_core_production_path` (context_core.py:67) — прямая часть «семьи» вызовов предиката, которую правка PRI-271 обязана не сломать.
- ID-320 [done] «Знаменатель context-recall неопределён у трети корпуса» — прецедент разделения статусов «знаменатель не определён» vs «знаменатель пуст» на соседней метрике (context-recall) — тот же паттерн, который PRI-271 п.5/крит.3 просит для core-recall.
- ID-300 [done] «Спайк: измерить фактическую цену и качество solve-task по накопленным артефактам» — исходный спайк-подход «посчитать вручную отдельным скриптом», ровно та боль (упомянута и в описании PRI-270 про RON-55), которую `measure-briefs`/`finish_task`-съём должны закрыть штатно.
(dropped 2: ID-307 «Повысить core-recall секции ## Relevant code брифа solve-task» — тот же участок кода, но другой механизм (качество самого ретрива, не репо-агностичность/точка съёма метрики); ID-313 «Свод: отбор рычагов по замеру и локализация правок в сборке брифа» — meta-роундап без конкретного паттерна кода для этой пары задач)

## Subsystems
- reviewer/policy — `ReviewPolicy`/`ContextLimits`: типизированный образец секции `.review.yml` (`from_review_yaml`) и гейтинг, куда добавляется новая секция `metrics.brief_quality.core_paths`.
- reviewer/config — многослойный резолв (`layers.py`, `deepmerge.py`, `committed.py`): как новый ключ политики мержится по слоям (home/committed/env) и попадает в `config show`.
- reviewer/mcp — `MCPReviewService`: сессия PR, `publish_review`/`finish_task`/`_record_brief_quality`/`_repo_clone_path` — весь код, который PRI-270 рефакторит.
- reviewer/services — `ReviewService`/`_create_vcs_provider`/`_ensure_history`; сюда же концептуально относится `reviewer/services/brief_quality.py` (в сводке не упомянут явно, но лежит в этом дереве).
- reviewer/entrypoints — три точки входа (`cli.py`/`launcher.py`/`mcp_server.py`); сюда встраивается новая команда `reviewer measure-briefs`.
- reviewer/tasks — доски задач, `pr_backlink.py` (двусторонняя связь PR↔задача) — код, которым `finish_task` резолвит repo/PR из `pr_url`.
- tests/mcp — 24 unit-теста `MCPReviewService`, включая `finish_task`/`publish_review`/историю сессий — куда лягут новые тесты идемпотентности.
- tests/skills — контрактные тесты скиллов (`finish-task`, `solve-task`) — где документируется поведение `finish_task` и формат брифа, который читает `briefs.py`.

## Relevant code

### PRI-271 — ядро метрики
- `reviewer/metrics/brief_quality/classify.py:7-23` — `is_core_production_path`: предикат для конфигурации. Блast radius (найден grep'ом; граф CALLS его не видит — `eval/` вне индекса, см. Constraints) — семья из 8 вызовов, все должны получить одну и ту же конфигурацию: `reviewer/metrics/brief_quality/context_core.py:67`, `reviewer/services/brief_quality.py:143`, `eval/solve_task_metrics/context_seeds.py:363,401`, `eval/solve_task_metrics/replay.py:76,153,239`, `eval/solve_task_metrics/snapshot.py:87`.
- `reviewer/metrics/brief_quality/classify.py:26-47` — `categorize_miss`: должна стать производной того же списка паттернов (сейчас захардкожены префиксы `tests/`, `docs/`, `plugin/`, `reviewer/<module>`).
- `reviewer/metrics/brief_quality/briefs.py:20` — `_KEY_RE = re.compile(r"(PRI-\d+)", re.IGNORECASE)`; используется в `extract_task_key` (`briefs.py:159-162`) — заменить на паттерн из `task_board.key_pattern`.
- `eval/solve_task_metrics/__main__.py:28-29` — `REPO_ROOT`/`BRIEFS_DIR` хардкод на уровне модуля, используется в `cmd_snapshot` (55), `cmd_forecast` (141), `cmd_replay`→`_replay_side` (232), `cmd_subqueries` (324) — все 4 команды нужно параметризовать `--repo-path`/`--briefs-dir`.
- `reviewer/services/brief_quality.py:25` — **второй**, независимый хардкод `BRIEFS_DIR = "docs/superpowers/briefs"` (найден при чтении файла, не упомянут в описании задачи) — не связан импортом с `__main__.py:29`; при вводе `metrics.brief_quality.core_paths` стоит проверить, не нужен ли этой константе аналогичный путь конфигурации (в задаче не упомянут явно — see Constraints).
- `reviewer/policy/context_limits.py:30-77` (`CodeSectionLimits`) и `:86-121` (`ContextLimits.from_review_yaml`) — образец типизированной секции `.review.yml` с явными дефолтами по ключу (PRI-259 doc-комментарий объясняет обоснование значений) — прямой шаблон для `metrics.brief_quality.core_paths`.
- `reviewer/policy/policy.py:43-59` (`ReviewPolicy._summary_paths_ignore`) — уже существующий tri-state паттерн (ключ отсутствует → `None` → дефолт; явный `[]` → выключение) — прямой прецедент для различения «не сконфигурировано» vs «явно пусто» (крит. 3 PRI-271).
- `reviewer/policy/policy.py:79-100` (`from_yaml`) и `:122-164` (`load_data`) — обе точки, где новый ключ политики должен быть подключён (committed-слой и explicit-override слой).
- `.review.yml:6-14` — `task_board.key_pattern: 'PRI-\d+'` уже присутствует в этом репозитории — подтверждает, что PRI-271 п.1 может читать существующий ключ, ничего нового в схему `task_board` не добавляя.
- `reviewer/config/task_board.py:42,57,283` и `reviewer/config/settings.py:121,171-172` — путь, которым `key_pattern` уже нормализуется/дефолтится (`normalize_task_board_config`, `Settings.task_board_default`).
- `tests/metrics/test_reexport_guard.py:1-51` (весь файл) — guard-тест, которого явно просит критерий приёмки 1 задачи, п. «Что проверить»: `test_eval_reexports_production_objects` (19-30) проверяет `is …is…` тождество объектов между `eval/solve_task_metrics/{briefs,classify,context_core,recall}.py` и `reviewer/metrics/brief_quality/*`; `test_production_core_does_not_import_eval` (33-51) — направление зависимости. Крит. 5 PRI-271 требует мутационной проверки именно этого/аналогичного guard-теста для нового конфиг-чтения.
- `reviewer/index/pathfilter.py:8-25` (`is_ignored`) — готовая fnmatch-нормализация паттернов (голое имя = поддерево); можно переиспользовать для матчинга `core_paths` вместо новой реализации glob.
- `eval/solve_task_metrics/ground_truth.py:118-135` (`collect`) — логика "PR-мержи по grep ключа задачи → diff первого родителя → union файлов", которую описание PRI-270 п.4 просит переиспользовать в `reviewer measure-briefs`.

### PRI-270 — съём без ревью PR
- `reviewer/services/brief_quality.py:87-168` (`measure`) — уже принимает ровно `(task_key, clone_path, changed_paths, changed_status)`, никогда не бросает — это и есть искомое «сервисное ядро»; извлекать в новый уровень нужно обвязку (запись в историю + резолв run_id), а не сам `measure`.
- `reviewer/mcp/service.py:3240-3273` (`_record_brief_quality`) — текущий единственный вызывающий, привязан к `PreparedReview p` и обязательному `run_id`; сигнатуру нужно ослабить до явных `(task_key, repo, pr, changed_paths, changed_status, clone_path)` + опциональный `run_id`.
- `reviewer/mcp/service.py:3197-3200` — гейт `publish_review`: `if not dry_run and posted and run_id is not None: self._record_brief_quality(...)` — остаётся одним из двух вызывающих после рефакторинга.
- `reviewer/mcp/service.py:1395-1452` (`finish_task`) — новая точка вызова; уже делает `_write_through` и `_backlink_pr` в этом же месте — измерение брифа встаёт рядом, после `result = resolved.provider.finish(...)`.
- `reviewer/mcp/service.py:1097-1136` (`_backlink_pr`) — точный код, которым `finish_task` уже резолвит repo/PR из `pr_url`: `parse_pr_url(pr_url)` → `target.owner/target.repo/target.number/target.platform/target.base_url`, затем `self._review_service._create_vcs_provider(target.owner, target.repo, platform=target.platform, base_url=target.base_url)` — переиспользовать один-в-один, а не резолвить заново.
- `reviewer/tasks/pr_backlink.py:26-52` (`PRTarget`, `parse_pr_url`) — сам парсер GitHub/GitLab ссылки на PR/MR.
- `reviewer/vcs/base.py:89-98` (`VCSProvider` Protocol) — `get_pull_request(number)` даёt `base_sha`/`head_sha`, `get_changed_files(number)` даёт `list[ChangedFile]` (path+status) — прямой источник `changed_paths`/`changed_status` внутри `finish_task`, где `PreparedReview` недоступен.
- `reviewer/services/review_service.py:128-140` (`_create_vcs_provider`) и `:166-177` (`_ensure_history`) — фабрика VCS-провайдера по (owner, repo, platform, base_url) и ленивое получение `ReviewHistory` — оба нужны внутри `finish_task`, у которого нет активной PR-сессии.
- `reviewer/mcp/service.py:1651-1674` (`_repo_clone_path`, kw-only `strict`) — уже используется `_record_brief_quality`; для `finish_task` подойдёт нестрогий вызов (fail-soft, как требует крит. 4 PRI-270).
- `reviewer/web/schema.sql:99-126` — таблица `brief_quality`: `run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE` (строки 100-102) нужно сделать nullable (крит. 5); индексы `brief_quality_repo_created_at`/`brief_quality_task_key` (124-126) есть, а **уникального constraint на `(repo, pr_number, task_key)` нет** — идемпотентность (крит. 2) нечем гарантировать на уровне БД без него.
- `reviewer/web/schema.sql:33-37` и `:61-63` — уже используемый в этом файле идиом идемпотентной миграции (`ALTER TABLE … ADD COLUMN IF NOT EXISTS`, `ALTER COLUMN … SET/DROP NOT NULL`) — образец для миграции `run_id` в nullable.
- `reviewer/web/history.py:142-151` (`init_schema`) — лениво применяет `schema.sql` при первом `_connect()`; новая миграция подхватится тем же путём без отдельного скрипта.
- `reviewer/web/history.py:526-581` (`record_brief_quality`) — текущий INSERT; нужно расширить под nullable `run_id` и добавить проверку идемпотентности (сейчас её нет — повторный вызов создаёт новую строку безусловно).
- `reviewer/entrypoints/cli.py:1148-1177` (`search`) и `:1181-1219` (`status`) — образец CLI-команды с `--repo`/`--path`/`--json`, `_resolve_repo`, ChunkStore/GraphStore lifecycle — шаблон для `reviewer measure-briefs [--repo] [--path]` (которой ещё и потребуется `ReviewHistory`/`git`, ни у одной существующей команды таких двух источников вместе нет).
(dropped 3: `reviewer/tasks/graph.py#TaskGraph.link_pr` — линковка PR↔задача в графе задач, отдельный механизм от съёма brief_quality, не трогается этой парой задач; `reviewer/tasks/boards/{clickup,yandex_tracker}.py#finish` — board-provider-специфичная идемпотентность закрытия задачи, уровнем ниже generic `finish_task`, не нужна для метрики; `reviewer/install.py#render_env`, `reviewer/tasks/boards/weeek.py#_placement`, `reviewer/web/api.py#make_router` — подмешаны similar-diffs, не относятся ни к одной из двух задач)

## Test exemplars
- `tests/metrics/test_reexport_guard.py` (весь файл, см. Relevant code) — обязателен к прогону/мутационной проверке для обеих задач.
- `tests/eval/test_snapshot.py#test_build_snapshot_counts_corpus_and_metrics` (`tests/eval/test_snapshot.py:30-47`) — показывает точную арифметику `expected_core`/`core_recall`, которую `--repo-path`/`--briefs-dir` не должны менять по умолчанию (крит. 2 PRI-271).
- `tests/eval/test_replay.py#test_empty_core_denominator_is_not_zero_recall` (`:114-124`) — текущий паттерн статуса `STATUS_EMPTY_CORE`/`core_recall is None`; новый статус «не сконфигурировано» — аналогичный тест рядом.
- `tests/eval/test_replay.py#test_measured_task_scores_core_recall` (`:98-111`) — эталон happy-path подсчёта, с которым сверяется `reviewer measure-briefs`.
- `tests/metrics/test_context_core.py#test_filters_non_core_paths` (`:55-62`) — прямой потребитель `is_core_production_path` через `derive_context_core`; после параметризации предиката этот тест — канарейка на то, что дефолт не сломан.
- `tests/metrics/test_recall.py#test_evaluate_task_core_recall` (`:7-18`) — формула `core_recall`/`raw_recall`/`precision`, не меняется, но фиксирует контракт `evaluate_task`.
- `tests/metrics/test_bulk_subsample.py#test_bulk_subsample_ignores_tasks_without_measurement` (`:30-35`) — задачи без измерения не подмешиваются в bulk-агрегат нулём — та же семантика нужна новому статусу «не сконфигурировано».
- `tests/entrypoints/test_config_commands.py#test_config_show_text_leaf_default_gets_env_source_next_to_layer_leaf` (`:119-139`) — образец теста на листовую гранулярность `sources`/`shadowed` в `config show`; новая секция `metrics.brief_quality.core_paths` обязана попасть туда так же.
- `tests/services/test_brief_quality.py#test_online_matches_offline_formula_on_full_diff` (`:201-216`) — стык онлайн (`measure`) и офлайн-формулы; ключевой регрессионный тест на сравнимость «до/после» между PRI-271/PRI-270 и офлайн-харнессом.
- `tests/services/test_brief_quality.py#test_no_brief_without_clone` (`:121-126`) — fail-soft контракт `measure()` при `clone_path=None`; ровно то, что нужно для `finish_task` при неизвестном клоне (крит. 4 PRI-270).
- `tests/mcp/test_publish.py#test_publish_records_brief_quality` (`:638-654`) и хелпер `_write_brief` (`:629-635`) — текущий тест единственной точки съёма; после рефакторинга нужен второй аналог для `finish_task`.
- `tests/mcp/test_finish_task.py#test_finish_task_backlink_failsoft_on_vcs_error` (`:250-258`) — точный fail-soft паттерн, который нужен PRI-270 крит. 4: доска уже закрыта, сбой VCS не откатывает успех `finish_task`.
- `tests/tasks/boards/test_yougile_finish.py#test_yougile_finish_idempotent_when_pr_present_and_done` (`:56-64`) — существующий образец идемпотентности на уровне board-provider (по наличию PR-ссылки/статусу) — концептуальный аналог того, что нужно у `brief_quality` по `(repo, pr, task_key)`.
- `tests/web/test_history.py#test_brief_quality_trend_unions_paths_not_averages_recall` (`:607-619`) — union путей по задаче, а не построчное усреднение; если `finish_task` и `publish_review` когда-нибудь дадут две строки одной задачи (разные PR), это поведение — гарантия корректности агрегата.
(dropped 5: `tests/mcp/test_prepare_task_context.py#test_test_queries_first_element_has_test_prefix_over_production_query` — про формирование under-the-hood subquery для `code`-секции, не про brief_quality; `tests/skills/test_solve_task_brief.py` (обе строки) — контракт текста скилла solve-task, не код метрики; `tests/skills/test_assembled_prompts.py#test_maintainability_assembled_schema_and_whatnot` — не относится ни к одной задаче, подмешан similar-diffs; `tests/services/test_gitutil_grep.py#test_paths_touched_by_grep_matches_task_key` — другой механизм (co-change grep для augmentation, PRI-257), не `ground_truth.collect`; `tests/mcp/test_server_tools.py#test_publish_review_tool_forwards_task_key` и `tests/mcp/test_service.py#test_publish_review_links_task_when_task_key` — MCP tool-wiring/task-graph linking, не съём brief_quality)

## Constraints / open questions
- **Мутационная проверка guard-теста (крит. 5 PRI-271).** `tests/metrics/test_reexport_guard.py` уже существует и покрывает *reexport*-инвариант, но не чтение нового конфиг-ключа. Нужен отдельный guard-тест на чтение `metrics.brief_quality.core_paths` (по образцу `tests/policy/test_policy_summary_paths.py`, обнаруженного как аналог `_summary_paths_ignore`), и приёмка обязана снять чтение ядра из конфига на копии файла вне рабочего дерева — тест обязан покраснеть, иначе он ничего не проверяет (см. `docs/…/guard-test-may-be-green-by-construction.md`).
- **Внешний клон `rondo` недоступен в этой сессии/репозитории (крит. 1 PRI-271, открытый вопрос).** Ни клона, ни доступа к нему здесь нет — критерий «core-recall 85% (11/13) на клоне rondo» физически не проверить в этой рабочей директории. Как проверять: либо получить доступ к клону rondo отдельно от этой ветки работы, либо ограничиться unit-тестом на синтетическом дереве с тем же ядром (`app/**/*.py` + `frontend/src/**`), не претендуя на воспроизведение точного числа 85%/11-13 без реального клона — вопрос к постановщику/ревьюеру перед тем, как считать критерий 1 закрытым.
- **Воспроизведение среза чисел (крит. 2 PRI-271).** `core_recall_median 0.5714`, `bulk_core_recall_median 0.3571` на корпусе 75 брифов — сравнимость с приёмками PRI-255…266 обязана сохраниться после рефакторинга под конфигурацию. Перед тем как считать PRI-271 сделанной, нужно прогнать `python -m eval.solve_task_metrics snapshot` на дефолтном `.review.yml` (без секции `core_paths`) и сверить именно эти два числа с текущим `eval/solve_task_metrics_report.md` (файл уже в git status как модифицированный — сверять с зафиксированной историей `eval/solve_task_metrics_history.jsonl`, не с промежуточным рабочим деревом).
- **Два независимых хардкода `BRIEFS_DIR`** — `eval/solve_task_metrics/__main__.py:29` (упомянут в задаче) и `reviewer/services/brief_quality.py:25` (найден отдельно, не упомянут). Не связаны импортом. Задача явно просит поправить только первый (крит. 4 требует `--briefs-dir` у офлайн-харнесса); второй, вероятно, вне скоупа PRI-271 (используется относительно `clone_path`, не абсолютного `REPO_ROOT`), но стоит явно решить на этапе плана — не пропустить молча, раз он не в тексте задачи.
- **Граф CALLS не видит блast radius в `eval/`** — `eval/` исключён из индекса ревью (`paths.ignore: [eval, …]` в `.review.yml:20`), поэтому граф-тул `callers` на `is_core_production_path` вернул только `reviewer/`- и `tests/`-вызовы; полный список из 8 вызовов (включая 5 в `eval/solve_task_metrics/{context_seeds,replay,snapshot}.py`) найден только grep'ом. При планировании правки предиката ориентироваться на grep-список из Relevant code, не на граф.
- **Идемпотентность (крит. 2 PRI-270) не имеет опоры в схеме БД.** В `brief_quality` нет уникального ограничения на `(repo, pr_number, task_key)` (только неуникальные индексы по `repo, created_at` и по `task_key`); `record_brief_quality` делает безусловный INSERT. Реализация обязана либо добавить такое ограничение (миграция) с `ON CONFLICT`, либо явную проверку «уже есть строка measured для этого (repo, pr, task_key)» перед записью — выбор оставлен плану, здесь только зафиксирован факт отсутствия готового механизма.
- **`run_id` в `brief_quality` — FK ON DELETE CASCADE.** Сделать колонку nullable (крит. 5) — это `ALTER COLUMN run_id DROP NOT NULL`; FK-ограничение `REFERENCES review_runs(id) ON DELETE CASCADE` само по себе с NULL совместимо (NULL не подчиняется FK), удалять/менять сам constraint не требуется — только `NOT NULL`.
- **`finish_task` не получает diff бесплатно.** В отличие от `publish_review`, у `finish_task` нет активной PR-сессии/`PreparedReview` — `changed_paths`/`changed_status` придётся получать через `vcs.get_changed_files(number)` (или `compare_files(base_sha, head_sha)` из `vcs.get_pull_request(number)`) отдельным сетевым вызовом; это лишняя цена по сравнению с publish_review-путём, но неизбежна при данной точке съёма — стоит явно замерить, не бьёт ли это в лимиты API/время `finish_task`.
- **CLI `measure-briefs` пишет в ту же историю, что `reviewer serve` читает** — команде нужен одновременно git (`ground_truth.git_runner`) И Postgres (`ReviewHistory`), чего нет ни у одной существующей команды `cli.py` (у `search`/`status` — только Postgres/Neo4j, без git-логики поверх офлайн-харнесса) — при планировании стоит решить, тянуть ли `eval.solve_task_metrics.ground_truth` как зависимость `reviewer/entrypoints/cli.py` (обратное направление импорта `reviewer → eval`, которое `test_production_core_does_not_import_eval` запрещает **для `reviewer/`**, но `cli.py` — часть `reviewer/entrypoints/`) или продублировать/переместить `ground_truth.collect` в `reviewer/metrics/brief_quality/` рядом с остальным репо-агностичным ядром.

Собран на: mid (Sonnet), сборка: subagent

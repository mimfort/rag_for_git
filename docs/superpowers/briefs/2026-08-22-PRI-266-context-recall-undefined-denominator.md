# Brief — PRI-266 Знаменатель context-recall неопределён у трети корпуса
https://ru.yougile.com/team/686c049c8af8/#PRI-266

## Task
- PRI-262 взяла гейт точности (41.8 % → 64.0 % осмысленных путей), но сузила покрытие: пути контекстного ядра 403 → 85, медиана размера ядра 4 → 0, задач без измерения контекста 5 → 16 (63 задачи, `indexed_sha=a07405c`).
- Причина 1: фильтр `allowed_names` не видит зависимость, которую изменённые строки не называют (PRI-251 потеряла `reviewer/index/chunker.py`, `reviewer/index/models.py`, `reviewer/gitutil.py` — все три настоящие; всего 23 → 16 осмысленных путей).
- Причина 2: слепое пятно на не-Python — ядро по `is_core_production_path` включает `plugin/**` кроме `.md`, а сиды строятся через `chunk_python` и видят только Python; у задачи с целиком не-Python ядром знаменатель НЕОПРЕДЕЛИМ, а не пуст (PRI-177, PRI-237, PRI-243).
- Что сделать: предзарегистрировать порог и выборку ДО замера и ДО реализации; проверить гипотезу «имена не только с изменённых строк, но и из СИГНАТУР изменённых символов»; замерить ОТДЕЛЬНО покрытие и точность; не-Python решить отдельным механизмом (определить знаменатель либо ввести явный статус «неопределим» ≠ «пусто»); при невосстановимом покрытии — зафиксировать числом и назвать область применимости, а не тянуть третий рычаг.
- Критерии приёмки: (1) порог и выборка названы в журнале до просмотра данных и не двигаются; (2) число задач с измеренным знаменателем растёт относительно 31 из 63, доля осмысленных путей не падает ниже 50 %; (3) не-Python ядро получает статус, отличающий «неопределим» от «пусто», PRI-177/237/243 проверены поимённо; (4) замер на одном `indexed_sha`, команды с явным `--branch dev`; (5) пер-задачные дельты — с учётом пола шума ±1 файл; (6) аддитивность и чистота: существующие числа не меняются, `brief_quality` без I/O, `eval/` — ре-экспорт.

## Related work
- PRI-262 (ID-316, PR #218) — родитель: механизм строчного сидирования, который сузил покрытие; образец предзарегистрации `eval/pri262_preregistration.md`, ручная сверка `eval/pri262_eye_check.md`.
- PRI-261 (ID-315) — ввела контекстное ядро и закрылась отрицательным результатом 41.8 % при пороге 50 %; прецедент «гейт не взят → метрика не принимает решений».
- PRI-251 (ID-306) — конкретная задача, на которой потеряны chunker/models/gitutil; её дифф — обязательный кейс проверки гипотезы сигнатур.
- PRI-257 (ID-307) — урок «нулевая дельта может быть механикой бюджета, а не свойством сигнала»: перед выпиливанием рычага проверить, доезжает ли кандидат до выдачи.
- PRI-265 (ID-319, PR #219, смержен) — только что добавила `context_retrieval_failed` как отдельный статус сбоя обхода и маркер слияния ручной части отчёта; новый статус «неопределим» строится ровно по этому образцу и обязан не сломать `report_merge`.
- PRI-303/ID-303 — исходная онлайн-метрика `brief_quality`, задаёт границу «brief_quality без I/O».
(dropped 3: ID-305 — семейство символов, другой механизм; ID-306 SCIP-рёбра — фон замера, не действие; прочие similar ниже рельсы ceiling.)

## Subsystems
- `reviewer/index` — `chunk_python`/`symbol_skeleton_hash`: источник символов сидов; из него же берутся сигнатуры для гипотезы.
- `reviewer/graph` — SCIP/tree-sitter граф, `CALLS`/`IMPLEMENTS`; обход соседей формирует контекстное ядро.
- `reviewer/policy` — `ReviewPolicy`/`ContextLimits`: `.review.yml`, `paths.ignore` (в нём `eval` — поэтому eval/ вне RAG-индекса).
- `reviewer/tools` — session-less тулы поиска/графа, через которые `live.py` даёт `neighbors` замеру.

## Relevant code
- `reviewer/metrics/brief_quality/context_core.py:37-68` — `derive_context_core(seed_ids, changed_core, traverse, allowed_names)`: точка обоих рычагов; `None` vs `set()` у `allowed_names` — намеренно разные высказывания, инвариант нельзя ломать. Чистый модуль: обход инъекцией, без I/O.
- `reviewer/metrics/brief_quality/context_core.py:23-34` — `node_paths` / `symbol_name`: фильтр имён работает по простому имени последнего сегмента fqn — сюда же ляжет расширенный набор имён.
- `reviewer/metrics/brief_quality/classify.py:7-23` — `is_core_production_path`: ядром считаются `reviewer/**/*.py`, `plugin/**` кроме `*.md`, корневые `*.py`; именно `plugin/**` не-Python и создаёт слепое пятно.
- `eval/solve_task_metrics/context_seeds.py:201-235` — `called_names(source, lines)`: имена ТОЛЬКО с изменённых строк (вызовы + базы классов). Гипотеза задачи расширяет источник до сигнатур изменённых символов.
- `eval/solve_task_metrics/context_seeds.py:172-191` — `_innermost_symbols`: самый внутренний чанк, покрывающий строку; отсюда берутся сиды и отсюда же доступны сигнатуры (`chunk_python` даёт `symbol_fqn`, `start_line`/`end_line`).
- `eval/solve_task_metrics/context_seeds.py:242-285` — `seeds_for_merge` / `collect_seeds`: сборка `SeedSet(symbols, called_names)` по мержам; `SeedSet.__or__` — точка расширения без ломки формы.
- `eval/solve_task_metrics/context_seeds.py:160-169` — `hunk_is_significant`: фильтр незначимых хунков (докстринг/комментарий/help), трогать не надо — он и взял гейт PRI-262.
- `eval/solve_task_metrics/context_seeds.py:182-184` — `chunk_python` в try/except: не-Python файл молча даёт пустые сиды; именно здесь «неопределим» становится неотличим от «пусто».
- `eval/solve_task_metrics/replay.py:20-42` — `STATUS_*` / `CONTEXT_STATUSES` / `CONTEXT_EVALUATED_STATUSES`: реестр статусов, куда добавляется «неопределим» (образец — `STATUS_CONTEXT_FAILED` из PRI-265).
- `eval/solve_task_metrics/replay.py:99-142` — `_evaluate`: вычисление `context_status` (`measured` / `empty_context_denominator` / `context_retrieval_failed`); третья ветка добавляется тут.
- `eval/solve_task_metrics/replay.py:186-201` — место вызова `collect_seeds` + `derive_context_core(..., allowed_names=seeds.called_names)`: единственная точка, где рычаги сходятся в прогоне.
- `eval/solve_task_metrics/replay.py:244-256` — сводка `context_statuses` в снимке; новый статус обязан попасть в счётчики.
- `reviewer/metrics/brief_quality/recall.py:57-89` — `evaluate_task`: `context_recall = hit/len(core)`, `None` при пустом ядре; `union_precision` по `expected ∪ context_core`.
- `reviewer/metrics/brief_quality/recall.py:92-140` — `aggregate`: `context_n_measured` / `no_context_measurement` / `context_recall_median` — числа, по которым читается покрытие.
- `eval/solve_task_metrics/replay_report.py:11,132-167` — рендер отчёта: строка `context-recall (медиана)`, секция `context_statuses`, пер-задачные колонки.
- `eval/solve_task_metrics/__main__.py:240-315,372-405` — CLI `replay`: `--variant`, `--baseline last`, `--branch`, `--repo`; `report_merge.ensure_mergeable` перед прогоном (PRI-265).
- `eval/solve_task_metrics/context_core.py:1-6` — ре-экспорт продакшн-модуля: направление зависимости `eval → reviewer`, обратное запрещено.
- `eval/solve_task_metrics/live.py` — живой провайдер (`neighbors`, `preflight`); граница «модуль не тянет reviewer» проверяется тестом.
- Blast radius: `derive_context_core` вызывается из `eval/solve_task_metrics/replay.py:193` (единственный живой потребитель) и из тестов; `called_names`/`collect_seeds` — только из `replay.py` и тестов; `is_core_production_path` — из `context_core.py`, `context_seeds.py`, `replay.py`, `recall`-пути и онлайн-`services/brief_quality.py` (правка классификатора трогает ОНЛАЙН-числа — критерий 6 про аддитивность именно об этом).

## Test exemplars
- `tests/metrics/test_context_core.py` — юниты `derive_context_core` с подставным `traverse`: образец для нового поведения фильтра имён, без графа.
- `tests/eval/test_context_seeds.py` — юниты хунков/значимости/`called_names`: сюда ложатся кейсы «имя из сигнатуры» и «не-Python файл».
- `tests/eval/test_replay.py:240-335` — статусы контекста, включая `context_retrieval_failed` и правило «в счётчики попадают только дошедшие до обхода»: точный образец для статуса «неопределим».
- `tests/eval/test_replay_report.py:130-155` — рендер секции `context_statuses` и совместимость снимка без ключа.
- `tests/metrics/test_reexport_guard.py` — guard идентичности объектов `eval` ↔ `reviewer` и запрет импорта `eval` из `reviewer`: новый общий код обязан пройти его.
- `tests/metrics/test_recall.py` — агрегаты `context_n_measured` / `no_context_measurement`.
(dropped 0)

## Constraints / open questions
- Порог и выборка должны быть предзарегистрированы в `eval/pri266_preregistration.md` ДО реализации и ДО просмотра данных (критерий 1); образец — `eval/pri262_preregistration.md`.
- `eval/` в `paths.ignore` репозитория → его файлы вне RAG-индекса; ретрив их не находит, читать напрямую.
- Замер требует живой инфраструктуры (Postgres 5433 + Neo4j + Voyage) и одного `indexed_sha`; индекс `dev` переиндексирован в этом прогоне до `a953bed` (546 файлов, граф SCIP: 7949 узлов, 19521 рёбер). Числа PRI-262 сняты на `a07405c` — сторона «до» обязана быть пересчитана на текущем sha, а не взята из отчёта.
- Каждая команда `replay` — с явным `--branch dev` (без флага молча берётся индекс `main`).
- Пол шума харнесса ±1 файл на задачу; 6 из 62 задач нестабильны между идентичными прогонами — пер-задачные дельты интерпретировать с этим.
- Открытый вопрос: «неопределим» для не-Python — это отдельный статус в `replay.py` (дёшево, аддитивно) или попытка определить знаменатель (нужен обход по не-Python символам, которого граф не строит). Задача разрешает оба, но требует поимённой проверки PRI-177/237/243.
- Открытый вопрос: расширение источника имён до сигнатур изменённых символов может вернуть мусор god-модулей — ровно то, что PRI-262 чинила. Требуется отдельный замер вклада в покрытие и в точность (критерий 3), рычаг не имеет права ухудшить ни одно из двух.
- Урок PRI-257: прежде чем объявить рычаг бесполезным по нулевой дельте, проверить, доезжает ли кандидат до выдачи.
- Стоп-правило по образцу PRI-262: если гейт не взят — закрыть отрицательным результатом с числом и назвать область применимости метрики, не тянуть третий рычаг.
Собран на: Opus (premium), сборка: inline

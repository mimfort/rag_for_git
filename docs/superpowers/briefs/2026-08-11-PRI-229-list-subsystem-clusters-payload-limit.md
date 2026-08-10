# Brief — PRI-229 list_subsystem_clusters: ограничить объём ответа (пагинация + режим без file-level delta)
https://ru.yougile.com/team/686c049c8af8/#PRI-229

## Task

Ответ `list_subsystem_clusters` на крупном репо не помещается в контекст LLM-клиента: сериализация
отдаёт по кластеру полный `files`, `top_symbols` и те же пути повторно в `added_files`/`changed_files`/
`moved_files` с fingerprint'ами. Размер растёт по числу файлов, ограничителя нет (`cap` режет только
stale-кластеры к пересборке, не payload). Задача разбита на 4 подзадачи (все берём):

- **PRI-230 (ID-284)** — сжатый режим ответа без file-level payload: параметр формата с дефолтом
  «как сейчас»; по кластеру только `cluster_key`, `num_members`, `source_hash`, `stale`, `bootstrap`,
  `full_rebuild`, `reused_files` + счётчики added/changed/removed/moved. Верхнеуровневые поля
  (`branch`, `depth`, `layout_token`, `depth_source`, `deferred`, `deferred_files`, `orphans`) неизменны.
- **PRI-231 (ID-285)** — детерминированная пагинация: стабильная сортировка по `cluster_key`,
  `offset`/`limit` с back-compat дефолтами, общее число кластеров и признак следующей страницы;
  `deferred`/`deferred_files`/`orphans`/`layout_token` считаются по ПОЛНОМУ множеству, не по странице.
- **PRI-232 (ID-286)** — устранить дублирование путей в полном формате: `files` не должен повторять
  пути delta-списков (вариант зафиксировать в докстринге); полный набор путей кластера остаётся
  восстановимым из ответа; обновить всех потребителей.
- **PRI-233 (ID-287)** — перевести `plugin/skills/summarize-subsystems/SKILL.md` на сжатое
  перечисление + пагинацию, детализация — через `get_subsystem_summary_work`; обновить CLAUDE.md /
  README.md / README.ru.md, если контракт там зафиксирован; прогнать полный цикл и зафиксировать
  размер ответа до/после; поправить guard-тесты `tests/skills/`.

Критерии приёмки родителя: сжатый ответ без путей и fingerprint'ов, O(числа кластеров); пагинация без
пропусков/дублей и с воспроизводимым порядком; `layout_token`/`deferred`/`deferred_files`/`orphans`
совпадают с текущими значениями для того же состояния индекса; скилл проходит полный цикл без
переполнения контекста; полный режим остаётся доступен и покрыт тестами.

## Замер «до» (этот репозиторий, dev, cap=0) — воспроизведён локально

`list_subsystem_clusters('mimfort/rag_for_git','dev',cap=0)` → **106 878 байт**, 40 кластеров,
**407 путей в `files`**, 248 элементов delta-списков. Т.е. payload — почти целиком пути и fingerprint'ы;
сжатый режим даёт O(40) записей вместо O(407+248).

## Related work

- ID-165 (done) — churn-контроль: freshness по структуре + `cap`/каденс. Именно `cap`
  (`service.py:1768-1794`) задаёт текущую семантику `deferred`, которую пагинация НЕ должна менять.
- ID-166 (done) — depth кластеризации + preflight-предупреждение: источник `depth_source` и
  preflight-шага скилла, который читает статистику из перечисления.
- ID-173 (done) — поле `stale` в `get_subsystem_summaries`: прецедент аддитивного расширения
  контракта read-path тула с сохранением обратной совместимости.

(dropped 0)

## Subsystems

- `reviewer/entrypoints` — Click CLI + FastMCP-сервер `reviewer-mcp`; операции делегируются
  `MCPReviewService`, тул — тонкая обёртка (сюда добавляются новые параметры формата/пагинации).
- `reviewer/index` — chunk store + `SummaryStore`: атомарная запись сводок/fragments, проверка
  coverage/generation, verified prune (источник `get_source_hashes`/`get_updated_ats`).
- `reviewer/services` — вычисление дельт фрагментов сводок и резолв репо/веток
  (`_resolve_repo_branch`, `build_fragment_delta`, `has_complete_fragment_generation`).
- `reviewer/retrieval` — прецедент ограничения размера выборки (cliff/floor/ceiling), стилистический
  ориентир для «предохранителей» лимитов.

## Relevant code

- `reviewer/mcp/service.py:1717-1832` — `MCPReviewService.list_subsystem_clusters`: точка входа всех
  четырёх подзадач (сигнатура, порядок кластеров, сериализация, верхнеуровневые поля).
- `reviewer/mcp/service.py:1798-1822` — тело цикла сериализации кластера: `files`, `top_symbols` и
  четыре delta-списка; здесь режется payload (PRI-230) и устраняется дублирование (PRI-232).
- `reviewer/mcp/service.py:1683-1691` — `_fragment_ref`: `{path, fingerprint, [from_cluster_key],
  [summary, provenance]}` — источник fingerprint'ов в перечислении.
- `reviewer/mcp/service.py:1693-1715` — `_serialize_summary_delta`: общий сериализатор, который
  переиспользует и `get_subsystem_summary_work` (`include_reused_content=True`) — менять осторожно,
  чтобы не задеть детальный тул.
- `reviewer/mcp/service.py:1751-1797` — `stored`/`stale`/`deltas`/`orphans`/`cap`/`deferred_keys`/
  `deferred_files`: всё считается по полному `state.clusters` — инвариант, который пагинация обязана
  сохранить (считать до среза страницы).
- `reviewer/mcp/service.py:1726-1750` — два ранних возврата (`note`: нерезолвленная ветка, пустой
  индекс) без `depth`/`layout_token`/`orphans`; новые поля пагинации должны быть согласованы и с ними.
- `reviewer/mcp/service.py:1834-1868` — `get_subsystem_summary_work`: возвращает `ready`, `branch`,
  `cluster_key`, `source_hash`, полный `_serialize_summary_delta(include_reused_content=True)`,
  `bootstrap`, `full_rebuild`. **Проверено: file-level данные, нужные скиллу на шаге 5, уже доступны
  здесь** — разрыва по PRI-233 п.2 нет; в перечислении delta нужна только для preflight-статистики.
- `reviewer/entrypoints/mcp_server.py:308-327` — тул-обёртка `list_subsystem_clusters` + докстринг,
  фиксирующий текущий контракт (обновляется в PRI-230/231/232).
- `plugin/skills/summarize-subsystems/SKILL.md:29-39` — шаг «List clusters» без ограничителей: читает
  `depth`, `layout_token`, `depth_source`, `deferred`, `deferred_files`, `orphans`, собирает
  `expected_source_hashes` из **каждого** возвращённого кластера (пагинация обязана сохранить полный
  обход, иначе сломается верифицированный prune).
- `plugin/skills/summarize-subsystems/SKILL.md:41-57` — preflight: статистика stale/bootstrap/
  deferred/orphans — единственный реальный потребитель delta-превью в перечислении (хватит счётчиков).
- `plugin/skills/summarize-subsystems/SKILL.md:66-91` — шаг сборки: детализация уже идёт через
  `get_subsystem_summary_work`; композитору явно запрещено передавать `files`/`top_symbols`.
- `plugin/skills/summarize-subsystems/SKILL.md:93-104` — prune: `layout_token` +
  `expected_source_hashes` со снапшота list; условие «полного прохода» (`deferred == 0`, без
  override) — пагинация не должна трактоваться как override/частичный проход.

(dropped 1: чанк `MCPReviewService` целиком — обзорный, конкретика взята из перечисленных диапазонов)

## Test exemplars

- `tests/mcp/test_subsystem_summaries.py:164-232` —
  `test_list_subsystem_clusters_adds_file_delta_without_changing_old_fields`: канонический guard
  обратной совместимости полного формата; ориентир для тестов PRI-230/232.
- `tests/mcp/test_subsystem_summaries.py:233-257` — `..._counts_pending_files_in_deferred_clusters`:
  фиксирует, что `deferred_files` считается по кластерам ВНЕ ответа → прямой прецедент для
  «считать по полному множеству, не по странице» (PRI-231).
- `tests/mcp/test_subsystem_summaries.py:1089-1130` — `cap_defers_lowest_priority` / `no_cap_returns_all`:
  семантика `cap`, которую нельзя смешивать с `limit` пагинации.
- `tests/mcp/test_subsystem_summaries.py:1180-1205` — `reports_depth_and_orphans` /
  `resolves_depth_when_not_given`: верхнеуровневые поля, неизменные в обоих форматах.
- `tests/mcp/test_subsystem_summaries.py:448-500` — same-key depth rebuild → stale+deferred: связка
  `layout_token`/`full_rebuild`, которую нельзя сдвинуть.
- `tests/mcp/test_subsystem_summaries.py:657-663` — `empty_index_returns_note`: форма раннего возврата.
- `tests/mcp/test_summary_depth_overrides.py:32-56` — вызовы с `cap=0` и проверка `layout_token`/depth.
- `tests/mcp/test_server.py:131` — реестр имён тулов MCP-сервера (регресс на регистрацию).
- `tests/skills/test_summarize_subsystems.py` — guard-тесты скилла: фиксируют шаги/поля перечисления,
  обновляются в PRI-233.

(dropped 0)

## Constraints / open questions

- **Порядок подзадач диктуется связностью**: PRI-231 (стабильная сортировка) и PRI-230 (формат)
  трогают один и тот же цикл сериализации `service.py:1798-1832`; PRI-232 меняет семантику `files`,
  которую PRI-230 в сжатом режиме вообще не отдаёт. Разумно делать 230+231 одним проходом по коду,
  232 следом, 233 последним (документация + скилл + замер).
- **Обратная совместимость — жёсткое требование**: полный формат должен остаться дефолтом и не
  меняться «байт-в-байт» (PRI-230 к.п.3), но PRI-232 намеренно меняет `files` в полном формате.
  Противоречие критериев: нужно решить, считается ли полный формат после PRI-232 «изменённым
  контрактом» (тогда к.п.3 PRI-230 читается как «до PRI-232») — зафиксировать в спеке.
- **Именование**: как назвать параметр формата (`compact: bool` vs `fields: "summary"|"full"`) и
  дефолты пагинации (`limit=None` = всё). Влияет на докстринг тула и на скилл.
- **`expected_source_hashes` при пагинации**: скилл собирает их со всех кластеров — надо либо
  требовать полный обход страницами перед prune, либо отдавать хеши компактно; выбрать в спеке.
- `top_symbols` в сжатом режиме не упомянут в описании подзадачи явно (перечислены только
  `cluster_key`, `num_members`, `source_hash`, `stale`, `bootstrap`, `full_rebuild`, `reused_files` +
  счётчики) → трактуем как «не отдавать».
- Замер «после» для PRI-233 к.п.2 нужно снять тем же способом, что и «до» (см. секцию замера), и
  записать в задачу.
- Индекс переиндексирован в ходе preflight (`dev` @ `ce2e50e`, 446 файлов, SCIP-граф 6553/31830),
  сводок 40, корпус задач синхронизирован (92 задачи, 0 изменений).
- Бриф собран inline (Path B): данные задачи и точные `file:line` уже были получены оркестратором,
  диспатч субагента дублировал бы работу.

Собран на: Opus 5 (сессионная модель), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 70 · out 17.1K · cache-write 227.9K · cache-read 2.8M
Всего: 3M токенов

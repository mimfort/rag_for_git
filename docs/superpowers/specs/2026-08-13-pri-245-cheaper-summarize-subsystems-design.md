# PRI-245 — Снизить стоимость summarize-subsystems

Бриф: `docs/superpowers/briefs/2026-08-13-PRI-245-cheaper-summarize-subsystems.md`

## Задача

Полный проход `/rag-reviewer:summarize-subsystems` на этом репозитории стоит более 1M токенов:
478 `.py` в индексе (~106k строк, из них ~68k — тесты) читаются целиком, каждым отдельным
субагентом на файл. Три независимые причины, три независимые правки:

1. **Рассогласование чтения и свежести.** `source_hash` кластера и пофайловый fingerprint
   считаются от `skeleton_hash` символов (`reviewer/graph/summaries.py:103`, `:113`), но файловый
   job читает файл целиком через harness-`Read` (`plugin/skills/summarize-subsystems/SKILL.md:86`).
   Это не только токены: утверждение сводки, выведенное из тела функции, протухает молча и не
   обновится никогда — fingerprint его не видит.
2. **Тесты кластеризуются наравне с продакшн-кодом.** `build_clusters` группирует всех members
   индекса; «подсистема `tests/skills`» бесполезна как высокоуровневый приор для
   `ask` / `pr-walkthrough` / `solve-task`, но даёт около двух третей объёма работы.
3. **Один субагент на файл.** При 478 файлах постоянные издержки диспатча (системный промпт +
   описания тулов на каждый job) на мелких файлах превышают полезную нагрузку.

## Скрытая сложность, вскрытая при проектировании

Задача формулирует шаг 1 как замену одной строки в `SKILL.md`: harness-`Read` →
`read_file(path, skeleton=True)`. **Это невыполнимо в текущем виде.** `read_file` — тул PR-сессии:
и `ToolContext`-версия (`reviewer/tools/code_tools.py:80`), и MCP-обёртка
(`reviewer/mcp/service.py:405`, `reviewer/entrypoints/mcp_server.py:46`) требуют `(repo, pr)` и
читают head-ревизию PR. `summarize-subsystems` работает **без** PR-сессии; в session-less наборе
(`plugin/skills/_common/tool-usage.md`) чтения исходника нет вовсе.

Следовательно шаг 1 требует **нового session-less MCP-тула**. Это изменение контракта публичных
тулов и главный источник риска в задаче.

## Решения

### Р1. Источник скелета — проиндексированные чанки, а не файл на диске

`skeleton_hash` считается `symbol_skeleton_hash(text)` по тексту **символа-чанка**
(`reviewer/index/store.py:311`), а не по файлу. Значит «job читает ровно то, что инвалидирует его
результат» достижимо **только** чтением из чанков. Файл с диска содержал бы материал, невидимый
для fingerprint, — то есть воспроизвёл бы ровно тот дефект, который задача чинит.

Скелет файла собирается как объединение скелетов его символов:

- взять `node_id` файла из members и их текст через
  `store.fetch_nodes_at(repo, node_ids, base_ref(branch))`;
- для каждого чанка `python_skeleton(text)` даёт номера строк **относительно чанка**; абсолютный
  номер = `chunk.start_line + relative - 1`;
- объединить в множество (вложенные символы естественно дедуплицируются: скелет класса уже
  содержит сигнатуры своих методов), отсортировать, отрендерить `N|строка` как у `read_file`.

**Осознанная цена:** module-level docstring и код вне символов в чанки не попадают
(`chunk_python` эмитит только `def`/`class`), поэтому в скелет они не войдут — хотя
`python_skeleton` их поддерживает. Расширение чанкинга module-docstring'ом меняет индексацию и
находится вне скоупа. Согласованность с fingerprint важнее полноты: сводка подсистемы — грубый
fail-open приор, а молча протухающая сводка — дефект.

**Отвергнутая альтернатива:** отдавать скелеты прямо в ответе `get_subsystem_summary_work`.
Тогда весь исходный материал проходит через контекст оркестратора — ровно то свойство, ради
отсутствия которого file-job'ы и существуют.

### Р2. Новый session-less MCP-тул `get_file_skeletons`

```
get_file_skeletons(repo: str, paths: list[str], branch: str | None = None) -> dict[str, str]
```

Возвращает `{path: skeleton_text}`. Приём **списка** путей — то, что делает батчинг (Р4) одним
вызовом вместо N. Поведение по краям зеркалит существующие session-less тулы (fail-soft, русские
служебные строки):

- путь без чанков в base-индексе ветки → `"(файл не найден в индексе: <path>)"`;
- пустой скелет → `"(нет определений для скелета)"`;
- на файл действует тот же кап 400 строк, что и у `read_file`, с маркером `(…усечено)`;
- `paths` капится сервером (`_MAX_SKELETON_PATHS = 25`), лишнее отбрасывается с записью-нотой —
  «no silent caps».

Регистрируется в `reviewer/entrypoints/mcp_server.py` и в session-less разделе
`plugin/skills/_common/tool-usage.md`. Существующий `read_file` не трогается.

### Р3. Фильтр кластеризации сводок — `summary_paths.ignore`

Отдельный от `paths.ignore` слой: `paths.ignore` управляет **индексацией и ревью**, новый ключ —
только **кластеризацией сводок**. Тесты обязаны остаться в ревью-индексе (находиться через
`search_codebase`, комментироваться в PR) и обязаны исчезнуть из сводок.

- Поле `ReviewPolicy.summary_paths_ignore: list[str]`, форма `.review.yml`:
  ```yaml
  summary_paths:
    ignore:
      - tests
  ```
- Матчер — существующий `reviewer/index/pathfilter.py::is_ignored` (fnmatch + «голое имя папки
  ловит поддерево»). Никакого второго матчера.
- **Дефолт-константа** `DEFAULT_SUMMARY_PATHS_IGNORE = ("tests", "test")` рядом с политикой; env-слоя
  нет — единообразно с `context_limits` (PRI-202). Голые имена, поэтому `is_ignored` ловит и сам
  каталог, и всё поддерево, но не `reviewer/testing.py`.
- Семантика override обычная: ключ присутствует → значение заменяет дефолт целиком; явный
  `ignore: []` **выключает** фильтр (сводки по тестам возвращаются), а не откатывается на дефолт.
- Применяется **при сборке members, до `build_clusters`**, в обеих точках построения кластеров:
  `_summary_state` (`reviewer/mcp/service.py:1802`) и `_current_subsystem_hashes` (`:2379`). Вторая
  точка обязательна: она считает `stale` для `get_subsystem_summaries`, и расхождение наборов
  сделало бы все сводки вечно stale.
- `build_clusters` **не меняется** — фильтрация остаётся слоем выше, чистая функция остаётся
  чистой.

Резолв — по образцу `_resolve_summary_depth`: fail-soft, при сбое политики откат на
дефолт-константу.

### Р4. Батчинг файловых job'ов

Один субагент на **порцию** путей вместо одного на файл:

- оркестратор режет `added_files + changed_files` на порции по `≤ 15` путей;
- job делает **один** вызов `get_file_skeletons(repo, paths, branch)` на всю порцию и возвращает
  список `{path, summary, provenance}`;
- ограничение по объёму обеспечивает сервер: 400 строк на файл × 15 файлов — предсказуемый потолок
  без обратной связи с оркестратором, поэтому оркестратору не нужно знать размеры заранее.

Инварианты действующего протокола сохраняются пофайлово:

- job никогда не вычисляет, не угадывает и не возвращает `fingerprint` (PRI-291);
- результат с `path` вне выданной порции **отбраковывается**; порция переиздаётся один раз, при
  повторном несовпадении кластер считается deferred и `raced` инкрементируется — как сейчас, но
  единицей отбраковки становится порция;
- пропущенный путь (job вернул меньше записей, чем путей в порции) обрабатывается тем же правилом:
  порция переиздаётся один раз, затем deferred + `raced`;
- ни оркестратор, ни job не читают неизменённые исходники.

### Р5. Инвалидация сохранённых данных — оба механизма

**(а) Содержание фрагмента.** `_GENERATION` в `reviewer/services/summary_fragments.py:10`:
`summary-fragment-v1` → `summary-fragment-v2`. `has_complete_fragment_generation` перестаёт
засчитывать старый штамп → кластеры приходят как `bootstrap` → фрагменты пересобираются на новом
входе. Старые сводки остаются читаемыми до атомарной замены каждого bundle.

**(б) Состав кластеров.** `summary_paths_ignore` входит в payload `layout_token`:

```python
payload = {
    "default_depth": int(default_depth),
    "overrides": list(normalized_overrides.items()),
    "summary_paths_ignore": normalized_ignore,   # strip("/"), dedup, sorted
}
```

`canonicalize_layout` получает третий параметр и возвращает нормализованный ignore вместе с
overrides и token. Включение/выключение/правка фильтра меняет token → полный пересбор → штатный
`prune_subsystem_summaries` на полном uncapped проходе вычищает осиротевшие `tests/*` сводки и
фрагменты. Ручных миграций БД нет.

Оба механизма избыточны по отношению друг к другу (смена payload меняет token у всех кластеров),
и это намеренно: они инвалидируют по разным осям, и каждая ось должна работать самостоятельно.

## Затрагиваемые компоненты

| Файл | Изменение |
|---|---|
| `reviewer/graph/summaries.py` | `canonicalize_layout`/`compute_layout_token` — третий компонент payload; нормализация ignore |
| `reviewer/policy/policy.py` | поле `summary_paths_ignore` + `from_yaml`/`from_settings`/`load_data` |
| `reviewer/config/layers.py` | `policy_to_public_data`, `_validate_public_policy_data`, валидация домашнего слоя |
| `reviewer/mcp/service.py` | фильтр members в `_summary_state` и `_current_subsystem_hashes`; `_resolve_summary_layout`; `get_file_skeletons` |
| `reviewer/entrypoints/mcp_server.py` | регистрация `get_file_skeletons` |
| `reviewer/services/summary_fragments.py` | `_GENERATION` → v2 |
| `plugin/skills/summarize-subsystems/SKILL.md` | шаг 5.2 — скелет + батч; шаг 3 preflight — фильтр как компонент layout policy |
| `plugin/skills/_common/tool-usage.md` | `get_file_skeletons` в session-less разделе |
| `.review.yml` | секция `summary_paths` |
| `CLAUDE.md`, `README.md`, `README.ru.md` | документация |

`reviewer config show` **правок не требует**: `_render_config_report` печатает generic'ом по всем
ключам `effective` (`reviewer/entrypoints/cli.py:169`), поэтому новый ключ появляется в выводе сам
— достаточно добавить его в `policy_to_public_data` и в оба валидатора публичной формы.

Правка чего-либо под `plugin/` меняет payload-digest → обязателен прогон
`update_codex_plugin_manifest.py`, иначе install-тесты краснеют.

## Тестирование

- **`tests/graph/test_summaries*.py`** — unit на `canonicalize_layout`: разные
  `summary_paths_ignore` дают разный token; нормализация (порядок элементов, дубли, ведущий/
  замыкающий `/`) даёт одинаковый; пустой список и дефолтный список дают разные token — то есть
  выключение фильтра тоже запускает пересборку.
- **`tests/policy/`** — unit на резолв `summary_paths_ignore`: без ключа берётся
  дефолт-константа, override заменяет её целиком, явный `[]` выключает фильтр.
- **`tests/test_review_yml_example.py`** — сверяет пример `.review.yml` со схемой политики;
  добавление секции `summary_paths` должно пройти через него, а не мимо.
- **`tests/mcp/`** — фильтр применён: members с `tests/...` не образуют кластеров; `_summary_state`
  и `_current_subsystem_hashes` дают согласованные наборы (сводка продакшн-кластера не становится
  вечно stale); `get_file_skeletons` — контракт: батч путей, кап, отсутствующий путь, пустой
  скелет, абсолютные номера строк.
- **`tests/skills/`** — guard-тесты на собранный текст скилла: job не инструктируется читать
  исходник harness-`Read`; присутствует `get_file_skeletons`; сохранены запрет echo `fingerprint` и
  правило отбраковки чужого `path`; батч описан как порция с потолком.
- Приёмка (вне автотестов, на живом деплое): полный проход после апгрейда помечает кластеры
  `bootstrap`, завершается `deferred == 0`, `raced == 0`, `completed=true`; `tests/*` уходят
  через prune; замер стоимости «до и после» записывается в задачу.

## Границы скоупа

Не входят: детерминированная сборка фрагмента из AST без LLM (заменяет шаг 1, а не дополняет);
ленивая пересборка протухшего кластера по обращению; расширение чанкинга module-level
docstring'ом; ретрофит `read_file` под session-less использование.

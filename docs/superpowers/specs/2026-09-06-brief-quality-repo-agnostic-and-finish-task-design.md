# Дизайн — репо-агностичное ядро метрики брифа + съём без ревью PR (PRI-271 + PRI-270)

Бриф: `docs/superpowers/briefs/2026-09-06-PRI-271-PRI-270-brief-quality-repo-agnostic-and-finish-task.md`

## Задача

Две парные задачи доски, решаемые одной веткой и одним PR.

**PRI-271** — ядро метрики `brief_quality` прибито к rag_for_git тремя хардкодами, и все три
провала тихие: `_KEY_RE = (PRI-\d+)` (`reviewer/metrics/brief_quality/briefs.py:20`) роняет
бриф чужого репозитория из корпуса без единой ошибки; `is_core_production_path`
(`reviewer/metrics/brief_quality/classify.py:7-23`) считает ядром только `reviewer/**/*.py`,
`plugin/**` и корневые `*.py`, поэтому у чужого репозитория знаменатель пуст и любая задача
получает `empty_core_denominator`, неотличимый от честного «в диффе только тесты и доки»;
`BRIEFS_DIR` (`eval/solve_task_metrics/__main__.py:29`) не даёт офлайн-харнессу посчитать
чужой клон вовсе.

**PRI-270** — метрика снимается единственной точкой в `publish_review`
(`reviewer/mcp/service.py:3199-3200`). Ревью PR запускается редко: 22 прогона за всю историю,
таблица `brief_quality` пуста. При этом самому замеру ревью не нужно: `brief_quality.measure`
требует `task_key`, `clone_path`, `changed_paths` и `changed_status`, а все четыре доступны в
`finish_task`, который вызывается на каждой закрытой задаче.

## Решения брейншторма

1. **`unconfigured_core_denominator` наступает на пересечении двух условий** — знаменатель ядра
   пуст И ключ `core_paths` не задан явно. Это снимает кажущееся противоречие критериев 2 и 3
   PRI-271: дефолт продолжает считать (числа rag_for_git не двигаются), а чужой ненастроенный
   репозиторий получает отдельный статус вместо `empty_core_denominator`.
2. **Критерий 1 PRI-271 закрывается синтетической фикстурой**, а не живым клоном rondo:
   арифметика 11/13 воспроизводится на управляемом временном репозитории с ядром
   `app/**/*.py` + `frontend/src/**` и брифом `RON-55`.
3. **`ground_truth` переезжает в ядро** (`reviewer/metrics/brief_quality/ground_truth.py`),
   `eval/solve_task_metrics/ground_truth.py` становится реэкспортом — тот же прецедент, что у
   `classify`/`recall`/`briefs` (PRI-250). Две копии логики «какой мерж считать за PR» — ровно
   тот разъезд чисел, от которого страхует guard-тест.
4. **Повторный съём обновляет строку**: уникальный индекс по
   `(repo, pr_number, COALESCE(task_key, ''))` + `ON CONFLICT DO UPDATE`.
5. **Дефолтный паттерн ключа — общий `[A-Z]+-\d+`**, когда `task_board.key_pattern` не задан:
   чужой клон без `.review.yml` считается без всякой настройки.
6. **Один конфиг метрики** вместо трёх независимых каналов; параметр `config` в функциях ядра
   обязателен.
7. **`categorize_miss` становится производным** от `core_paths`; репо-специфичные ярлыки
   уходят.
8. **История офлайн-харнесса пишется рядом с целевым клоном**, а не в `eval/` этого репозитория.

## Архитектура

### Конфигурация метрики

Новый модуль `reviewer/metrics/brief_quality/config.py`:

```python
@dataclass(frozen=True)
class BriefQualityConfig:
    core_paths: tuple[str, ...]   # glob-паттерны ядра; "!"-префикс — исключение
    key_pattern: str              # task_board.key_pattern, иначе DEFAULT_KEY_PATTERN
    briefs_dir: str               # "docs/superpowers/briefs"
    configured: bool              # был ли core_paths задан явно
```

- `DEFAULT` воспроизводит нынешнее поведение rag_for_git:
  `("reviewer/**/*.py", "plugin/**", "!plugin/**/*.md", "!eval/**", "*.py")`.
- `key_pattern` по умолчанию — `DEFAULT_KEY_PATTERN` из `reviewer/services/task_keys.py:16`
  (`[A-Z]+-\d+`). Второй дефолт не заводится: константа уже есть в проде и используется
  `review_service.py:365`.
- `briefs_dir` — поле конфига, а не константа модуля. Это устраняет второй, независимый
  хардкод `BRIEFS_DIR` в `reviewer/services/brief_quality.py:25`; ключа в `.review.yml` у
  каталога брифов нет — он переопределяется только флагом `--briefs-dir`.
- `from_policy_data(data)` читает `metrics.brief_quality.core_paths` и `task_board.key_pattern`
  из уже отрезолвленных данных политики.

**Ключ `.review.yml`** — `metrics.brief_quality.core_paths`, tri-state ровно как у
`ReviewPolicy._summary_paths_ignore` (`reviewer/policy/policy.py:44-59`): ключа нет или значение
`null` → дефолт и `configured=False`; явный список, включая пустой, → `configured=True`.
`metrics` — обычный mapping, поэтому `config/deepmerge.py` сливает его рекурсивно, а
`reviewer config show` показывает листовой путь `metrics.brief_quality.core_paths`.

**Матчер путей — собственный glob→regex, не `fnmatch`.** `fnmatch("reviewer/x.py", "*.py")`
возвращает `True`, потому что не знает про `/`, и правило «только `*.py` в корне» на нём
невыразимо. Семантика: `**` пересекает `/`, `*` и `?` — нет. Путь принадлежит ядру, если
совпал хотя бы с одним позитивным паттерном и ни с одним `!`-исключением.

### Ядро метрики

- `classify.is_core_production_path(path, config)` — предикат становится производным от
  `config.core_paths`.
- `classify.categorize_miss(path, existed_before, config)` — категории выводятся из тех же
  паттернов: новый файл первым, затем «ядро: верхний сегмент + модуль» и «не ядро: верхний
  сегмент». Репо-специфичные ярлыки (`.review.yml/конфиги`, `plugin/skills/*.md`,
  `plugin/ (прочее)`) исчезают. Числа recall это не трогает — категории в них не входят, —
  но ярлыки в офлайн-отчёте изменятся; размен принят сознательно, потому что на чужом
  репозитории прежние ярлыки врут (пункт 3 постановки PRI-271).
- `briefs.extract_task_key(filename, config)` — паттерн из конфига вместо `_KEY_RE`.
- `ground_truth` переезжает в ядро; `filter_pr_merges` начинает возвращать пару
  `(sha, pr_number)` — номер уже присутствует в субъекте `Merge pull request #N from …` и нужен
  строке измерения.
- `context_core.derive_context_core` и остальные вызовы получают `config` явным аргументом:
  всего 8 вызовов `is_core_production_path`, из них 5 в `eval/`.

Направление зависимости не меняется: `reviewer/**` по-прежнему не импортирует `eval/**`, а
`eval/solve_task_metrics/{briefs,classify,context_core,recall,ground_truth}.py` реэкспортируют
продакшн-объекты. `tests/metrics/test_reexport_guard.py` расширяется на `ground_truth` и
`config`.

### Точки съёма

Сервисное ядро съёма — `reviewer/services/brief_quality.py`:

```python
def measure_and_record(*, task_key, repo, pr_number, head_sha, changed_paths,
                       changed_status, clone_path, config, history, run_id=None) -> str | None
```

Считает измерение и записывает строку; полностью fail-soft — возвращает статус, никогда не
бросает наружу. `measure(...)` и `find_brief(...)` тоже принимают `config`: каталог брифов и
предикат ядра больше не константы модуля.

- **`publish_review` поведения не меняет.** Гейт `if not dry_run and posted and run_id is not
  None` (`service.py:3199`) остаётся дословно прежним; `_record_brief_quality` сжимается до
  сборки аргументов для `measure_and_record`.
- **`finish_task`** (`service.py:1395-1452`) получает съём после `_backlink_pr`, рядом с
  `_write_through`. `PRTarget` резолвится `parse_pr_url`, VCS создаётся тем же
  `_create_vcs_provider`, `vcs.get_changed_files(number)` даёт полный дифф → `changed_status`
  и `changed_paths` (полный список, не отфильтрованный: знаменатель обязан совпадать с
  офлайновым), клон — нестрогий `_repo_clone_path(repo)`, конфиг — из `_resolve_policy(repo)`,
  `run_id=None`.
  Резолв URL и жизненный цикл VCS выносятся в общий приватный контекст, которым пользуются и
  бэклинк, и съём, — чтобы не открывать соединение дважды. Три исхода бэклинка PRI-238
  (`added` / `already_present` / `failed`) при этом не меняются.
- **Ответ `finish_task` расширяется аддитивно** полем `brief_quality_status`
  (`measured` | `no_brief` | … | `null`). Прежние поля не меняются.

### Хранение

Три идемпотентных изменения в `reviewer/web/schema.sql`:

1. `ALTER TABLE brief_quality ALTER COLUMN run_id DROP NOT NULL`. FK
   `REFERENCES review_runs (id) ON DELETE CASCADE` не трогается: `NULL` ему не подчиняется.
2. Схлопывание возможных дублей по `(repo, pr_number, COALESCE(task_key, ''))` с сохранением
   строки максимального `id` — иначе следующий шаг падает на деплое с историей.
3. `CREATE UNIQUE INDEX IF NOT EXISTS brief_quality_identity ON brief_quality
   (repo, pr_number, COALESCE(task_key, ''))`. `COALESCE` обязателен: обычный UNIQUE не
   покрывает строки с `task_key IS NULL`, так как в SQL `NULL ≠ NULL`.

`ReviewHistory.record_brief_quality` принимает `run_id: int | None` и получает
`ON CONFLICT … DO UPDATE`, где `run_id = COALESCE(EXCLUDED.run_id, brief_quality.run_id)`:
`finish_task` не затирает уже проставленный `run_id`, а `publish_review` дописывает его в
строку, созданную ранее.

Чтение (`brief_quality_trend`, `reviewer/web/api.py:148`) с `review_runs` не джойнится, поэтому
nullable `run_id` его не задевает.

### CLI и офлайн-харнесс

**`reviewer measure-briefs [--repo owner/name] [--path <клон>] [--briefs-dir <dir>] [--json]`.**
Перечисляет брифы каталога, извлекает ключ, для каждого зовёт `ground_truth.collect` и на
каждый настоящий PR-мерж пишет строку через `measure_and_record` с `run_id=None`. Строка на PR,
а не на задачу: идентичность строки — `(repo, pr, task_key)`, а task-level число собирается
union'ом на чтении (`reviewer/web/history.py:697-705`) той же линейкой, что офлайн-baseline.
Статусы файлов берутся из `git diff --name-status <sha>^1 <sha>` — эквивалент офлайновой
проверки `cat-file -e`. Voyage не задействован. Повторный прогон обновляет строки, а не
плодит.

**Офлайн-харнесс** получает `--repo-path` и `--briefs-dir`; `REPO_ROOT` и `BRIEFS_DIR`
(`eval/solve_task_metrics/__main__.py:28-29`) становятся дефолтами этих флагов у всех четырёх
команд, которые их используют: `snapshot`, `forecast`, `replay`, `subqueries`. История и отчёт
пишутся в `<repo-path>/eval/`, поэтому ряды разных репозиториев не смешиваются и замеры
приёмок PRI-255…266 остаются нетронутыми.

## Обработка ошибок

Контракт fail-soft не ослабляется ни в одной точке: недоступный VCS, неизвестный клон,
отсутствующий бриф и нечитаемый бриф остаются именованными статусами измерения, а не
исключениями наружу. `finish_task` возвращает прежний результат даже при полном провале съёма.
Невалидный `key_pattern` из конфига обрабатывается так же, как в `task_keys.extract_task_keys`:
warning и пустой результат, без падения.

## Тестирование

- Фикстура «чужого репозитория»: временный git-репозиторий с ядром `app/**/*.py` +
  `frontend/src/**`, `task_board.key_pattern: RON-\d+`, брифом `RON-55` — распознавание ключа и
  арифметика 11/13 (критерии 1 и 4 PRI-271).
- Новый статус `unconfigured_core_denominator` против прежнего `empty_core_denominator` —
  по образцу `tests/eval/test_replay.py:114-124` (критерий 3 PRI-271).
- Guard-тест на чтение `metrics.brief_quality.core_paths` из конфига; расширение
  `tests/metrics/test_reexport_guard.py` на `ground_truth` и `config`.
- Идемпотентность: повторный `finish_task` и последующий `publish_review` по тому же PR дают
  одну строку, `run_id` дописывается (критерий 2 PRI-270); строка пишется при `run_id IS NULL`
  (критерий 5 PRI-270).
- Регрессия дефолта: `python -m eval.solve_task_metrics snapshot` на конфиге без `core_paths`
  даёт прежние `core_recall_median 0.5714` и `bulk_core_recall_median 0.3571`. Оговорка: бриф
  этой задачи попадает в корпус и мержей ещё не имеет, поэтому сверяются медианы измеренных
  задач, а не размер корпуса (75 → 76 брифов — ожидаемое изменение).
- Ручной шаг приёмки (критерий 5 PRI-271): мутационная проверка guard-теста — снять чтение
  конфига на копии вне рабочего дерева, тест обязан покраснеть.

## Документация

- `CLAUDE.md` — раздел «Неочевидные факты»: почему `unconfigured_core_denominator` требует двух
  условий сразу, почему матчер путей свой, а не `fnmatch`, и почему уникальность строки
  измерения нуждается в `COALESCE`.
- `README.md` и `README.ru.md` — новая команда `reviewer measure-briefs` и ключ
  `metrics.brief_quality.core_paths`; оба файла правятся синхронно.

## Вне скоупа

- Живой прогон на клоне rondo: критерий 1 закрывается фикстурой; сверка на настоящем клоне
  возможна позже отдельно и на вердикт этой работы не влияет.
- Конфигурируемость каталога брифов через `.review.yml`: только флаг CLI.
- Изменение гейта `publish_review` и любых чисел ревью.
- Вебхук на мерж PR и любые новые источники наблюдений помимо `finish_task` и
  `measure-briefs`.

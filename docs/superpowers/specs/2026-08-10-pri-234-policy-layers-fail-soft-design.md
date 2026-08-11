# PRI-234 — сбой VCS при чтении коммиченного `.review.yml` не обнуляет локальные слои политики

Бриф: `docs/superpowers/briefs/2026-08-10-PRI-234-vcs-failure-must-not-drop-local-policy-layers.md`
Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-234

## Проблема

`resolve_policy_data` (`reviewer/config/layers.py:319`) читает коммиченный слой политики выражением
`_read_mapping(fetch_repo_yaml(ref), ".review.yml")` без обработки исключений. Любой сбой фетчера
(нет сети, нет токена, 404, заглушенный remote) выбрасывается наружу и уничтожает весь резолв: уже
смерженный `home:review.yml` (`layers.py:318`) теряется, а самый приоритетный слой
`home:repos/<owner>/<name>.yml` (`layers.py:328`) не домерживается вовсе.

В результате `paths.ignore`, `context_limits`, `summary_cluster_depth*` и `task_board` не
применяются, хотя объявлены в локальном файле, который читается без сети и всё равно перекрыл бы
коммиченный слой. Резолв веток (`reviewer/config/branches.py:664`) в VCS не ходит и тот же сбой
переживает без потерь — асимметрия видна пользователю: ветки печатаются, политика обнуляется.

Диагностика не помогает: `reviewer/entrypoints/cli.py:220` печатает голое `type(exc).__name__`
(например, `HTTPStatusError`) без слоя, кода ответа и репозитория. Скупость вида намеренная — она
не даёт эхоить URL и токены из VCS-клиента, — но отладить по ней нельзя.

## Цели

1. Сбой чтения коммиченного слоя не обнуляет домашние слои: резолв продолжается, слой считается
   неприменённым, причина фиксируется структурно.
2. Потребитель может отличить «слоя нет в репозитории» от «слой не прочитан».
3. Пути, где тихая потеря политики недопустима (ревью PR, индексация, миграция), остаются громкими.
4. Диагностика структурная и бессекретная: слой, репозиторий, ref, категория, HTTP-код — без URL,
   заголовков, тела ответа и токена.
5. `config show` при недоступном коммиченном слое печатает эффективные значения из домашних слоёв и
   отмечает пропуск.

Вне скоупа: замена сетевого фетчера локальным чтением из клона — это смежная задача PRI-235.
Контракт `fetch_repo_yaml: Callable[[str], str | None]` сохраняется без изменений, чтобы PRI-235
могла подставить локальный фетчер поверх этой работы.

## Решения

Приняты в ходе brainstorming; здесь зафиксированы как требования.

- **Граница fail-soft — доставка и парсинг.** Мягко обрабатывается и сбой `fetch_repo_yaml`, и
  ошибка разбора уже полученного текста. Это симметрия с существующим `merge_home`
  (`layers.py:284-316`), где оба класса сбоя ведут себя одинаково; `config show` при этом никогда не
  теряет вывод.
- **Строгий режим — отдельный флаг `strict_committed`**, а не расширение `strict_home`. `config show`
  уже вызывает резолвер с `strict_home=True` (`cli.py:210`); расширение флага заставило бы его падать
  ровно там, где критерий приёмки №1 требует печатать effective + warning.
- **Структура вместо флага** — общий для всех слоёв кортеж `skipped: tuple[SkippedLayer, ...]`.
  `warnings` остаются человекочитаемыми, `skipped` — машиночитаемый источник для JSON и гейтов.
- **Классификатор — отдельная чистая функция** в своём модуле: `layers.py` не должен знать форму
  HTTP-исключений, а функция без зависимостей тестируется на отсутствие секретов напрямую.
- **`config show` сохраняет ненулевой код возврата** при пропуске слоя: эффективная политика
  неполная и может отличаться от реальной, внешние скрипты (`config show; echo $?`) продолжают
  ловить деградацию без парсинга JSON.
- **MCP `_resolve_policy` остаётся мягким.** У него четыре потребителя: `sync_board`
  (`mcp/service.py:931`), `_resolve_summary_depth` (`:1389`), `_resolve_summary_topk_threshold`
  (`:1403`), `_resolve_context_limits` (`:1414`). Три последних уже обёрнуты в собственный
  `except Exception` с откатом на env-дефолты — строгий режим ниже по стеку заставил бы их молча
  потерять `home:repos/<repo>.yml`, то есть воспроизвёл бы исходный баг этажом выше и нарушил
  критерий приёмки №2. Ревью при этом не страдает: `prepare_review` резолвит политику в
  `review_service.py:222`, а `publish_review` использует уже готовую `prepared.policy`.

## Архитектура

### Новый модуль `reviewer/config/fetch_errors.py`

```python
def classify_fetch_error(exc: BaseException) -> tuple[str, int | None]:
    """Вернуть (transport, http_status) по форме исключения фетчера."""
```

- Определяет форму по атрибутам: `getattr(getattr(exc, "response", None), "status_code", None)` и
  имени класса исключения.
- **Никогда не читает `str(exc)`, `repr(exc)`, `exc.args`, `request.url`** — там живут URL и токен.
- `transport` ∈ `{"http", "timeout", "connection", "unknown"}`. `http_status` — валидный int в
  диапазоне 100–599 либо `None`.
- Не импортирует `httpx` и вообще ничего из VCS-слоя: модуль без зависимостей, чистая функция.

### Изменения в `reviewer/config/layers.py`

```python
@dataclass(frozen=True)
class SkippedLayer:
    layer: str             # "home:review.yml" | ".review.yml" | "home:repos/o/r.yml"
    repo: str
    ref: str | None        # ref только у коммиченного слоя; у домашних — None
    category: str          # "unavailable" | "malformed" | "invalid" | "credential"
    transport: str | None  # заполняется только при category == "unavailable"
    http_status: int | None

    def as_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class ResolutionMeta:
    sources: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...]
    skipped: tuple[SkippedLayer, ...] = ()
```

Поле объявляется с дефолтом: `ResolutionMeta` — frozen dataclass, и без дефолта сломались бы
`_empty_meta()` (`layers.py:475`) и конструкторы в тестах. `as_dict()` получает ключ `"skipped"` со
списком словарей.

Сигнатура резолвера:

```python
def resolve_policy_data(
    repo, ref, fetch_repo_yaml, *,
    config_root=None, strict_home=False, strict_committed=False,
) -> tuple[dict[str, object], ResolutionMeta]:
```

Внутри появляется `merge_committed()` рядом с существующим `merge_home()`; общими остаются замыкания
`merge()` и новое `record_skip()`. Обобщать `merge_home` в единый загрузчик **не следует**: слои
валидируются по-разному (у домашних есть `stat`/`S_ISREG`-проба, запрет credential-ключей и
`_validate_known_policy_data`, у коммиченного — вырезание ключа `repository` с warning), и унификация
молча изменила бы правила валидации коммиченного слоя.

`merge_committed()` состоит из двух раздельных `try`:

1. `text = fetch_repo_yaml(ref)` — при исключении: `category="unavailable"`,
   `(transport, http_status) = classify_fetch_error(exc)`.
2. `_read_mapping(text, ".review.yml")` — при `yaml.YAMLError | HomeConfigError | RecursionError |
   UnicodeError`: `category="malformed"`.

В обоих случаях в мягком режиме пишется запись в `skipped` и строка в `warnings`, резолв продолжается
и `home:repos/<repo>.yml` домерживается поверх. В строгом режиме — `raise HomeConfigError(...)
from None`; `from None` обязателен, иначе исходное исключение с URL всплывёт в трейсбеке.

Домашние слои тоже начинают писать в `skipped` (категории `malformed`, `invalid`, `credential`) —
сейчас их пропуск виден только строкой в `warnings`. Механизм один на все слои.

**Инвариант отсутствия.** Если фетчер вернул `None`, слой отсутствует: записи в `skipped` нет и в
`sources` он не появляется. Запись в `skipped` означает ровно одно — слой существовал или мог
существовать, но не был применён. Это и есть требуемое различение «слоя нет» vs «слой не прочитан».

### Режим в каждой точке вызова

| Точка | `strict_committed` | Обоснование |
|---|---|---|
| `reviewer/services/review_service.py:222` (ревью PR) | `True` | тихая потеря политики недопустима; согласовано с комментарием на `:209` |
| `reviewer/entrypoints/cli.py:813` (`reviewer index`) | `True` | неполный `paths.ignore` → мусор в индексе; сохраняет текущее поведение |
| `reviewer/config/layers.py:770`, `:926` (миграция) | `True` | перенос неполной политики в home-файл необратим |
| `reviewer/entrypoints/cli.py:206` (`config show`) | `False` | критерий приёмки №1 |
| `reviewer/mcp/service.py:1370` | `False` | критерий приёмки №2; выбор помечается комментарием со ссылкой на четырёх fail-soft потребителей |

Дефолт `strict_committed=False` вместе с явными `True` в трёх местах означает, что поведение
меняется ровно там, где этого требует задача.

### `config show`

- Переподнятие `vcs_error` (`cli.py:204`) убирается. Недоступный VCS-провайдер превращается в
  фетчер, который бросает при вызове, — и попадает в тот же механизм `skipped`, а не в отдельную
  ветку.
- `build_config_report` (`layers.py:450`) добавляет в отчёт ключ `"skipped"`.
- `_render_config_report` (`cli.py:125`) печатает `effective`/`sources`/`shadowed`/`warnings` как
  раньше и дополнительно блок пропущенных слоёв: слой, репозиторий, ref, категория, транспорт,
  HTTP-код.
- `policy_error` остаётся только для настоящих ошибок команды — когда эффективную политику собрать
  не удалось вовсе (битая effective policy, невалидный home-слой при `strict_home=True`), — и
  по-прежнему печатается вместо секции effective. Пропуск коммиченного слоя к `policy_error` больше
  не приводит: политика собрана, просто неполна, и это выражается через `skipped`.
- Код возврата `1` — при наличии `policy_error` **или** непустого `skipped`.

**Осознанное изменение поведения:** `HomeCredentialError` (запрещённый credential-ключ в домашнем
файле) сейчас даёт warning и код возврата 0; после правки он становится записью в `skipped`
с `category="credential"` и даёт код возврата 1. Это то же событие — слой не применён.

## Обработка ошибок и безопасность

- Диагностик собирается только из структурированных полей: слой, репозиторий, ref, категория,
  транспорт, HTTP-код. Текст исключения в него не попадает ни в каком виде.
- Строгий режим бросает `HomeConfigError` с тем же бессекретным текстом и `from None`, чтобы
  цепочка исключений не вынесла URL в трейсбек.
- `repo` (`owner/name`) и `ref` (ветка или SHA) секретами не являются и включаются намеренно —
  без них диагностика бесполезна в мульти-репо деплое.
- Существующий санитайзер `_config_error_message` (`cli.py:107`) сохраняется для `policy_error`.

## Тестирование

Новые файлы и тесты:

- `tests/config/test_fetch_errors.py` — классификатор на фейковых исключениях: объект с
  `response.status_code`, таймаут, ошибка соединения, неизвестный тип; отдельный тест, что
  исключение с URL и токеном в `str(exc)`, `args` и `request.url` не влияет на результат.
- `tests/config/test_layers.py`:
  - фетчер бросает → `paths`/`context_limits`/`task_board` из `home:repos/o/r.yml` применены, ровно
    один warning, `skipped` содержит `.review.yml` с repo, ref и категорией `unavailable`; следом
    `pytest.raises(HomeConfigError)` при `strict_committed=True` (образец —
    `test_runtime_warns_when_home_file_probe_fails:102-130`);
  - битый YAML коммиченного слоя → `category="malformed"`, та же пара мягкий/строгий;
  - фетчер вернул `None` → `skipped` пуст, warnings пусты;
  - секреты: исключение с URL и токеном → подстроки отсутствуют в `warnings`, в `as_dict()`
    записи `skipped` и в тексте исключения строгого режима (образец негативного assert —
    `test_committed_repository_key_is_ignored_with_warning:860-868`).
- `tests/services/` — `ReviewService.prepare` со сбойным фетчером падает (строгий режим сохранён).
- MCP — при недоступном VCS `context_limits`, `summary_cluster_depth` и `task_board` приходят из
  домашнего слоя, а не из env-дефолтов.

Обновляются существующие:

- `tests/entrypoints/test_cli_config_show_branches.py:38-54` —
  `test_policy_error_does_not_echo_raw_exception_text` жёстко ждёт
  `payload["policy_error"] == "RuntimeError"`; ожидание меняется на
  структурированный `skipped`, инвариант «нет сырого текста исключения» сохраняется и усиливается.
- `tests/entrypoints/test_config_commands.py:113`, `:200` — сценарии «VCS недоступен» теперь ждут
  напечатанную секцию effective и `skipped`, код возврата остаётся ненулевым.

## Документация

- `CLAUDE.md` — пункт в «Неочевидные факты»: сбой чтения коммиченного слоя не обнуляет домашние,
  строгий режим сохранён для ревью/индексации/миграции.
- `README.md` и `README.ru.md` — синхронно, в разделе о слоях политики и `config show`.

## Критерии приёмки

1. При недоступном коммиченном `.review.yml` `config show` печатает эффективные значения из
   домашних слоёв и отмечает пропуск слоя вместо одной строки `policy_error`.
2. `paths.ignore`, `context_limits`, `summary_cluster_depth`, `task_board` из
   `home:repos/<owner>/<name>.yml` применяются при недоступном VCS — во всех путях, включая MCP.
3. Строгий режим сохранён для ревью PR, `reviewer index` и миграции: там сбой чтения политики
   остаётся громким.
4. Диагностик содержит слой, репозиторий, ref, категорию и HTTP-код при его наличии и не содержит
   URL, заголовков, тела ответа и токена; закреплено тестом.
5. Тесты на fail-soft и на строгий режим зелёные; обновлённые существующие тесты зелёные.

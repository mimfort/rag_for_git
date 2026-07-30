# PRI-221 — домашний слой конфигурации репозиториев

Исходный бриф:
`docs/superpowers/briefs/2026-07-30-PRI-221-home-repository-config-layer.md`.

## Контекст

Сейчас политика репозитория разрешается только из ENV и закоммиченного
`.review.yml`. Сам YAML читается независимо в шести местах: дважды при подготовке
ревью, трижды в MCP для сводок и лимитов контекста и один раз при локальной
индексации. Из-за этого оператор нескольких сервисных репозиториев вынужден
коммитить одинаковую конфигурацию в каждый репозиторий.

У reviewer уже есть стабильный XDG-каталог для `.env`, но в нём нельзя хранить
общие или per-repo несекретные настройки. PRI-221 добавляет такие слои, не меняя
два инварианта:

- конфигурация репозитория не читается из незакоммиченного рабочего дерева;
- автор PR не может изменить политику собственного ревью: committed-слой
  читается из целевого commit/ref через VCS API.

## Цели

- Добавить `$XDG_CONFIG_HOME/rag-reviewer/review.yml` для общих настроек и
  `$XDG_CONFIG_HOME/rag-reviewer/repos/<repo>.yml` для локального per-repo
  переопределения.
- Централизовать разрешение YAML-слоёв, provenance, shadowing и предупреждения.
- Сохранить ENV как нижний слой и обратную совместимость `ReviewPolicy.load`.
- Перевести все шесть существующих чтений на один resolver.
- Сделать эффективную конфигурацию наблюдаемой через CLI и историю прогонов.
- Дать безопасную, неразрушающую и идемпотентную миграцию существующего
  `.review.yml`.
- Не допустить чтение, применение или вывод credentials из домашних YAML.

## Не входит

- Чтение `.review.yml` из рабочего дерева вместо указанного git-ref.
- Перенос секретов из `.env` в YAML.
- Удаление либо изменение `.review.yml` командой миграции.
- Глубокое слияние вложенных mapping или объединение списков.
- Кэширование разрешённой политики между вызовами.
- Централизованная синхронизация домашних файлов между хостами.

## Принятые решения

1. Разрешение слоёв живёт в новом `reviewer/config/layers.py`; `ReviewPolicy`
   остаётся моделью политики без скрытого filesystem/VCS I/O.
2. YAML мержится по верхним ключам: присутствующее значение следующего слоя
   полностью заменяет предыдущее, включая mapping, список и `null`.
3. Порядок слоёв: ENV → `home:review.yml` → `.review.yml` целевого ref →
   `home:repos/<repo>.yml`.
4. Ошибка домашнего YAML на runtime-пути пропускает только этот слой с warning.
   Диагностические `config show` и `config migrate` завершаются ошибкой.
5. Credential-подобный ключ приводит к пропуску всего домашнего файла. Ни warning,
   ни JSON-вывод не содержат значение ключа.
6. `config migrate` считает semantic-equivalent destination успешным no-op, а
   отличающийся destination не перезаписывает и не мержит.
7. `ReviewService.prepare` разрешает политику один раз для точного `base_sha` и
   переиспользует её для ignore, review policy, task board и аудита.
8. История хранит provenance как JSONB, а persisted MCP session переносит его
   backward-compatible полем `PreparedReview`.

## Каталог и пути

Новый модуль предоставляет единый helper корня:

```text
$XDG_CONFIG_HOME/rag-reviewer
└── review.yml
└── repos/
    └── owner/
        └── name.yml
```

При отсутствии `XDG_CONFIG_HOME` используется `~/.config/rag-reviewer`. Тесты
инъектируют `config_root`, поэтому не читают реальный домашний каталог.

Repo-id нормализуется в lowercase и разбивается на безопасные POSIX-сегменты.
Разрешены минимум два непустых сегмента, поэтому `group/subgroup/repo` естественно
становится `repos/group/subgroup/repo.yml`. Сегменты `.`, `..`, абсолютные пути,
обратные слеши и NUL отклоняются до filesystem I/O. Общая
`reviewer.services.repo_id.normalize_repo` расширяется на GitLab-подгруппы, чтобы
слой был достижим не только из прямого вызова resolver.

## Модель результата

`reviewer/config/layers.py` вводит неизменяемую metadata-модель:

```python
@dataclass(frozen=True)
class ResolutionMeta:
    sources: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...]
```

Главная функция:

```python
resolve_policy_data(
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    *,
    config_root: Path | None = None,
    strict_home: bool = False,
) -> tuple[dict, ResolutionMeta]
```

`fetch_repo_yaml(ref)` является единственным способом получить committed
`.review.yml`. Resolver не знает о GitHub, GitLab или локальном git checkout.

`sources` содержит победивший источник каждого верхнего YAML-ключа. Для ключей,
которых нет ни в одном YAML, `config show` добавляет источник `env` при рендеринге
эффективной `ReviewPolicy`. Стабильные labels:

- `env`;
- `home:review.yml`;
- `.review.yml`;
- `home:repos/<repo>.yml`.

`shadowed[key]` перечисляет ранее встретившиеся источники в порядке слоёв. Источник
считается затенённым даже при равном значении: это важно для объяснения будущих
изменений после миграции. Metadata содержит только имена ключей и источников.

## Загрузка и слияние

Каждый YAML должен декодироваться в mapping либо быть пустым. Скаляр или список на
верхнем уровне является ошибкой слоя. Merge выполняется обычным присваиванием
верхнего ключа:

```python
for key, value in layer.items():
    if key in merged:
        shadowed[key].append(sources[key])
    merged[key] = value
    sources[key] = layer_source
```

В частности:

- `paths.ignore` заменяется вместе со всем ключом `paths`;
- `context_limits` не deep-merge'ится между файлами;
- `task_board: null` явно выключает доску;
- неизвестные ключи сохраняются в merged data для forward compatibility, но
  игнорируются `ReviewPolicy.load_data` и не попадают в effective output
  `config show`, потому что фактически не влияют на политику.

Ошибка чтения, декодирования или валидации домашнего слоя:

- при `strict_home=False` добавляет безопасный warning и пропускает слой;
- при `strict_home=True` поднимает типизированную `HomeConfigError`.

Ошибки callback или committed `.review.yml` не маскируются самим resolver. Поэтому
при отсутствии домашних файлов сохраняется текущая семантика каждого потребителя:
ReviewService/index могут завершиться ошибкой, а существующие MCP fail-soft wrappers
сведут её к дефолту.

## Защита credentials

Проверка домашних слоёв рекурсивно анализирует только имена ключей. Запрещены:

- secret env names и aliases из registry board providers;
- точные имена секретных полей `Settings`;
- нормализованные suffix-формы `_token`, `_password`, `_secret`, `_api_key`,
  `_private_key`, `_client_secret`, `_access_key`.

Проверка не считает credential обычные лимиты вроде `max_tokens`: совпадение должно
быть точным либо по singular suffix. При обнаружении запрещённого пути весь домашний
слой пропускается. Warning сообщает source и путь ключа, но не сериализует значение
или соседние данные.

Committed `.review.yml` сохраняет нынешнюю совместимость. Его `task_board`
по-прежнему проходит существующую registry-driven проверку credentials в
`normalize_task_board_config`; общий новый denylist применяется только к домашним
слоям.

## `ReviewPolicy`

В `reviewer/policy/policy.py` добавляется:

```python
ReviewPolicy.load_data(settings, data: Mapping[str, object]) -> ReviewPolicy
```

Метод начинает с `from_settings(settings)` и применяет присутствующие ключи той же
логикой, что текущий `load`. `load(settings, yaml_text)` только парсит YAML и
делегирует в `load_data`. Поведение `from_yaml`, публичные поля и существующие тесты
остаются совместимыми.

Для CLI вводится детерминированный serializer effective policy обратно в публичную
YAML-shaped форму. Он не использует `Settings.model_dump()` и потому физически не
может вывести токены, пароли, DSN или другие server settings.

## Интеграция runtime

### Подготовка ревью

После получения PR и проверки tracked branch `ReviewService.prepare` вызывает
resolver один раз:

- repo — канонический repo-id;
- ref — `prq.base_sha`, чтобы ignore и полная policy относились к одному commit;
- callback — `vcs.get_file_at_ref(".review.yml", ref)`.

Полученная `ReviewPolicy` используется для base index sync, graph patch, overlay,
review gate и task-board extraction. Второе чтение `.review.yml` по `base_ref`
удаляется. `PreparedReview.config_sources` получает JSON-friendly metadata.

### MCP session-less paths

`MCPReviewService` получает общий helper `_resolve_policy(repo, branch)`, который
владеет lifecycle VCS provider и возвращает `(policy, meta)`. Его используют:

- `_resolve_summary_depth`;
- `_resolve_summary_topk_threshold`;
- `_resolve_context_limits`.

Существующие fail-soft fallback и закрытие provider сохраняются. Source для
`summary_topk_threshold` берётся из одноимённого верхнего ключа. Для legacy tuple
depth/overrides источник выбирается детерминированно: источник
`summary_cluster_depth`, если ключ задан; иначе источник overrides, если задан
только он; иначе `env`. Если оба ключа пришли из разных слоёв, `config show`
остаётся полной картиной, а legacy строка намеренно отражает источник depth.

### Локальная индексация

`reviewer index` уже вычисляет канонический `repo_id` и git-ref. Resolver получает
callback на `file_at_ref(local_repo, ".review.yml", ref)`. Фильтрация файлов и
`update_base(ignore=...)` используют `ReviewPolicy.ignore` из всех слоёв. Рабочий
файл вне указанного ref не читается.

## CLI `reviewer config`

Добавляется Click group `config` с командами `show` и `migrate`. CLI отвечает только
за параметры, открытие/закрытие VCS provider, форматирование и преобразование
типизированных ошибок в `click.ClickException`; merge и migration decisions живут
в `reviewer/config/layers.py`.

### `config show`

```text
reviewer config show --repo owner/name --branch main [--json]
```

Branch по умолчанию — `Settings.primary_branch()`. Committed YAML читается через
тот же VCS provider, что runtime-review; локальный checkout не требуется.

Human output показывает каждый верхний ключ, эффективное значение, winning source
и затенённые источники. JSON имеет стабильную форму:

```json
{
  "repo": "owner/name",
  "branch": "main",
  "effective": {},
  "sources": {},
  "shadowed": {},
  "warnings": []
}
```

`effective` строится только из публичной модели политики. Serializer явно
перечисляет policy fields и никогда не сериализует `Settings` целиком или неизвестные
YAML-ключи. Домашний файл с credentials пропускается с warning; значение credential
не попадает ни в human, ни в JSON output.

### `config migrate`

```text
reviewer config migrate --repo owner/name --branch main
```

Алгоритм:

1. Строго загрузить домашние слои и committed `.review.yml`.
2. Ошибиться, если committed файл отсутствует, пуст, не является mapping либо не
   проходит credential-проверку как будущий домашний файл; секретный payload даже
   временно не записывается в домашний каталог.
3. Вычислить canonical effective values до миграции.
4. Определить `repos/<repo>.yml` и проверить отсутствие symlink/path traversal.
5. Если destination существует и semantic-equal committed mapping — напечатать
   успешный no-op.
6. Если destination существует и отличается — ничего не писать, перечислить только
   конфликтующие верхние ключи и завершиться non-zero.
7. Если destination отсутствует — атомарно записать исходный committed YAML через
   temporary file в том же каталоге и `os.replace`; новый файл получает mode `0600`.
8. Повторно разрешить policy и сравнить canonical effective values до/после.
9. При несовпадении удалить только что созданный destination и завершиться ошибкой.
10. Напечатать итог `config show` и предупреждение, что оставшийся `.review.yml`
    затенён по перечисленным ключам.

Команда не редактирует, не удаляет и не коммитит repository file. Сравнение
идемпотентности семантическое, поэтому различия комментариев и форматирования сами
по себе не вызывают конфликт.

## Аудит и persisted sessions

В `PreparedReview` добавляется поле с default:

```python
config_sources: dict = field(default_factory=dict)
```

`session_serde.to_payload` пишет поле, а `from_payload` читает через `.get`, поэтому
старые persisted sessions остаются восстанавливаемыми.

В `reviewer/web/schema.sql` добавляется идемпотентная колонка
`review_runs.config_sources JSONB`. `ReviewHistory.record_run` и MCP publish path
передают объект:

```json
{
  "sources": {},
  "shadowed": {},
  "warnings": []
}
```

Warning-тексты уже санитизированы и не содержат YAML values. History API возвращает
поле вместе с деталями прогона; отдельный индекс для JSONB не нужен.

## `configure-review` и документация

Skill `reviewer_configure-review` сначала определяет repo-id и спрашивает цель
записи:

1. `home:repos/<repo>.yml` — рекомендуемый default, действует без коммита, но не
   виден команде;
2. `.review.yml` указанного git-ref — явный командный вариант, виден в git.

После выбора остальной анализ структуры/churn и подтверждение `paths.ignore`
остаются прежними. Skill сохраняет неизвестные ключи выбранного файла и никогда не
переносит credentials.

`README.md` и `README.ru.md` описывают:

- пути и приоритет четырёх слоёв;
- top-level replacement;
- `config show` и `config migrate`;
- shadowing после миграции;
- отсутствие чтения рабочего дерева;
- риск service-account deployment, где один домашний каталог влияет на все сессии
  хоста.

## Тестирование

### Unit

- `ReviewPolicy.load_data` эквивалентен старому `load` для всех текущих ключей.
- Порядок четырёх слоёв, `null`, неизвестные ключи и top-level replacement.
- `paths.ignore` и `context_limits` заменяются, а не deep-merge'ятся.
- Точные `sources` и `shadowed`, включая равные значения.
- XDG/default paths, subgroup repo-id и path-traversal rejection.
- Malformed/non-mapping YAML в runtime и strict режимах.
- Credential detection не выводит значения и не ловит `max_tokens`.

### Runtime wiring

- ReviewService без committed `.review.yml` получает ignore/policy/task-board из
  home и использует один committed fetch для exact base SHA.
- Per-repo home перебивает committed policy.
- MCP depth/top-k/context limits используют resolver и сохраняют fail-soft.
- `reviewer index` исключает пути только из домашнего слоя.
- Отсутствие домашних файлов сохраняет существующие результаты и ошибки.

### CLI и persistence

- `config show` human/JSON, source labels, shadowing и sanitization.
- `config migrate`: create, semantic no-op, conflict refusal, atomic rollback,
  symlink refusal и неизменность repository file.
- `PreparedReview` round-trip со старым и новым payload.
- Идемпотентная schema migration и запись/чтение `config_sources`.
- Обновлённый configure-review prompt и двуязычные documentation guards.

## Порядок поставки

1. Чистый layers resolver, provenance и policy `load_data`.
2. ReviewService и index wiring.
3. MCP resolver и source reporting.
4. CLI `config show` и `config migrate`.
5. Session/history audit.
6. Configure-review и двуязычная документация.

Каждый этап начинается с failing unit/integration test. Реализация не создаёт
внешних сетевых зависимостей в тестах: VCS callback, config root, filesystem и
history connections инъектируются либо заменяются fake-объектами.

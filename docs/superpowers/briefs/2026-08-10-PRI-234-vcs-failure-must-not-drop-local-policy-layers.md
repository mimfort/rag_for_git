# Brief — PRI-234 Сбой VCS при чтении коммиченного .review.yml не должен обнулять локальные слои политики
https://ru.yougile.com/team/686c049c8af8/#PRI-234

## Task
- `resolve_policy_data` читает коммиченный слой вызовом `fetch_repo_yaml(ref)` без `try` — любое исключение фетчера (нет сети/токена, 404, заглушенный remote) уничтожает весь резолв: уже смерженный `home:review.yml` теряется, а приоритетнейший `home:repos/<owner>/<name>.yml` не домерживается вовсе.
- Следствие: `paths.ignore`, `context_limits`, `summary_cluster_depth*`, `task_board` не применяются, хотя объявлены в локальном файле, который читается без сети и всё равно перекрыл бы коммиченный слой.
- Требуется: (1) fail-soft обёртка вокруг `fetch_repo_yaml` с записью причины в `ResolutionMeta.warnings`; (2) отдельное поле в `ResolutionMeta` — «слоя нет» vs «слой не прочитан»; (3) строгий режим для путей ревью (`review_service`, MCP `_resolve_policy`); (4) структурированный диагностик вместо голого `type(exc).__name__` (слой, репо, ref, категория, HTTP-код — без URL/заголовков/токена); (5) `config show` печатает прочитанные слои + warning; (6) тесты на fail-soft, строгий режим и отсутствие секретов в диагностике.
- Приёмка: `config show` при недоступном VCS печатает effective из домашних слоёв + warning вместо одной строки `policy_error`; home-политика применяется; строгий режим для ревью сохранён; диагностик закреплён тестом.

## Related work
- ID-289 / PRI-235 «Читать коммиченный .review.yml из локального клона в config show и MCP» — прямой сосед (`links: related`), правит те же точки (`cli.py:206`, `mcp/service.py:1370`): убирает сетевой вызов там, где клон доступен. PRI-234 делает резолв устойчивым к сбою, PRI-235 — устраняет причину. Решение PRI-234 не должно мешать подстановке локального фетчера (контракт `fetch_repo_yaml` остаётся `Callable[[str], str | None]`).
- ID-274 (done) «Домашний слой конфига репозиториев» — ввёл сами слои `home:review.yml` / `home:repos/<repo>.yml` и `merge_home` с fail-soft/`strict_home`; образец, по которому строится fail-soft коммиченного слоя.
(dropped 6: ID-277 init/onboarding, ID-133 GitLab-провайдер, ID-168 configure-review, ID-141 preflight freshness, ID-115 review-local — та же область конфигов, но другой механизм; PR-диффы не тянул.)

## Subsystems
- `reviewer/config` — резолв веток/policy/settings из YAML+env с фиксированным порядком слоёв; инварианты: валидация известных полей, отсутствие credential-данных, безопасное чтение.
- `reviewer/policy` — `ReviewPolicy.load_data` из env-дефолтов + `.review.yml` целевой ветки; потребитель резолва (категории, ignore, `context_limits`, `summary_cluster_depth*`, `task_board`).
- `reviewer/services` — `ReviewService.prepare`: резолв политики по `prq.base_sha`, «безопасная обработка недоступных хранилищ».
- `tests/config`, `tests/policy` — приоритеты слоёв, shadowing, warnings, secret-free metadata, YAML-ошибки.

## Relevant code
- `reviewer/config/layers.py:319` — `committed = _read_mapping(fetch_repo_yaml(ref), ".review.yml")`: голый вызов, точка правки №1. Рядом `_read_mapping` может бросить свои ошибки парсинга — решить, покрывает ли их fail-soft (см. open questions).
- `reviewer/config/layers.py:284-316` — `merge_home`: готовый образец fail-soft (`warnings.append` vs `raise` под `strict_home`) и формат сообщения `f"{source}: конфиг не прочитан: {type(exc).__name__}"`.
- `reviewer/config/layers.py:37-47` — `ResolutionMeta(sources, shadowed, warnings)` + `as_dict()`: сюда добавляется поле «слой пропущен». Frozen dataclass → новое поле с дефолтом, иначе ломаются `_empty_meta()` (`layers.py:475`) и конструкторы в тестах.
- `reviewer/config/layers.py:450-472` — `build_config_report`: собирает `effective/sources/shadowed/warnings`; сюда добавляется признак пропуска слоя.
- `reviewer/entrypoints/cli.py:199-231` — `config show`: `vcs_error` переподнимается (`:204`), общий `except Exception` пишет `payload["policy_error"] = type(exc).__name__` (`:220`) — точка правки №4/№5; `SystemExit(1)` (`:231`) — решить, остаётся ли ненулевой код при fail-soft warning.
- `reviewer/entrypoints/cli.py:107-111` — `_config_error_message`: существующий санитайзер публичной диагностики; новый структурированный диагностик строится в той же дисциплине.
- `reviewer/entrypoints/cli.py:125-152` — `_render_config_report`: при `policy_error` печатает одну строку и `return` — именно это обнуляет вывод, надо печатать effective + warning.
- `reviewer/mcp/service.py:1358-1383` — `_resolve_policy`: вызов без `strict`, warnings уходят в `log.warning`; потребители — `_resolve_summary_depth` (`:1385`), gate ревью, `task_board`. Точка правки №3.
- `reviewer/services/review_service.py:217-230` — `prepare`: резолв по `prq.base_sha`; комментарий на `:209` уже фиксирует принцип «битый конфиг обязан быть громким» — сюда строгий режим.
- `reviewer/entrypoints/cli.py:813-821` — `reviewer index`: пятый вызывающий, фетчер локальный (`file_at_ref`), в сеть не ходит; менять поведение не требуется, но сигнатура/режим по умолчанию его затрагивают.
- `reviewer/config/layers.py:770,926` — `_existing_migration_result` / `migrate_repo_config` вызывают `resolve_policy_data(..., strict_home=True)`: миграция обязана падать на нечитаемом коммиченном слое (иначе перенесёт неполную политику) — режим выбрать явно.
- `reviewer/config/branches.py:664-679` — `resolve_repo_branches`: эталон асимметрии (в VCS не ходит, тот же сбой переживает без потерь) и образец докстринга про порядок слоёв/строгость.

## Test exemplars
- `tests/config/test_layers.py:102-130` — `test_runtime_warns_when_home_file_probe_fails`: точный шаблон целевого теста — стаб бросает исключение → данные из уцелевшего слоя, ровно один warning, затем `pytest.raises(HomeConfigError)` в строгом режиме.
- `tests/config/test_layers.py:84-107` — `test_invalid_home_task_sync_filter_is_quarantined_or_rejected`: пара «мягкий/строгий» и точное сравнение текста warning.
- `tests/config/test_layers.py:27-52` — `test_layers_replace_top_level_values_and_report_sources`: базовая фикстура слоёв (`_write` в `tmp_path`, `config_root=tmp_path`, `lambda ref: committed`) — на ней строится проверка критерия №2 (`paths`/`context_limits`/`task_board` из home-repo при недоступном фетчере).
- `tests/config/test_layers.py:860-868` — `test_committed_repository_key_is_ignored_with_warning`: образец негативного assert «в warning нет запрещённого значения» → шаблон для «нет URL и токена в диагностике».
- `tests/entrypoints/test_cli_config_show_branches.py:38-54` — `test_policy_error_does_not_echo_raw_exception_text` жёстко ждёт `payload["policy_error"] == "RuntimeError"`; пункт 4 задачи ломает этот тест — его надо переписать, сохранив инвариант «нет сырого текста исключения».
- `tests/entrypoints/test_config_commands.py:113,200` — сценарии «VCS недоступен: branch-секция печатается, policy уходит в `policy_error`, код возврата ненулевой» — тоже под пересмотр.
(dropped: остальной хвост `tests/` из выдачи — не про резолв слоёв.)

## Constraints / open questions
- Секреты: диагностик обязан содержать слой/репо/ref/категорию/HTTP-код и НЕ содержать URL, тело ответа, заголовки, токен. Ошибки VCS-клиента (`httpx.HTTPStatusError`) несут URL в `str(exc)` — категорию и код извлекать структурно, а не форматированием исключения.
- `ResolutionMeta` — frozen dataclass без дефолтов; новое поле требует дефолта и обновления `_empty_meta()`.
- Обратная совместимость `config show`: JSON-контракт (`policy_error`, код возврата 1) закреплён тестами; при fail-soft надо решить, остаётся ли exit-code ненулевым (внешние скрипты `config show; echo $?`).
- Открытый вопрос: покрывает ли fail-soft только сбой фетчера, или и ошибку парсинга уже полученного текста (`_read_mapping`/`yaml.YAMLError` на `layers.py:319`). Битый коммиченный YAML — это «слой прочитан, но невалиден», семантически иной случай.
- Открытый вопрос: имя/форма строгого режима — отдельный флаг (напр. `strict_committed`) vs расширение существующего `strict_home`; влияет на все 5 точек вызова, включая миграцию (`layers.py:770,926`) и `reviewer index` (`cli.py:813`).
- Пересечение с PRI-235: не закладываться на то, что фетчер всегда сетевой; контракт `Callable[[str], str | None]` сохранить.
- Индекс `dev` переиндексирован в рамках preflight (`drift: 0`, SCIP: 6553 узла / 31830 рёбер, сводок 40); корпус задач синхронизирован (92 задачи, 0 изменений).
- Задача не выполнена: `layers.py:319` в рабочем дереве по-прежнему без обработки исключений.

Собран на: Opus 5 (сессионная модель), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 80 · out 14K · cache-write 198.9K · cache-read 3M
Всего: 3.2M токенов

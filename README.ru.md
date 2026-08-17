# rag-reviewer

[English](README.md)

AI-ревью pull request с контекстом всего репозитория: гибридный поиск, граф кода и
inline-комментарии, привязанные к изменённым строкам.

> Нужны Python 3.11–3.13 и внешние сервисы Voyage, PostgreSQL/ParadeDB и Neo4j.
> Для чтения и публикации ревью также нужны credentials выбранного VCS-провайдера.

[![PyPI](https://img.shields.io/pypi/v/rag-reviewer?color=2563eb&label=PyPI)](https://pypi.org/project/rag-reviewer/)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-2563eb)](https://pypi.org/project/rag-reviewer/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)

## Начните здесь

Выберите кратчайший маршрут для текущей цели. После него обе аудитории переходят к общим
сценариям и справочникам ниже.

| Если вы хотите… | Маршрут |
|---|---|
| Попробовать reviewer и получить первый результат | [Попробовать reviewer](#попробовать-reviewer) |
| Использовать reviewer командой на одном shared host | [Развёртывание для команды](#развёртывание-для-команды) |

## Попробовать reviewer

Понадобятся Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), Docker, API-ключ Voyage и токен
системы контроля версий (VCS), если reviewer должен читать или публиковать ревью. Хранилища
работают локально; запросы эмбеддингов и реранкинга отправляются в Voyage.

1. Установите launcher, синхронизируйте управляемые reviewer artifacts, поднимите хранилища и
   настройте reviewer:

   ```bash
   uv tool install rag-reviewer
   reviewer update
   docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d
   reviewer init
   ```

   `reviewer update` создаёт управляемый Compose-файл рядом с env-файлом в
   `$XDG_CONFIG_HOME/rag-reviewer/` (по умолчанию `~/.config/rag-reviewer/`). Поэтому один набор
   хранилищ обслуживает все репозитории, а имя Compose-проекта не зависит от текущего каталога.
   Команда также обновляет обнаруженные AI-client integrations и скиллы.

2. Посмотрите поддерживаемые AI-клиенты и подключите нужный:

   ```bash
   reviewer install --list
   reviewer install codex
   ```

3. Постройте branch-scoped поисковый snapshot — base index, затем проверьте окружение и свежесть
   индекса:

   ```bash
   reviewer index /path/to/repo --ref main
   reviewer check
   reviewer status /path/to/repo --branch main --json
   ```

   Индексация создаёт схему `chunks`, которую запрашивает `reviewer check`, поэтому в свежей
   установке сначала нужен index. Check проверяет каждый настроенный VCS provider; успешная проверка
   identity не подтверждает repository-specific permissions. Status payload должен показать indexed
   SHA и `drift == 0`.
   Полная индексация отправляет чанки кода в Voyage и может быть медленной на free tier. Без base
   index ревью видит только дифф и временный индекс изменённых файлов (overlay), поэтому получает
   более узкий контекст репозитория.

4. Откройте новую сессию клиента и запустите первое ревью:

   ```text
   # Claude Code
   /rag-reviewer:review-pr owner/repo#123 --dry-run

   # Codex
   $rag-reviewer:review-pr owner/repo#123
   ```

   Синтаксис вызова зависит от клиента. Dry run возвращает обоснованные findings без публикации;
   обычный запуск публикует через `publish_review` и требует VCS write credentials.

Для временного запуска launcher без постоянной установки:

```bash
uvx --from rag-reviewer@latest reviewer
```

## Развёртывание для команды

Этот маршрут предполагает, что участники команды открывают сессии AI-клиентов на одном shared host
под одним service account. Каждый клиент запускает собственный stdio-процесс `reviewer-mcp`; эти
процессы совместно используют PostgreSQL/ParadeDB и Neo4j из Compose, привязанные к `127.0.0.1`, и
reviewer env этого account. Центрального MCP daemon здесь нет. MCP requests передают repo, branch,
project и `provider_options`, а tool results возвращают выбранный контекст кода AI-клиенту. Для
отдельных workstations используйте защищённые network-accessible stores и настройте их DSN и
reviewer env на каждой машине вместо loopback defaults из Compose.

1. **На shared host поднимите хранилища и настройте секреты service account.**

   ```bash
   reviewer update
   docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d
   reviewer init
   ```

2. **Выберите repo и branch scope.** Задайте `DEFAULT_REPO` как fallback repo, а ветки — либо
   упорядоченным allowlist `REVIEW_BRANCHES` (CSV) в server env, либо (предпочтительно) домашним
   per-repo слоем — см. [Репозитории и ветки](#репозитории-и-ветки). Per-repo policy, игнорируемые
   пути, context limits и несекретные board metadata храните в `.review.yml`.

3. **Постройте и проверьте каждую отслеживаемую ветку.**

   ```bash
   reviewer index /srv/rag_for_git --ref main --repo mimfort/rag_for_git
   reviewer check
   reviewer status /srv/rag_for_git --branch main --json
   ```

4. **Подключите клиенты команды.**

   ```bash
   reviewer install --all
   reviewer install codex --dry-run
   ```

   Выполните установку на shared host от того же service account. `--all` настраивает
   поддерживаемые клиенты этого account, а `--dry-run` показывает планируемые записи конфигурации.
   Затем откройте новую chat- или CLI-сессию; интеграциям IDE также может понадобиться Reload
   Window.

5. **Добавьте опциональный board-контекст.** Выберите зарегистрированный provider в
   `.review.yml`, оставьте credentials в reviewer env и проверьте точный project:

   ```bash
   reviewer check --board-project TYPE=PROJECT
   ```

   Повторите `--board-project` для дополнительных providers. См.
   [раздел о досках](#доски-задач) и
   [справочник провайдеров](docs/board-providers.md).

## Основные сценарии

Reviewer workflows поставляются как namespaced skills. Каждый skill определяет собственные
границы чтения/записи и confirmation gates; MCP server выполняет операции с хранилищами, графом,
VCS и доской.

### Ревью pull request

Используйте `review-pr` для поиска багов. Skill готовит PR-сессию, подтягивает код и
графовый контекст, анализирует изменённые файлы, проверяет findings и публикует только обоснованный
результат. Для проверки deployment начните с `--dry-run`. Inline-комментарии возможны только на
commentable строках диффа; off-diff findings уходят в сводку.

### Решение задачи

`solve-task` превращает задачу доски или текстовый запрос в сохраняемый brief до начала
разработки. Skill проверяет свежесть индекса, прогревает task context, собирает related work и код,
затем передаёт brief в brainstorming. Саму задачу этот skill не реализует.

### Обоснованный вопрос по кодовой базе

`ask` подходит для онбординга и Q&A. Ответы ссылаются на реальные `path:line` из base
index и code graph. Skill читает и объясняет, но не ревьюит PR и не изменяет код.

### Гид по PR для ревьюера

`pr-walkthrough` строит порядок чтения: с чего начать, что меняет каждый файл и на каких
callers влияет правка. Это отдельный сценарий, а не bug review.

### Сфокусированное ревью

`performance-review` ищет повторный I/O, N+1, плохую асимптотику, проблемы batching,
caching и памяти. `maintainability-review` ищет сложность, дублирование, проблемы
читаемости, границ и соглашений репозитория. Оба остаются в явно выбранном измерении.

### Создание, декомпозиция и завершение задач доски

`create-task` собирает каноническое тело и пишет только после подтверждения.
`decompose-task` превращает одного сохранённого родителя в полностью показанный preview batch
нативных дочерних задач, запрашивает одно подтверждение, сохраняет previewed idempotency key при
retry, затем синхронизирует и проверяет каждую связь и child read.
`finish-task` добавляет PR, переводит задачу в обнаруженный done target, добавляет ссылку
на задачу в PR body и повторно синхронизирует корпус—тоже только после подтверждения.

### Грунтовка reviewer в фазах план/ревью (опционально)

Грунтовка позволяет planning/review-фазам использовать session-less reviewer tools при свежем
base index.

> **Грунтовка reviewer (план/ревью, опционально, fail-open).** Сначала запустите
> `reviewer status /path/to/repo --branch main --json`. При `drift == 0` используйте
> `search_codebase` для кросс-файловых фактов, а `callers`, `related_symbols`, `definition`,
> `implementations` или `family` — только для центральных символов. Base index не видит
> незакоммиченные правки, поэтому изменённые файлы читайте с диска. Если reviewer или индекс
> недоступен, откатитесь к локальным search/read tools и не блокируйте работу.

- `family(repo, node_id, branch)` — семейство однотипных символов («кто ещё такой
  же»): наследование + структурное соответствие контракту. Для задач-развёрток
  («добавить поле во все провайдеры»), где один найденный файл — представитель
  семейства из N.

## Как это работает

RAG (retrieval-augmented generation) означает, что модель получает код, выбранный гибридным
семантическим и лексическим поиском, а не только PR-дифф. Граф добавляет структурные связи.

```text
PR → prepare_review → base + overlay retrieval → skill analysis
   → verify → policy gate → grounding → dedup → inline comments + summary → cleanup
```

- **Base index.** Постоянные чанки живут под `base:<branch>`. PostgreSQL/ParadeDB объединяет
  approximate nearest-neighbor (ANN) поиск pgvector с лексическим ранжированием BM25; Voyage
  строит эмбеддинги и реранжирует кандидатов.
- **Overlay.** Изменённые файлы PR используют эфемерный ref `pr:N`. Retrieval берёт неизменённые
  файлы из base, а изменённые — из overlay.
- **Code graph.** Узлы Neo4j используют `node_id = path#fqn`, где `fqn` — fully qualified name.
  SCIP, внешний type-aware индексатор кода, даёт `CALLS` и метод-уровневый `IMPLEMENTS`; режим
  `auto` откатывается к tree-sitter `CALLS` и class-level `IMPLEMENTS` из синтаксиса, когда SCIP
  недоступен.
- **Grounded publishing.** Finding обязан цитировать реальный изменённый код. GitHub suggestion
  создаётся только для безопасной замены в RIGHT-части диффа.
- **Idempotency.** Скрытые fingerprint не дают повторно опубликовать тот же finding. Overlay и
  session очищаются после публикации, ошибки очистки fail-soft.

Карта модулей и инварианты находятся в [CLAUDE.md](CLAUDE.md).

## Установка и конфигурация

### Требования

- Python `>=3.11,<3.14`;
- Docker для стандартного PostgreSQL/ParadeDB и Neo4j;
- credentials Voyage для эмбеддингов и реранкинга;
- VCS credentials для чтения и публикации PR;
- поддерживаемый AI-клиент с reviewer MCP integration.

### Установка и обновление

Постоянный CLI:

```bash
uv tool install rag-reviewer
reviewer update
```

`uv tool install` принимает имя пакета и ставит обе его команды — `reviewer` и `reviewer-mcp`.
Опция `--from` здесь лишь уточняет источник того же пакета (`--from rag-reviewer==0.4.3`,
`--from git+…`); форма `--from PACKAGE COMMAND` относится к `uvx`, и `uv tool install` её
отвергает.

Для одноразового перехода с 0.4.3 запустите новый lifecycle через latest uvx и явно разрешите ему
обновить существующий persistent tool:

```bash
uvx --refresh --from rag-reviewer@latest reviewer update --upgrade-tool
```

Все последующие обновления выполняются короткой командой `reviewer update`. Она запускает единый
lifecycle:

- проверяет PyPI и обновляет persistent `uv tool` package при наличии новой версии;
- обновляет MCP integration, native plugin и файловые скиллы каждого обнаруженного AI-клиента;
- синхронизирует `$XDG_CONFIG_HOME/rag-reviewer/docker-compose.yml` с каноническим репозиторием;
- записывает hash управляемого Compose-содержимого в `.reviewer-update.json`.

Если Compose-файл отличается от записанного hash, reviewer считает его изменённым пользователем,
оставляет без изменений и выводит предупреждение. Update не запускает `docker compose pull`, не
перезапускает services, не удаляет containers или volumes, поэтому существующие БД, индексы,
задачи и subsystem summaries сохраняются. Новое Compose-описание можно применить позже
документированной командой `docker compose ... up -d`.

Временный/latest запуск:

```bash
uvx --from rag-reviewer@latest reviewer --help
```

Обычный uvx-запуск не меняет отдельный persistent tool; это делает только явный bootstrap-флаг
`--upgrade-tool`. `reviewer install CLIENT --dry-run` показывает запись конкретной integration до
изменения файлов.

### AI-клиенты

`reviewer update` автоматически обновляет все обнаруженные клиенты. `reviewer install --list` и
именная установка нужны при первом подключении клиента, пока его ещё нельзя обнаружить:

```bash
reviewer install codex
reviewer install --all
reviewer install-skills codex
```

Lifecycle Codex:

```bash
uvx --from rag-reviewer@latest reviewer install codex
uvx --from rag-reviewer@latest reviewer install codex --dry-run
codex plugin list --json
codex mcp list
```

Глобальный plugin Claude Code:

```bash
uvx --from rag-reviewer@latest reviewer install claude-code
claude plugin list --json
claude plugin marketplace list --json
```

После установки или обновления начните New Chat/new CLI session; в IDE также выполните Reload
Window.

### Миграция с ломающим изменением имён скиллов

В этом релизе из имени каждого скилла удалён избыточный сегмент `reviewer_`. Старые вызовы
скиллов не поддерживаются: обновите plugin/cache, используйте короткие имена ниже, затем откройте
New Chat или новую CLI-сессию. В IDE также выполните Reload Window.

### Сервисы и credentials

`reviewer init` записывает выбранный env-файл, а `reviewer check` проверяет его. Порядок поиска:
`REVIEWER_ENV_FILE` → `$XDG_CONFIG_HOME/rag-reviewer/.env` → `./.env`.

Основные группы:

- Voyage: `VOYAGE_API_KEY`;
- хранилища: `PG_DSN`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`;
- VCS: provider token и опциональный API base;
- repo scope: `DEFAULT_REPO`, `REVIEW_BRANCHES` (fallback branch allowlist; домашний per-repo
  слой имеет приоритет — см. [Репозитории и ветки](#репозитории-и-ветки));
- board credentials: provider-specific env из registry.

Публикуемые хостовые порты storage-сервисов Compose заданы переменными, а не литералами:
`PARADEDB_PUBLISH_PORT` (дефолт `5433`), `NEO4J_BOLT_PUBLISH_PORT` (дефолт `7687`) и
`NEO4J_HTTP_PUBLISH_PORT` (дефолт `7474`). Контейнерные порты фиксированы. `reviewer init`
спрашивает их в группе хранилищ и выводит первые два из `PG_DSN` и `NEO4J_URI`, поэтому строка
подключения и публикуемый порт не разъезжаются молча; расхождение на локальном хосте печатает
предупреждение, но не блокирует.

```bash
PARADEDB_PUBLISH_PORT=6543 NEO4J_BOLT_PUBLISH_PORT=7999 \
  docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d
```

`reviewer start` и `reviewer stop` управляют этим Compose-файлом:

```bash
reviewer start   # up -d --wait, ждёт готовности healthcheck ParadeDB и Neo4j
reviewer stop    # останавливает контейнеры; named volumes и построенный индекс сохраняются
```

`reviewer stop` останавливает и веб-админку, если она была поднята через `--profile web`:
без явного выбора профиля docker compose её не видит. Тестовые сервисы (`--profile test`) он не
трогает — их поднимает клон репозитория в своём Compose-проекте. У обоих хранилищ задан
`stop_grace_period: 60s`: дефолтных 10 с JVM Neo4j не хватает на штатное завершение, и store
уходил на восстановление при следующем старте.

Обе работают под явным именем Compose-проекта `rag-reviewer`. Клон этого репозитория поднимает
собственный стек под именем `rag_for_git` — они публикуют одни и те же хостовые порты и держат
разные тома, поэтому одновременно их запускать нельзя. Контрибьюторам внутри клона следует
по-прежнему пользоваться `docker compose up -d`.

`reviewer stop` не удаляет тома никогда: он выполняет `docker compose stop`, у которого флага
`-v` не существует.

На Docker Engine старше 25.0 ключ healthcheck `start_interval` игнорируется, поэтому первая проба
neo4j происходит только после обычного `interval` (300s) — ровно того таймаута `--wait`, что
использует `reviewer start`. На таких движках `reviewer start` может сообщить об ошибке по
таймауту, даже если стек поднялся нормально; апгрейд Docker Engine убирает проблему.

Настраивайте переменными, а не правкой Compose-файла: отредактированный вручную
`~/.config/rag-reviewer/docker-compose.yml` перестаёт совпадать с записанным hash, поэтому
`reviewer update` считает его изменённым пользователем (статус `preserved`) и больше не доставляет
в него новые Compose-описания. Файл со статусом `preserved` перестаёт получать и новые определения
healthcheck, поэтому `reviewer start` для него сводится к ожиданию состояния `running`, а не
реальной готовности.

Credentials остаются на сервере. **Credentials are not returned** board metadata/discovery tools
и не должны попадать в `.review.yml`.

### Владение конфигурацией

| Location | Owner | Stores | Must not store |
|---|---|---|---|
| global `.env` | deployment/operator | secrets, credentials, DSNs, runtime infrastructure и compatibility fallbacks | repository policy |
| home global YAML | OS account, запускающий reviewer | общие non-secret defaults | credentials |
| home per-repo YAML | OS account, запускающий reviewer | `repository.primary_branch`, `repository.index_branches`, operator-owned repo policy | credentials |
| committed `.review.yml` | команда репозитория | team-visible review policy и non-secret task-board metadata | credentials или `repository` |
| git remote / CLI | репозиторий/operator | canonical `owner/name` identity и явные command overrides | persisted secrets |
| Postgres / Neo4j | reviewer runtime | derived indexes, task/review state и code graph | source-of-truth configuration |

#### Один репозиторий

Запустите `reviewer init` из клона, проверьте previews для global `.env` и home per-repo, затем
выполните `reviewer check` и `reviewer config show --repo owner/name`.

#### Второй репозиторий

Запустите `reviewer init --scope repo` из второго клона. Команда создаёт или показывает preview
только home per-repo YAML этого репозитория и не перезаписывает global `.env` или конфиг первого
репозитория.

#### CI / server

Передавайте secrets в global `.env` или process из secret manager. Используйте noninteractive init
только для deterministic preview/write, монтируйте home YAML для service account, а team-owned policy
храните в committed `.review.yml`. Если подходящего git remote нет, передайте `--repo owner/name`.

### VCS credentials

| Provider | Environment | Minimum access | Reviewer reads | Reviewer writes | `reviewer check` |
|---|---|---|---|---|---|
| GitHub | `GITHUB_TOKEN` | fine-grained PAT: Pull requests: Read and write; Contents: Read | PR metadata, files, comments, contents, compare | review comments/summary и PR body backlink | authenticates `/user` identity |
| GitLab | `GITLAB_URL`, `GITLAB_TOKEN` | PAT/project token with `api` scope | MR metadata, changes, notes, repository files, compare | discussions/notes и MR description backlink | authenticates `/api/v4/user` identity |

Health check подтверждает URL/token authentication, но не каждое granular repository permission.
Выбранные repository permissions проверяются реальным review. `reviewer init` показывает тот же
contract перед запросом credentials только выбранного provider.

### Репозитории и ветки

`DEFAULT_REPO` задаёт fallback `owner/name`. Repo-тег резолвится как `--repo` →
`git remote origin` → `DEFAULT_REPO`, и резолв сообщает собственное происхождение: `cli`,
`git:origin` или `env:DEFAULT_REPO`. Индекс, записанный под чужим тегом, обнаруживается только по
странной выдаче поиска, поэтому `reviewer index` **отказывается** писать, если имя подставлено из
`DEFAULT_REPO`, а не выведено из клона, — укажите `--repo owner/name` или почините URL origin.
`reviewer status` остаётся fail-open и вместо отказа показывает происхождение: строку-предупреждение
в текстовом выводе и ключ `repo_source` в `--json`.

Отслеживаемые ветки репозитория резолвятся слоями —
первый источник, где они заданы, выигрывает целиком (без поветочного слияния): домашний per-repo
файл `$XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml` → домашний глобальный `review.yml` →
env-allowlist `REVIEW_BRANCHES` (CSV) → `["main"]`. В любом источнике первая запись — primary,
если `primary_branch` не задан явно. У каждой ветки отдельные чанки `base:<branch>` и graph nodes.
Команда `reviewer config show --repo owner/name` показывает effective-ветки и слой-источник.

```bash
reviewer index /path/to/repo --ref main --repo owner/name
reviewer status /path/to/repo --branch main --json
reviewer search "token verification" --branch main
```

Команда `reviewer config migrate --repo owner/name` копирует env-allowlist `REVIEW_BRANCHES` в
домашний per-repo слой (no-op, если домашний слой уже задаёт ветки); после обновления legacy
unscoped base index один раз запустите `reviewer migrate-branches`.

### Per-repo `.review.yml`

Per-repo policy переопределяет server defaults и читается из target/base branch. Основные поля:

```yaml
paths:
  ignore:
    - generated

summary_cluster_depth: 2
summary_topk_threshold: 20

summary_paths:
  ignore:
    - tests
    - test

context_limits:
  search_codebase:
    floor: 4
    ceiling: 15
  graph:
    hops: 1
  code_section:
    max_files: 12
    max_chunks_per_file: 1
    chars_per_file: 1300
```

`summary_paths.ignore` фильтрует только состав кластеризации сводок подсистем — в отличие от
`paths.ignore`, он не влияет на индексацию и ревью PR. Дефолт `["tests", "test"]`; env-слоя нет
(как у `context_limits`), явный пустой список выключает фильтр.

`context_limits` состоит из четырёх подсекций: `search_codebase` (гибрид + graph-expansion +
Voyage rerank для `/ask`, грунтовки и ревью PR), `search_tasks` (RRF-only отбор задач), `graph`
(глубина обхода от топ-хитов) и `code_section` — файловый бюджет секции `code` контекста задачи
(PRI-256). Единица бюджета `code_section` — файл, а не чанк: секция вмещает не более `max_files`
файлов, из каждого — не более `max_chunks_per_file` чанков по `chars_per_file` символов. Символьный
потолок секции отдельным ключом
не задаётся — он производный: операционный бюджет равен `max_files × max_chunks_per_file ×
chars_per_file`, а страховочный потолок после рендера — `max_files × max_chunks_per_file ×
chars_per_file × 3 // 2`.

### Слоистая политика репозитория

Политика разрешается строго в таком порядке; более поздний источник выигрывает для одинакового
верхнеуровневого ключа:

```text
ENV
  < $XDG_CONFIG_HOME/rag-reviewer/review.yml
  < committed .review.yml at the selected target ref
  < $XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml
```

Если `XDG_CONFIG_HOME` не задан, home-root — `~/.config/rag-reviewer`. Слияние выполняется только
на верхнем уровне: более поздний mapping, list или `null` целиком заменяет прежнее значение;
вложенные mappings не объединяются глубоко. Такая замена называется **shadowing**. Посмотреть
effective policy, источник каждого ключа и перекрытые источники можно так:

```bash
reviewer config show --repo group/service --branch main --json
```

Committed-слой берётся на выбранном ref, поэтому разрешение review/config никогда не читает
незакоммиченный `.review.yml` из worktree.

Читается он **из локального клона, если тот пригоден**, и только иначе — через API хостинга.
`config show` берёт клон из `--path <клон>`, а без него — из текущего каталога; MCP-сервер берёт
путь, записанный командой `reviewer index` (она и так выполняется из клона). Кандидат принимается,
только если это git-репозиторий, remote которого совпадает с целевым repo, — клон **без**
распознаваемого remote тоже принимается, и это ровно тот случай, когда коммиченный слой раньше был
недостижим в принципе. Если ref в клоне не резолвится (ветка не выкачана), чтение уходит в API, а не
объявляет слой пустым. Способ чтения виден в отчёте:

```bash
reviewer config show --repo group/service --branch main --path /srv/clones/service
# committed: local        ← резолв прошёл без единого сетевого вызова
```

В JSON это ключ `committed_source` (`local` / `vcs`); сам путь к клону не печатается.

Чтобы скопировать безопасную committed policy в
repo-specific home-слой, не меняя committed-файл, выполните:

```bash
reviewer config migrate --repo group/service --branch main
```

Миграция неразрушающая: эквивалентный destination даёт no-op, а отличающийся destination
сообщается как конфликт и остаётся без изменений. Home-файлы с credential-подобными ключами
отклоняются как policy layers, а их значения никогда не выводятся; credentials храните только в
server environment. Home-конфигурация принадлежит OS account, запускающему reviewer. На shared
service account она может незаметно влиять на workloads этого account, поэтому team-visible policy
держите в committed `.review.yml` и ограничивайте права на home-конфигурацию service account.

`configure-review` меняет context fields, сохраняя посторонние ключи. По умолчанию он
предлагает per-repo home target, но по явному выбору обновляет committed `.review.yml` для
team-visible policy.

### Доски задач

Выбор board generic и registry-driven. Credentials приходят из server env; `.review.yml` содержит
только несекретные metadata:

```yaml
task_board:
  type: <registered-provider>
  project: PRI
  key_pattern: "[A-Z]+-\\d+"
  url_template: "https://tasks.example/{code}"
  create_target: Backlog
  done_target: Done
  options:
    <provider-option>: <discovered-value>
  sync_filter:
    max_age_days: 180
    include_archived: false
```

Repo block имеет приоритет; явный пустой `task_board:` отключает доску. Если блока нет, сервер
может использовать **non-secret deploy-wide fallback**.
Вызовы используют configured registry credentials, не возвращая их клиенту.

`sync_filter` — generic sibling provider-`options`. По умолчанию `max_age_days` отсутствует (age
limit нет), а `include_archived: true`. Возраст считается по task last-modified с inclusive cutoff:
задача точно на границе остаётся eligible. Unknown age не фильтруется по возрасту. Только при
`include_archived: false` unknown archive сам по себе не исключает строку, а archive warning
появляется только тогда. Age filtering выполняется первым и всё ещё может исключить строку; в этом
случае archive uncertainty не считается и warning не появляется. Archive не равен terminal/done.
Репозитории с одинаковым `task_board.project` используют один task corpus. Retention сам ничего не
удаляет: purge включается явно. Изменение фильтра backfill-ит вновь eligible задачи на следующем
успешном полном sync.

Server-side workflow — **store-first**:

1. `sync_board` перечисляет и нормализует задачи, затем хранит vectors и task graph metadata под
   `tasks:<type>:<board>`.
2. Skills вызывают `get_task(key, project=...)`; связанные задачи, PR и код приходят из task
   context tools.
3. Client models не перечисляют provider напрямую и не передают credentials.

MCP server сейчас предоставляет **42 tools**, включая batch-операцию нативных подзадач.

Legacy aliases остаются как **legacy metadata for older clients** на одно compatibility window:
`TASK_BOARD_API_KEY → YOUGILE_API_KEY` и
`TASK_BOARD_API_BASE → YOUGILE_API_BASE`. Новые deployments используют registry-declared
provider credentials. Актуальные matrix, target discovery, options, setup и rotation описаны в
[docs/board-providers.md](docs/board-providers.md).

### Наблюдаемость и tuning

`reviewer serve` открывает историю ревью и traces через optional web extra. Summary depth, top-k
threshold, graph backend и retrieval ceilings меняют cost/recall; сначала используйте defaults.

Учёт расхода ревью идёт по двум независимым каналам. Клиентский `PreToolUse`-хук плагина
(`plugin/hooks/review_cost.py`) читает транскрипт сессии Claude Code на клиенте и пишет sidecar с
расходом по стадиям; `publish_review` читает его на сервере, взвешивая бакеты токенов (свежий
input, output, cache write, cache read), а не суммируя сырые токены. Пошаговый трейс тул-вызовов
(`review_steps`, страница трейса прогона) сервер пишет сам, независимо от хука. `total_cost` и
разрез по стадиям — взвешенные условные единицы, не доллары.

## Справочник CLI

| Цель | Команды |
|---|---|
| Настройка и integrations | `init`, `install`, `install-skills`, `update` |
| Проверка окружения | `check` |
| Управление локальной инфраструктурой | `start`, `stop` |
| Управление индексом | `index`, `status`, `search`, `migrate-branches`, `gc` |
| Observability UI | `serve` |
| Прямой запуск MCP | `reviewer-mcp` |

Текущие options показывает `reviewer COMMAND --help`. `status` не расходует Voyage tokens;
`search` и индексация расходуют.

## Справочник skills

Примеры ниже используют Claude-синтаксис `/rag-reviewer:...`. В Codex те же namespaced skills
доступны как `$rag-reviewer:...`.

### `review-pr` — полное ревью PR

- **Когда:** найти correctness, security, performance и maintainability проблемы в PR.
- **Вызов:** `/rag-reviewer:review-pr owner/repo#123 --dry-run`.
- **Нужно:** reviewer MCP, VCS access, хранилища и желательно свежий base index/graph.
- **Чтение/запись:** читает PR, код и task context; публикует через `publish_review`, кроме dry-run.
- **Результат:** grounded inline comments и summary; deterministic publish выполняет dedup.

### `solve-task` — от задачи к brief разработки

- **Когда:** начать реализацию по ключу `PRI-220` или текстовому запросу.
- **Вызов:** `/rag-reviewer:solve-task PRI-220`.
- **Нужно:** reviewer MCP; board context опционален, pipeline продолжает board-less.
- **Чтение/запись:** читает task/code context и пишет один brief в `docs/superpowers/briefs/`.
- **Результат:** компактный brief для brainstorming; реализация идёт в следующих skills.
- **Сбор контекста:** один серверный вызов `prepare_task_context` заменяет прежнюю цепочку
  `reviewer status` → `sync_board` → `get_task` → `search_*` — preflight, прогрев доски, сама
  задача, связанные/похожие задачи, релевантные подсистемы и код приходят одним payload'ом.
  Fail-open семантика сохранена: то, что недоступно (устаревший индекс, доска не настроена, пустой
  поиск), отражается по-секционно в `gaps`, а не обрывает скилл. Графовые расширения
  (`get_related_symbols`, `callers`, `implementations`, `family`, …) и `get_pr_diff` остаются
  отдельными вызовами по суждению LLM — они зависят от того, что найдёт brief.
- **Мультизапросный ретрив секции `code`:** секции `code` и `test_exemplars` ищутся **набором**
  подзапросов, а не одним запросом на весь текст задачи. Подзапросы извлекаются детерминированно
  (`reviewer/mcp/subqueries.py`: пункты списков под заголовками «что сделать»/«критерии приёмки»
  плюс пул технических идентификаторов), ограничены числом 20, эмбеддятся одним батчем Voyage,
  каждый гоняется через гибридный поиск, выдачи сливаются по RRF. RRF здесь — **финальный** ранкер:
  ни реранкера, ни cliff-отсечки, потому что cliff считался по скорам реранкера против того же
  многотемного запроса и обрушал выдачу до `floor`. Текст каждого блока обрезается по границе строк,
  чтобы один огромный чанк не выжигал весь бюджет рендера; обрезка идёт по файловому бюджету
  (`CodeSectionLimits.chars_per_file`, PRI-256), а не по отдельной модульной константе — прежняя
  `MAX_BLOCK_CHARS` снята, её роль занял `chars_per_file`. Публичный тул `search_codebase` остаётся
  однозапросным и неизменным, как и `Retriever.search_base`; секция `subsystems` по-прежнему
  получает один запрос.
- **Стартовый опрос:** одна панель `AskUserQuestion` до всех остальных шагов спрашивает три вещи —
  тир модели для брифа (`cheap`/`mid`/`premium`), режим взаимодействия и стратегию исполнения.
  Нет ответа или headless-прогон — применяются дефолты `mid` / `normal` / `subagent`, пайплайн не
  блокируется.
- **Режимы взаимодействия:** `normal` — вопросы брейншторма плюс апрув спеки и плана; `auto` —
  вопросы задаются, апрувы не запрашиваются; `full-auto` — вопросов нет, на каждой развилке
  берётся рекомендованный вариант, апрувов нет. В любом режиме спека и план всё равно пишутся,
  проходят self-review и коммитятся. `full-auto` по-прежнему спрашивает перед `git push`,
  созданием PR и записью в доску.
- **Стратегии исполнения:** `inline` (executing-plans), `subagent` (subagent-driven-development),
  `lite` (`plugin/skills/_profiles/execution-lite.md` — один ревьюер на группу до 3 задач с общими
  файлами, потолок fix-раундов 3, обязательное финальное ревью всей ветки) и `auto` (решается
  после плана по упорядоченной рубрике: рисковые признаки, либо >8 задач, либо >10 файлов →
  `subagent`; ≤3 задач и ≤3 файлов → `inline`; иначе `lite`).
- **Файл прогона:** выбранные режим и стратегия пишутся в `.superpowers/solve-task/<KEY>.md`,
  который git-ignored — и никогда в бриф, спеку или план.

### `ask` — обоснованный Q&A по коду

- **Когда:** узнать, где лежит код или как устроена подсистема.
- **Вызов:** `/rag-reviewer:ask как работает свежесть индекса?`.
- **Нужно:** построенные base index и graph.
- **Чтение/запись:** читает repo context и локальные файлы; не меняет и не ревьюит код.
- **Результат:** русское объяснение с реальными `path:line`.

### `pr-walkthrough` — порядок чтения PR

- **Когда:** провести ревьюера-человека по PR без bug review.
- **Вызов:** `/rag-reviewer:pr-walkthrough owner/repo#123`.
- **Нужно:** reviewer MCP, PR access, base index и graph.
- **Чтение/запись:** читает impact/diffs/callers; постит только по явному запросу.
- **Результат:** centrality-first порядок, per-file summary и grounded impact.

### `performance-review` — только performance

- **Когда:** проверить repeated work, N+1 I/O, asymptotics, batching, caching и memory.
- **Вызов:** `/rag-reviewer:performance-review`.
- **Нужно:** diff/PR или явно выбранный scope; reviewer context fail-open.
- **Чтение/запись:** читает изменения и ближайший контекст; сам не публикует.
- **Результат:** только конкретные performance findings с явными assumptions.

### `maintainability-review` — только maintainability

- **Когда:** проверить complexity, readability, duplication, boundaries и repo conventions.
- **Вызов:** `/rag-reviewer:maintainability-review`.
- **Нужно:** diff/PR или выбранный scope плюс локальные инструкции репозитория.
- **Чтение/запись:** читает изменения и соседние patterns; не меняет behavior.
- **Результат:** сфокусированные simplification findings без посторонних советов.

### `create-task` — создать каноническую задачу

- **Когда:** завести grounded task на настроенной доске.
- **Вызов:** `/rag-reviewer:create-task опиши требуемое изменение`.
- **Нужно:** registered board config, обнаруженные create target/options и credentials.
- **Чтение/запись:** читает код; вызывает `create_task` только после явного подтверждения.
- **Результат:** каноническое тело, key/URL и обновлённый task corpus.

### `decompose-task` — создать нативные дочерние задачи

- **Когда:** разложить существующую board task на grounded и независимо выполнимые native children.
- **Вызов:** `/rag-reviewer:decompose-task PRI-224`.
- **Нужно:** сохранённый parent, настроенная доска, authoritative capability `native_subtasks`, task
  context, похожие задачи и релевантный код из `search_codebase`.
- **Board config:** один раз проверяет repository key `task_board`. Значение null/empty/disabled
  явно отключает board work и не вызывает deploy-wide `get_board_config`. Только отсутствующий
  repository key разрешает один вызов `get_board_config`; mapping фиксирует generic `type`,
  `project` и `options` на весь flow.
- **Preview/confirmation:** показывает provider, parent, idempotency key и полное каноническое тело
  каждого child, затем запрашивает одно явное confirmation всего preview; до него записей нет.
- **Запись/проверка:** отправляет ровно один подтверждённый initial batch. Каждая фактически начатая
  batch-запись проверяется независимо от статуса (`ok`, `partial`, `error` или timeout) до
  объявления результата или предложения recovery.
- **Проверка:** выполняет ровно один project-scoped sync, перечитывает parent через `get_task` и
  graph/context через `get_task_context` даже если child keys не возвращены, затем точечно читает
  каждый возвращённый child key через `get_task`.
- **Recovery:** recovery после partial, timeout или error никогда не запускается автоматически.
  Skill сохраняет и показывает `status`, `category` и `retryable`. После проверки только transport
  timeout/unknown outcome или `retryable=true` допускает новый явный выбор: точный retry или stop;
  `retryable=false`, а `unsupported`, `conflict` и `parent_not_found` останавливают flow без retry.
  Точный retry повторяет те же полные payload, order и idempotency key; не создаёт новый key, не
  редактирует wording и не отправляет только remainder.
- **Результат:** created/attached/unattached/pending children и warnings без догадок.

### `finish-task` — закрыть задачу после PR

- **Когда:** PR готов и board task нужно связать и завершить.
- **Вызов:** `/rag-reviewer:finish-task PRI-220 https://github.com/owner/repo/pull/123`.
- **Нужно:** task key, PR URL, registered board config и обнаруженный done target/options.
- **Чтение/запись:** после подтверждения идемпотентно добавляет PR, обновляет задачу, добавляет
  task backlink в PR body и запускает sync.
- **Результат:** done state и отчёт `already_closed`/`task_link_status` (`added` | `already_present`
  | `failed`) без duplicate links; `task_link_added` сохраняет прежнюю семантику («записали сейчас»).

### `report-bug` — сообщить о дефекте самого reviewer

- **Когда:** MCP-тул reviewer нарушил собственный документированный контракт, шаг скилла
  невыполним доступным набором тулов, сломался заявленный инвариант или в трейсбеке появились
  фреймы reviewer. Проблемы проекта пользователя (окружение, внешние сервисы, права, его
  собственный код) сознательно вне скоупа: канал ценен ровно до тех пор, пока молчит о чужом.
- **Вызов:** `/rag-reviewer:report-bug`.
- **Нужно:** только MCP-сервер; GitHub-токен — лишь для пути публикации.
- **Чтение/запись:** сервер классифицирует симптом, детерминированно анонимизирует на Python
  каждое текстовое поле (фрагменты исходников, абсолютные пути, имена репо/веток/файлов, ключи и
  URL задач, хосты self-hosted, e-mail, токены) и собирает issue для `mimfort/rag_for_git`.
  **Наружу уходит** анонимизированное описание и блок «Окружение» — только форма, без содержимого:
  модели оркестратора и субагентов, режим, CLI и ОС, версии reviewer/плагина/Python и способ
  установки, зарегистрированный тип доски, тип VCS и факт self-hosted (никогда сам хост), бэкенд
  графа, наличие индекса и дрейф числом, целочисленные счётчики кластеров/файлов/находок/задач.
  Точный итоговый текст показывается до отправки, а блок «Окружение» можно урезать построчно или
  исключить целиком — это не мешает публикации.
- **Апрув:** публикация происходит **только** после явного согласия человека и никогда — в
  headless-, cron- и фоновых прогонах; гарантию даёт сервер, а не промпт. Issue создаётся от
  GitHub-аккаунта пользователя, поэтому его ник станет виден в публичном репозитории — скилл
  предупреждает об этом до вопроса. При совпадении с открытой issue добавляется комментарий,
  а не дубль.
- **Результат:** `published` / `commented` со ссылками либо `fallback` с готовым markdown и
  ссылкой-заготовкой для ручной публикации — сбой репорта никогда не ломает сессию.
- **Автотриггер:** хук `PostToolUse` смотрит на результаты reviewer-тулов и детерминированно
  распознаёт две формы — traceback с фреймами `reviewer/*` и значение `status` вне
  документированного набора тула, — поэтому «заметить дефект» не отдано на внимательность модели.
  Штатные сбои проверяются **первыми** и всегда побеждают: недоступные хранилища, отсутствующие
  ключи и токены, лимиты доски, 401/403/404, сетевые таймауты, непостроенный или устаревший индекс
  и неотслеживаемая ветка напоминания не порождают. Нарушения инвариантов (идемпотентность, дедуп,
  счётчики) остаются за моделью: в одном ответе их не видно, а гадать по одному вызову — прямой
  путь к шуму. Напоминание несёт только форму сбоя, срабатывает не чаще раза на симптом за сессию
  и не стоит ничего, когда всё в порядке.
- **Выключатель:** `bug_reports: false` в `.review.yml` репозитория выключает канал и хук для него,
  `REVIEW_BUG_REPORTS=false` — для всего деплоя.

### `sync-codebase` — построить или обновить base index

- **Когда:** создать индекс, обновить stale code или перестроить graph.
- **Вызов:** `/rag-reviewer:sync-codebase --path /srv/repo --ref main`.
- **Нужно:** git clone, `uvx`, reviewer services, Voyage и опциональный SCIP.
- **Чтение/запись:** читает выбранный git ref и пишет branch-scoped vectors/graph nodes.
- **Результат:** incremental index report; ошибки называют отсутствующий prerequisite.

### `sync-tasks` — прогреть vectors и graph задач

- **Когда:** синхронизировать доску перед task search или solve-task.
- **Вызов:** `/rag-reviewer:sync-tasks`.
- **Нужно:** запустите `reviewer init`, настройте provider по `docs/board-providers.md`, затем
  проверьте его через `reviewer check`.
- **Чтение/запись:** вызывает идемпотентный server-side `sync_board` в repo mode с canonical repo и
  tracked branch. Effective policy резолвит сервер; клиент её не реконструирует. Tool читает board
  и не пишет в неё. Policy error не повторяется как unfiltered explicit call.
- **Результат:** `eligible`, `filtered_by_age`, `filtered_archived`, `age_unknown`, `archive_unknown`,
  `filter_applied`, `filter_fingerprint`, `filter_source`, `by_board`, `purge` и `warnings`;
  отсутствие config остаётся board-less/fail-open.

### `summarize-subsystems` — GraphRAG summaries подсистем

- **Когда:** построить architectural prior для Q&A и PR walkthrough.
- **Вызов:** `/rag-reviewer:summarize-subsystems`.
- **Нужно:** свежий base index, code graph, reviewer MCP и подтверждённый cluster depth.
- **Чтение/запись:** читает скелеты только добавленных/изменённых файлов через
  `get_file_skeletons` (вход job'а — скелет, а не исходник), батчами до 15 путей на job, и пишет
  grounded summaries в summary store.
- **Результат:** fresh/pruned summaries с отчётом deferred и orphans.
- **Объём ответа:** перечисление кластеров идёт в сжатом режиме с пагинацией
  (`compact=True`, `offset`/`limit`): метаданные и счётчики `added`/`changed`/`removed`/`moved`,
  без путей и fingerprint'ов — размер растёт по числу кластеров, а не файлов
  (на этом репозитории 10 922 Б в сжатом режиме против 97 530 Б в полном; до PRI-229 полный
  формат весил 106 878 Б). Детализация по кластеру — через `get_subsystem_summary_work`. В полном
  формате `files` перечисляет только неизменённые файлы: пути delta-списков в нём не дублируются,
  полный состав = `files ∪ added_files ∪ changed_files ∪ moved_files`.

### `configure-review` — обновить layered policy и ветки

- **Когда:** настроить tracked branches, ignored paths, retrieval limits, summary clustering или
  board metadata.
- **Вызов:** `/rag-reviewer:configure-review`.
- **Нужно:** git repo; MCP и databases не нужны для baseline analysis.
- **Чтение/запись:** читает tracked Python structure/history и меняет одобренные YAML fields либо в
  `home:repos/<owner>/<name>.yml`, либо в committed `.review.yml`; значения веток всегда пишет в home
  per-repo YAML.
- **Результат:** сохранённые посторонние keys/comments и точная rebuild guidance.

## Эксплуатация, диагностика и ограничения

### Проверка здоровья

Начинайте диагностику с трёх команд:

```bash
reviewer check
reviewer status /path/to/repo --json
docker compose ps
```

`reviewer check` проверяет credentials и подключение сервисов без расхода Voyage quota. `status`
сравнивает indexed SHA с выбранным локальным ref и показывает chunks, graph nodes, сводки
подсистем и commit drift каждой отслеживаемой ветки.

### Свежесть индекса и восстановление

- `drift == 0`: base index соответствует выбранному ref.
- `drift > 0`: запустите `reviewer index /path/to/repo --ref BRANCH`, учитывая стоимость Voyage.
- `drift == null` или zero chunks: у ветки нет пригодной base-записи; постройте её явно.
- Нет рёбер `IMPLEMENTS`: установите SCIP и полностью перестройте graph с SCIP backend.
- Остались orphaned overlays `pr:N` или истёкшие sessions: запустите `reviewer gc`.

Base indexes отслеживают committed refs, а не working-tree edits. В planning/review-фазах читайте
незакоммиченные файлы напрямую с диска.

### Типовые сбои

| Симптом | Вероятная причина | Следующее действие |
|---|---|---|
| `reviewer check` не видит Postgres/Neo4j | Stores не запущены или DSN отличается | Запустите `docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d`, затем повторите `reviewer check` |
| Voyage отвечает 429 | Исчерпан free-tier RPM/TPM | Дождитесь quota window; повторите incremental index, не удаляя существующий |
| PR пропущен | Target branch не отслеживается для этого репозитория (см. `reviewer config show`), или draft policy его исключает | Прочитайте reason `prepare_review`; для намеренной target branch добавьте её в домашний per-repo слой (или fallback `REVIEW_BRANCHES`), а не только в policy |
| `config show` показывает пропущенный слой `.review.yml` и ненулевой код возврата | Коммиченный слой политики не доставлен (нет сети/токена, 404) или не разбирается | Домашние слои всё равно применены — смотрите `category`/`http_status` в выводе; почините remote или коммиченный YAML. Ревью, индексация и миграция остаются громкими и падают. Домашний слой с запрещённым credential-ключом тоже попадает в `skipped` и даёт код возврата `1`, хотя слой всего лишь исключён из резолва |
| Task lookup пуст | Board отключён/не настроен или corpus не прогрет | Проверьте [board setup](docs/board-providers.md), затем запустите `/rag-reviewer:sync-tasks` |
| Q&A не видит новый локальный код | Base index содержит только committed ref | Прочитайте локальный файл или commit/index нужную ветку |
| AI-клиент не видит новые skills | Session открыта до установки | Начните New Chat/new CLI session; в IDE выполните Reload Window |

Вторичный контекст намеренно fail-open: недоступный graph, board, subsystem prior или исторический
PR diff уменьшает контекст и даёт warning, а не порождает выдуманные данные.

### Web admin

Опциональная web UI показывает review runs, findings, traces и агрегированную статистику:

```bash
pip install -e ".[web]"
cd web/frontend && npm install && npm run build && cd ../..
reviewer serve
```

Страница **Quality** показывает динамику онлайн-метрики качества брифа solve-task по задачам:
медиану core-recall (precision показан по задачам линией на графике, отдельной медианы у него нет),
bulk-подвыборку (задачи с `expected_core >= 10`, порог `BULK_CORE_THRESHOLD`) с горизонталью
офлайн-базы для сравнения «до/после», и разбивку промахов по таксономии. Источник данных — таблица
`brief_quality`, которая пополняется при каждом реальном `publish_review` (пишет
`MCPReviewService`, не отдельный процесс). Если бриф задачи не найден или в нём вовсе нет секции
`## Relevant code` — измерение пропускается: точка не появляется на графике вместо нуля или
ошибки. Присутствующая, но пустая секция — не пропуск, а валидное измерение с `predicted = 0`.

В контейнерном сценарии внутренний listen-port отделён от опубликованного loopback-порта. Образ
собирается один раз, оба порта выбираются при запуске (`database` замените на доступный из
контейнера Postgres host):

```bash
docker build -f web/Dockerfile -t rag-reviewer-web .
docker run --rm \
  --env PG_DSN=postgresql://reviewer:reviewer@database:5432/reviewer \
  --env REVIEWER_WEB_PORT=8080 \
  --publish 127.0.0.1:18000:8080 \
  rag-reviewer-web
```

Compose-сервис включается явно, поэтому обычный `docker compose up` по-прежнему запускает только
инфраструктуру:

```bash
docker compose --profile web up -d web
REVIEWER_WEB_PORT=8080 REVIEWER_WEB_PUBLISH_PORT=18000 \
  docker compose --profile web up -d web
```

Без переопределений внутренний и опубликованный порты равны `8000`.

Перед доступом не только с localhost задайте `WEB_ADMIN_USER` и `WEB_ADMIN_PASSWORD`. Ошибки store
и API сообщаются явно; там, где это безопасно, процесс сохраняет fail-soft startup.

### Безопасность

- Voyage, VCS, board, database и web-admin credentials хранятся в server env, а не `.review.yml`.
- Выдавайте VCS tokens минимально нужные права; publishing и `finish-task` выполняют внешние writes.
- Проверяйте confirmation gate перед comments, board tasks, status transitions и изменением PR body.
- Сохранённые копии остаются в настроенных databases, но code chunks и search text отправляются
  в Voyage; PR diff и retrieved context AI-клиент также передаёт своему AI model provider.
- Вызовы внешних providers требуют сеть; обычные unit tests её не используют.

### Известные ограничения

- Поддерживаемый язык анализа — Python; наиболее точный graph даёт SCIP.
- Без SCIP tree-sitter строит полезный, но name-based `CALLS` graph плюс class-level `IMPLEMENTS`
  из синтаксиса; метод-уровневый override `IMPLEMENTS` остаётся только за SCIP.
- GitHub принимает inline-комментарии только на commentable diff lines; остальные findings идут в
  summary.
- Полная индексация может упереться в free-tier limits Voyage; updates incremental и повторно
  используют embeddings.
- Base index branch-scoped и не видит незакоммиченные working-tree changes.
- OAuth loopback не поддерживается в headless/SSH integrations; используйте документированные
  PAT/API-key credentials.
- Board опционален. Без provider config task-aware skills продолжают board-less, а code retrieval
  не блокируется.

## Разработка

Создайте изолированное окружение и установите dev dependencies:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
git config core.hooksPath .githooks
```

Последняя команда включает версионируемый хук `pre-commit`: он гоняет `ruff check` по staged
`.py` и не даёт закоммитить, пока они не чистые. Git не умеет включать хуки сам, поэтому каждый
клон подключает их один раз. Разовый обход — `git commit --no-verify`.

Unit tests запрещают external/localhost sockets и по умолчанию исключают integration:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Integration services запускаются в изолированном test profile:

```bash
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration
docker compose --profile test rm -sfv paradedb-test neo4j-test
```

Не используйте `docker compose --profile test down -v`: test и development services входят в один
Compose project, поэтому команда может удалить development volumes.

### Метрики этапа solve-task (офлайн)

Офлайн-харнесс считает цену этапа и качество ретрива по накопленному корпусу
брифов (`docs/superpowers/briefs/`), хранит историю срезов и умеет сравнивать
прогоны. Ретроспективные команды не требуют Postgres, Neo4j и сети — только
локальный git; исключение — `replay`, которому нужен живой ретрив.

```bash
python -m eval.solve_task_metrics snapshot            # пересчитать метрики, сохранить срез, обновить отчёт
python -m eval.solve_task_metrics stats --last 10     # тренд последних срезов таблицей, без пересчёта
python -m eval.solve_task_metrics compare --back 1    # дельты последнего среза против среза N шагов назад
python -m eval.solve_task_metrics forecast            # прогноз core-recall с разбросом
python -m eval.solve_task_metrics replay              # прогнать ретрив по корпусу заново (baseline)
python -m eval.solve_task_metrics replay --variant limits --set search_codebase.ceiling=25 --baseline last   # A/B против сохранённого снимка
```

**`replay`** заново собирает кандидатов вызовом продакшн-ретрива по тексту задачи
из стора (а не по тексту брифа) и сравнивает варианты конфигурации: отчёт
`eval/replay_report.md` показывает дельту и по агрегату, и по каждой задаче,
снимки — в `eval/replay_history.jsonl`. Требует Postgres, Neo4j, Voyage и
построенный base-индекс. Линия `replay` **несравнима** с линией `snapshot`:
snapshot считает пути, отобранные LLM, а replay — всю выдачу ретрива.

Цена считается во взвешенных input-эквивалентах (`output ×5`, `cache-write ×1.25`,
`cache-read ×0.1`); сырая сумма токенов показывается только справочно — она не
пропорциональна стоимости. Качество — core-recall на суженном знаменателе;
задачи без файлов ядра в diff'е учитываются как «нет точки измерения», а не как
нулевой recall. Срезы лежат в `eval/solve_task_metrics_history.jsonl`, отчёт —
в `eval/solve_task_metrics_report.md`.

| Область | Ответственность |
|---|---|
| `reviewer/index/`, `reviewer/retrieval/` | chunking, vectors/BM25, freshness, reranking |
| `reviewer/graph/` | построение tree-sitter/SCIP graph и доступ к Neo4j |
| `reviewer/mcp/`, `reviewer/services/` | PR sessions, tools, prepare/publish orchestration |
| `reviewer/tasks/` | task store, graph, sync и registered board providers |
| `plugin/skills/` | user-facing agent workflows |
| `tests/` | offline unit и isolated integration contracts |

Перед изменением архитектуры и инвариантов прочитайте [CLAUDE.md](CLAUDE.md). Сохраняйте русские
комментарии, docstrings и CLI messages; используйте Conventional Commits без self-attribution.

## Лицензия

[MIT](LICENSE) © rag_for_git contributors.

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
| Развернуть один reviewer для команды | [Развёртывание для команды](#развёртывание-для-команды) |

## Попробовать reviewer

Понадобятся Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), Docker, API-ключ Voyage и токен
системы контроля версий (VCS), если reviewer должен читать или публиковать ревью. Хранилища
работают локально; запросы эмбеддингов и реранкинга отправляются в Voyage.

1. Установите launcher, скачайте Compose-файл репозитория, поднимите хранилища и настройте
   reviewer:

   ```bash
   uv tool install --from rag-reviewer reviewer
   curl -O https://raw.githubusercontent.com/mimfort/rag_for_git/main/docker-compose.yml
   docker compose up -d
   reviewer init
   ```

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
   установке сначала нужен index. Сейчас check требует `GITHUB_TOKEN` даже для GitLab-only
   конфигурации; до снятия этого ограничения проверяйте `GITLAB_TOKEN` индексацией или подготовкой
   GitLab MR. Status payload должен показать indexed SHA и `drift == 0`. Полная индексация
   отправляет чанки кода в Voyage и может быть медленной на free tier. Без base index ревью видит
   только дифф и временный индекс изменённых файлов (overlay), поэтому получает более узкий
   контекст репозитория.

4. Откройте новую сессию клиента и запустите первое ревью:

   ```text
   # Claude Code
   /rag-reviewer:reviewer_review-pr owner/repo#123 --dry-run

   # Codex
   $rag-reviewer:reviewer_review-pr owner/repo#123
   ```

   Синтаксис вызова зависит от клиента. Dry run возвращает обоснованные findings без публикации;
   обычный запуск публикует через `publish_review` и требует VCS write credentials.

Для временного запуска launcher без постоянной установки:

```bash
uvx --from rag-reviewer@latest reviewer
```

## Развёртывание для команды

В team deployment нет одного центрального MCP daemon. Каждый установленный AI-клиент запускает
свой stdio-процесс `reviewer-mcp`; эти процессы совместно используют PostgreSQL/ParadeDB, Neo4j,
Voyage и одинаковую provider configuration. Credentials провайдеров остаются в окружении каждого
процесса. MCP requests передают repo, branch, project и `provider_options`, а tool results
возвращают выбранный контекст кода AI-клиенту.

1. **Поднимите хранилища и настройте секреты.**

   ```bash
   curl -O https://raw.githubusercontent.com/mimfort/rag_for_git/main/docker-compose.yml
   docker compose up -d
   reviewer init
   ```

2. **Выберите repo и branch scope.** Задайте `DEFAULT_REPO` и упорядоченный allowlist
   `REVIEW_BRANCHES` в server env. Per-repo policy, игнорируемые пути, context limits и несекретные
   board metadata храните в `.review.yml`.

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

   Выполните установку на каждой рабочей станции, где нужен reviewer. `--all` настраивает
   поддерживаемые клиенты на этой машине, а `--dry-run` показывает планируемые записи
   конфигурации. Затем откройте новую chat- или CLI-сессию; интеграциям IDE также может
   понадобиться Reload Window.

5. **Добавьте опциональный board-контекст.** Выберите зарегистрированный provider в
   `.review.yml`, оставьте credentials в server-side env и проверьте project scope через
   `reviewer check`. См. [раздел о досках](#доски-задач) и
   [справочник провайдеров](docs/board-providers.md).

## Основные сценарии

Reviewer workflows поставляются как namespaced skills. Каждый skill определяет собственные
границы чтения/записи и confirmation gates; MCP server выполняет операции с хранилищами, графом,
VCS и доской.

### Ревью pull request

Используйте `reviewer_review-pr` для поиска багов. Skill готовит PR-сессию, подтягивает код и
графовый контекст, анализирует изменённые файлы, проверяет findings и публикует только обоснованный
результат. Для проверки deployment начните с `--dry-run`. Inline-комментарии возможны только на
commentable строках диффа; off-diff findings уходят в сводку.

### Решение задачи

`reviewer_solve-task` превращает задачу доски или текстовый запрос в сохраняемый brief до начала
разработки. Skill проверяет свежесть индекса, прогревает task context, собирает related work и код,
затем передаёт brief в brainstorming. Саму задачу этот skill не реализует.

### Обоснованный вопрос по кодовой базе

`reviewer_ask` подходит для онбординга и Q&A. Ответы ссылаются на реальные `path:line` из base
index и code graph. Skill читает и объясняет, но не ревьюит PR и не изменяет код.

### Гид по PR для ревьюера

`reviewer_pr-walkthrough` строит порядок чтения: с чего начать, что меняет каждый файл и на каких
callers влияет правка. Это отдельный сценарий, а не bug review.

### Сфокусированное ревью

`reviewer_performance-review` ищет повторный I/O, N+1, плохую асимптотику, проблемы batching,
caching и памяти. `reviewer_maintainability-review` ищет сложность, дублирование, проблемы
читаемости, границ и соглашений репозитория. Оба остаются в явно выбранном измерении.

### Создание и завершение задач доски

`reviewer_create-task` собирает каноническое тело и пишет только после подтверждения.
`reviewer_finish-task` добавляет PR, переводит задачу в обнаруженный done target, добавляет ссылку
на задачу в PR body и повторно синхронизирует корпус—тоже только после подтверждения.

### Грунтовка reviewer в фазах план/ревью (опционально)

Грунтовка позволяет planning/review-фазам использовать session-less reviewer tools при свежем
base index.

> **Грунтовка reviewer (план/ревью, опционально, fail-open).** Сначала запустите
> `reviewer status /path/to/repo --branch main --json`. При `drift == 0` используйте
> `search_codebase` для кросс-файловых фактов, а `callers`, `related_symbols`, `definition` или
> `implementations` — только для центральных символов. Base index не видит незакоммиченные правки,
> поэтому изменённые файлы читайте с диска. Если reviewer или индекс недоступен, откатитесь к
> локальным search/read tools и не блокируйте работу.

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
  SCIP, внешний type-aware индексатор кода, даёт `CALLS` и `IMPLEMENTS`; режим `auto`
  откатывается к tree-sitter `CALLS`, когда SCIP недоступен.
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
uv tool install --from rag-reviewer reviewer
reviewer update
```

Временный/latest запуск:

```bash
uvx --from rag-reviewer@latest reviewer --help
```

`reviewer update` сначала проверяет версию и только потом меняет постоянную uv tool installation.
`reviewer install CLIENT --dry-run` показывает записи integration до изменения файлов.

### AI-клиенты

Список клиентов показывает `reviewer install --list`; можно установить один или все обнаруженные:

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

После установки начните новую chat/CLI session; в IDE также выполните Reload Window.

### Сервисы и credentials

`reviewer init` записывает выбранный env-файл, а `reviewer check` проверяет его. Порядок поиска:
`REVIEWER_ENV_FILE` → `$XDG_CONFIG_HOME/rag-reviewer/.env` → `./.env`.

Основные группы:

- Voyage: `VOYAGE_API_KEY`;
- хранилища: `PG_DSN`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`;
- VCS: provider token и опциональный API base;
- repo scope: `DEFAULT_REPO`, `REVIEW_BRANCHES`;
- board credentials: provider-specific env из registry.

Credentials остаются на сервере. **Credentials are not returned** board metadata/discovery tools
и не должны попадать в `.review.yml`.

### Репозитории и ветки

`DEFAULT_REPO` задаёт fallback `owner/name`. `REVIEW_BRANCHES` — упорядоченный CSV allowlist;
первая запись является primary. У каждой ветки отдельные чанки `base:<branch>` и graph nodes.

```bash
reviewer index /path/to/repo --ref main --repo owner/name
reviewer status /path/to/repo --branch main --json
reviewer search "token verification" --branch main
```

После обновления legacy unscoped base index один раз запустите `reviewer migrate-branches`.

### Per-repo `.review.yml`

Per-repo policy переопределяет server defaults и читается из target/base branch. Основные поля:

```yaml
paths:
  ignore:
    - generated

summary_cluster_depth: 2
summary_topk_threshold: 20

context_limits:
  search_codebase:
    floor: 4
    ceiling: 15
  graph:
    hops: 1
```

`reviewer_configure-review` меняет context fields, сохраняя посторонние ключи.

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
```

Repo block имеет приоритет; явный пустой `task_board:` отключает доску. Если блока нет, сервер
может использовать **non-secret deploy-wide fallback**.
Вызовы используют configured registry credentials, не возвращая их клиенту.

Server-side workflow — **store-first**:

1. `sync_board` перечисляет и нормализует задачи, затем хранит vectors и task graph metadata под
   `tasks:<type>:<board>`.
2. Skills вызывают `get_task(key, project=...)`; связанные задачи, PR и код приходят из task
   context tools.
3. Client models не перечисляют provider напрямую и не передают credentials.

Legacy aliases остаются как **legacy metadata for older clients** на одно compatibility window:
`TASK_BOARD_API_KEY → YOUGILE_API_KEY` и
`TASK_BOARD_API_BASE → YOUGILE_API_BASE`. Новые deployments используют registry-declared
provider credentials. Актуальные matrix, target discovery, options, setup и rotation описаны в
[docs/board-providers.md](docs/board-providers.md).

### Наблюдаемость и tuning

`reviewer serve` открывает историю ревью и traces через optional web extra. Summary depth, top-k
threshold, graph backend и retrieval ceilings меняют cost/recall; сначала используйте defaults.

## Справочник CLI

| Цель | Команды |
|---|---|
| Настройка и integrations | `init`, `install`, `install-skills`, `update` |
| Проверка окружения | `check` |
| Управление индексом | `index`, `status`, `search`, `migrate-branches`, `gc` |
| Observability UI | `serve` |
| Прямой запуск MCP | `reviewer-mcp` |

Текущие options показывает `reviewer COMMAND --help`. `status` не расходует Voyage tokens;
`search` и индексация расходуют.

## Справочник skills

Примеры ниже используют Claude-синтаксис `/rag-reviewer:...`. В Codex те же namespaced skills
доступны как `$rag-reviewer:...`.

### `reviewer_review-pr` — полное ревью PR

- **Когда:** найти correctness, security, performance и maintainability проблемы в PR.
- **Вызов:** `/rag-reviewer:reviewer_review-pr owner/repo#123 --dry-run`.
- **Нужно:** reviewer MCP, VCS access, хранилища и желательно свежий base index/graph.
- **Чтение/запись:** читает PR, код и task context; публикует через `publish_review`, кроме dry-run.
- **Результат:** grounded inline comments и summary; deterministic publish выполняет dedup.

### `reviewer_solve-task` — от задачи к brief разработки

- **Когда:** начать реализацию по ключу `PRI-220` или текстовому запросу.
- **Вызов:** `/rag-reviewer:reviewer_solve-task PRI-220`.
- **Нужно:** reviewer MCP; board context опционален, pipeline продолжает board-less.
- **Чтение/запись:** читает task/code context и пишет один brief в `docs/superpowers/briefs/`.
- **Результат:** компактный brief для brainstorming; реализация идёт в следующих skills.

### `reviewer_ask` — обоснованный Q&A по коду

- **Когда:** узнать, где лежит код или как устроена подсистема.
- **Вызов:** `/rag-reviewer:reviewer_ask как работает свежесть индекса?`.
- **Нужно:** построенные base index и graph.
- **Чтение/запись:** читает repo context и локальные файлы; не меняет и не ревьюит код.
- **Результат:** русское объяснение с реальными `path:line`.

### `reviewer_pr-walkthrough` — порядок чтения PR

- **Когда:** провести ревьюера-человека по PR без bug review.
- **Вызов:** `/rag-reviewer:reviewer_pr-walkthrough owner/repo#123`.
- **Нужно:** reviewer MCP, PR access, base index и graph.
- **Чтение/запись:** читает impact/diffs/callers; постит только по явному запросу.
- **Результат:** centrality-first порядок, per-file summary и grounded impact.

### `reviewer_performance-review` — только performance

- **Когда:** проверить repeated work, N+1 I/O, asymptotics, batching, caching и memory.
- **Вызов:** `/rag-reviewer:reviewer_performance-review`.
- **Нужно:** diff/PR или явно выбранный scope; reviewer context fail-open.
- **Чтение/запись:** читает изменения и ближайший контекст; сам не публикует.
- **Результат:** только конкретные performance findings с явными assumptions.

### `reviewer_maintainability-review` — только maintainability

- **Когда:** проверить complexity, readability, duplication, boundaries и repo conventions.
- **Вызов:** `/rag-reviewer:reviewer_maintainability-review`.
- **Нужно:** diff/PR или выбранный scope плюс локальные инструкции репозитория.
- **Чтение/запись:** читает изменения и соседние patterns; не меняет behavior.
- **Результат:** сфокусированные simplification findings без посторонних советов.

### `reviewer_create-task` — создать каноническую задачу

- **Когда:** завести grounded task на настроенной доске.
- **Вызов:** `/rag-reviewer:reviewer_create-task опиши требуемое изменение`.
- **Нужно:** registered board config, обнаруженные create target/options и credentials.
- **Чтение/запись:** читает код; вызывает `create_task` только после явного подтверждения.
- **Результат:** каноническое тело, key/URL и обновлённый task corpus.

### `reviewer_finish-task` — закрыть задачу после PR

- **Когда:** PR готов и board task нужно связать и завершить.
- **Вызов:** `/rag-reviewer:reviewer_finish-task PRI-220 https://github.com/owner/repo/pull/123`.
- **Нужно:** task key, PR URL, registered board config и обнаруженный done target/options.
- **Чтение/запись:** после подтверждения идемпотентно добавляет PR, обновляет задачу, добавляет
  task backlink в PR body и запускает sync.
- **Результат:** done state и отчёт `already_closed`/`task_link_added` без duplicate links.

### `reviewer_sync-codebase` — построить или обновить base index

- **Когда:** создать индекс, обновить stale code или перестроить graph.
- **Вызов:** `/rag-reviewer:reviewer_sync-codebase --path /srv/repo --ref main`.
- **Нужно:** git clone, `uvx`, reviewer services, Voyage и опциональный SCIP.
- **Чтение/запись:** читает выбранный git ref и пишет branch-scoped vectors/graph nodes.
- **Результат:** incremental index report; ошибки называют отсутствующий prerequisite.

### `reviewer_sync-tasks` — прогреть vectors и graph задач

- **Когда:** синхронизировать доску перед task search или solve-task.
- **Вызов:** `/rag-reviewer:reviewer_sync-tasks`.
- **Нужно:** запустите `reviewer init`, настройте provider по `docs/board-providers.md`, затем
  проверьте его через `reviewer check`.
- **Чтение/запись:** вызывает идемпотентный server-side `sync_board`; читает board и не пишет в неё.
- **Результат:** компактные counts/warnings; отсутствие config остаётся board-less/fail-open.

### `reviewer_summarize-subsystems` — GraphRAG summaries подсистем

- **Когда:** построить architectural prior для Q&A и PR walkthrough.
- **Вызов:** `/rag-reviewer:reviewer_summarize-subsystems`.
- **Нужно:** свежий base index, code graph, reviewer MCP и подтверждённый cluster depth.
- **Чтение/запись:** читает symbols кластеров и пишет grounded summaries в summary store.
- **Результат:** fresh/pruned summaries с отчётом deferred и orphans.

### `reviewer_configure-review` — обновить `.review.yml`

- **Когда:** настроить ignored paths, retrieval limits, summary clustering или board metadata.
- **Вызов:** `/rag-reviewer:reviewer_configure-review`.
- **Нужно:** git repo; MCP и databases не нужны для baseline analysis.
- **Чтение/запись:** читает tracked Python structure/history и меняет только одобренные YAML fields.
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
сравнивает indexed SHA с выбранным локальным ref и показывает chunks, graph nodes и commit drift
каждой отслеживаемой ветки.

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
| `reviewer check` не видит Postgres/Neo4j | Stores не запущены или DSN отличается | Запустите `docker compose up -d`, затем повторите `reviewer check` |
| Voyage отвечает 429 | Исчерпан free-tier RPM/TPM | Дождитесь quota window; повторите incremental index, не удаляя существующий |
| PR пропущен | Target branch вне `REVIEW_BRANCHES` или draft policy его исключает | Прочитайте reason `prepare_review`; меняйте policy только для намеренной target branch |
| Task lookup пуст | Board отключён/не настроен или corpus не прогрет | Проверьте [board setup](docs/board-providers.md), затем запустите `reviewer_sync-tasks` |
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
- Без SCIP tree-sitter строит полезный, но name-based `CALLS` graph без точного `IMPLEMENTS`.
- GitHub принимает inline-комментарии только на commentable diff lines; остальные findings идут в
  summary.
- Полная индексация может упереться в free-tier limits Voyage; updates incremental и повторно
  используют embeddings.
- Base index branch-scoped и не видит незакоммиченные working-tree changes.
- OAuth loopback не поддерживается в headless/SSH integrations; используйте документированные
  PAT/API-key credentials.
- Сейчас `reviewer check` проверяет `GITHUB_TOKEN` и GitHub API даже в GitLab-only deployment;
  проверяйте `GITLAB_TOKEN` индексацией или подготовкой GitLab MR.
- Board опционален. Без provider config task-aware skills продолжают board-less, а code retrieval
  не блокируется.

## Разработка

Создайте изолированное окружение и установите dev dependencies:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

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

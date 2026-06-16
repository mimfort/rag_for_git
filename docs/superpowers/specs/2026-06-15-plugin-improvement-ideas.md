# Идеи по улучшению плагина rag-reviewer

- **Дата:** 2026-06-15
- **Статус:** черновик идей, ожидает выбора направления
- **Автор:** brainstorming с superpowers

## 1. Контекст

`rag-reviewer` — это MCP-сервер + набор skills для AI-ассистентов (Claude Code, Cursor, Kimi, Codex, Gemini, Mimo, OpenCode, Copilot). Основной сценарий: пользователь вызывает skill `/rag-reviewer:reviewer_review-pr`, плагин через MCP готовит PR, запускает сабагентов анализа с доступом к RAG и графу кода, затем публикует inline-комментарии и сводку на GitHub.

### Что уже реализовано (база отсчёта)

- Гибридный RAG (Postgres/ParadeDB + Voyage) + граф кода (Neo4j, SCIP/tree-sitter).
- Мультирепо и мультибранчевой base-индекс (`base:<branch>` + overlay `pr:N`).
- Task-board интеграция (Yougile/Jira), skills `sync-tasks` и `solve-task`.
- Веб-админка наблюдаемости (runs, findings, stats, trace).
- Улучшения качества и стоимости анализа: `code_quote`-грунтовка строк, verify по умолчанию агентный, графово-смежный PR-bundle, прицельный tool-loop.
- Автоустановка MCP и skills через `reviewer install` / `reviewer install-skills`.
- Детерминированный хвост `publish_review`: gate, grounding, dedup, fingerprint-идемпотентность.

### Текущие ограничения, которые мотивируют идеи

- End-to-end ревью доступно только через skill в AI-ассистенте; сам `reviewer` CLI не умеет анализировать PR целиком.
- Ревью не запускается автоматически: нет GitHub App, webhook или GitHub Actions-интеграции.
- MCP-сессия живёт только в процессе `reviewer-mcp`; перезапуск сервера между `prepare_review` и `publish_review` теряет состояние.
- Cost/usage-трекинг для Claude Code-пути неполный: LLM-вызовы происходят в skill, а не в MCP-сервисе.
- Веб-админка read-only: нельзя пере-запустить ревью или триггернуть индексацию из UI.
- Завязка на Voyage (`voyage-code-3` + `rerank-2.5`) и GitHub; другие провайдеры/VCS — только абстракции.
- Большие PR ограничены GitHub compare API (300 файлов) и пагинацией списка файлов.

## 2. Цель

Предложить конкретные, приоритизированные направления улучшения плагина, которые:

1. Повышают автономность (`reviewer` работает без обязательного AI-ассистента).
2. Снижают ручной труд (автотриггеры, actions в админке).
3. Улучшают надёжность и observability (persistent sessions, полный cost tracking).
4. Расширяют охват (другие VCS/embedding-провайдеры, языки).

## 3. Не-цели

- Здесь не делается полная имплементация; цель — выбрать направление и зафиксировать дизайн.
- Не переписываем существующую архитектуру ради рефакторинга; все идеи должны встраиваться в текущие абстракции (`VCSProvider`, `GraphBackend`, `Embedder`, `Retriever`, `MCPReviewService`).

## 4. Три подхода

| # | Подход | Суть | Сложность | Ценность | Риск |
|---|---|---|---|---|---|
| A | **Автономный CLI-ревью** | Добавить `reviewer review owner/repo#N`, который сам делает prepare → analyze → publish | Средняя | Высокая | Дублирование логики skill-анализа |
| B | **Автоматические триггеры** | GitHub App / GitHub Actions / webhook для авто-ревью при открытии/апдейте PR | Высокая | Очень высокая | Инфраструктура, секреты, rate limits |
| C | **Углубление AI-ассистентского опыта** | Persistent MCP-сессии, cost tracking, actions в админке, новые dimension-skills | Низкая–средняя | Средняя–высокая | Много мелких изменений |

**Рекомендация:** реализовывать в порядке **C → A → B**.

- **C** даёт быстрые победы в текущем UX и готовит инфраструктуру (cost tracking, persistent sessions) для A.
- **A** делает `reviewer` самодостаточным и открывает путь к CI/CD.
- **B** — логичное завершение: когда есть A, обернуть его в GitHub Action/App тривиально.

## 5. Детальный разбор идей

### Подход A. Автономный CLI-ревью

#### A1. Команда `reviewer review owner/repo#N`

**Проблема:** сейчас end-to-end ревью возможно только в Claude Code/Codex через skill. Для CI, cron или headless-запуска нужна standalone-команда.

**Решение:** новая подкоманда CLI, которая полностью повторяет пайплайн skill:

```bash
reviewer review owner/repo#42 --dry-run
reviewer review owner/repo#42 --model claude-3-7-sonnet
reviewer review owner/repo#42 --dimension correctness,security
```

**Компоненты:**
- `reviewer/entrypoints/cli.py`: новая команда `review`.
- `reviewer/agent/orchestrator.py`: программный аналог skill-логики (fan-out сабагентов по файлам, verify, synthesize). Сейчас эта логика в `plugin/skills/review-pr/SKILL.md` и частично в MCP-инструментах; нужно вынести reusable код в библиотеку.
- `reviewer/llm/`: добавить LLM-провайдеров (Anthropic, OpenRouter, OpenAI) с единым интерфейсом, чтобы MCP-сервер и CLI использовали один код.
- `reviewer/agent/usage.py`: полный захват токенов/стоимости на всех этапах.

**Интеграция:**
- Использует тот же `MCPReviewService.prepare_review` / `publish_review`.
- Анализ может идти либо через внутренний LangGraph-анализатор, либо через вызов Claude Code API напрямую.

**Риски:**
- Дублирование логики skill → нужно выделить shared orchestrator.
- Claude Code subagents дают хороший результат благодаря tool loop внутри Claude Code; в standalone нужно либо использовать Claude API с function calling, либо принять компромисс в качестве.

**Оценка:** 2–3 недели.

#### A2. Поддержка dimension-обзоров в CLI

**Проблема:** уже есть skills `performance-review` и `maintainability-review`, но они работают только через skill-вызов.

**Решение:** флаг `--dimension correctness,security,performance,maintainability`, который фильтрует категории и подключает специализированные промпты.

**Компоненты:**
- Параметризация `ReviewPolicy` и промптов анализа.
- Переиспользование `references/analyze-prompt.md` и dimension-specific playbooks.

**Оценка:** 3–5 дней поверх A1.

### Подход B. Автоматические триггеры

#### B1. GitHub Actions reusable workflow

**Проблема:** ревью требует ручного вызова skill.

**Решение:** опубликовать reusable workflow в GitHub Marketplace:

```yaml
uses: mimfort/rag-reviewer/.github/workflows/review.yml@main
with:
  pr_number: ${{ github.event.pull_request.number }}
secrets:
  VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

**Компоненты:**
- `.github/workflows/review.yml` в этом репо.
- Документация по подключению.
- При наличии A1 workflow просто вызывает `reviewer review`.

**Риски:**
- Время прогона может быть долгим (минуты); workflow должен быть async или иметь generous timeout.
- Секреты: пользователь должен хранить `VOYAGE_API_KEY` и `GITHUB_TOKEN` в repo/org secrets.

**Оценка:** 1 неделя поверх A1.

#### B2. GitHub App с вебхуками

**Решение:** отдельный сервис (может быть тот же `reviewer serve` в расширенном режиме), который слушает webhook `pull_request.opened`/`synchronize`, проверяет `REVIEW_BRANCHES`, вызывает `reviewer review`.

**Компоненты:**
- `reviewer/web/webhooks.py`: роуты GitHub App webhooks, верификация подписи.
- Настройка App: permissions (Pull requests r/w, Contents r), webhook URL, private key.
- Job queue: можно использовать Postgres или Redis для надёжной обработки.

**Риски:**
- Хостинг, масштабирование, retry-логика, безопасность вебхуков.
- Большие команды могут упереться в лимиты Voyage/GitHub.

**Оценка:** 3–4 недели.

### Подход C. Углубление AI-ассистентского опыта

#### C1. Persistent MCP-сессии

**Проблема:** `prepare_review` хранит `PreparedReview` в памяти процесса `reviewer-mcp`. Перезапуск клиента/сервера между prepare и publish теряет сессию.

**Решение:** опциональное persisted session store (Postgres/Redis). `prepare_review` возвращает `session_id`; `publish_review` принимает `session_id` и восстанавливает состояние.

**Компоненты:**
- `reviewer/mcp/session_store.py`: `SessionStore` интерфейс + `PostgresSessionStore`/`MemorySessionStore`.
- `MCPReviewService`: сохранять `PreparedReview` + `ToolContext` после prepare; восстанавливать при publish.
- TTL и cleanup для эфемерных сессий.

**Риски:**
- Увеличение объёма данных в Postgres; нужны cleanup-джобы.
- Секретность: сессия может содержать VCS-токены/код → шифрование at rest.

**Оценка:** 1–1.5 недели.

#### C2. Полный cost tracking для Claude Code-пути

**Проблема:** в `review_runs` поля `usage` и `total_cost` пустые при использовании Claude Code skill, потому что LLM-вызовы происходят в skill, а не в MCP-сервисе.

**Решение:** skill после завершения вызывает новый MCP-tool `report_usage(run_id, usage)` или пишет usage в виде structured markdown, который `publish_review` парсит.

**Компоненты:**
- Новый MCP tool: `report_usage(run_id: int, usage: dict)`.
- `plugin/skills/review-pr/SKILL.md`: skill собирает usage из subagents и вызывает `report_usage` перед `publish_review`.
- `reviewer/web/history.py`: обновление `review_runs.usage` / `total_cost`.

**Риски:**
- Skill должен доверять клиенту; злоупышленник может подделать usage → это только observability, не биллинг.

**Оценка:** 3–5 дней.

#### C3. Actions в веб-админке

**Проблема:** админка только показывает историю; нельзя пере-запустить ревью или запустить индексацию.

**Решение:** добавить в API/FastAPI endpoints:
- `POST /api/runs/{id}/rerun` — повторить ревью для того же PR.
- `POST /api/index` — триггер `reviewer index` для выбранного repo/branch.
- `POST /api/review` — запустить ревью для указанного PR.

**Компоненты:**
- Бэкенд: async background jobs (можно через `BackgroundTask` или очередь).
- Фронтенд: кнопки на Dashboard и RunDetail.
- Auth: обязательный Basic Auth при включении actions (чтобы не открывать запись/запуск всем).

**Риски:**
- DoS: нужен rate limiting и авторизация.
- Долгие задачи → нужна очередь и статус job-а.

**Оценка:** 1.5–2 недели.

#### C4. Cache для embeddings и rerank

**Проблема:** повторные одинаковые запросы к `search_code`/`search_codebase` каждый раз эмбеддятся и реранжируются; для больших PR это лишние вызовы Voyage.

**Решение:** LRU/Redis cache для `(query_hash) → embedding` и `(query_hash, candidate_hashes) → rerank-result`.

**Компоненты:**
- `reviewer/index/embed_cache.py`: `EmbeddingCache` interface + `LRUEmbeddingCache` / `RedisEmbeddingCache`.
- Интеграция в `Embedder.embed_query` и `Reranker.rerank`.
- TTL для кэша (эмбеддинги стабильны, rerank — зависит от корпуса).

**Риски:**
- Устаревание rerank-кэша при изменении индекса; нужна инвалидация по ref/branch.

**Оценка:** 3–5 дней.

#### C5. Поддержка альтернативных embedding-провайдеров

**Проблема:** жёсткая завязка на Voyage; нельзя использовать локальные модели (Ollama), OpenAI, Cohere.

**Решение:** интерфейс `Embedder` / `Reranker` уже есть; добавить реализации:
- `OpenAIEmbedder`, `OllamaEmbedder`, `SentenceTransformersEmbedder`.
- `Settings.embedder_provider: Literal["voyage", "openai", "ollama", "local"]`.

**Компоненты:**
- `reviewer/index/embeddings.py`: фабрика по провайдеру.
- Опциональные зависимости extras: `[openai]`, `[local]`.

**Риски:**
- Качество поиска может упасть на дешёвых локальных моделях; нужно тестировать.
- Размеры векторов разные → конфигурация размерности в pgvector.

**Оценка:** 1–1.5 недели.

#### C6. Новые dimension-skills

**Проблема:** есть performance и maintainability; но можно добавить security, testing, documentation.

**Решение:** новые skills:
- `/rag-reviewer:reviewer_security-review <pr>`
- `/rag-reviewer:reviewer_test-review <pr>` (проверяет, что изменения покрыты тестами/что тесты корректны)

**Компоненты:**
- `plugin/skills/security-review/SKILL.md`, `plugin/skills/test-review/SKILL.md`.
- Параметризация `ReviewPolicy.categories` и специализированные промпты.

**Оценка:** 3–5 дней на skill.

### Дополнительные долгосрочные идеи (вне ближайшего scope)

- **Поддержка GitLab / Bitbucket:** реализовать `GitLabProvider` / `BitbucketProvider` за интерфейсом `VCSProvider`.
- **Мульти-язычность:** обобщить chunker и graph backend за Python (tree-sitter уже поддерживает много языков, SCIP — только Python).
- **Smart reindex scheduling:** авто-обновление base-индекса по расписанию или webhook от GitHub push.
- **Findings-as-issues:** конвертировать находки ревью в задачи на доске (Yougile/Jira).

## 6. Ещё идеи по улучшению текущего решения

Дополнительный пул идей, не попавший в три основных подхода. Сгруппированы по областям.

### 6.1 Качество и точность ревью

#### D1. Review presets / templates

**Проблема:** сейчас policy единая для всех PR; hotfix, refactor и feature требуют разного фокуса.

**Решение:** сохраняемые preset'ы в `.review.yml` или в `~/.config/rag-reviewer/presets/`:

```yaml
presets:
  hotfix:
    categories: { correctness: true, security: true }
    severity_threshold: high
    max_comments: 10
  refactor:
    categories: { maintainability: true, performance: false }
```

Вызов: `reviewer review owner/repo#42 --preset hotfix` или `/rag-reviewer:reviewer_review-pr owner/repo#42 --preset hotfix`.

**Компоненты:** `ReviewPolicy.load_presets()`, CLI/skill флаг `--preset`, мерж preset → policy.

**Ценность:** быстрый переключение фокуса ревью; меньше шума на неподходящих PR.

#### D2. Custom rules in `.review.yml`

**Проблема:** команды хотят проверять свои соглашения, которые не выводятся из общих категорий.

**Решение:** блок `rules:` с пользовательскими правилами, которые подмешиваются в системный промпт analyze:

```yaml
rules:
  - "Все публичные функции должны иметь docstring"
  - "Запрещено использовать `requests` напрямую — используй `httpx`"
  - "Любое изменение API-роута должно сопровождаться тестом"
```

**Компоненты:** новая секция в `ReviewPolicy`, рендер правил в `ANALYZE_SYSTEM`, лимит количества/длины.

**Ценность:** адаптация под conventions команды без изменения кода плагина.

#### D3. Semantic diff / public API impact annotation

**Проблема:** агент не всегда понимает, какие изменения ломают публичный API.

**Решение:** перед анализом прогонять changed symbols через граф и аннотировать в PR bundle:
- "эта функция имеет N внешних callers";
- "этот класс реализован в N местах";
- "изменена сигнатура публичного метода".

**Компоненты:** `reviewer/agent/impact.py`, интеграция в `_pr_bundle`.

**Ценность:** лучшее понимание scope изменений, меньше пропущенных регрессий.

#### D4. Model routing по размеру/сложности PR

**Проблема:** одна и та же модель для 2-файлового hotfix и 40-файлового рефактора — неэффективно.

**Решение:** эвристический router, который выбирает модель:
- маленький PR → дешёвая/быстрая модель;
- большой PR или изменения в security/critical paths → мощная модель.

**Компоненты:** `reviewer/agent/router.py`, настройки в `Settings`, интеграция в orchestrator.

**Ценность:** экономия cost на простых PR, лучшее качество на сложных.

### 6.2 UX плагина и AI-ассистентов

#### D5. Review local uncommitted changes

**Проблема:** ревью возможно только для уже созданного PR; разработчик не может получить фидбек до коммита.

**Решение:** новый skill `/rag-reviewer:reviewer_review-local` и CLI `reviewer review-local`:
- берёт `git diff` / `git diff --cached`;
- строит ephemeral overlay из рабочей копии;
- анализирует и печатает находки в терминал (без публикации на GitHub).

**Компоненты:** `VCSProvider` для локального git, ephemeral `ref="local:<timestamp>"`, CLI/skill.

**Ценность:** ранний фидбек, меньше исправлений в PR.

#### D6. Review summary templates / tone

**Проблема:** сводка всегда в одном стиле; разные команды хотят разный тон или формат.

**Решение:** в `.review.yml` задавать markdown-шаблон сводки и тон:

```yaml
summary:
  tone: friendly          # strict | friendly | concise
  language: ru            # auto | en | ru
  template: |
    ## 🤖 Ревью от rag-reviewer
    {{ findings_inline }} inline-комментариев, {{ findings_summary }} в сводке.
```

**Компоненты:** `reviewer/agent/summary_template.py`, интеграция в `assemble.py`.

**Ценность:** брендирование и локализация вывода бота.

#### D7. Stacked PR bundle review

**Проблема:** при stacked PR каждый PR ревьюится изолированно; находки дублируются между зависимыми PR.

**Решение:** опционально указывать зависимые PR (`--depends-on owner/repo#41`), ревью строится против объединённого диффа.

**Компоненты:** `ReviewService.prepare` поддерживает несколько base/head пар; overlay объединяет changed paths.

**Ценность:** корректное ревью stacked changes, меньше дублирования.

#### D8. Interactive comment threads / follow-up

**Проблема:** разработчик не может задать вопрос к конкретному комментарию бота.

**Решение:** GitHub App может слушать `issue_comment.created` на PR и отвечать на вопросы к своим находкам (по fingerprint в `<!-- ai-review:hash -->`).

**Компоненты:** webhook handler, retrieval по fingerprint, MCP-tool для follow-up.

**Ценность:** снижение friction, когда разработчик не согласен или не понимает находку.

### 6.3 Производительность и экономия

#### D9. Query result memoization across subagents

**Проблема:** несколько сабагентов делают одинаковые `search_code` / `find_callers` запросы.

**Решение:** shareable cache внутри одного прогона (`deps.tool_cache` уже есть, но можно расширить TTL и scope).

**Компоненты:** расширить `ToolContext.tool_cache`, добавить cross-subagent shared cache.

**Ценность:** меньше повторных вызовов embedder/graph/LLM.

#### D10. Lazy graph expansion

**Проблема:** graph expansion 2 hops может тащить слишком много символов.

**Решение:** ленивая экспансия: сначала 1 hop, если агент явно запрашивает `get_related_symbols` — добирать до 2 hops.

**Компоненты:** параметр `lazy_graph_expansion` в `Retriever`, интеграция с tool loop.

**Ценность:** снижение размера контекста и стоимости.

### 6.4 Observability и feedback loop

#### D11. Findings feedback loop (👍/👎)

**Проблема:** нет данных о том, какие находки были полезны, а какие — шум.

**Решение:** после публикации бот добавляет в сводку реакции (GitHub reactions) или комментарий-шаблон; сохраняем feedback в БД и используем для:
- калибровки confidence thresholds;
- выявления категорий с высоким уровнем ложных срабатываний;
- дообучения/подстройки промптов.

**Компоненты:** таблица `review_feedback`, webhook handler, dashboard-виджет.

**Ценность:** continuous improvement качества ревью.

#### D12. A/B testing for prompts

**Проблема:** изменения в промптах сложно оценить без реальных прогонов.

**Решение:** возможность запускать две версии промпта на одном PR (`--prompt-variant A`) и сравнивать findings/cost в админке.

**Компоненты:** версионирование промптов, поле `prompt_variant` в `review_runs`, сравнительный виджет.

**Ценность:** data-driven улучшение промптов.

#### D13. Team-wide analytics

**Проблема:** админка показывает общие цифры, но не разбивает по командам/репозиториям/времени.

**Решение:** фильтры и дашборды:
- findings по repo/team/time;
- среднее время исправления находки;
- топ файлов с повторяющимися проблемами.

**Компоненты:** расширение API `/api/stats`, фронтенд фильтры.

**Ценность:** метрики качества кода для руководителей команд.

### 6.5 Безопасность и compliance

#### D14. Dependency change audit

**Проблема:** изменения в `requirements.txt` / `package.json` могут втащить уязвимости или несовместимые лицензии.

**Решение:** если PR меняет зависимости, дополнительно проверять:
- известные CVE (через OSV / Snyk API);
- лицензии;
- устаревание пакетов.

**Компоненты:** `reviewer/agent/dependency_audit.py`, интеграция в analyze pipeline.

**Ценность:** ловит supply-chain риски на этапе PR.

#### D15. Findings export (SARIF / JSON / CSV)

**Проблема:** находки живут только в GitHub comments и админке; сложно интегрировать с другими security/quality tools.

**Решение:** CLI/API экспорт:

```bash
reviewer export --run-id 42 --format sarif > findings.sarif
```

**Компоненты:** `reviewer/entrypoints/export.py`, форматтеры SARIF/JSON/CSV.

**Ценность:** интеграция с SIEM, SonarQube, GitHub Advanced Security.

#### D16. CODEOWNERS-aware review

**Проблема:** не все файлы одинаково важны; изменения в core-модулях требуют более жёсткого ревью.

**Решение:** читать `CODEOWNERS`, аннотировать файлы без владельца или с критичными владельцами, повышать severity threshold для них.

**Компоненты:** `reviewer/vcs/codeowners.py`, интеграция в `ReviewPolicy`.

**Ценность:** фокус на критичных областях codebase.

### 6.6 Автоматизация и CI/CD

#### D17. Silent / report-only CI mode

**Проблема:** команды не готовы сразу включать постинг комментариев бота.

**Решение:** режим `--report-only`: ревью выполняется, но findings пишутся в артефакт CI (markdown/SARIF), не постятся на GitHub.

**Компоненты:** флаг `report_only` в `publish_review`, форматтер артефактов.

**Ценность:** мягкое внедрение, возможность посмотреть находки до включения публичных комментариев.

#### D18. Nightly full-repo audit

**Проблема:** ревью только для PR; накопившийся technical debt в основной ветке не проверяется.

**Решение:** `reviewer audit owner/repo --ref main`: ревью всего codebase, публикация summary-отчёта.

**Компоненты:** использует `search_codebase` + analyze по всем файлам, batching.

**Ценность:** регулярная проверка основной ветки на technical debt.

#### D19. GitHub check run integration

**Проблема:** сводка бота — отдельный комментарий, не видна в статусе PR checks.

**Решение:** публиковать findings как GitHub Check Run с аннотациями на строках; статус PR зависит от severity threshold.

**Компоненты:** `GitHubProvider.create_check_run()`, интеграция в `publish_review`.

**Ценность:** привычный CI/CD UX, блокировка PR при критических находках.

### 6.7 Распространение и marketplace

#### D20. Plugin marketplace expansion

**Проблема:** сейчас плагин в marketplace только для Claude Code; другие клиенты требуют ручной установки.

**Решение:**
- Cursor: подготовить `cursor-tools.json` / publish в Cursor marketplace.
- Windsurf: интеграция с Codeium Windsurf MCP registry.
- OpenCode / Mimo / Kimi: автоустановка через `reviewer install` уже есть, но можно добавить marketplace listing.

**Компоненты:** метаданные плагина для разных marketplaces, CI публикации.

**Ценность:** снижение friction установки, охват больше пользователей.

## 7. Рекомендуемая дорожная карта

| Этап | Идеи | Срок | Зависимости |
|---|---|---|---|
| **Phase 1 — Quick wins** | C1 persistent sessions, C2 cost tracking, C4 embedding cache, D9 query memoization | 2–3 недели | Нет |
| **Phase 2 — Расширение skills / UX** | C3 admin actions, C6 new dimension skills, D1 presets, D5 review-local, D6 summary templates | 3–4 недели | Phase 1 |
| **Phase 3 — Автономность** | A1 standalone CLI review, A2 dimensions in CLI, D17 report-only mode | 2–3 недели | Phase 1 |
| **Phase 4 — Автоматизация** | B1 GitHub Actions workflow, B2 GitHub App, D19 check runs | 3–5 недели | Phase 3 |
| **Phase 5 — Качество и observability** | D3 impact annotation, D11 feedback loop, D12 A/B prompts, D13 team analytics | 3–4 недели | Phase 1–3 |
| **Phase 6 — Экосистема** | C5 alternative embedders, GitLab, multi-language, D14 dependency audit, D15 SARIF export | 4+ недели | Phase 3–4 |

## 8. Глубокие идеи, которые действительно улучшат плагин

Эти идеи не просто расширяют функциональность, а решают ключевые боли текущего решения: высокий порог входа, стоимость ревью, время прогона и замыкание цикла «нашёл → исправил».

### E1. Lazy / on-demand indexing

**Боль:** чтобы начать ревью, нужно проиндексировать весь репозиторий. На большом mono-repo это часы и десятки долларов Voyage.

**Почему важно:** это главный барьер для adoption. Если пользователь может получить первое ревью через 2 минуты вместо 2 часов, conversion резко растёт.

**Решение:**
- При `prepare_review` индексировать только изменённые файлы PR (overlay уже есть).
- При `search_code` / `get_related_symbols` проверять, проиндексирован ли запрошенный файл/символ.
- Если нет — лениво чанковать и эмбеддить только нужный файл, используя `content_hash` для дедупликации.
- Фоново (low-priority) добирать остальные файлы по мере свободных ресурсов.

**Компоненты:**
- `reviewer/index/lazy_indexer.py`: индексация одного файла или small bundle по запросу.
- `Retriever` с fallback: сначала ищет в индексе; при промахе — lazy index.
- Фоновый worker/queue для добора остальных файлов.

**Эффект:** первое ревью возможно сразу после установки; полный индекс строится постепенно.

### E2. Test-aware review

**Боль:** агент анализирует изменения, но не проверяет, покрыты ли они тестами, и не смотрит связанные тесты как контекст.

**Почему важно:** плохое тестовое покрытие изменений — одна из самых дорогих ошибок. Человек-ревьюер всегда спрашивает: «а где тесты?».

**Решение:**
- Для changed symbols находить связанные тесты через граф (`CALLS` / `TESTED_BY` рёбра или heuristic по имени).
- В PR bundle включать diff связанных тестов.
- Агент проверяет:
  - добавлены ли тесты на новое поведение;
  - обновлены ли существующие тесты под изменения;
  - есть ли ветки условий без покрытия.

**Компоненты:**
- `reviewer/agent/test_linker.py`: поиск связанных тестов.
- Новая категория находок `testing`.
- Промпт, требующий оценить тестовое покрытие.

**Эффект:** меньше регрессий, более полные PR.

### E3. Smart pre-filter с дешёвой моделью

**Боль:** каждый файл ревьюится дорогой моделью, даже если в нём очевидно нет рисков (например, rename, docstring-правки, конфиги).

**Почему важно:** на больших PR 70% файлов — тривиальные. Тратить на них Claude — неэффективно.

**Решение:**
- Перед analyze запускать дешёвую модель (Haiku / Gemini Flash / local) для скрининга.
- Модель отвечает: `risk_score` (low/medium/high) + краткая причина.
- Только medium/high попадают в полноценный analyze; low — быстрый skim или skip.

**Компоненты:**
- `reviewer/agent/prefilter.py`.
- Конфигурация prefilter model и threshold.
- Параллельный prefilter для всех changed files.

**Эффект:** снижение cost на 30–60% для больших PR без потери качества.

### E4. Batch analyze для маленьких файлов

**Боль:** fan-out по одному сабагенту на файл даёт overhead на системный промпт и контекст. Маленькие файлы (200 строк) можно анализировать пачками.

**Решение:**
- Группировать changed files по размеру/связности.
- Один LLM-call обрабатывает несколько мелких файлов с общим системным промптом.
- Большие/рискованные файлы — отдельно.

**Компоненты:**
- `reviewer/agent/batch_analyzer.py`.
- Правила группировки (max total tokens, max files per batch).

**Эффект:** меньше LLM-calls, меньше cost, быстрее прогон.

### E5. Inline fix apply: `reviewer fix <pr>`

**Боль:** бот находит проблему и предлагает fix, но разработчик должен вручную применять suggestions.

**Почему важно:** закрывает цикл. Для многих находок (опечатки, простые рефакторинги, missing imports) fix однозначен.

**Решение:**
- `reviewer fix owner/repo#N` — скачивает PR branch, применяет applyable suggestions, коммитит и пушит.
- Применяются только безопасные фиксы: exact replacement, no overlap, no semantic ambiguity.
- Опционально создаёт fixup-commit или PR в PR.

**Компоненты:**
- `reviewer/vcs/fix_applier.py`.
- Интеграция с `assemble.py` для генерации applyable suggestions.
- CLI/skill команда.

**Эффект:** меньше ручной работы, быстрее исправления.

### E6. Pre-commit hook

**Боль:** ревью происходит после создания PR, когда исправления дороже.

**Решение:**
- `reviewer-pre-commit` hook, который ревьюит staged changes перед коммитом.
- Быстрый режим: только критичные категории, без публикации.
- Интеграция с `pre-commit` framework.

**Компоненты:**
- `plugin/pre-commit/`.
- CLI `reviewer review-local --pre-commit`.

**Эффект:** ранний фидбек, меньше циклов в PR.

### E7. Cross-PR memory / organization learning

**Боль:** каждый прогон начинается с чистого листа. Бот не помнит, что в прошлом PR разработчик отклонил находку как false positive.

**Решение:**
- Индексировать findings с feedback (👍/👎) и их resolution.
- При анализе подмешивать в контекст похожие прошлые находки.
- Если паттерн часто отклоняется — снижать confidence или skip.

**Компоненты:**
- `reviewer/agent/memory.py`.
- Vector store для findings + metadata.
- Интеграция с D11 feedback loop.

**Эффект:** fewer false positives over time, персонализация под команду.

### E8. Security static analysis integration

**Боль:** LLM может пропустить известные security-антипаттерны, особенно если они неочевидны из контекста.

**Решение:**
- Запускать semgrep, bandit, trivy, gitleaks на changed files.
- Результаты подаются как seed findings в analyze.
- LLM добавляет контекст: «это действительно уязвимость?», «влияет ли на callers?».

**Компоненты:**
- `reviewer/agent/security_scanners.py`.
- Sandboxed execution сканеров (или их API).
- Новая категория `security`.

**Эффект:** higher recall security-проблем, меньше пропусков.

### E9. Cost guardrails / alerts

**Боль:** пользователь не знает, сколько сейчас потратит, пока не получит счёт.

**Решение:**
- `REVIEW_MAX_COST_PER_RUN` / `REVIEW_MAX_COST_PER_MONTH`.
- При приближении к лимиту — warn/abort/switch to cheaper model.
- Админка: алёртинг (email/Slack) при превышении порогов.

**Компоненты:**
- `reviewer/config/settings.py` — лимиты.
- `CostGuard` в orchestrator.
- Алёрты в admin panel.

**Эффект:** предсказуемый бюджет, нет сюрпризов.

### E10. One-command onboarding wizard

**Боль:** чтобы начать, нужно: Docker, Voyage key, GitHub token, `.env`, `reviewer index`, настройка MCP. Много шагов.

**Решение:**
- `reviewer init --interactive`:
  - проверяет Docker/uv;
  - запрашивает ключи;
  - поднимает Docker compose;
  - запускает `reviewer check`;
  - предлагает установить MCP/skills для обнаруженных AI-инструментов;
  - опционально запускает lazy index первого repo.

**Компоненты:**
- Расширение `reviewer init`.
- Интерактивные prompts (rich/click).

**Эффект:** время onboarding с 30 минут до 5 минут.

### E11. Self-healing index

**Боль:** индекс может рассинхронизироваться с base-веткой, пропасть чанки, испортиться граф. Пользователь об этом не узнает.

**Решение:**
- Периодическая проверка целостности: SHA в `index_meta` vs remote, count chunks, orphaned graph nodes.
- Автоматический repair: re-index missing/corrupted pieces.
- Health check endpoint `/api/health/index`.

**Компоненты:**
- `reviewer/index/health.py`.
- Scheduler/background job.
- API endpoint.

**Эффект:** меньше silent failures, стабильнее работа.

## 9. Риски и откат

- **A1 standalone CLI:** риск дублирования логики skill. Откат — оставить CLI только для prepare/publish, а анализ оставить в skill.
- **B2 GitHub App:** самый инфраструктурно сложный. Можно начать с B1 Actions workflow и добавлять App позже.
- **C1 persistent sessions:** риск утечки данных. Откат — оставить memory-only режим по умолчанию, persisted store — opt-in.
- **C4 cache:** риск stale results. Откат — `EMBED_CACHE_TTL=0` отключает кэш.
- **C5 alternative embedders:** риск регресса качества. Откат — вернуть `embedder_provider=voyage`.
- **D2 custom rules:** риск «rule explosion» и роста стоимости. Откат — лимит числа/длины правил, opt-in per repo.
- **D5 review-local:** риск утечки незакоммиченного кода через логи/overlay. Откат — ephemeral ref с TTL, no history.
- **D8 interactive threads:** риск циклов и спама. Откат — rate limiting, ответ только на reply к бот-комментариям.
- **D11 feedback loop:** риск skewed data (мало feedback). Откат — использовать только для аналитики, не для автоматической калибровки порогов до накопления данных.
- **D14 dependency audit:** риск ложных срабатываний и внешних API-лимитов. Откат — отдельная категория `dependency_audit`, которую можно выключить.
- **D19 check runs:** риск блокировки PR при нестабильных findings. Откат — `check_run_status=neutral` вместо `failure`.
- **E1 lazy indexing:** риск фрагментации индекса и неполного контекста. Откат — явное требование полного `reviewer index` перед ревью.
- **E3 smart pre-filter:** риск пропуска находок в файлах, отсеянных как low-risk. Откат — threshold=0 (prefilter пропускает всё).
- **E5 fix apply:** риск неправильного автоматического коммита. Откат — только dry-run apply, ручное подтверждение.
- **E8 security scanners:** риск ложных positives. Откат — отключение сканера, только LLM-находки.

## 10. Self-review

- **Placeholder scan:** нет `TBD`/`TODO`; все идеи имеют описание, компоненты, оценку.
- **Internal consistency:** идеи не противоречат существующей архитектуре и уже реализованным фичам.
- **Scope check:** это roadmap-спека; каждая идея достаточно мала для отдельного implementation plan, кроме B2 (App), который декомпозирован на workflow → App.
- **Ambiguity check:** термины «skill», «MCP», «dimension-review», «base/overlay» используются в том же смысле, что и в README/CLAUDE.md.
- **Coverage:** новые D/E-идеи дополняют A/B/C, не дублируют их; roadmap обновлён с учётом quick wins из D.

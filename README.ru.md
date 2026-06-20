# rag_for_git

> 🇬🇧 English version: [README.md](README.md)

Агент автоматического ревью pull/merge request'ов на основе **RAG + графа кода + Claude Code**.

На событие «появился/обновился PR» агент берёт дифф, собирает релевантный контекст **по всему репозиторию** (гибридный поиск + граф связей кода), прогоняет его через Claude Code-скилл с инструментами поиска (agentic RAG), отсеивает ложные срабатывания и постит результат обратно в GitHub: **inline-комментарии на строки диффа + сводку**.

> Статус: рабочий v1. Целевой язык анализа — **Python**. VCS — **GitHub** (за интерфейсом `VCSProvider`, под GitLab/др. заложена абстракция). Проверено вживую: ловит реальные баги, видит влияние на вызывающий код и существующие тесты.

---

## Содержание
- [Зачем это и в чём идея](#зачем-это-и-в-чём-идея)
- [Архитектура: как связаны части](#архитектура-как-связаны-части)
- [Как работает ревью (поток данных)](#как-работает-ревью-поток-данных)
- [Свежесть индекса на «живом» репозитории](#свежесть-индекса-на-живом-репозитории)
- [Промт для быстрой установки](#промт-для-быстрой-установки)
- [Быстрый старт](#быстрый-старт)
- [Конфигурация (справочник env)](#конфигурация-справочник-env)
- [CLI (справочник команд)](#cli-справочник-команд)
- [Скиллы (справочник с параметрами)](#скиллы-справочник-с-параметрами)
- [MCP-тулы (справочник)](#mcp-тулы-справочник)
- [Использование плагина](#использование-плагина)
- [Политика per-repo и доска задач](#политика-per-repo-и-доска-задач)
- [Эксплуатация и веб-админка](#эксплуатация-и-веб-админка)
- [Пример ревью «от диффа до комментария»](#пример-ревью-от-диффа-до-комментария)
- [Структура проекта](#структура-проекта)
- [Тесты](#тесты)
- [Ограничения и заметки](#ограничения-и-заметки)

---

## Зачем это и в чём идея

Обычные линтеры ловят синтаксис и стиль, но не видят **смысла и связей**: сломанный контракт функции, влияние правки на вызывающих, удалённую проверку, противоречие существующему тесту. Идея агента — дать LLM **тот же контекст, что у живого ревьюера**:

- **RAG** — найти по всему репозиторию похожий/связанный код семантически (вектора) и лексически (BM25);
- **Граф кода** — структурно подтянуть вызывающих/вызываемых/реализации/тесты изменённого символа;
- **LLM с инструментами** — рассуждать над диффом, дотягивая нужный код тулзами, и выносить замечания;
- **Verify-проход** — отсеять галлюцинации, не теряя реальных багов.

Один прогон ревью идёт тремя стадиями: **`prepare_review` (MCP)** → **анализ (Claude subagents)** → **`publish_review` (MCP)**.

## Архитектура: как связаны части

```
                ┌──────────────────────────── reviewer (ядро-библиотека) ────────────────────────────────┐
                │                                                                                        │
  GitHub PR ───▶│  VCSProvider (github.py)  ──дифф/файлы/патчи──▶  MCPReviewService                      │
  (owner/repo#N)│        ▲  публикация inline+сводка                     │                               │
                │        │                                               │ prepare_review                │
                │        │                                               ▼                               │
                │        │                         ┌──────────── retrieval/Retriever ─────────────┐      │
                │        │                         │  гибрид-поиск          graph-expansion       │      │
                │        │                         │  ┌────────────────┐   ┌───────────────────┐  │      │
                │        │                         │  │ Postgres       │   │ Neo4j             │  │      │
                │        │                         │  │ (ParadeDB)     │   │ Symbol(path#fqn)  │  │      │
                │        │                         │  │ pgvector(HNSW) │   │ -[:CALLS]-> (граф)│  │      │
                │        │                         │  │ + pg_search    │   │ (IMPLEMENTS: SCIP)│  │      │
                │        │                         │  │   (BM25, RRF)  │   │ expand 1–2 хопа   │  │      │
                │        │                         │  └──────▲─────────┘    └─────────▲────────┘  │      │
                │        │                         │         │ chunks (vector+text)  │ узлы/рёбра │      │
                │        │                         │         │                       │            │      │
                │        │                         │      Voyage embed/rerank   tree-sitter граф  │      │
                │        │                         └──────────────────┬───────────────────────────┘      │
                │        │                                            ▼ ContextPack                      │
                │        │                         Claude Code subagents (скилл /rag-reviewer:reviewer_review-pr) │
                │        │                           инструменты: search_code, get_related_symbols,      │
                │        │                           read_file, get_definition, find_callers,            │
                │        │                           get_changed_file_diff                               │
                │        └──────────────────── publish_review (gate/grounding/dedup/assemble) ◀─────────┘│
                └────────────────────────────────────────────────────────────────────────────────────────┘

  Хранилища поднимаются в Docker:  Postgres/ParadeDB (:5433)  ·  Neo4j (:7687)  ·  веб-админка (:8000)
  Внешние API:  Voyage (эмбеддинги voyage-code-3 + reranker rerank-2.5)
```

Кратко, кто за что отвечает:

| Часть | Модуль | Роль |
|---|---|---|
| VCS-провайдер | `reviewer/vcs/` | получить PR/дифф/файлы, запостить ревью; маппинг строк диффа; идемпотентность |
| Индекс (RAG) | `reviewer/index/` | чанкинг (tree-sitter), эмбеддинги (Voyage), хранилище (pgvector+BM25), свежесть |
| Граф кода | `reviewer/graph/` | построение рёбер `CALLS` + `IMPLEMENTS` (SCIP-бэкенд) или только `CALLS` (tree-sitter); оркестрация в `backend.py`; хранение и обход в Neo4j |
| Ретрив | `reviewer/retrieval/` | гибрид (RRF) + graph-expansion + Voyage rerank → контекст |
| Инструменты | `reviewer/tools/` | `search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`; session-less `search_codebase`/`related_symbols`/`callers`/`definition` для Q&A |
| Задачи/доски | `reviewer/tasks/` | нормализация в `TaskBrief`, REST-провайдеры досок (`TaskBoardProvider`, yougile — референс), `TaskService.index_batch` |
| MCP-сервис | `reviewer/mcp/` | `MCPReviewService`: prepare/tool-вызовы/publish; управление сессиями PR |
| Сервис | `reviewer/services/` | `ReviewService.prepare`: ingest PR, overlay, units |
| Агент | `reviewer/agent/` | state (ReviewUnit) · assemble · dedup |
| LLM утилиты | `reviewer/llm/` | `_retry.py` (retry/backoff для Voyage) |
| Политика | `reviewer/policy/` | гейтинг findings (категория/severity/confidence/пути) |

**Единый ключ связи** между RAG и графом — `node_id = "path#fqn"` (напр. `rag/embedder.py#VoyageEmbedder.embed_query`). И чанк в Postgres, и узел в Neo4j используют его, поэтому graph-expansion и ретрив чанков «сшиваются» без дополнительной маппинг-таблицы.

## Как работает ревью (поток данных)

Ревью запускается скиллом `/rag-reviewer:reviewer_review-pr` в Claude Code. Поток на один PR:

```
──────────────── prepare_review (MCP → MCPReviewService) ─────────────────────
1. ingest      GitHub: PR (base_sha, head_sha, base_ref) + изменённые файлы с патчами
                  │
2. overlay     изменённые .py → чанкинг (tree-sitter) → эмбеддинг (Voyage) →
               upsert в Postgres под ref="pr:N"  (content-hash дедуп)
                  │
3. plan        дифф → review-units (по файлу): {path, node_ids изменённых символов, patch}
               → payload скиллу: юниты/политика/патчи/конфиг доски
                  │
────────── analyze: Claude subagents (скилл /rag-reviewer:reviewer_review-pr) ──────────
4. analyze     Subagents в tool-loop по каждому файлу:
                 • search_code(query)        → Retriever:
                        embed_query (Voyage) → гибрид-поиск по (base \ changed ∪ overlay):
                          pgvector ANN  +  pg_search BM25  → слияние RRF
                        + graph.expand(изменённые символы) → Neo4j callers/callees (impl/тесты при наличии рёбер)
                        + Voyage rerank → top-N  → ContextPack (код с цитатами path:line)
                 • get_related_symbols(node) → связанные символы из графа
                 • read_file, get_definition, find_callers, get_changed_file_diff
               параллельно: dimension-проходы performance / maintainability
               (+ requirements, если подключена доска) и финальный verify (отсев галлюцинаций)
               → findings (JSON): category, severity, line, message, suggestion, confidence
                  │  (findings аккумулируются со всех файлов)
                  │
─────────── publish_review (MCP → MCPReviewService) ──────────────────────────
5. gate        policy.gate: категория включена? severity ≥ порога? confidence ≥ порога?
               путь не в ignore?
                  │
6. grounding   уточнение номера строки по дословной code_quote (анти-галлюцинация)
               + dedup по fingerprint (схлопываем одинаковые находки)
                  │
7. assemble    findings → разделение:
                 • строка попадает в дифф → inline-комментарий (RIGHT/LEFT)
                 • иначе → пункт в сводку (с ссылкой file:line)
               + кап max_comments + идемпотентность по фингерпринту (не дублировать на повторном push)
                  │
8. publish     GitHubProvider: один review = сводка + массив inline-комментариев;
               при task_key — линковка PR к задаче в графе
```

Ключевые свойства:
- **agentic RAG** — ретрив только засеивает контекст; LLM сам дотягивает нужный код тулзами.
- **graph + RAG вместе** — вектора находят «похожее по смыслу», граф добавляет «структурно связанное» (кто сломается), что эмбеддинги часто упускают.
- **inline только на строках диффа** — GitHub разрешает комментарии лишь на изменённых/контекстных строках хунка; остальное уходит в сводку (инвариант зашит в `assemble`).
- **идемпотентность** — каждый комментарий помечен скрытым фингерпринтом `<!-- ai-review:hash -->`; повторный прогон не плодит дубликаты.

## Свежесть индекса на «живом» репозитории

Код меняется с каждым push'ем, а полный реиндекс большого репо дорог. Решение — **стабильная база + content-hash дедуп + overlay на PR**:

- **`ref="base:<branch>"`** — персистентный индекс отслеживаемой ветки (напр. `base:main`, `base:master`). Каждая ветка из `REVIEW_BRANCHES` имеет **изолированный** индекс. Обновляется инкрементально (`reviewer index --ref <branch>`): чанкуются только изменённые файлы, эмбеддятся только чанки с новым `content_hash` (эмбеддинги переиспользуются между ветками по хешу — экономия Voyage).
- **`ref="pr:N"`** — эфемерный overlay: только изменённые файлы PR на его HEAD.
- **На запросе**: `retrieval = (base:<branch>, где path ∉ изменённых) ∪ overlay`. Для изменённых файлов агент видит **новую** версию, для остального — стабильную базу. Это и есть условие «находить произвольный релевантный код по всему репо, но по актуальной версии».
- **Мульти-бранч**: PR ревьюится против индекса своей целевой ветки (`base_ref` из PR). PR в ветку вне `REVIEW_BRANCHES` пропускается (`prepare_review` → `{"status":"skipped",...}`). Граф кода (`:Symbol` в Neo4j) тоже разрезан по ветке через свойство `branch` (составная уникальность `(repo, branch, id)`).

## Промт для быстрой установки

Скопируйте и вставьте в любой AI-инструмент (Claude Code, Cursor, Gemini CLI, Mimo Code, OpenCode, Kimi Code, Codex, Copilot…):

```
Настрой MCP-сервер rag-reviewer в этом AI-инструменте.

Правила:
- Для запуска сервера используй `uvx --from rag-reviewer@latest reviewer-mcp` — не pip install, не клонирование репозитория.
- Обёртка `bash -lc` обязательна на macOS/Linux, чтобы uvx находился GUI-инструментами: command="/bin/bash", args=["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]. На Windows: command="uvx", args=["--from","rag-reviewer@latest","reviewer-mcp"].
- Определи, в каком AI-инструменте ты работаешь, и запиши MCP-запись в нужный конфиг-файл:
  - Claude Code / generic: .mcp.json в корне проекта ({"mcpServers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Cursor: ~/.cursor/mcp.json (тот же формат)
  - Gemini CLI: ~/.gemini/settings.json ({"mcpServers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Antigravity: ~/.gemini/antigravity/mcp_config.json (тот же формат mcpServers)
  - Windsurf: ~/.codeium/windsurf/mcp_config.json (тот же формат mcpServers)
  - Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json (macOS) или %APPDATA%\Claude\claude_desktop_config.json (Windows) — тот же формат mcpServers
  - Mimo Code: ~/.config/mimocode/mimocode.json ({"mcp":{"reviewer":{"type":"local","command":["/bin/bash","-lc","uvx --from rag-reviewer@latest reviewer-mcp"],"enabled":true}}})
  - OpenCode: ~/.config/opencode/opencode.json ({"mcp":{"reviewer":{"type":"local","command":["/bin/bash","-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Kimi Code: ~/.kimi-code/mcp.json ({"mcpServers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Codex CLI: ~/.codex/config.toml ([mcp_servers.reviewer] command="/bin/bash" args=["-lc","uvx --from rag-reviewer@latest reviewer-mcp"])
  - VS Code: ~/Library/Application Support/Code/User/mcp.json — ключ "servers" (не "mcpServers"): {"servers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}}
- После записи конфига выполни: uvx --from rag-reviewer reviewer check
- Сообщи, в какой файл записал и прошла ли проверка.
```

---

## Быстрый старт

MCP-сервер опубликован на PyPI как [`rag-reviewer`](https://pypi.org/project/rag-reviewer/)
и запускается через `uvx` — **клонировать этот репозиторий не нужно**.

Нужны: Docker, [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (включает `uvx`),
ключ Voyage, GitHub-токен. Python 3.11–3.13 — только для `pip`/editable-установки (у `uvx` свой).

### Быстрая установка (рекомендуется, все платформы)

```bash
# 0) Установить reviewer CLI — один раз, глобально
uv tool install rag-reviewer
# uv и uvx — одна бинарка; устанавливая uv, вы получаете оба.
# MCP-сервер, который запускает редактор, использует uvx @latest и обновляется сам.

# 1) Инфраструктура
curl -O https://raw.githubusercontent.com/mimfort/rag_for_git/main/docker-compose.yml
docker compose up -d          # Postgres/ParadeDB (:5433) + Neo4j (:7687) + web-админка (:8000)

# 2) Настроить ключи и параметры интерактивно
reviewer init
#    Пошаговый wizard: VOYAGE_API_KEY, GITHUB_TOKEN и опциональные группы
#    (хранилища, мульти-репо, доска задач). Запускайте повторно в любое время для изменения.
#    CI / без интерактива: reviewer init --yes  (принимает все дефолты молча)

# 3) Прописать MCP-сервер (и скиллы) в ваш редактор/CLI
reviewer install --all        # автодетект клиентов + установка скиллов
#    или конкретный: reviewer install cursor|vscode|claude-code|claude-desktop|windsurf|gemini|antigravity|mimo|opencode|kimi|trae|codex
#    скиллы ставятся в клиенты, которые их поддерживают (Gemini/Mimo/Kimi); --no-skills чтобы пропустить

# 4) Проверить
reviewer check

# Обновить CLI позже:
uv tool upgrade rag-reviewer
```

> **`reviewer install` кроссплатформенный** (Windows / macOS / Linux). Подставляет абсолютный
> путь к `uvx` автоматически — обёртка `bash -lc` не нужна. Ручные JSON-конфиги ниже используют
> `bash -lc` только для macOS/Linux; на Windows используйте `reviewer install` или указывайте
> `"command": "uvx"` с `"args": ["--from", "rag-reviewer@latest", "reviewer-mcp"]` напрямую.

> **Claude Code: тулы работают из коробки.** `reviewer install claude-code` дополнительно
> прописывает allowlist-правило `mcp__reviewer__*` в **глобальный** `~/.claude/settings.json`
> (`permissions.allow`), поэтому тулы reviewer MCP работают **во всех проектах** без обращения
> к safety-classifier `auto`-режима — без ручного редактирования настроек. Глобальное правило
> покрывает и установку плагином (marketplace): там сервер доступен везде, но плагин не раздаёт
> разрешений.

> **Откуда читаются ключи.** `.env` резолвится из фиксированного места, **не** из текущего каталога —
> MCP-клиенты запускают сервер с произвольным CWD, поэтому проектный `./.env` ненадёжен. Порядок
> поиска: `$REVIEWER_ENV_FILE` → `$XDG_CONFIG_HOME/rag-reviewer/.env` (по умолчанию
> `~/.config/rag-reviewer/.env`) → `./.env` (удобно при запуске из клона репо). Реальные переменные
> окружения всегда побеждают файл, поэтому ключи можно передать и блоком
> `"env": { "VOYAGE_API_KEY": "…", "GITHUB_TOKEN": "…" }` в конфиге MCP-клиента — работает везде.

Где взять ключи:
- **Voyage** (`VOYAGE_API_KEY`): https://dashboard.voyageai.com/ — есть бесплатный пул; привяжите карту, чтобы снять лимит 3 RPM / 10K TPM.
- **GitHub** (`GITHUB_TOKEN`): PAT с правами *Pull requests: Read and write* + *Contents: Read* (fine-grained) или scope `repo` (classic). Быстрый вариант: `gh auth token`.

Остальные настройки имеют дефолты (см. `.env.example` и [справочник env](#конфигурация-справочник-env) ниже).

### Ручная установка плагина (альтернатива)

Если вы предпочитаете прописать конфиг вручную, а не через `reviewer install`:

| Инструмент | Глобальный конфиг | Проектный конфиг | Инструкция |
|---|---|---|---|
| **Claude Code** | `/plugin marketplace add` (см. ниже) | `.claude-plugin/` ✓ | — |
| **Cursor** | `~/.cursor/mcp.json` | `.cursor/mcp.json` ✓ | — |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` | — | — |
| **Claude Desktop** | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`; Windows: `%APPDATA%\Claude\claude_desktop_config.json` | — | — |
| **Antigravity** | `~/.gemini/antigravity/mcp_config.json` | — | — |
| **Mimo Code** | `~/.config/mimocode/mimocode.json` | `.mimocode/mimocode.json` ✓ | [INSTALL.md](.mimocode/INSTALL.md) |
| **OpenCode** | `~/.config/opencode/opencode.json` | `.opencode/opencode.json` ✓ | [INSTALL.md](.opencode/INSTALL.md) |
| **Kimi Code** | `~/.kimi-code/mcp.json` | `.kimi-code/mcp.json` ✓ | [INSTALL.md](.kimi-code/INSTALL.md) |
| **Gemini CLI** | `~/.gemini/settings.json` | `.gemini/settings.json` ✓ | [GEMINI.md](GEMINI.md) |
| **Codex CLI** | `~/.codex/config.toml` | `.codex-plugin/plugin.json` ✓ | [AGENTS.md](AGENTS.md) |
| **Copilot CLI** | — | `.github-copilot/plugin.json` ✓ | — |
| **Trae IDE** | `~/Library/Application Support/Trae/User/mcp.json` | — | — |
| **VS Code** | `~/Library/Application Support/Code/User/mcp.json` (ключ: `servers`, не `mcpServers`) | — | — |

Файлы, помеченные ✓, уже есть в этом репозитории — если открыть `rag_for_git` как проект
в соответствующем инструменте, MCP-сервер подключится автоматически. Для **глобальной установки**
(работает из любого проекта) добавьте запись в глобальный конфиг-файл.

Формат записи (macOS/Linux — на Windows используйте `reviewer install`):

**Mimo Code** (`mimocode.json`):
```json
{
  "$schema": "https://mimo.xiaomi.com//config.json",
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer@latest reviewer-mcp"],
      "enabled": true
    }
  }
}
```

**OpenCode** (`opencode.json`):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

**Kimi Code / Cursor / Gemini CLI / Trae / Claude Desktop / Windsurf / Antigravity** (стандартный MCP JSON):
```json
{
  "mcpServers": {
    "reviewer": {
      "command": "/bin/bash",
      "args": ["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

**VS Code** (`mcp.json` — ключ `servers`, не `mcpServers`):
```json
{
  "servers": {
    "reviewer": {
      "command": "/bin/bash",
      "args": ["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

**Codex CLI** (`~/.codex/config.toml`):
```toml
[mcp_servers.reviewer]
command = "/bin/bash"
args = ["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
```

После добавления перезапустите инструмент — `reviewer` появится рядом с другими MCP-серверами.

#### Claude Code (marketplace)

Из любого проекта двумя командами:

```text
/plugin marketplace add mimfort/rag_for_git
/plugin install rag-reviewer@rag-reviewer-marketplace
```

Вы получаете:

- **Скиллы:** `/rag-reviewer:reviewer_review-pr`, `/rag-reviewer:reviewer_solve-task`,
  `/rag-reviewer:reviewer_sync-codebase`, `/rag-reviewer:reviewer_sync-tasks`,
  `/rag-reviewer:reviewer_performance-review`, `/rag-reviewer:reviewer_maintainability-review`,
  `/rag-reviewer:reviewer_ask` (см. [справочник скиллов](#скиллы-справочник-с-параметрами)).
- **MCP-сервер** `reviewer` с 20 тулами (см. [справочник MCP-тулов](#mcp-тулы-справочник)).

> Команда `/plugin` покажет, что `rag-reviewer` установлен и включён.

#### Глобальная установка скиллов (опционально)

7 скиллов оборачивают MCP-тулы в управляемый сценарий. Без них можно вызывать тулы напрямую,
но скиллы — основная точка входа.

**`reviewer install` уже ставит их** для клиентов с файловыми скиллами (Gemini, Mimo, Kimi).
Чтобы (пере)установить только скиллы или выбрать конкретного клиента:

```bash
uvx --from rag-reviewer reviewer install-skills --all     # все обнаруженные клиенты со скиллами
uvx --from rag-reviewer reviewer install-skills gemini    # конкретный
uvx --from rag-reviewer reviewer install-skills --list    # показать цели и каталоги
```

Команда скачивает скиллы с GitHub (без клона репо) и распаковывает в глобальную папку скиллов
каждого клиента (с защитой от path traversal). Ручной аналог:

```bash
curl -sL https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz -o /tmp/rag-reviewer.tgz
mkdir -p ~/.gemini/skills
tar xz -C ~/.gemini/skills --strip-components=3 -f /tmp/rag-reviewer.tgz 'rag_for_git-main/plugin/skills'
rm /tmp/rag-reviewer.tgz
```

| Инструмент | Глобальная папка скиллов |
|---|---|
| Gemini CLI | `~/.gemini/skills/` |
| Mimo Code | `~/.config/mimocode/skills/` |
| Kimi Code | `~/.kimi-code/skills/` + `extra_skill_dirs` в `~/.kimi-code/config.toml` |
| OpenCode | `~/.config/opencode/skills/` |
| Claude Code | поставляется в плагине (шаг выше) |
| Cursor | на уровне проекта через `.cursor-plugin/plugin.json` |

---

## Конфигурация (справочник env)

Всё ключевое — через `.env` (см. `.env.example` с комментариями). **Единственный обязательный
внешний ключ — `VOYAGE_API_KEY`**; `GITHUB_TOKEN` обязателен для ревью PR. Остальное имеет дефолты,
совпадающие с поставляемым `docker-compose.yml`. Порядок резолва `.env`: `$REVIEWER_ENV_FILE` →
`~/.config/rag-reviewer/.env` → `./.env` (реальные переменные окружения всегда побеждают).

### Voyage — эмбеддинги + реранкер (обязательно)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `VOYAGE_API_KEY` | `""` | **Обязателен.** Ключ Voyage для эмбеддингов и реранкинга. |
| `EMBEDDING_MODEL` | `voyage-code-3` | Модель эмбеддингов. |
| `EMBEDDING_DIM` | `1024` | Размерность; **должна совпадать** с колонкой `vector(N)` в Postgres — смена требует реиндекса. |
| `EMBEDDING_BATCH_SIZE` | `256` | Текстов в одном запросе эмбеддинга (≤1000 и ≤120K токенов). |
| `RERANK_MODEL` | `rerank-2.5` | Модель реранкера Voyage. |

### GitHub (обязательно для ревью PR)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `GITHUB_TOKEN` | `""` | PAT — *Pull requests: Read and write* + *Contents: Read*. |
| `GITHUB_RETRY_ATTEMPTS` | `3` | Ретраи на сетевые сбои GitHub API. |
| `GITHUB_RETRY_BACKOFF_BASE` | `1.0` | База экспоненциального backoff (сек). |

### Хранилища (Postgres/ParadeDB + Neo4j)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `PG_DSN` | `postgresql://reviewer:reviewer@localhost:5433/reviewer` | ParadeDB (pgvector + pg_search) на host-порту **5433**. |
| `PG_POOL_MIN_SIZE` | `1` | Минимум соединений в пуле Postgres. |
| `PG_POOL_MAX_SIZE` | `4` | Максимум соединений в пуле Postgres. |
| `NEO4J_URI` | `neo4j://localhost:7687` | bolt-URI Neo4j. |
| `NEO4J_USER` | `neo4j` | Пользователь Neo4j. |
| `NEO4J_PASSWORD` | `reviewerpass` | Пароль Neo4j (одноразовый dev-дефолт). |
| `GRAPH_BACKEND` | `auto` | Движок графа: `auto` (SCIP если `scip-python` в PATH, иначе tree-sitter), `scip`, `treesitter`. |

### Мульти-репо / мульти-ветки (опционально)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `DEFAULT_REPO` | `""` | Дефолтный `owner/name` для session-less тулов и `reviewer index` без `--repo`; пусто = мульти-репо (репо передаётся явно). |
| `REVIEW_BRANCHES` | `main` | CSV отслеживаемых веток; первая — **первичная** (дефолт для `reviewer index --ref` и CLI search). PR в ветку вне списка пропускается. |

### Политика ревью (env-дефолты; per-repo `.review.yml` переопределяет)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `REVIEW_SEVERITY_THRESHOLD` | `medium` | Мин. важность находки: `low`/`medium`/`high`/`critical`. |
| `REVIEW_MIN_CONFIDENCE` | `0.5` | Отбрасывать находки ниже уверенности (0..1). |
| `REVIEW_MAX_COMMENTS` | `25` | Кап inline-комментариев на ревью. |
| `REVIEW_MAX_FILES` | `50` | Кап файлов `.py` на ревью; лишние — в сводку как пропущенные. |
| `REVIEW_CATEGORIES` | `""` | CSV вайтлист категорий (`correctness`, `security`, `performance`, `style`, `requirements`); пусто = все. |
| `REVIEW_SUGGESTIONS` | `apply` | `apply` = applyable GitHub `suggestion`-блоки; `text` = только текстовые советы. |
| `REVIEW_OUTPUT_LANGUAGE` | `ru` | Язык текста публикуемых находок. |
| `REVIEW_SKIP_DRAFTS` | `true` | Не ревьюить draft-PR. |
| `MAX_TOOL_RESULT_CHARS` | `8000` | Макс. длина результата tool-вызова в промпте. |

### Наблюдаемость и сессии (опционально)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `REVIEW_HISTORY` | `true` | Писать историю прогонов в Postgres (`review_runs`/`review_findings`), fail-soft. |
| `REVIEW_SESSION_PERSIST` | `true` | Персистить сессию PR в Postgres (crash-recovery). |
| `REVIEW_SESSION_TTL_HOURS` | `24` | TTL персистнутой сессии (часы). |
| `WEB_ADMIN_USER` | `""` | Basic-auth логин для `reviewer serve` / сервиса `web`; пусто = без auth. |
| `WEB_ADMIN_PASSWORD` | `""` | Basic-auth пароль; пусто = без auth. |

### Доска задач (опционально) — глобальный дефолт деплоя

Подключение к доске одинаково для всех репозиториев команды, поэтому задаётся **один раз** в env
reviewer-mcp, а не дублируется в `.review.yml` каждого репо. См.
[Политику per-repo и доску задач](#политика-per-repo-и-доска-задач).

| Переменная | Дефолт | Назначение |
|---|---|---|
| `TASK_BOARD_TYPE` | `""` | Тип доски: `yougile`, `jira`, … (выбирает плейбук скилла). |
| `TASK_BOARD_MCP` | `""` | Имя подключённого MCP-сервера доски (тулы LLM-стороны `mcp__<mcp>__*`). |
| `TASK_BOARD_KEY_PATTERN` | `""` | Регэксп ключа задачи, напр. `[A-Z]+-\d+`. |
| `TASK_BOARD_URL_TEMPLATE` | `""` | Шаблон ссылки на задачу, напр. `https://ru.yougile.com/team/<id>/#{code}`. |
| `TASK_BOARD_API_KEY` | `""` | **REST API-ключ** для server-side болк-синка (`sync_board`). Server-internal — клиентам не отдаётся. |
| `TASK_BOARD_API_BASE` | `""` | Base URL REST API; пусто → дефолт по типу (`yougile`: `https://yougile.com/api-v2`). |

> **Как получить `TASK_BOARD_API_KEY` (Yougile).** UI: `Ctrl + ~` (или ⚙ рядом с названием компании →
> «Настроить») → **API** → создать/скопировать ключ. REST: узнать `companyId` (`Ctrl + Alt + Q`, либо
> `POST /api-v2/auth/companies {login,password}`), затем `POST /api-v2/auth/keys {login,password,companyId}`.
> Ключ кладётся **только** в env reviewer-mcp (`~/.config/rag-reviewer/.env`), не в чат и не в конфиг клиента.

---

## CLI (справочник команд)

Команды запускаются как `uvx --from rag-reviewer <command>`, либо после `uv tool install rag-reviewer` /
`pip install -e ".[dev]"` — просто `reviewer`. Ставятся две точки входа: `reviewer` (CLI ниже) и
`reviewer-mcp` (MCP-сервер, запускается редактором/плагином).

| Команда | Аргументы | Опции | Что делает |
|---|---|---|---|
| `check` | — | — | Проверка готовности окружения (ключи, Postgres, Neo4j, GitHub). ✓/✗ по пунктам; exit 1 при проблеме. Квоту Voyage не тратит. |
| `init` | — | `--path FILE` (по умолчанию `~/.config/rag-reviewer/.env`), `--yes` (принять дефолты, CI-режим) | Интерактивный wizard записи `.env` (Voyage/GitHub + опциональные группы). |
| `install` | `[client]` | `--all`, `--list`, `--path FILE`, `--pin VERSION`, `--no-latest`, `--no-skills`, `--dry-run` | Прописать MCP-сервер (и скиллы) в AI-клиенты (кроссплатформенно). |
| `install-skills` | `[client]` | `--all`, `--list`, `--path FILE` | Установить только скиллы в глобальную папку клиента. |
| `update` | — | — | Проверить PyPI на новую версию `rag-reviewer`. |
| `index` | `<repo>` (путь к клону) | `--ref BRANCH` (git-ref для чтения; дефолт — первичная ветка), `--branch NAME` (ключ хранения; дефолт = `--ref`), `--repo OWNER/NAME` (дефолт из git `origin`) | Построить/обновить base-индекс ветки (вектора + граф). Раз, далее инкрементально. |
| `search` | `<query>` | `--repo OWNER/NAME` (дефолт `DEFAULT_REPO`), `--branch NAME` (дефолт — первичная) | Диагностический гибрид-поиск по base-индексу ветки. |
| `status` | `[path]` (дефолт `.`) | `--repo OWNER/NAME` (дефолт из git `origin`), `--branch NAME` (дефолт — все `REVIEW_BRANCHES`) | Здоровье/свежесть индекса vs HEAD клона. Квоту Voyage не тратит. |
| `migrate-branches` | — | — | Разово: переименовать legacy `ref="base"` → `base:<primary>` после апгрейда на мульти-бранч. |
| `serve` | — | `--host HOST` (дефолт `127.0.0.1`), `--port PORT` (дефолт `8000`) | Веб-админка наблюдаемости на хосте. |
| `reviewer-mcp` | — | — | MCP-сервер (stdio). Запускается плагином/редактором автоматически. |

Примеры:

```bash
# Первичная настройка
uvx --from rag-reviewer reviewer init
uvx --from rag-reviewer reviewer install --all
uvx --from rag-reviewer reviewer check

# Построить base-индекс (контекст всего репо для RAG + графа)
uvx --from rag-reviewer reviewer index /path/to/repo --ref main --repo owner/name
uvx --from rag-reviewer reviewer index /path/to/repo --ref master --repo owner/name   # вторая ветка

# Диагностика
uvx --from rag-reviewer reviewer search "token verification" --branch master
uvx --from rag-reviewer reviewer status /path/to/repo --branch dev

# Веб-админка
uvx --from rag-reviewer reviewer serve --host 127.0.0.1 --port 8000
```

Ревью работает и без предварительного `index` — тогда контекст ограничен диффом и overlay
(RAG/граф «тонкие»). Для полноценного анализа влияния на весь репозиторий запустите `index` по
целевой ветке.

---

## Скиллы (справочник с параметрами)

Скиллы — управляемые точки входа. С установленным плагином зовутся как `/rag-reviewer:<name>` в
Claude Code (`/rag-reviewer:` — namespace плагина; в других клиентах имя скилла такое же). Аргументы
передаются свободным текстом после имени (`$ARGUMENTS`).

### `reviewer_review-pr` — полное ревью PR

Оркестрирует три стадии (`prepare_review` → subagents → `publish_review`).

- **Аргументы:** PR как `owner/repo#N`, `owner/repo N` или GitHub PR URL. Флаг `--dry-run` — собрать
  и вернуть полный отчёт **без** постинга в GitHub.
- **MCP-тулы:** `prepare_review`, `search_code`, `get_related_symbols`, `read_file`,
  `get_definition`, `find_callers`, `get_changed_file_diff`, `publish_review`; плюс
  `index_task` / `get_task_context` / `search_tasks`, если подключена доска.
- **Поток:** prepare (PR + политика + units + конфиг доски) → fan-out одного субагента на файл →
  параллельно dimension-проходы **performance** / **maintainability** (+ **requirements**, если есть
  `TaskBrief`) → **verify** (отсев находок с `is_real=false`) → publish (gate/grounding/dedup/assemble).
  Если `prepare_review` вернул `status:"skipped"` (ветка вне `REVIEW_BRANCHES`) — стоп; draft-PR
  пропускаются, если не `REVIEW_SKIP_DRAFTS=false`.

### `reviewer_solve-task` — собрать контекст задачи и передать в разработку

Читает задачу (если есть ключ и доска), тянет связанные/похожие задачи и релевантный код, сводит
бриф и входит в brainstorming. Дисциплинирует сбор контекста — **код не пишет**.

- **Аргументы:** ключ задачи (напр. `PRI-4`, по `key_pattern`) **или** свободное описание (напр.
  «add a logout endpoint»). Board-less режим: описание + поиск по коду.
- **MCP-тулы:** `get_board_config`, `index_task`, `get_task_context`, `search_tasks`,
  `search_codebase`, `related_symbols`, `callers`, `definition`, `get_pr_diff`; плюс подключённая
  доска (`mcp__<board>__*`) для чтения задачи.
- **Поток:** резолв конфига доски → идентификация задачи (ключ vs текст) → best-effort fail-open сбор
  контекста (граф задач, похожие задачи, релевантный код, ленивые диффы PR похожих задач) → бриф
  (Task / Related work / Relevant code / Constraints) → передача в `superpowers:brainstorming`.

### `reviewer_sync-codebase` — построить/обновить base-индекс

Тонкая обёртка над `reviewer index` (вектор + граф) из локального клона.

- **Аргументы (все опц.):** `--path <path>` (дефолт: CWD), `--ref <branch>` (дефолт: `main`),
  `--repo <owner/name>` (дефолт: из `git remote get-url origin`),
  `--backend <auto|scip|treesitter>` (дефолт: `auto`, задаёт `GRAPH_BACKEND`).
- **MCP-тулы:** нет — вызывает `uvx --from rag-reviewer reviewer index`.
- **Поток:** резолв входов → проверка пререквизитов (`uvx`, git-репо, `reviewer check`, Docker up) →
  индексация → опц. `reviewer search` для проверки → отчёт по чанкам/узлам/рёбрам и бэкенду графа.

### `reviewer_sync-tasks` — прогреть граф и вектор задач

Тонкий триггер server-side ETL-тула `sync_board` — reviewer сам перечисляет доску по REST, LLM не
передаёт текст задач (O(1) токенов независимо от размера доски).

- **Аргументы (все опц.):** `--board <name>` (одна доска/проект), `--limit <N>` (smoke-прогон;
  **отключает purge и продвижение курсора**), `--purge-orphaned` (удалить задачи, которых больше нет
  на доске; по умолчанию off), `--no-keep-with-prs` (с purge — удалять и задачи с PR-историей,
  по умолчанию защищены).
- **MCP-тулы:** `sync_board` (один вызов).
- **Поток:** маппинг аргументов → один `sync_board(...)` → печать counts-сводки (перечислено/изменено/
  заэмбеждено/без изменений/ошибок, purge, warnings). При `{"status":"error",...}` доска не настроена
  на сервере — задать `TASK_BOARD_*` в `~/.config/rag-reviewer/.env` и переподключить MCP.

### `reviewer_performance-review` — ревью только производительности

Смотрит дифф только на риски производительности (N+1, повторная работа, плохая асимптотика, нет
батчинга/кэша, блокирующий I/O, рост памяти).

- **Аргументы (standalone):** scope — `staged`, `unstaged`, незакоммиченное, branch-vs-base, коммит,
  сравнение веток, список файлов, PR-подобный объём. Если неясно — спрашивает. Внутри
  `reviewer_review-pr` запускается как dimension по диффам units.
- **MCP-тулы (в PR-конвейере):** `search_code`, `read_file`, `find_callers`, `get_related_symbols`,
  `get_definition`, `get_changed_file_diff`.
- **Вывод:** JSON `{"findings":[{category:"performance", severity, file, line, side, code_quote,
  message, suggestion, fix, confidence}]}`.

### `reviewer_maintainability-review` — ревью только сопровождаемости

Смотрит дифф только на риски сопровождаемости (лишняя сложность, плохая читаемость, дублирование,
слабое разделение ответственности, дрейф от конвенций репо).

- **Аргументы (standalone):** те же опции scope, что у performance-ревью. Внутри
  `reviewer_review-pr` — как dimension по диффам units.
- **MCP-тулы (в PR-конвейере):** `search_code`, `get_related_symbols`, `read_file`,
  `get_definition`, `find_callers`, `get_changed_file_diff`.
- **Вывод:** JSON `{"findings":[{category:"maintainability", severity, file, line, side, code_quote,
  message, suggestion, fix, confidence}]}`.

### `reviewer_ask` — обоснованный Q&A по коду

Отвечает на свободный вопрос о коде с цитатами (`path:line`), используя RAG + граф. Для онбординга /
объяснения подсистемы — **не** для ревью PR. Нужен построенный base-индекс.

- **Аргументы:** свободный вопрос (напр. «где аутентификация», «как работает свежесть индекса»,
  «объясни ретрив», «как устроено…»).
- **MCP-тулы:** `search_codebase`, `related_symbols`, `callers`, `definition`; плюс harness
  `Read`/`Grep`/`Glob`.
- **Поток:** резолв repo/branch → `search_codebase` → опц. расширение графом → ответ с Evidence-списком
  цитат `path:line`.

---

## MCP-тулы (справочник)

Сервер `reviewer-mcp` отдаёт 20 тулов. Тулы PR-сессии требуют активного `prepare_review` для того же
`(repo, pr)` в том же запущенном сервере; остальные — session-less.

### Жизненный цикл ревью

| Тул | Сигнатура | Что делает / возвращает |
|---|---|---|
| `prepare_review` | `(repo: str, pr: int)` | Открыть сессию PR: досинк base-индекса, overlay PR, политика, per-file units. Возвращает мету PR + политику + units (или `{"status":"skipped"}` для нетрекаемой целевой ветки). |
| `publish_review` | `(repo, pr, summary, findings: list[dict], dry_run=False, task_key=None)` | Детерминированный хвост: gate → grounding → dedup → inline/summary → пост в GitHub → история → очистка overlay. `dry_run=true` возвращает отчёт без постинга; `task_key` линкует PR к задаче при реальной публикации. |

Поле находки: `{category, severity(low|medium|high|critical), file, line, side(RIGHT|LEFT),
code_quote, message, suggestion, fix:{start_line,end_line,replacement}|null, confidence:0..1}`.

### Тулы PR-сессии (требуют `prepare_review`)

| Тул | Сигнатура | Что делает |
|---|---|---|
| `search_code` | `(repo, pr, query: str)` | Гибрид-поиск по `base ∪ overlay`. |
| `get_related_symbols` | `(repo, pr, node_id: str)` | Соседи графа (calls/implementations) узла `path#fqn`. |
| `read_file` | `(repo, pr, path, start=1, end=400)` | Исходник файла на HEAD PR (1-based, inclusive). |
| `get_definition` | `(repo, pr, symbol: str)` | Определение символа (граф → индекс → семантический фолбэк). |
| `find_callers` | `(repo, pr, node_id: str)` | Прямые вызывающие узла `path#fqn` (impact-анализ). |
| `get_changed_file_diff` | `(repo, pr, path: str)` | Unified diff другого изменённого файла этого PR. |

### Session-less тулы (Q&A, `solve-task`)

| Тул | Сигнатура | Что делает |
|---|---|---|
| `search_codebase` | `(repo, query, top_k=10, branch=None, include_tests=False)` | Гибрид-поиск по base-индексу репо; с номерами строк, дедуп, тесты исключены по умолчанию. |
| `related_symbols` | `(repo, node_id, branch=None)` | Соседи графа (calls/implements/tests) символа. |
| `callers` | `(repo, node_id, branch=None)` | Входящие `CALLS` узла `path#fqn`. |
| `definition` | `(repo, symbol, branch=None)` | Определение символа (граф → индекс → семантический фолбэк). |
| `get_pr_diff` | `(repo, number: int)` | Unified diff любого (исторического) PR; кап, fail-soft. |

### Задачи / доски

| Тул | Сигнатура | Что делает |
|---|---|---|
| `sync_board` | `(board=None, limit=None, purge_orphaned=False, keep_with_prs=True)` | Server-side ETL: перечислить доску по REST, нормализовать в `TaskBrief`, проиндексировать. Инкрементально по watermark; O(1) токенов. |
| `index_task` | `(task: dict)` | Индексировать один `TaskBrief` в граф + вектор (идемпотентно). |
| `index_tasks_batch` | `(tasks: list[dict])` | То же для списка, одним вызовом Voyage. |
| `search_tasks` | `(query, top_k=5)` | Семантически похожие задачи из индекса. |
| `get_task_context` | `(key: str)` | Граф-контекст: задача, её PR, связанные задачи и их PR, затронутый код. |
| `purge_orphaned_tasks` | `(active_keys: list[str], keep_with_prs=True)` | Удалить задачи, которых больше нет на доске (с PR-историей защищены по умолчанию). |
| `get_board_config` | `()` | Deploy-wide конфиг доски (`TASK_BOARD_*`); фолбэк для `sync-tasks`/`solve-task`. Креды **не** отдаёт. |

---

## Использование плагина

С установленным плагином и Claude Code, открытым в корне репо, вызовите скилл:

```text
/rag-reviewer:reviewer_review-pr owner/repo#42        # ревью PR (prepare → subagents → publish)
/rag-reviewer:reviewer_review-pr owner/repo#42 --dry-run   # собрать отчёт без постинга
/rag-reviewer:reviewer_sync-codebase --ref main      # построить/обновить вектор + граф
/rag-reviewer:reviewer_sync-tasks                    # прогреть граф задач (server-side ETL)
/rag-reviewer:reviewer_solve-task PRI-4             # собрать контекст задачи и передать в разработку
/rag-reviewer:reviewer_ask как работает свежесть индекса   # обоснованный Q&A по коду
```

Типичный сценарий:

```bash
git clone https://github.com/ORG/REPO /tmp/REPO
reviewer index /tmp/REPO --ref main --repo ORG/REPO   # построить базу+граф
reviewer index /tmp/REPO --ref master --repo ORG/REPO # опц. вторая ветка (REVIEW_BRANCHES=main,master)
# в Claude Code: /rag-reviewer:reviewer_review-pr ORG/REPO#42   # ревью PR #42
```

---

## Политика per-repo и доска задач

Файл `.review.yml` в **целевой (base) ветке** репозитория переопределяет env-дефолты (PR не может
ослабить собственное ревью):

```yaml
categories: { correctness: true, security: true, performance: true, style: false, requirements: true }
severity_threshold: medium
min_confidence: 0.5
paths: { ignore: ["**/migrations/**", "vendor/**"] }
max_comments: 25

# Контекст задачи (опц.): читать задачу с доски и проверять соответствие требованиям.
# Доску (MCP) подключает пользователь на стороне сессии Claude Code; плагин её не бандлит.
task_board:
  type: yougile          # yougile | jira — выбирает плейбук скилла
  mcp: yougile           # имя подключённого MCP-сервера доски (тулы зовутся mcp__<mcp>__*)
  key_pattern: "[A-Z]+-\\d+"   # опц.; подходит Yougile PRI-34/ID-34 и Jira PROJ-123
  # url_template: "https://ru.yougile.com/team/<teamId>/#{code}"  # опц.; ссылка на задачу в сводке
```

**Блок `task_board` — глобальный дефолт деплоя, а не per-repo требование.** Подключение к доске
одинаково для всех репозиториев команды, поэтому настраивается **один раз** в `.env` reviewer-mcp
(`TASK_BOARD_TYPE` / `TASK_BOARD_MCP` / `TASK_BOARD_KEY_PATTERN` / `TASK_BOARD_URL_TEMPLATE`), и
каждый репо его наследует — `.review.yml` ради доски не нужен. Блок `task_board` в `.review.yml` репо
**переопределяет** дефолт для этого репо; пустой `task_board:` **выключает** доску для него.
`review-pr` читает это через политику; `solve-task` — через тул `get_board_config` (и board-MCP на
стороне LLM) как фолбэк, когда в локальном `.review.yml` блока нет.

**Болк-синк задач — server-side, не LLM (`sync_board`).** Скилл `sync-tasks` — тонкий триггер: один
вызов `sync_board(board, limit, purge_orphaned, keep_with_prs)`, и сервер сам ходит на доску по
**REST** (`reviewer/tasks/boards/`, за интерфейсом `TaskBoardProvider`; yougile — референс),
нормализует каждую задачу в `TaskBrief` в Python и индексирует через батч-индексатор. LLM не передаёт
текст задач → синк стоит O(1) токенов независимо от размера доски. Инкрементальность — timestamp-
watermark на доску в `index_meta` (`ref="tasks:<board>"`): повторный синк трогает ~0 задач; `--limit`
отключает purge и продвижение курсора. Креды REST-доски живут только в env reviewer-mcp
(`TASK_BOARD_API_KEY` / `TASK_BOARD_API_BASE`). Это разворачивает инвариант «reviewer Python никогда
не трогает доску» **только для болк-синка** — одиночное чтение задачи в `solve-task` / `review-pr`
по-прежнему идёт через board-MCP на стороне LLM. Граф задач (`:Task`) глобален — одна задача может
охватывать PR из нескольких микросервисных репозиториев.

**Граф и RAG по задачам.** Прочитанная задача индексируется в граф (Neo4j: узлы `:Task`/`:PR`, рёбра
`TASK_LINK`/`IMPLEMENTED_BY`/`TOUCHES`) и в вектор (Postgres, таблица `tasks`) тулом `index_task`. При
ревью агент видит связанные задачи и их PR/код через `get_task_context`, а похожие по смыслу — через
`search_tasks`; при публикации PR автоматически линкуется к задаче (`task_key` в `publish_review`).
Канонический ключ узла — сквозной код доски (Yougile `ID-N` / Jira key), прочие коды (Yougile `PRI-N`)
хранятся как `aliases`, поэтому PR по любому коду резолвится в один узел.

---

## Эксплуатация и веб-админка

### Диагностика и устойчивость

```bash
reviewer check          # ✓/✗ по ключам, Postgres, Neo4j, GitHub; exit 1 при проблеме
```

- Транзиентные ошибки Voyage/LLM (HTTP 429/5xx) ретраятся с экспоненциальным backoff и jitter.
- Ошибка анализа одного файла не прерывает ревью — файл помечается как неудачный и попадает в сводку.
- Эфемерный overlay `pr:N` удаляется из Postgres автоматически по окончании `publish_review` (и при
  сбое prepare — fail-soft).

### Свежесть base-индекса

`reviewer index` фиксирует SHA проиндексированного ref в `index_meta`. При каждом `prepare_review`
сверяется этот SHA с `base_sha` PR: при расхождении автоматически досинхронизируются чанки изменившихся
файлов через GitHub compare API (без пересборки всего индекса). Граф (Neo4j) **также** инкрементально
досинхронизируется в этом шаге (tree-sitter, repo-scoped, входящие `CALLS`-рёбра из неизменённых
вызывающих сохраняются, fail-soft). Полная точность графа (рёбра `IMPLEMENTS`, все `CALLS`)
восстанавливается ручным `reviewer index` с SCIP.

### Веб-админка наблюдаемости

Каждый `publish_review` пишет прогон в Postgres (`review_runs` / `review_findings`): репо/PR, модель,
тайминги, статус, находки с вердиктами и фактом публикации. Запись **fail-soft** и гейтится
`REVIEW_HISTORY` (дефолт `true`). Стоимости в записи нет — LLM-вызовы идут по подписке Claude Code.
Админка (FastAPI + React/Vite SPA) показывает историю прогонов, агрегаты (% отсева gate, графики во
времени, находки по категориям/severity) и детали каждого прогона с drill-down.

```bash
# Через Docker (без ручных шагов) — сервис web сам собирает фронт и поднимает FastAPI:
docker compose up -d                 # Postgres + Neo4j + web-админка → http://127.0.0.1:8000

# На хосте (для разработки фронта):
pip install -e ".[web]"
(cd web/frontend && npm install && npm run build)
reviewer serve                       # http://127.0.0.1:8000 (опции: --host/--port)
# hot-reload фронта (отдельный терминал при запущенном serve):
cd web/frontend && npm run dev       # http://localhost:5173, /api проксируется на :8000
```

API: `GET /api/runs` (список с фильтрами repo/status, пагинация), `GET /api/runs/{id}`
(прогон + находки), `GET /api/runs/{id}/trace` (пошаговый трейс, forward-only — пусто для прогонов до
включения фичи), `GET /api/stats?days=N` (агрегаты).

### Капы и флаги

| Переменная | Дефолт | Назначение |
|---|---|---|
| `REVIEW_MAX_FILES` | 50 | максимум файлов .py на ревью; лишние — в сводку как пропущенные |
| `REVIEW_SKIP_DRAFTS` | `true` | не ревьюить draft-PR |
| `REVIEW_MAX_COMMENTS` | 25 | кап inline-комментариев на ревью |

---

## Пример ревью «от диффа до комментария»

PR удаляет «лишнюю», на первый взгляд, проверку в `rag/embedder.py`:

```diff
@@ def _embed(self, texts, input_type):
         items = sorted(data["data"], key=lambda item: item["index"])
         vectors = [item["embedding"] for item in items]
-
-        for i, vec in enumerate(vectors):
-            if len(vec) != self._dim:
-                raise RuntimeError(f"Эмбеддинг {i} ... ожидается {self._dim} ...")
         return vectors
```

Дифф выглядит безобидно («упростить»). Агент:
1. строит overlay из новой версии файла, ретривом и графом подтягивает связанный код (`_embed` вызывается из `embed_query`/`embed_documents`, те — из `Retriever.retrieve`, `ingest_file`, `_index_text`) и существующие тесты;
2. LLM понимает, что удалён fail-fast контракт размерности;
3. verify подтверждает, политика пропускает (severity=medium ≥ порога, confidence ≥ 0.5).

Итоговый inline-комментарий на PR:

> **[correctness/medium]** Удалена fail-fast проверка размерности эмбеддингов. Ломается тест `test_embed_dimension_mismatch_raises`. Векторы неверной размерности пройдут дальше в `Retriever.retrieve`, `ingest_file`, `_index_text`, где упадут позже с неинформативной ошибкой (или тихо деградируют при смене модели).
>
> 💡 _Предложение:_ вернуть проверку `len(vec) != self._dim` перед `return vectors`.

Обрати внимание: упоминание **конкретного существующего теста** и **вызывающих** — это результат RAG (поиск по базе) и графа (обход связей), а не только диффа.

> Агент **только комментирует** — он никогда не меняет и не откатывает код сам. Предложения могут приходить как **applyable** GitHub-блоки `suggestion` (кнопка «Apply»), но безопасно: блок ставится только когда модель даёт точную замену конкретного непрерывного диапазона строк диффа (диапазон целиком в RIGHT-части, без пересечений; иначе — текстовый совет). Поведение задаётся `REVIEW_SUGGESTIONS` (`apply`/`text`).

## Структура проекта

```
reviewer/
  config/      Settings (pydantic-settings): env → пороги ревью, хранилища, ветки, доска
  vcs/         VCSProvider + github.py (httpx) · diff.py (строки, доступные для inline)
  index/       chunker(tree-sitter) · embeddings(Voyage) · reranker · store(pgvector+pg_search/RRF) · freshness
  graph/       builder(tree-sitter call-graph) · scip(точный парсер SCIP) · backend(оркестратор бэкенда) · store(Neo4j)
  retrieval/   Retriever: гибрид + graph-expansion + rerank → ContextPack
  llm/         _retry.py (retry/backoff для Voyage)
  tools/       инструменты агента (search_code, get_related_symbols, read_file, get_definition, …)
  tasks/       нормализация TaskBrief · boards/ (TaskBoardProvider REST: yougile) · TaskService.index_batch
  agent/       state (ReviewUnit) · assemble · dedup
  mcp/         MCPReviewService: prepare/tool-вызовы/publish; управление сессиями
  services/    ReviewService.prepare: ingest PR, overlay, units
  policy/      ReviewPolicy: env-дефолты + .review.yml + гейтинг
  entrypoints/ cli.py (Click) · mcp_server.py (FastMCP, 20 тулов)
  install.py   reviewer init / install / install-skills (кроссплатформенная привязка клиентов)
  web/         FastAPI + React/Vite SPA — веб-админка наблюдаемости
  app.py       сборка зависимостей из Settings
plugin/        Claude Code-плагин (7 скиллов /rag-reviewer:reviewer_*)
docker-compose.yml   ParadeDB (pgvector+pg_search) + Neo4j + web-админка
```

## Тесты

```bash
.venv/bin/pytest -q                 # unit (быстрые, на фейках; внешние API не дёргают)
.venv/bin/pytest -m integration     # integration: нужны поднятые Postgres/Neo4j + ключ Voyage
.venv/bin/ruff check .              # линт (line-length 100, target py311)
```

`pytest` по умолчанию исключает integration-тесты (`addopts = -m 'not integration'`). Внешние сервисы
изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.

## Ограничения и заметки

- **Нет автотриггера.** Ревью не запускается на открытие/обновление PR — это ручной вызов скилла в Claude Code (нет GitHub App / webhook / CI из коробки).
- **v1 — только Python** (чанкер и SCIP-бэкенд `scip-python`). Другие языки — за теми же интерфейсами чанкера/`GraphIndexer`.
- **VCS — только GitHub**: `VCSProvider` реализован лишь для GitHub (абстракция под GitLab/Bitbucket есть, провайдеров нет).
- **Граф кода — два бэкенда** (`GRAPH_BACKEND=auto|scip|treesitter`):
  - **SCIP** (`@sourcegraph/scip-python`, npm): точный type-aware граф с рёбрами `CALLS` + `IMPLEMENTS`; требует `scip-python` в PATH; индексирует через временный git worktree.
  - **tree-sitter** (fallback): быстрый, без внешних зависимостей, только `CALLS` по имени.
  - Режим `auto` (по умолчанию): SCIP если найден, иначе tree-sitter; при ошибке SCIP — автооткат на tree-sitter с предупреждением, при `scip` — ошибка пробрасывается.
- **Авто-реиндекс графа инкрементальный, не полной точности.** На `prepare_review` при дрейфе SHA граф патчится по изменённым файлам (tree-sitter, repo-scoped); рёбра `IMPLEMENTS`, исходящие `CALLS` в неизменённые файлы и новые входящие `CALLS` восстанавливаются ручным `reviewer index` с SCIP.
- **Мульти-репо через `repo`-дискриминатор**: один деплой обслуживает N репозиториев, изолированных колонкой/свойством `repo` (`owner/name`) в Postgres и Neo4j; кросс-репо ретрива нет. Граф задач `:Task` намеренно глобален. Внутри репо каждая ветка имеет изолированный индекс (`ref="base:<branch>"`; `branch` на узлах `:Symbol`, уникальность `(repo, branch, id)`).
- **MCP-сессия в процессе.** Состояние между `prepare_review` и `publish_review` живёт в запущенном `reviewer-mcp` (`_Session` в `MCPReviewService`); оба вызова одного PR должны попасть в **тот же** сервер (смягчается `REVIEW_SESSION_PERSIST`).
- **Voyage free tier** = 3 RPM / 10K TPM: полная индексация большого репо троттлится (есть retry/backoff с jitter); привязка карты снимает лимит. Ревью одного PR (overlay + query-эмбеддинги) в лимит укладывается.
- **Стоимость LLM.** Ревью разворачивает Claude-субагентов на файл плюс dimension-проходы — это реальная стоимость токенов.
- **Поверхность ревью.** Inline — только на строках диффа; остальное — в сводку. Applyable `suggestion` ставится только при безопасных инвариантах (`apply`, точная замена, диапазон целиком в RIGHT, без пересечений), иначе — текст.
- **Капы GitHub API.** Список файлов PR пагинируется по 100; compare API для досинка базы отдаёт максимум 300 файлов — очень большие диффы усекаются.
- **`.review.yml` берётся из base-ветки** (по дизайну — PR не может ослабить собственное ревью), не из head PR.
- **Auth веб-админки опционален**: basic-auth включается только при `WEB_ADMIN_USER`/`WEB_ADMIN_PASSWORD`; по умолчанию слушает loopback в `docker-compose.yml`.
```

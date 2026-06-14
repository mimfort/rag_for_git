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
- [Быстрый старт](#быстрый-старт)
- [Использование (CLI)](#использование-cli)
- [Эксплуатация](#эксплуатация)
- [Пример ревью «от диффа до комментария»](#пример-ревью-от-диффа-до-комментария)
- [Конфигурация](#конфигурация)
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

  Хранилища поднимаются в Docker:  Postgres/ParadeDB (:5433)  ·  Neo4j (:7687)
  Внешние API:  Voyage (эмбеддинги voyage-code-3 + reranker rerank-2.5)
```

Кратко, кто за что отвечает:

| Часть | Модуль | Роль |
|---|---|---|
| VCS-провайдер | `reviewer/vcs/` | получить PR/дифф/файлы, запостить ревью; маппинг строк диффа; идемпотентность |
| Индекс (RAG) | `reviewer/index/` | чанкинг (tree-sitter), эмбеддинги (Voyage), хранилище (pgvector+BM25), свежесть |
| Граф кода | `reviewer/graph/` | построение рёбер `CALLS` + `IMPLEMENTS` (SCIP-бэкенд) или только `CALLS` (tree-sitter); оркестрация в `backend.py`; хранение и обход в Neo4j |
| Ретрив | `reviewer/retrieval/` | гибрид (RRF) + graph-expansion + Voyage rerank → контекст |
| Инструменты | `reviewer/tools/` | `search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`; `index_task` — индексирует задачу в граф и вектор; `search_tasks` — семантический поиск по задачам; `get_task_context` — граф/история задачи и связанных PR; `search_codebase` — session-less гибрид-поиск по base-индексу (для /solve-task) |
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
               → payload скиллу: юниты/политика/патчи
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
8. publish     GitHubProvider: один review = сводка + массив inline-комментариев
```

Ключевые свойства:
- **agentic RAG** — ретрив только засеивает контекст; LLM сам дотягивает нужный код тулзами.
- **graph + RAG вместе** — вектора находят «похожее по смыслу», граф добавляет «структурно связанное» (кто сломается), что эмбеддинги часто упускают.
- **inline только на строках диффа** — GitHub разрешает комментарии лишь на изменённых/контекстных строках хунка; остальное уходит в сводку (инвариант зашит в `assemble`).
- **идемпотентность** — каждый комментарий помечен скрытым фингерпринтом `<!-- ai-review:hash -->`; повторный прогон не плодит дубликаты.

## Свежесть индекса на «живом» репозитории

Код меняется с каждым push'ем, а полный реиндекс большого репо дорог. Решение — **стабильная база + content-hash дедуп + overlay на PR**:

- **`ref="base"`** — персистентный индекс целевой ветки. Обновляется инкрементально (`reviewer index`): чанкуются только изменённые файлы, эмбеддятся только чанки с новым `content_hash`.
- **`ref="pr:N"`** — эфемерный overlay: только изменённые файлы PR на его HEAD.
- **На запросе**: `retrieval = (base, где path ∉ изменённых) ∪ overlay`. То есть для изменённых файлов агент видит **новую** версию, для остального — стабильную базу. Это и есть условие «находить произвольный релевантный код по всему репо, но по актуальной версии».

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

Нужны: Docker, `uv` (`pip install uv`), ключ Voyage, GitHub-токен.

### Быстрая установка (рекомендуется, все платформы)

```bash
# 1) Инфраструктура
curl -O https://raw.githubusercontent.com/mimfort/rag_for_git/main/docker-compose.yml
docker compose up -d          # Postgres/ParadeDB (:5433) + Neo4j (:7687) + web-админка (:8000)

# 2) Ключи — создаёт ~/.config/rag-reviewer/.env из шаблона
uvx --from rag-reviewer reviewer init
#    заполните VOYAGE_API_KEY и GITHUB_TOKEN в этом файле

# 3) Прописать MCP-сервер (и скиллы) в ваш редактор/CLI
uvx --from rag-reviewer reviewer install --all   # автодетект клиентов + установка скиллов
#    или конкретный: reviewer install cursor|vscode|claude-code|windsurf|gemini|antigravity|mimo|opencode|kimi|trae|codex
#    скиллы ставятся в клиенты, которые их поддерживают (Gemini/Mimo/Kimi); --no-skills чтобы пропустить

# 4) Проверить
uvx --from rag-reviewer reviewer check

# Обновиться позже:
uvx --from rag-reviewer reviewer update
```

> **`reviewer install` кроссплатформенный** (Windows / macOS / Linux). Подставляет абсолютный
> путь к `uvx` автоматически — обёртка `bash -lc` не нужна. Ручные JSON-конфиги ниже используют
> `bash -lc` только для macOS/Linux; на Windows используйте `reviewer install` или указывайте
> `"command": "uvx"` с `"args": ["--from", "rag-reviewer@latest", "reviewer-mcp"]` напрямую.

Где взять ключи:
- **Voyage** (`VOYAGE_API_KEY`): https://dashboard.voyageai.com/ — есть бесплатный пул; привяжите карту, чтобы снять лимит 3 RPM / 10K TPM.
- **GitHub** (`GITHUB_TOKEN`): PAT с правами *Pull requests: Read and write* + *Contents: Read* (fine-grained) или scope `repo` (classic). Быстрый вариант: `gh auth token`.

`DEFAULT_REPO` (опц.) задаёт дефолтный `owner/name` — тогда `--repo` у CLI и тулов можно не указывать.

### 2. Ручная установка плагина (альтернатива)

Если вы предпочитаете прописать конфиг вручную, а не через `reviewer install`:

У каждого AI-инструмента свой конфиг-файл:

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

Формат записи по типу инструмента (macOS/Linux — на Windows используйте `reviewer install`):

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

#### Claude Code

Из любого проекта двумя командами:

```text
/plugin marketplace add mimfort/rag_for_git
/plugin install rag-reviewer@rag-reviewer-marketplace
```

Вы получаете:

- **Скиллы:** `/rag-reviewer:reviewer_review-pr`, `/rag-reviewer:reviewer_solve-task`, `/rag-reviewer:reviewer_sync-codebase`, `/rag-reviewer:reviewer_sync-tasks`
  (а также `/rag-reviewer:reviewer_maintainability-review` и `/rag-reviewer:reviewer_performance-review`).
- **MCP-сервер** `reviewer` с тулами: `prepare_review`, `publish_review`, `search_code`,
  `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`,
  `index_task`, `search_tasks`, `get_task_context`, `search_codebase`.

> Команда `/plugin` покажет, что `rag-reviewer` установлен и включён.

### 3. Глобальная установка скиллов (опционально)

Скиллы (`reviewer_review-pr`, `reviewer_solve-task`, `reviewer_sync-codebase`, `reviewer_sync-tasks`, `reviewer_performance-review`, `reviewer_maintainability-review`)
дают полный рабочий процесс ревью одной командой. Без них можно вызывать MCP-тулы напрямую,
но скиллы оборачивают их в управляемый сценарий.

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

## Использование

После `pip install -e .` доступны команды `reviewer` (CLI) и `reviewer-mcp` (MCP-сервер для плагина).

### CLI

```bash
# Создать ~/.config/rag-reviewer/.env из шаблона (заполнить VOYAGE_API_KEY + GITHUB_TOKEN).
# Флаги: --path FILE (другое расположение), --force (перезаписать существующий).
reviewer init

# Прописать MCP-сервер (и скиллы) в установленные AI-клиенты (кроссплатформенно).
# --all автодетектирует установленных клиентов; или укажи конкретный:
# cursor, claude-desktop, claude-code, vscode, windsurf, gemini, antigravity,
# mimo, opencode, kimi, trae, codex.
# Скиллы ставятся в клиенты, которые их поддерживают (Gemini/Mimo/Kimi); --no-skills чтобы пропустить.
# Флаги: --list, --dry-run, --path FILE, --pin VERSION, --no-latest, --no-skills.
reviewer install --all
reviewer install cursor

# Установить только скиллы в глобальную папку клиента (Gemini/Mimo/Kimi).
# --all автодетект; --list показать цели и каталоги; --path переопределить каталог.
reviewer install-skills --all

# Проверить готовность окружения: ключи, Postgres, Neo4j, GitHub.
# Выводит ✓/✗ по каждому пункту; exit 1 при любой проблеме. Квоту Voyage не тратит.
reviewer check

# Обновить rag-reviewer до последней версии с PyPI.
reviewer update

# Проиндексировать базу целевой ветки локального клона (вектора + граф).
# Делается один раз и обновляется инкрементально; даёт RAG/графу контекст всего репо.
# --repo можно опустить, если origin-remote является GitHub (owner/name дерайвится автоматически)
# или задан DEFAULT_REPO в .env.
reviewer index /path/to/repo --ref main --repo owner/name

# Диагностический гибрид-поиск по базе (проверить, что индекс работает).
reviewer search "token verification"
```

### Ревью через Claude Code-плагин

С установленным плагином (см. [Быстрый старт](#быстрый-старт)) и Claude Code, открытым в корне репо, вызовите скилл:

```text
/rag-reviewer:reviewer_review-pr owner/repo#42
```

Плагин вызывает `prepare_review` (через MCP), затем запускает subagents с инструментами поиска `search_code`, `get_related_symbols`, `read_file` и т.д., наконец `publish_review` (через MCP) постит результат в GitHub.

Типичный сценарий:

```bash
git clone https://github.com/ORG/REPO /tmp/REPO
reviewer index /tmp/REPO --ref main --repo ORG/REPO   # построить базу+граф
# в Claude Code: /rag-reviewer:reviewer_review-pr ORG/REPO#42   # ревью PR #42
```

> Ревью работает и без предварительного `index` — тогда контекст ограничен диффом и overlay (RAG/граф «тонкие»). Для полноценного анализа влияния на весь репозиторий запустите `index` по целевой ветке.

## Эксплуатация

### Диагностика и первый запуск

```bash
# Проверить готовность окружения: ключи, Postgres, Neo4j, GitHub.
# Выводит ✓/✗ по каждому пункту; exit 1 при любой проблеме.
reviewer check
```

Прогон без публикации: в скилле передайте `--dry-run` — `publish_review` соберёт отчёт, не постя в GitHub.

### Веб-админка наблюдаемости

Каждый `publish_review` записывает прогон в Postgres (таблицы `review_runs` / `review_findings`):
репозиторий/PR, модель, тайминги, статус, находки с вердиктами и фактом публикации. Запись
**fail-soft** (сбой лога не ломает ревью) и гейтится `REVIEW_HISTORY` (дефолт `true`). Стоимости
в записи нет — LLM-вызовы идут по подписке Claude Code.

Веб-админка (FastAPI + React/Vite SPA) показывает историю прогонов, агрегаты (% отсева gate,
графики во времени, находки по категориям/severity) и детали каждого прогона — с drill-down по
находкам.

**Через Docker (без ручных шагов).** Сервис `web` в `docker-compose.yml` сам собирает фронт
(multi-stage: node → python) и поднимает FastAPI, читая ту же БД, что пишет `publish_review`:

```bash
docker compose up -d                 # поднимает Postgres + Neo4j + web-админку
# открыть http://127.0.0.1:8000
```

**На хосте (для разработки фронта).** Альтернатива без Docker:

```bash
pip install -e ".[web]"
cd web/frontend && npm install && npm run build && cd -
reviewer serve                       # http://127.0.0.1:8000 (опции: --host/--port)

# hot-reload фронта (в отдельном терминале, при запущенном reviewer serve):
cd web/frontend && npm run dev       # http://localhost:5173, /api проксируется на :8000
```

API: `GET /api/runs` (список с фильтрами repo/status, пагинация), `GET /api/runs/{id}`
(прогон + находки), `GET /api/runs/{id}/trace` (пошаговый трейс), `GET /api/stats?days=N` (агрегаты).

> Трейс пишется только для **новых** прогонов (инструментация forward-only) — у прогонов,
> сделанных до включения фичи, вкладка «Трейс» покажет пустое состояние.

### Свежесть base-индекса

`reviewer index` фиксирует SHA проиндексированного ref в таблице `index_meta`. При каждом `prepare_review` сверяется этот SHA с `base_sha` PR: если есть расхождение — автоматически досинхронизирует чанки изменившихся файлов через GitHub compare API (без пересборки всего индекса). Граф кода (Neo4j) **также** инкрементально досинхронизируется в этом же шаге (tree-sitter, repo-scoped, входящие `CALLS`-рёбра из неизменённых вызывающих сохраняются, fail-soft). Полная точность графа (рёбра `IMPLEMENTS`, все `CALLS`) восстанавливается ручным `reviewer index` с SCIP.

### Капы и флаги

| Переменная | Дефолт | Назначение |
|---|---|---|
| `REVIEW_MAX_FILES` | 50 | максимум файлов .py на ревью; лишние — в сводку как пропущенные |
| `REVIEW_SKIP_DRAFTS` | `true` | не ревьюить draft-PR |
| `REVIEW_MAX_COMMENTS` | 25 | кап inline-комментариев на ревью |

### Устойчивость к ошибкам

- Транзиентные ошибки LLM (HTTP 429/5xx) ретраятся с экспоненциальным backoff.
- Ошибка анализа одного файла не прерывает ревью — файл помечается как неудачный и попадает в сводку.

### Несколько репозиториев (мульти-репо)

Один деплой (одна БД Postgres + Neo4j) обслуживает N репозиториев через `repo`-дискриминатор (`owner/name`): данные изолированы колонкой/свойством `repo` в Postgres (`chunks`, `index_meta`) и Neo4j (`:Symbol.repo`, составная уникальность `(repo, id)`). Каждое ревью выполняется в рамках своего репозитория — кросс-репо ретрив отсутствует.

Для индексации: `reviewer index <path> --repo owner/name` (или `owner/name` дерайвится автоматически из git remote `origin`, если это GitHub; либо задаётся `DEFAULT_REPO` в `.env`).

Граф задач (`:Task`) намеренно остаётся **глобальным** — одна задача может охватывать PR из нескольких микросервисных репозиториев.

> **Миграция с single-repo.** Если у вас уже есть данные в индексе, выполните `reviewer index --repo owner/name` один раз (или задайте `DEFAULT_REPO`), чтобы проставить `repo`-дискриминатор.

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

## Конфигурация

Всё ключевое — через `.env` (см. `.env.example` с комментариями). Главное:

| Переменная | Назначение |
|---|---|
| `VOYAGE_API_KEY` | ключ Voyage (эмбеддинги + ранжирование) |
| `GITHUB_TOKEN` | токен GitHub (PAT: *Pull requests: RW* + *Contents: R*) |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | модель Voyage и размерность (= колонке `vector(N)`; смена ⇒ реиндекс) |
| `RERANK_MODEL` | модель реранкера Voyage |
| `REVIEW_SEVERITY_THRESHOLD` | мин. важность: `low/medium/high/critical` |
| `REVIEW_MIN_CONFIDENCE` | отбрасывать findings ниже уверенности (0..1) |
| `REVIEW_MAX_COMMENTS` | кап inline-комментариев |
| `REVIEW_CATEGORIES` | CSV вайтлист категорий (пусто = все) |
| `REVIEW_SUGGESTIONS` | `apply` = applyable `suggestion`-блоки (кнопка «Apply»), `text` = только текстовые советы |
| `REVIEW_MAX_FILES` | кап файлов PR; лишние — в сводку как пропущенные |
| `REVIEW_OUTPUT_LANGUAGE` | язык текста находок в публикуемом ревью (дефолт `ru`) |
| `REVIEW_SKIP_DRAFTS` | `true` = не ревьюить draft-PR |
| `REVIEW_HISTORY` | `true` = сохранять историю прогонов в Postgres |
| `PG_DSN`, `NEO4J_URI/USER/PASSWORD`, `GITHUB_TOKEN` | подключения и доступ |

Эфемерный overlay `pr:N` удаляется из Postgres автоматически по окончании `publish_review`.

**Политика per-repo.** Файл `.review.yml` в **целевой ветке** репозитория переопределяет env-дефолты (PR не может ослабить собственное ревью):

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
  key_pattern: "[A-Z]+-\\d+"   # опц.; дефолт такой же (подходит Yougile PRI-34/ID-34 и Jira PROJ-123)
  # url_template: "https://ru.yougile.com/team/<teamId>/#{code}"  # опц.; ссылка на задачу в сводке
  #   Yougile: {code} → код проекта PRI-N (он в URL-фрагменте «#PRI-4»); {key} = канонический ID-N, в URL не идёт
```

**Контекст задачи (фаза 2).** Если задан `task_board` и в PR (title/body/ветка) найден ключ
по `key_pattern`, скилл читает задачу с доски через её MCP и запускает проверку соответствия —
новая категория находок `requirements` (включена по умолчанию). Находки без конкретной строки
диффа уходят в сводку. Доска не настроена, ключ не найден или MCP недоступен → ревью работает
как обычно, без деградации.

**Граф и RAG по задачам (фаза 3).** Прочитанная задача индексируется в граф (Neo4j: узлы
`:Task`/`:PR`, рёбра `TASK_LINK`/`IMPLEMENTED_BY`/`TOUCHES`) и в векторный индекс (Postgres,
таблица `tasks`) тулом `index_task`. При ревью агент видит связанные задачи и их PR/код через
`get_task_context`, а похожие по смыслу — через `search_tasks`; при публикации PR
автоматически линкуется к задаче. Скилл `/reviewer_sync-tasks` прогревает корпус задач с доски (идемпотентно,
с backoff под Voyage). Канонический ключ узла — сквозной код доски (Yougile `ID-N` / Jira key),
прочие коды (Yougile `PRI-N`) хранятся как `aliases`, поэтому PR по любому коду резолвится в один
узел. Neo4j/доска недоступны → контекст пуст с предупреждением, ревью продолжается.

**Скилл `/reviewer_solve-task` (фаза 4).** `/reviewer_solve-task <ключ | свободный текст>` собирает контекст под
задачу — читает задачу с доски (если есть ключ и подключена доска), тянет связанные и похожие
задачи с их PR и кодом (`get_task_context`/`search_tasks`), ищет релевантный код по формулировке
(`search_codebase` — session-less гибрид-поиск по base-индексу: BM25+ANN → graph-expansion →
rerank), сводит **только релевантное** в структурированный бриф и передаёт его в штатный цикл
разработки (`brainstorming` → план → реализация). Скилл дисциплинирует сбор контекста, не заменяя
разработку; fail-open — без доски/графа работает по формулировке и коду.

## Структура проекта

```
reviewer/
  config/      Settings (pydantic-settings): env → пороги ревью, хранилища
  vcs/         VCSProvider + github.py (httpx) · diff.py (строки, доступные для inline)
  index/       chunker(tree-sitter) · embeddings(Voyage) · reranker · store(pgvector+pg_search/RRF) · freshness
  graph/       builder(tree-sitter call-graph) · scip(точный парсер SCIP) · backend(оркестратор бэкенда) · store(Neo4j)
  retrieval/   Retriever: гибрид + graph-expansion + rerank → ContextPack
  llm/         _retry.py (retry/backoff для Voyage)
  tools/       инструменты агента (search_code, get_related_symbols, read_file, get_definition, …)
  agent/       state (ReviewUnit) · assemble · dedup
  mcp/         MCPReviewService: prepare/tool-вызовы/publish; MCP-сервер (server.py)
  services/    ReviewService.prepare: ingest PR, overlay, units
  policy/      ReviewPolicy: env-дефолты + .review.yml + гейтинг
  entrypoints/ cli.py (index / search / check / serve)
  web/         FastAPI + React/Vite SPA — веб-админка наблюдаемости
  app.py       сборка зависимостей из Settings
plugin/        Claude Code-плагин (скиллы /rag-reviewer:reviewer_review-pr, reviewer_solve-task, reviewer_sync-codebase, reviewer_sync-tasks)
docker-compose.yml   ParadeDB (pgvector+pg_search) + Neo4j + web-админка
```

## Тесты

```bash
.venv/bin/pytest -q                 # unit (быстрые, на фейках; внешние API не дёргают)
.venv/bin/pytest -m integration     # integration: нужны поднятые Postgres/Neo4j + ключ Voyage
```

Внешние сервисы изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.

## Ограничения и заметки

- **v1 — только Python** (чанкер и граф). Другие языки — за тем же интерфейсом `GraphIndexer`/чанкера.
- **Граф кода — два бэкенда** (настройка `GRAPH_BACKEND=auto|scip|treesitter`):
  - **SCIP** (`@sourcegraph/scip-python`, npm): точный type-aware граф с рёбрами `CALLS` + `IMPLEMENTS`; требует `scip-python` в PATH; индексирует через временный git worktree.
  - **tree-sitter** (fallback): быстрый, без внешних зависимостей, только `CALLS` по имени.
  - Режим `auto` (по умолчанию): SCIP если найден, иначе tree-sitter; при ошибке SCIP — автооткат на tree-sitter с предупреждением.
- **Voyage free tier** = 3 RPM / 10K TPM: полная индексация большого репо требует привязанной карты (бесплатные 200M токенов сохраняются) либо медленной инкрементальной индексации; в коде есть retry/backoff.
- **VCS** — пока GitHub; GitLab/др. добавляются реализацией `VCSProvider`.
- **Запуск** — пока CLI; webhook-сервис добавляется как точка входа (ядро уже библиотека).

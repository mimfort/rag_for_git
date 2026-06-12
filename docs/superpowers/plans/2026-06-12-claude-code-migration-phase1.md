# Фаза 1: MCP-сервер reviewer-mcp + плагин rag-reviewer + скилл /review-pr — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перенести мозг ревью из OpenRouter/LangGraph в Claude Code: Python-инфраструктура оборачивается в MCP-сервер (`prepare_review`/поиск/`publish_review`), ревью оркеструется скиллом `/review-pr`; после эвал-гейта OpenRouter-слой удаляется.

**Architecture:** «Толстый MCP, тонкий скилл» (спека `docs/superpowers/specs/2026-06-12-claude-code-migration-design.md`). MCP-сервер — FastMCP поверх существующего `build_components(settings)`; сессия ревью PR кэшируется в процессе сервера между `prepare_review` и `publish_review`. Все инварианты (commentable lines, `_can_apply`, fingerprint, gate, кап) остаются детерминированным Python. Скиллы — английские, выдача — язык из `.review.yml`.

**Tech Stack:** Python 3.11, `mcp` (FastMCP), существующие psycopg/pgvector, neo4j, voyageai, httpx; Claude Code plugin (`.claude-plugin/plugin.json`, `skills/`, `.mcp.json`).

**Ветка:** `feat/claude-code-phase1` (через git worktree). Коммиты — Conventional Commits на русском, **без Co-Authored-By**.

**Прогон тестов:** `.venv/bin/pytest -q` (unit), линт `.venv/bin/ruff check .`.

---

## Карта файлов

| Файл | Роль |
|---|---|
| `reviewer/services/review_service.py` (modify) | Извлечь `prepare()` → `PreparedReview`; `run_review` использует её |
| `reviewer/agent/assemble.py` (create) | Чистая сборка inline/summary (`assemble_review`, `ground_line`) — общая для nodes и MCP |
| `reviewer/agent/nodes.py` (modify) | `make_assemble_node` делегирует в `assemble.py` |
| `reviewer/policy/policy.py` (modify) | Поле `output_language` (+ `.review.yml`) |
| `reviewer/config/settings.py` (modify) | `review_output_language: str = "ru"` |
| `reviewer/mcp/__init__.py`, `reviewer/mcp/service.py` (create) | `MCPReviewService`: prepare/search/publish + сессии по PR |
| `reviewer/entrypoints/mcp_server.py` (create) | FastMCP-обвязка + `main()` (console script `reviewer-mcp`) |
| `.claude-plugin/plugin.json`, `plugin/.mcp.json` (create) | Манифест плагина `rag-reviewer` |
| `plugin/skills/review-pr/SKILL.md` + `references/*.md` (create) | Оркестрация ревью (EN) |
| `plugin/skills/performance-review/SKILL.md`, `plugin/skills/maintainability-review/SKILL.md` (create) | Адаптация скиллов из заготовок (EN, наша findings-схема) |
| `tests/mcp/*` (create), `tests/agent/test_assemble.py` (create) | Тесты новых модулей |
| `docs/plans/2026-06-12-claude-code-eval.md` (create) | Протокол эвал-гейта |
| Выпиливание (Task 12): `reviewer/llm/openrouter.py`, `budget.py`, `agent/analyzer.py`, `prompts.py`, `graph.py`, `nodes.py` и др. | Только после прохождения эвал-гейта |

---

### Task 1: Язык выдачи — `output_language` в Settings и ReviewPolicy

**Files:**
- Modify: `reviewer/config/settings.py`
- Modify: `reviewer/policy/policy.py`
- Test: `tests/policy/test_policy.py`

- [ ] **Step 1: Написать падающий тест**

В `tests/policy/test_policy.py` добавить (стиль существующих тестов файла):

```python
def test_output_language_default_and_yaml_override():
    p = ReviewPolicy.from_yaml("severity_threshold: medium")
    assert p.output_language == "ru"

    p2 = ReviewPolicy.from_yaml("output_language: en")
    assert p2.output_language == "en"
```

Если в файле используется `ReviewPolicy.load(settings, yaml)` вместо `from_yaml` — повторить локальный паттерн файла (посмотреть соседние тесты).

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/policy/test_policy.py::test_output_language_default_and_yaml_override -q`
Expected: FAIL (`AttributeError: output_language` или `TypeError`)

- [ ] **Step 3: Реализация**

`reviewer/config/settings.py` — добавить поле рядом с другими `review_*`:

```python
review_output_language: str = "ru"  # язык текста находок в публикуемом ревью
```

`reviewer/policy/policy.py` — в dataclass `ReviewPolicy` добавить поле:

```python
output_language: str = "ru"
```

В `from_settings` проставить `output_language=settings.review_output_language`; в `load`/`from_yaml` (метод, разбирающий YAML) добавить:

```python
if "output_language" in data:
    policy.output_language = str(data["output_language"])
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/policy -q`
Expected: PASS (все)

- [ ] **Step 5: Коммит**

```bash
git add reviewer/config/settings.py reviewer/policy/policy.py tests/policy/test_policy.py
git commit -m "feat(policy): настраиваемый язык выдачи ревью (output_language)"
```

---

### Task 2: Извлечь `ReviewService.prepare()` → `PreparedReview`

Сейчас подготовка (fetch PR → синк base → отбор файлов → overlay → policy → юниты) зашита внутрь `run_review` (`reviewer/services/review_service.py:113–362`). Извлекаем её в отдельный метод, который переиспользуют и `run_review`, и MCP.

**Files:**
- Modify: `reviewer/services/review_service.py`
- Test: `tests/services/test_review_service.py` (существующие — регресс) + новый тест там же

- [ ] **Step 1: Изучить существующие фейки**

Прочитать `tests/services/test_review_service.py` — там фейки Store/VCS/Embedder для `run_review`. Новый тест строить на них же.

- [ ] **Step 2: Написать падающий тест**

Добавить в `tests/services/test_review_service.py`:

```python
def test_prepare_returns_units_policy_and_overlay():
    """prepare() собирает юниты, policy и overlay без запуска LLM-графа."""
    svc, fakes = _make_service()  # переиспользовать локальную фабрику фейков файла
    prepared = svc.prepare("o", "r", 7, vcs_provider=fakes.vcs)
    assert prepared.prq.number == 7
    assert prepared.overlay_ref == "pr:7"
    assert [u.path for u in prepared.units] == ["a.py"]
    assert prepared.patches["a.py"] is not None
    assert prepared.policy.max_comments > 0
    assert prepared.changed_paths == ["a.py"]
```

(Имена фабрики/фейков взять из файла; если фабрики нет — собрать сервис так же, как ближайший тест `run_review`.)

- [ ] **Step 3: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_review_service.py::test_prepare_returns_units_policy_and_overlay -q`
Expected: FAIL (`AttributeError: prepare`)

- [ ] **Step 4: Реализация — dataclass + извлечение**

В `reviewer/services/review_service.py` добавить:

```python
@dataclass
class PreparedReview:
    """Подготовленный контекст ревью PR: всё, что нужно analyze-этапу и публикации."""
    prq: PullRequest
    units: list[ReviewUnit]
    policy: ReviewPolicy
    patches: dict[str, str | None]       # path -> unified diff
    sources: dict[str, str]              # path -> head-версия файла
    changed_paths: list[str]
    changed_node_ids: list[str]
    skipped_paths: list[str]
    overlay_ref: str                     # "pr:<n>"
    vcs: VCSProvider
```

Метод `prepare`:

```python
def prepare(self, owner: str, repo: str, pr_number: int,
            vcs_provider: VCSProvider | None = None) -> PreparedReview:
    """Подготовка ревью: PR → синк base → отбор файлов → overlay → policy → юниты."""
```

Тело — **перенос существующих блоков** из `run_review` (без изменения логики):
получение PR (`vcs.get_pull_request`), синхронизация base-индекса
(`get_index_meta`/`update_base`/`set_index_meta`), `_select_changed_files`,
загрузка head-исходников (`get_file_at_ref`), `build_overlay(...)`,
загрузка `.review.yml` из base-ветки и `ReviewPolicy.load`,
конструирование `ReviewUnit`-ов и `changed_node_ids` — ровно те же строки,
что сейчас формируют входы `Deps`. Старый overlay чистить в начале:

```python
self.components.store.delete_ref(f"pr:{pr_number}")  # self-healing перед построением
```

`run_review` переписать так, чтобы он вызывал `self.prepare(...)` и дальше строил `Deps`
из полей `PreparedReview`. Draft-check остаётся в `run_review` (но `prq` берёт из
`prepared.prq`). Finally-cleanup overlay в `run_review` не трогать.

- [ ] **Step 5: Прогнать все тесты сервиса и агента (регресс)**

Run: `.venv/bin/pytest tests/services tests/agent -q`
Expected: PASS (все существующие + новый)

- [ ] **Step 6: Коммит**

```bash
git add reviewer/services/review_service.py tests/services/test_review_service.py
git commit -m "refactor(services): извлечён ReviewService.prepare() -> PreparedReview"
```

---

### Task 3: Извлечь сборку ревью в `reviewer/agent/assemble.py`

Логика assemble (`reviewer/agent/nodes.py:73–173`: `_sane_line`, ранжирование, `_can_apply`, fingerprint-фильтр, кап, inline/summary) нужна и узлу LangGraph (до выпиливания), и MCP `publish_review`. Извлекаем в чистую функцию.

**Files:**
- Create: `reviewer/agent/assemble.py`
- Modify: `reviewer/agent/nodes.py`
- Test: `tests/agent/test_assemble.py` (create)

- [ ] **Step 1: Написать падающие тесты**

`tests/agent/test_assemble.py`:

```python
from reviewer.agent.assemble import AssembledReview, assemble_review, ground_line
from reviewer.vcs.base import Finding

PATCH = "@@ -1,3 +1,4 @@\n line\n+x = 1\n line2\n line3"


def _f(line=2, **kw):
    d = dict(category="correctness", severity="high", file="a.py", line=line,
             side="RIGHT", message="bug", suggestion=None, confidence=0.9)
    d.update(kw)
    return Finding(**d)


def test_inline_on_commentable_line_and_cap():
    res = assemble_review(
        [_f(), _f(message="bug2"), _f(message="bug3")],
        patches={"a.py": PATCH},
        sources={"a.py": "line\nx = 1\nline2\nline3\n"},
        existing_fps=set(),
        max_comments=2,
        suggestions_mode="off",
    )
    assert len(res.inline_comments) == 2          # кап сработал
    assert "bug3" in res.summary                  # переполнение ушло в сводку


def test_existing_fingerprint_skipped():
    f = _f()
    res = assemble_review([f], patches={"a.py": PATCH},
                          sources={"a.py": "line\nx = 1\nline2\nline3\n"},
                          existing_fps={f.fingerprint()},
                          max_comments=10, suggestions_mode="off")
    assert res.inline_comments == [] and f.message not in res.summary


def test_line_outside_diff_goes_to_summary():
    res = assemble_review([_f(line=99)], patches={"a.py": PATCH},
                          sources={"a.py": "line\nx = 1\nline2\nline3\n"},
                          existing_fps=set(), max_comments=10, suggestions_mode="off")
    assert res.inline_comments == [] and "bug" in res.summary


def test_ground_line_unique_quote_wins():
    src = "a\nx = compute()\nb\n"
    assert ground_line(src, "x = compute()", 9) == 2
    assert ground_line(src, None, 9) == 9
    assert ground_line(src, "nope", 9) == 9
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `.venv/bin/pytest tests/agent/test_assemble.py -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.agent.assemble`)

- [ ] **Step 3: Реализация `assemble.py`**

Создать `reviewer/agent/assemble.py`. Содержимое — **перенос** кода из `nodes.py`
(`_SEVERITY_RANK`-сортировка, `_sane_line`, `_range_in_diff`, `_overlaps`, `_can_apply`,
формирование body/suggestion-блока, fingerprint-фильтр, кап) в виде чистой функции:

```python
"""Сборка итогового ревью из верифицированных находок (без I/O)."""
from dataclasses import dataclass, field

from reviewer.vcs.base import Finding, InlineComment
from reviewer.vcs.diff import commentable_lines


@dataclass
class AssembledReview:
    inline_comments: list[InlineComment]
    summary: str                      # markdown-секция «прочие находки» ('' если пусто)
    skipped_existing: int = 0         # отфильтровано по fingerprint
    moved_to_summary: int = 0
    capped: int = 0
    findings_rows: list[dict] = field(default_factory=list)  # для review_findings


def ground_line(source: str | None, code_quote: str | None, line: int | None) -> int | None:
    """Уточнить номер строки по точной цитате из исходника (анти-галлюцинация)."""
    if not source or not code_quote:
        return line
    needle = code_quote.strip()
    if not needle:
        return line
    hits = [i for i, ln in enumerate(source.splitlines(), 1) if ln.strip() == needle]
    if not hits or line in hits:
        return line
    return hits[0]


def assemble_review(verified: list[Finding], *, patches: dict[str, str | None],
                    sources: dict[str, str], existing_fps: set[str],
                    max_comments: int, suggestions_mode: str) -> AssembledReview:
    ...
```

Внутри — та же логика, что в `make_assemble_node` (nodes.py:73–173): commentable по
`commentable_lines(patches[f.file])`, сортировка `(-severity, -confidence)`,
fingerprint-скип, `_can_apply` → ```suggestion```-блок, иначе inline-текст на
валидной строке, иначе строка в `summary`. В каждый body добавляется
`\n\n<!-- ai-review:{f.fingerprint()} -->` (как сейчас). `findings_rows` заполнять
словарями `{file, line, category, severity, confidence, fingerprint, message, inline}`.

В `nodes.py` `make_assemble_node` переписать на вызов `assemble_review(...)`
(узел оставляет себе только I/O: `deps.vcs.list_existing_fingerprints` с fail-soft
и склейку итогового summary state).

Примечание: если после улучшений D2 в `analyzer.py` уже есть хелпер грунтовки по
`code_quote` — перенести его сюда под именем `ground_line` (поведение из теста —
эталон), в `analyzer.py` заменить на импорт.

- [ ] **Step 4: Прогнать новые и старые тесты**

Run: `.venv/bin/pytest tests/agent -q`
Expected: PASS (включая `test_nodes_fail_soft.py` — поведение узла не изменилось)

- [ ] **Step 5: Коммит**

```bash
git add reviewer/agent/assemble.py reviewer/agent/nodes.py tests/agent/test_assemble.py
git commit -m "refactor(agent): сборка ревью выделена в assemble.py (общая для nodes и MCP)"
```

---

### Task 4: Зависимость `mcp` + каркас `MCPReviewService.prepare_review`

**Files:**
- Modify: `pyproject.toml`
- Create: `reviewer/mcp/__init__.py`, `reviewer/mcp/service.py`
- Test: `tests/mcp/test_service.py` (create)

- [ ] **Step 1: Добавить зависимость и установить**

В `pyproject.toml` в `dependencies` добавить строку:

```toml
"mcp>=1.9,<2",
```

Run: `.venv/bin/pip install -e ".[dev]" -q && .venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print('ok')"`
Expected: `ok`

- [ ] **Step 2: Написать падающий тест**

`tests/mcp/__init__.py` — пустой. `tests/mcp/test_service.py` (фейки — по образцу `tests/services/test_review_service.py`, переиспользовать оттуда):

```python
from reviewer.mcp.service import MCPReviewService


def test_prepare_review_returns_units_and_caches_session():
    svc = _make_mcp_service()  # обёртка: Settings + фейковые Components + фейковый VCS
    out = svc.prepare_review("o/r", 7)
    assert out["pr"]["number"] == 7
    assert out["policy"]["max_comments"] > 0
    assert out["policy"]["output_language"] == "ru"
    unit = out["units"][0]
    assert unit["path"] == "a.py"
    assert isinstance(unit["commentable_right"], list)
    assert ("o/r", 7) in svc._sessions


def test_search_without_prepare_raises_clear_error():
    svc = _make_mcp_service()
    try:
        svc.search_code("o/r", 7, "query")
        assert False, "ожидали ошибку"
    except ValueError as e:
        assert "prepare_review" in str(e)
```

`_make_mcp_service()` — локальная фабрика в тесте: собирает `MCPReviewService(settings, components, vcs_factory=lambda o, r: fake_vcs)`.

- [ ] **Step 3: Прогнать — падает**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.mcp`)

- [ ] **Step 4: Реализация `reviewer/mcp/service.py`**

```python
"""Сервисный слой MCP-сервера: prepare/search/publish поверх Components.

Состояние сессии (PreparedReview + инструменты) живёт в процессе сервера
между вызовами prepare_review и publish_review одного PR.
"""
import logging
from dataclasses import dataclass

from reviewer.app import Components
from reviewer.config.settings import Settings
from reviewer.services.review_service import PreparedReview, ReviewService
from reviewer.tools.code_tools import ToolContext, make_tools
from reviewer.vcs.diff import commentable_lines

log = logging.getLogger(__name__)


@dataclass
class _Session:
    prepared: PreparedReview
    tools: dict  # имя инструмента -> StructuredTool (реюз memoization из make_tools)


class MCPReviewService:
    def __init__(self, settings: Settings, components: Components,
                 vcs_factory=None) -> None:
        self.settings = settings
        self.components = components
        self._review_service = ReviewService(settings, components)
        self._vcs_factory = vcs_factory  # для тестов; None = GitHubProvider
        self._sessions: dict[tuple[str, int], _Session] = {}

    def prepare_review(self, repo: str, pr: int) -> dict:
        owner, name = repo.split("/", 1)
        vcs = self._vcs_factory(owner, name) if self._vcs_factory else None
        prepared = self._review_service.prepare(owner, name, pr, vcs_provider=vcs)
        ctx = self._tool_context(prepared)
        tools = {t.name: t for t in make_tools(ctx)}
        self._sessions[(repo, pr)] = _Session(prepared, tools)
        return self._prepared_payload(prepared)

    def _session(self, repo: str, pr: int) -> _Session:
        s = self._sessions.get((repo, pr))
        if s is None:
            raise ValueError(
                f"Сессия для {repo}#{pr} не найдена — сначала вызови prepare_review")
        return s
```

`_tool_context(prepared)` строит `ToolContext` **ровно так же**, как его строит
текущий analyze-путь (найти место конструирования `ToolContext` в
`reviewer/agent/analyzer.py` / `review_service.py` и повторить: retriever, graph,
store, overlay_ref, changed_paths, changed_node_ids, head_sha, patches, vcs,
лимиты из settings). `_prepared_payload`:

```python
    def _prepared_payload(self, p: PreparedReview) -> dict:
        units = []
        for u in p.units:
            lines = commentable_lines(p.patches.get(u.path))
            units.append({
                "path": u.path,
                "patch": p.patches.get(u.path),
                "commentable_right": sorted(lines["RIGHT"]),
                "commentable_left": sorted(lines["LEFT"]),
            })
        return {
            "pr": {"number": p.prq.number, "title": p.prq.title, "body": p.prq.body,
                   "base_sha": p.prq.base_sha, "head_sha": p.prq.head_sha,
                   "base_ref": p.prq.base_ref, "draft": p.prq.draft},
            "policy": {"severity_threshold": p.policy.severity_threshold,
                       "min_confidence": p.policy.min_confidence,
                       "max_comments": p.policy.max_comments,
                       "categories": p.policy.categories,
                       "ignore": p.policy.ignore,
                       "output_language": p.policy.output_language},
            "units": units,
            "skipped_paths": p.skipped_paths,
            "skip_drafts": self.settings.review_skip_drafts,
            "suggestions_mode": self._suggestions_mode(),
        }

    def _suggestions_mode(self) -> str:
        # то же значение, что сейчас кладётся в Deps.suggestions_mode
        # (найти фактическое имя поля settings по месту конструирования Deps)
        return getattr(self.settings, "review_suggestions_mode", "apply")
```

Заготовка `search_code` для второго теста:

```python
    def search_code(self, repo: str, pr: int, query: str) -> str:
        s = self._session(repo, pr)
        return s.tools["search_code"].invoke({"query": query})
```

- [ ] **Step 5: Прогнать**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add pyproject.toml reviewer/mcp tests/mcp
git commit -m "feat(mcp): MCPReviewService.prepare_review с сессией PR"
```

---

### Task 5: MCP-инструменты поиска (паритет с tool-loop агента)

Экспонируем все шесть инструментов агента (`reviewer/tools/code_tools.py:54–124`) через делегирование в `make_tools` (мемоизация сохраняется).

**Files:**
- Modify: `reviewer/mcp/service.py`
- Test: `tests/mcp/test_service.py`

- [ ] **Step 1: Написать падающий тест**

```python
def test_search_tools_delegate_to_make_tools():
    svc = _make_mcp_service()
    svc.prepare_review("o/r", 7)
    assert isinstance(svc.search_code("o/r", 7, "token check"), str)
    assert isinstance(svc.get_related_symbols("o/r", 7, "a.py#f"), str)
    assert isinstance(svc.read_file("o/r", 7, "a.py", 1, 10), str)
    assert isinstance(svc.get_definition("o/r", 7, "f"), str)
    assert isinstance(svc.find_callers("o/r", 7, "a.py#f"), str)
    assert isinstance(svc.get_changed_file_diff("o/r", 7, "a.py"), str)
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/mcp/test_service.py::test_search_tools_delegate_to_make_tools -q`
Expected: FAIL (`AttributeError`)

- [ ] **Step 3: Реализация**

В `MCPReviewService` добавить методы-делегаты (имена аргументов инструментов сверить
с сигнатурами в `code_tools.py`):

```python
    def get_related_symbols(self, repo: str, pr: int, node_id: str) -> str:
        return self._session(repo, pr).tools["get_related_symbols"].invoke({"node_id": node_id})

    def read_file(self, repo: str, pr: int, path: str, start: int = 1, end: int = 400) -> str:
        return self._session(repo, pr).tools["read_file"].invoke(
            {"path": path, "start": start, "end": end})

    def get_definition(self, repo: str, pr: int, symbol: str) -> str:
        return self._session(repo, pr).tools["get_definition"].invoke({"symbol": symbol})

    def find_callers(self, repo: str, pr: int, node_id: str) -> str:
        return self._session(repo, pr).tools["find_callers"].invoke({"node_id": node_id})

    def get_changed_file_diff(self, repo: str, pr: int, path: str) -> str:
        return self._session(repo, pr).tools["get_changed_file_diff"].invoke({"path": path})
```

- [ ] **Step 4: Прогнать**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): инструменты поиска кода (паритет с tool-loop агента)"
```

---### Task 6: MCP `publish_review` — детерминированный хвост

Gate → grounding → dedup → assemble → публикация → история → очистка overlay. Findings приходят словарями в схеме analyze-промпта (`category, severity, file, line, code_quote, message, suggestion, fix{start_line,end_line,replacement}, confidence`).

**Files:**
- Modify: `reviewer/mcp/service.py`
- Test: `tests/mcp/test_publish.py` (create)

- [ ] **Step 1: Написать падающие тесты**

`tests/mcp/test_publish.py` (фейковый VCS записывает вызовы; фейковая history):

```python
RAW = {
    "category": "correctness", "severity": "high", "file": "a.py", "line": 2,
    "code_quote": "x = 1", "message": "bug here", "suggestion": None,
    "fix": None, "confidence": 0.9,
}


def test_publish_posts_inline_and_records_history():
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = svc.publish_review("o/r", 7, summary="Overall fine", findings=[RAW])
    assert report["posted"] is True
    assert vcs.published[0]["comments"][0]["path"] == "a.py"
    assert history.runs[0]["pr_number"] == 7
    assert ("o/r", 7) not in svc._sessions          # сессия закрыта


def test_publish_dry_run_does_not_post_but_reports():
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
    assert report["posted"] is False and vcs.published == []
    assert report["inline"][0]["line"] == 2


def test_publish_gates_low_severity_and_grounds_line():
    svc, vcs, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    low = dict(RAW, severity="low")                       # ниже threshold=medium
    wrong_line = dict(RAW, line=99)                       # грунтовка по code_quote → 2
    report = svc.publish_review("o/r", 7, summary="s",
                                findings=[low, wrong_line], dry_run=True)
    assert report["dropped_by_gate"] == 1
    assert report["inline"][0]["line"] == 2


def test_publish_cleans_overlay_even_on_vcs_error():
    svc, vcs, _ = _make_mcp_service_with_publish(vcs_fails=True)
    svc.prepare_review("o/r", 7)
    report = svc.publish_review("o/r", 7, summary="s", findings=[RAW])
    assert report["posted"] is False and report["error"]
    assert "pr:7" in svc.components.store.deleted_refs
```

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/mcp/test_publish.py -q`
Expected: FAIL (`AttributeError: publish_review`)

- [ ] **Step 3: Реализация**

В `reviewer/mcp/service.py`:

```python
def _finding_from_dict(d: dict) -> Finding:
    fix = d.get("fix") or {}
    return Finding(
        category=d.get("category", "correctness"),
        severity=d.get("severity", "medium"),
        file=d["file"],
        line=d.get("line"),
        side=d.get("side", "RIGHT"),
        message=d.get("message", ""),
        suggestion=d.get("suggestion"),
        confidence=float(d.get("confidence", 0.5)),
        fix_start=fix.get("start_line"),
        fix_end=fix.get("end_line"),
        replacement=fix.get("replacement"),
    )
```

Метод:

```python
    def publish_review(self, repo: str, pr: int, summary: str,
                       findings: list[dict], dry_run: bool = False) -> dict:
        s = self._session(repo, pr)
        p = s.prepared
        parsed = []
        for d in findings:
            f = _finding_from_dict(d)
            f.line = ground_line(p.sources.get(f.file), d.get("code_quote"), f.line)
            parsed.append(f)
        kept = [f for f in parsed if p.policy.gate(f)]
        deduped = dedup_findings(kept)
        try:
            existing = p.vcs.list_existing_fingerprints(pr)
        except Exception:
            log.warning("Не удалось получить существующие fingerprint", exc_info=True)
            existing = set()
        asm = assemble_review(deduped, patches=p.patches, sources=p.sources,
                              existing_fps=existing,
                              max_comments=p.policy.max_comments,
                              suggestions_mode=self._suggestions_mode())
        full_summary = summary + ("\n\n" + asm.summary if asm.summary else "")
        error, posted = "", False
        if not dry_run:
            try:
                p.vcs.publish_review(pr, p.prq.head_sha, full_summary, asm.inline_comments)
                posted = True
            except Exception as e:
                error = str(e)
        run_id = self._record_history(repo, pr, p, parsed, deduped, asm,
                                      dry_run=dry_run, posted=posted, error=error)
        self._cleanup(repo, pr)
        return {
            "posted": posted, "dry_run": dry_run, "error": error, "run_id": run_id,
            "summary": full_summary,
            "inline": [{"path": c.path, "line": c.line, "side": c.side, "body": c.body}
                       for c in asm.inline_comments],
            "dropped_by_gate": len(parsed) - len(kept),
            "deduped": len(kept) - len(deduped),
            "already_posted": asm.skipped_existing,
            "moved_to_summary": asm.moved_to_summary,
        }

    def _cleanup(self, repo: str, pr: int) -> None:
        self._sessions.pop((repo, pr), None)
        try:
            self.components.store.delete_ref(f"pr:{pr}")
        except Exception:
            log.warning("Не удалось очистить overlay pr:%s", pr, exc_info=True)
```

`_record_history` — fail-soft, через существующий `ReviewHistory.record_run`
(см. `reviewer/web/history.py:73–161`; как `_record_history` в `review_service.py`,
но `usage`/`total_cost`/`steps` → `None`, `model="claude-code"`; findings-строки —
из `asm.findings_rows`). Импорты: `from reviewer.agent.assemble import assemble_review, ground_line`,
`from reviewer.agent.dedup import dedup_findings`, `from reviewer.vcs.base import Finding`.

- [ ] **Step 4: Прогнать**

Run: `.venv/bin/pytest tests/mcp -q && .venv/bin/ruff check reviewer/mcp`
Expected: PASS, без замечаний линта

- [ ] **Step 5: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_publish.py
git commit -m "feat(mcp): publish_review — gate, grounding, dedup, assemble, история, cleanup"
```

---

### Task 7: FastMCP-обвязка и console script `reviewer-mcp`

**Files:**
- Create: `reviewer/entrypoints/mcp_server.py`
- Modify: `pyproject.toml`
- Test: `tests/mcp/test_server.py` (create)

- [ ] **Step 1: Написать падающий тест**

`tests/mcp/test_server.py` — in-process вызов через FastMCP:

```python
import asyncio

from reviewer.entrypoints.mcp_server import create_server


def test_server_registers_all_tools():
    server = create_server(_make_mcp_service())   # фабрика фейков из test_service.py
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"prepare_review", "search_code", "get_related_symbols",
                     "read_file", "get_definition", "find_callers",
                     "get_changed_file_diff", "publish_review"}


def test_prepare_review_callable_via_mcp():
    server = create_server(_make_mcp_service())
    result = asyncio.run(server.call_tool("prepare_review", {"repo": "o/r", "pr": 7}))
    assert result  # структурированный ответ с units
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/mcp/test_server.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Реализация `reviewer/entrypoints/mcp_server.py`**

```python
"""MCP-сервер reviewer-mcp: RAG + граф кода + публикация ревью для Claude Code."""
import logging

from mcp.server.fastmcp import FastMCP

from reviewer.mcp.service import MCPReviewService

log = logging.getLogger(__name__)


def create_server(service: MCPReviewService) -> FastMCP:
    mcp = FastMCP("reviewer-mcp")

    @mcp.tool()
    def prepare_review(repo: str, pr: int) -> dict:
        """Prepare a PR review session: sync base index, build overlay, load policy.
        Returns PR metadata, policy and review units (path, patch, commentable lines).
        Call this first, before any other tool."""
        return service.prepare_review(repo, pr)

    @mcp.tool()
    def search_code(repo: str, pr: int, query: str) -> str:
        """Hybrid semantic+lexical code search over the repo index (base + PR overlay)."""
        return service.search_code(repo, pr, query)

    @mcp.tool()
    def get_related_symbols(repo: str, pr: int, node_id: str) -> str:
        """Code-graph neighbors (calls/implementations) of a symbol node_id 'path#fqn'."""
        return service.get_related_symbols(repo, pr, node_id)

    @mcp.tool()
    def read_file(repo: str, pr: int, path: str, start: int = 1, end: int = 400) -> str:
        """Read exact source lines of a file at the PR head revision."""
        return service.read_file(repo, pr, path, start, end)

    @mcp.tool()
    def get_definition(repo: str, pr: int, symbol: str) -> str:
        """Find a symbol definition (graph -> index -> semantic fallback)."""
        return service.get_definition(repo, pr, symbol)

    @mcp.tool()
    def find_callers(repo: str, pr: int, node_id: str) -> str:
        """List direct callers of a symbol node_id."""
        return service.find_callers(repo, pr, node_id)

    @mcp.tool()
    def get_changed_file_diff(repo: str, pr: int, path: str) -> str:
        """Unified diff of another changed file in the same PR."""
        return service.get_changed_file_diff(repo, pr, path)

    @mcp.tool()
    def publish_review(repo: str, pr: int, summary: str,
                       findings: list[dict], dry_run: bool = False) -> dict:
        """Deterministic publish tail: policy gate, line grounding, dedup,
        inline/summary split, suggestion invariants, fingerprint idempotency,
        comment cap, GitHub review post, history record, overlay cleanup.
        With dry_run=true nothing is posted; the full report is returned."""
        return service.publish_review(repo, pr, summary, findings, dry_run)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from reviewer.app import build_components
    from reviewer.config.settings import Settings

    settings = Settings()
    components = build_components(settings)
    create_server(MCPReviewService(settings, components)).run()  # stdio


if __name__ == "__main__":
    main()
```

В `pyproject.toml` добавить console script:

```toml
[project.scripts]
reviewer = "reviewer.entrypoints.cli:cli"
reviewer-mcp = "reviewer.entrypoints.mcp_server:main"
```

- [ ] **Step 4: Прогнать + переустановить**

Run: `.venv/bin/pip install -e ".[dev]" -q && .venv/bin/pytest tests/mcp -q && .venv/bin/ruff check .`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/entrypoints/mcp_server.py pyproject.toml tests/mcp/test_server.py
git commit -m "feat(mcp): FastMCP-сервер reviewer-mcp (stdio) и console script"
```

---

### Task 8: Манифест плагина `rag-reviewer`

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `plugin/.mcp.json`
- Create: `plugin/README.md`

- [ ] **Step 1: Создать манифест**

`.claude-plugin/plugin.json`:

```json
{
  "name": "rag-reviewer",
  "version": "0.1.0",
  "description": "Agentic PR review: hybrid RAG + code graph via MCP, review skills for Claude Code",
  "skills": "./plugin/skills/",
  "mcpServers": "./plugin/.mcp.json"
}
```

`plugin/.mcp.json` (сервер запускается из venv репозитория; `.env` подхватывается из cwd):

```json
{
  "mcpServers": {
    "reviewer": {
      "command": "${CLAUDE_PLUGIN_ROOT}/.venv/bin/python",
      "args": ["-m", "reviewer.entrypoints.mcp_server"],
      "cwd": "${CLAUDE_PLUGIN_ROOT}"
    }
  }
}
```

`plugin/README.md`:

```markdown
# rag-reviewer — Claude Code plugin

Плагин = этот репозиторий. Требования: выполненная установка
`python -m venv .venv && .venv/bin/pip install -e ".[dev]"`, заполненный `.env`,
поднятые ParadeDB/Neo4j (`docker compose up -d`), построенный base-индекс
(`reviewer index /path/to/repo --ref main`).

Подключение: `claude --plugin-dir /path/to/rag_for_git` (или установка через
локальный marketplace). Скиллы: `/rag-reviewer:review-pr`,
`/rag-reviewer:performance-review`, `/rag-reviewer:maintainability-review`.

Headless: claude --plugin-dir . -p "/rag-reviewer:review-pr owner/repo#123 --dry-run" --permission-mode bypassPermissions
```

- [ ] **Step 2: Проверить JSON и загрузку плагина**

Run: `.venv/bin/python -c "import json; json.load(open('.claude-plugin/plugin.json')); json.load(open('plugin/.mcp.json')); print('ok')"`
Expected: `ok`

Smoke (вручную, требует claude CLI): `claude --plugin-dir . -p "list available skills" --output-format text | head -20` — в выводе упомянут `rag-reviewer`.

- [ ] **Step 3: Коммит**

```bash
git add .claude-plugin plugin/.mcp.json plugin/README.md
git commit -m "feat(plugin): манифест Claude Code плагина rag-reviewer с MCP-сервером"
```

---

### Task 9: Скилл `/review-pr` (EN) + промпты-референсы

**Files:**
- Create: `plugin/skills/review-pr/SKILL.md`
- Create: `plugin/skills/review-pr/references/analyze-prompt.md`
- Create: `plugin/skills/review-pr/references/verify-prompt.md`

- [ ] **Step 1: Создать SKILL.md**

`plugin/skills/review-pr/SKILL.md`:

````markdown
---
description: Review a GitHub pull request with the RAG + code-graph pipeline (reviewer MCP server). Use when the user asks to review a PR ("review PR 123", "заревьюй PR", a PR URL). Requires ParadeDB/Neo4j running and a built base index.
---

# PR Review Pipeline

Orchestrate a full PR review using the `reviewer` MCP server tools. The deterministic
tail (policy gate, line validation, dedup, idempotency, comment cap, publishing) is
handled by `publish_review` — your job is analysis quality, not formatting rules.

## Inputs

Parse from $ARGUMENTS: target PR as `owner/repo#N`, `owner/repo N`, or a GitHub PR URL.
`--dry-run` flag → pass `dry_run=true` to publish_review and show the report instead
of posting.

## Pipeline

1. **Prepare.** Call `prepare_review(repo, pr)`. If `pr.draft` is true and
   `skip_drafts` is true, stop and tell the user. Note `policy.output_language` —
   ALL finding messages, suggestions and the summary MUST be written in that language.
2. **Analyze (fan-out).** For each unit in `units`, dispatch a subagent (Task tool,
   run independent subagents in parallel; batch units if there are more than ~10) with:
   - the contents of `references/analyze-prompt.md` (read it once, include verbatim);
   - the unit's `path` and `patch`, the PR `title`/`body`;
   - the repo/pr identifiers so the subagent can call MCP tools
     (`search_code`, `get_related_symbols`, `read_file`, `get_definition`,
     `find_callers`, `get_changed_file_diff`).
   Each subagent returns a JSON object `{"findings": [...]}` (schema in the prompt).
3. **Dimensions (parallel with step 2).** Dispatch two whole-diff subagents:
   - performance: instructions in `../performance-review/SKILL.md` (Goal, Review
     method, Severity guidance sections);
   - maintainability: instructions in `../maintainability-review/SKILL.md`.
   Both must return the same findings JSON schema (category `performance` /
   `maintainability`).
4. **Verify.** Collect all findings into one numbered list. Dispatch one subagent
   with `references/verify-prompt.md`, the findings list and the diffs. It returns
   `{"verdicts": [{"index": N, "is_real": true|false}]}`. Drop findings with
   `is_real=false`. If the verifier fails or returns malformed output, KEEP all
   findings (recall-safe).
5. **Publish.** Compose a short review summary (2-5 sentences, in
   `policy.output_language`): what the PR does, overall assessment, key risks.
   Call `publish_review(repo, pr, summary, findings, dry_run)`. Report to the user:
   posted/dry-run, inline count, what was gated/deduped/moved to summary, run_id.

## Failure handling

- A failed analyze subagent must not abort the run: continue with the other units
  and mention the skipped file in the summary.
- If `prepare_review` fails, surface its error text to the user as-is (it contains
  the remediation hint, e.g. "docker compose up -d").
- Never post comments yourself via gh/git — only through `publish_review`.
````

- [ ] **Step 2: Создать analyze-prompt**

`plugin/skills/review-pr/references/analyze-prompt.md` — английский перенос правил `ANALYZE_SYSTEM` (`reviewer/agent/prompts.py:1–70`; сверить и сохранить все анти-шумовые правила оригинала):

````markdown
You are a senior code reviewer analyzing ONE changed file of a pull request.

Rules:
- Review ONLY the changed lines of the diff and their direct consequences.
  Pre-existing issues in untouched code are out of scope.
- Use tools BEFORE claiming cross-file effects: `search_code` for usages,
  `get_related_symbols` / `find_callers` for impacted callers, `read_file` for
  exact context, `get_changed_file_diff` for other files of this PR.
- Targeted search: make each tool call answer ONE specific question about the
  diff; do not browse. Stop calling tools once you can decide.
- Anti-noise: do NOT report missing error handling, missing tests, missing docs,
  style, or hypothetical edge cases unless the diff makes the failure concrete.
  Do not invent issues to fill quota; an empty findings list is a valid result.
- Every finding MUST carry an exact `code_quote` — one line copied verbatim from
  the NEW version of the file (it is used to ground the line number).
- `fix` block only when you are sure of the exact replacement for a line range
  in the new file; otherwise use `suggestion` text or null.

Return ONLY a JSON object (no prose around it):

```json
{"findings": [{
  "category": "correctness|security|performance|maintainability|style",
  "severity": "low|medium|high|critical",
  "file": "<path of the reviewed file>",
  "line": <line number in the NEW file or null>,
  "code_quote": "<exact line from the new file>",
  "message": "<what is wrong and why it matters>",
  "suggestion": "<short advice or null>",
  "fix": {"start_line": N, "end_line": M, "replacement": "<new code>"} | null,
  "confidence": 0.0-1.0
}]}
```

Write `message` and `suggestion` in the output language given by the orchestrator.
````

- [ ] **Step 3: Создать verify-prompt**

`plugin/skills/review-pr/references/verify-prompt.md` (перенос `VERIFY_SYSTEM`, `prompts.py:72–109`):

````markdown
You are a skeptical review verifier. Input: a numbered list of candidate findings
and the PR diffs. Your job is to kill FALSE POSITIVES, not to find new issues.

For each finding decide `is_real`:
- false if the quoted code or line does not exist in the new version of the file;
- false if the finding talks about unchanged code outside the diff;
- false if the claim is speculative ("might", "could") with no concrete failure path;
- verify doubtful claims with tools (`read_file`, `search_code`, `find_callers`)
  before rejecting them;
- when still in doubt, answer true (recall-safe: a human will re-check).

Return ONLY: {"verdicts": [{"index": N, "is_real": true|false}]}
````

- [ ] **Step 4: Проверка структуры**

Run: `find plugin/skills/review-pr -type f` → три файла; `head -5 plugin/skills/review-pr/SKILL.md` → frontmatter с `description`.

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/review-pr
git commit -m "feat(plugin): скилл /review-pr — оркестрация ревью с фан-аутом сабагентов"
```

---

### Task 10: Адаптированные скиллы performance-review и maintainability-review

Источник — `~/Downloads/skills/{performance-review,maintainability-review}/SKILL.md`. Сохраняем методологию (Goal/Review method/Severity guidance), меняем: вывод `::code-comment` → наша findings-схема; добавляем режим работы внутри `/review-pr`.

**Files:**
- Create: `plugin/skills/performance-review/SKILL.md`
- Create: `plugin/skills/maintainability-review/SKILL.md`

- [ ] **Step 1: Создать performance-review**

`plugin/skills/performance-review/SKILL.md`:

````markdown
---
description: Review code changes only for performance and efficiency risks (N+1 queries, repeated work, bad asymptotics, missing batching/caching, blocking I/O, memory growth). Use when the user explicitly asks for a performance review of a diff/PR.
---

# Performance Review

## Scope

Standalone: ask the user which diff to review (uncommitted vs branch-vs-base) if
unclear; with an explicit scope (PR, commit, files) use exactly that.
Inside `/review-pr`: the orchestrator provides the whole PR diff — review that.

## Goal

Only performance and efficiency risks. Ignore style, architecture and general
correctness unless they materially affect performance. Prioritize:
- N+1 queries and repeated remote calls
- unnecessary loops or repeated work; bad asymptotics on hot paths
- redundant serialization, parsing, allocations, copies
- missing batching, caching, pagination or streaming where the diff makes the risk likely
- blocking I/O or CPU-heavy work on latency-sensitive paths
- memory growth and large payload handling

## Method

1. Read the diff first. 2. Open only nearby code needed to judge whether the path
is performance-sensitive (in `/review-pr` use the MCP tools: `read_file`,
`search_code`, `find_callers`). 3. Prefer concrete findings over speculation;
state assumptions explicitly. 4. If a path is probably not hot, do not invent issues.

## Severity

- critical/high: likely severe latency, throughput or resource regression on an important path
- medium: meaningful inefficiency or scaling risk worth fixing
- low: worthwhile optimization note, not a blocker

## Output

Return ONLY the findings JSON used by the review pipeline, with
`"category": "performance"`:

```json
{"findings": [{"category": "performance", "severity": "...", "file": "...",
  "line": N, "code_quote": "...", "message": "...", "suggestion": "...",
  "fix": null, "confidence": 0.0-1.0}]}
```

Standalone runs may additionally render the findings as a readable list.
If there are no meaningful findings, return {"findings": []} and say so.
````

- [ ] **Step 2: Создать maintainability-review**

`plugin/skills/maintainability-review/SKILL.md` — та же структура; Goal/heuristics/What-not-to-flag перенести из заготовки (`unnecessary branching/nesting, mixed-responsibility functions, duplication, pass-through wrappers, hidden side effects, naming harming readability, drift from repo conventions`; не флагать: стилистику при ясном коде, legacy вне скоупа, гипотетические абстракции, formatter-ниты, переименования без вреда пониманию). Repository Context: перед project-practice находками читать `CLAUDE.md`/contributor docs (в `/review-pr` — через `read_file`). Output — findings JSON с `"category": "maintainability"`, секция Severity: critical/high = сильный рост сложности/драйф конвенций, medium = заметное усложнение, стоит упростить до мержа, low = желательная чистка. Suggestion обязан содержать конкретную упрощающую альтернативу, сохраняющую поведение.

- [ ] **Step 3: Проверка**

Run: `grep -L "description:" plugin/skills/*/SKILL.md`
Expected: пустой вывод (frontmatter везде)

- [ ] **Step 4: Коммит**

```bash
git add plugin/skills/performance-review plugin/skills/maintainability-review
git commit -m "feat(plugin): скиллы performance/maintainability-review (адаптация под findings-схему)"
```

---

### Task 11: Эвал-гейт — сравнение с OpenRouter-бейзлайном

Бейзлайн: замер D2 в `docs/plans/2026-06-11-agent-quality-cost-improvements.md` (PR `mimfort/rag_for_git#2`, 5 файлов) + история `review_runs`/`review_findings` в админке.

**Files:**
- Create: `docs/plans/2026-06-12-claude-code-eval.md`

- [ ] **Step 1: Подготовить окружение**

Run: `docker compose up -d && .venv/bin/reviewer check`
Expected: все проверки `✓`

- [ ] **Step 2: Прогнать dry-run нового пути**

```bash
claude --plugin-dir . \
  -p "/rag-reviewer:review-pr mimfort/rag_for_git#2 --dry-run" \
  --permission-mode bypassPermissions --output-format text
```

Сохранить отчёт publish_review (inline, summary, счётчики) в файл результатов.

- [ ] **Step 3: Прогнать бейзлайн для сравнения (старый путь ещё жив)**

Run: `.venv/bin/reviewer review mimfort/rag_for_git 2 --dry-run`

- [ ] **Step 4: Заполнить протокол**

`docs/plans/2026-06-12-claude-code-eval.md`:

```markdown
# Эвал-гейт миграции на Claude Code (фаза 1)

Эталонный PR: mimfort/rag_for_git#2 (5 файлов). Бейзлайн: замер D2 (2026-06-12).

## Критерии (все обязательны)
- [ ] Полнота: новый путь находит не меньше реальных проблем, чем бейзлайн
      (ручная сверка списков находок)
- [ ] 0 галлюцинаций file:line: каждая inline-находка указывает на существующую
      строку диффа (отчёт publish_review: moved_to_summary из-за невалидной
      строки = 0)
- [ ] Идемпотентность: повторный прогон без dry-run не плодит дубликаты
- [ ] Время прогона ≤ 15 мин

## Результаты
| Метрика | OpenRouter (бейзлайн) | Claude Code |
|---|---|---|
| Находок (после verify) | | |
| Inline / summary | | |
| Галлюцинаций file:line | | |
| Время | | |

## Вердикт
- [ ] ПРОЙДЕН — можно выпиливать OpenRouter-слой (Task 12)
```

- [ ] **Step 5: Коммит и стоп-точка**

```bash
git add docs/plans/2026-06-12-claude-code-eval.md
git commit -m "docs(plans): протокол эвал-гейта миграции на Claude Code"
```

**ВАЖНО: Task 12 выполняется только после явного подтверждения пользователем вердикта «ПРОЙДЕН».**

---

### Task 12: Выпиливание OpenRouter-слоя (после эвал-гейта)

**Files:**
- Delete: `reviewer/llm/openrouter.py`, `reviewer/llm/budget.py`, `reviewer/agent/analyzer.py`, `reviewer/agent/prompts.py`, `reviewer/agent/graph.py`, `reviewer/agent/nodes.py`
- Modify: `reviewer/agent/state.py` (оставить только `ReviewUnit`), `reviewer/app.py` (убрать `llm_provider`), `reviewer/services/review_service.py` (убрать `run_review` и LLM-части, оставить `prepare` + хелперы), `reviewer/entrypoints/cli.py` (убрать команду `review`, из `check` — проверку OPENROUTER), `reviewer/config/settings.py`, `pyproject.toml`, `CLAUDE.md`, `README.md`
- Delete tests: `tests/agent/test_analyzer.py`, `tests/agent/test_nodes_fail_soft.py`, LLM-тесты OpenRouter/budget в `tests/llm/`, тесты команды `review` в `tests/entrypoints/`

- [ ] **Step 1: Удалить модули LLM-слоя**

```bash
git rm reviewer/llm/openrouter.py reviewer/llm/budget.py \
       reviewer/agent/analyzer.py reviewer/agent/prompts.py \
       reviewer/agent/graph.py reviewer/agent/nodes.py
```

`reviewer/llm/trace.py`, `usage.py`, `verdicts.py`: проверить потребителей —
`grep -rn "trace\|UsageLog\|verdicts" reviewer/ --include="*.py" | grep -v test` —
удалить те, что использовались только analyzer/review_service.run_review.
`Finding` живёт в `reviewer/vcs/base.py` и остаётся; `reviewer/llm/base.py`
(протокол `LLMProvider`) удалить, если grep не покажет других потребителей.

- [ ] **Step 2: Почистить использующий код**

- `state.py`: удалить `Deps`, `ReviewState`; оставить `ReviewUnit`.
- `app.py`: удалить поле `llm_provider` и создание `OpenRouterProvider`.
- `review_service.py`: удалить `run_review`, `ReviewResult`, `_record_history`
  (его MCP-аналог уже в `reviewer/mcp/service.py`); `prepare` и `_select_changed_files` остаются.
- `cli.py`: удалить команду `review`; в `check` убрать `OPENROUTER_API_KEY`.
- `settings.py`: удалить `openrouter_*`, `review_max_tool_iterations`,
  `review_agentic_verify`, `review_synthesis`, `review_verify_min_severity`,
  `review_verify_max_iterations`, `review_trace`, `review_verdict_log`.
  Оставить: `review_history`, `review_output_language`, пороги policy,
  `review_max_files`, `review_max_comments`, `review_skip_drafts`, suggestions mode.
- `pyproject.toml`: удалить `langchain-openai`, `langgraph` (оставить
  `langchain-core` — на нём `StructuredTool` в `code_tools.py`).

- [ ] **Step 3: Перенести/удалить тесты**

```bash
git rm tests/agent/test_analyzer.py tests/agent/test_nodes_fail_soft.py
```

Плюс удалить OpenRouter/budget/trace-тесты в `tests/llm/` и тесты команды `review`
в `tests/entrypoints/` (точный список — по падению `pytest` после Step 2).
Тесты dedup/assemble/policy/mcp остаются — они покрывают выживший код.

- [ ] **Step 4: Полный прогон + грепы чистоты**

Run: `.venv/bin/pip install -e ".[dev]" -q && .venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS

Run: `grep -rni "openrouter\|langgraph" reviewer/ tests/ pyproject.toml`
Expected: пусто

- [ ] **Step 5: Обновить документацию**

- `CLAUDE.md`: секции «Команды» (убрать `reviewer review`, добавить плагин и
  `/rag-reviewer:review-pr`, headless-пример), «Архитектура» (поток: prepare → скилл
  → publish; убрать LangGraph/OpenRouter), «Неочевидные факты» (убрать пункты про
  structured output minimax и OPENROUTER_MODEL_VERIFY/prompt cache; добавить: сессия
  MCP живёт в процессе сервера между prepare и publish; плагин = корень репо).
- `README.md`: переписать раздел про запуск ревью на плагин/скилл.

- [ ] **Step 6: Коммит**

```bash
git add -A
git commit -m "feat!: ревью через Claude Code (скилл + MCP); OpenRouter-слой удалён"
```

---

## Definition of Done фазы 1

1. `pytest -q` и `ruff check .` зелёные.
2. `claude --plugin-dir . -p "/rag-reviewer:review-pr <PR> --dry-run"` выдаёт отчёт с findings.
3. Эвал-гейт пройден и подтверждён пользователем; после Task 12 в кодовой базе нет упоминаний OpenRouter/LangGraph.
4. Прогон без dry-run постит один review (сводка + inline) и пишет `review_runs`/`review_findings`; повторный прогон не дублирует комментарии.

# Bilingual README Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переписать `README.md` и `README.ru.md` как синхронный слоистый онбординг с двумя равноправными маршрутами, проверяемыми ссылками и полным справочником из 12 skills.

**Architecture:** Обе языковые версии получают один семантический каркас, но естественный текст на своём языке. Новый stdlib-only contract test фиксирует порядок разделов, публичные команды, динамический inventory skills, относительные ссылки и GitHub-style anchors; существующие README guardrails сохраняют факты о grounding и board workflow.

**Tech Stack:** Markdown, Python 3.11–3.13, pytest 8, stdlib `pathlib`/`re`/`urllib.parse`/`collections`, Ruff.

## Global Constraints

- Реализация следует утверждённой spec `docs/superpowers/specs/2026-07-28-pri-220-bilingual-readme-onboarding-design.md`.
- Меняются только `README.md`, `README.ru.md` и README-contract тесты; runtime-код, manifests, версия пакета и содержимое skills не меняются.
- `README.md` и `README.ru.md` имеют одинаковый порядок, команды, возможности, ограничения и ссылки, но не являются дословными переводами.
- Обе аудитории равноправны: новый пользователь и инженер, разворачивающий reviewer для команды.
- RAG, MCP, base index, overlay и другие специальные термины объясняются при первом употреблении.
- Каждый из 12 каталогов `plugin/skills/*/SKILL.md` получает отдельный элемент справочника; `_common` не регистрируется как skill.
- Новые тесты используют только stdlib, не открывают внешние или localhost-сокеты и проходят под `pytest --disable-socket`.
- Внешние URL проверяются синтаксически; unit-тесты не ходят в сеть.
- Все CLI-команды и параметры сверяются с `.venv/bin/reviewer COMMAND --help`; install/skill поведение — с кодом installer и соответствующим `SKILL.md`.
- Env-факты сверяются с `.env.example` и `reviewer/config/settings.py`; per-repo policy — с `.review.yml` и `reviewer/policy/`.
- Board-факты сверяются с registry и `docs/board-providers.md`; закрытый список из двух legacy providers не возвращается в документацию.
- Существующие тесты не ослабляются: при смене заголовка или порядка разделов меняется способ выбора секции, а не проверяемый факт.
- Python-код тестов соблюдает line length 100.
- Коммиты — Conventional Commits на русском, без self-attribution.

---

## File Map

- `README.md` — английский dual-track онбординг, общие workflows, архитектура, справочники и эксплуатация.
- `README.ru.md` — русский документ с тем же семантическим каркасом и естественной локализацией.
- `tests/docs/test_readme_onboarding.py` — единый контракт структуры, parity-маркеров, skill inventory, локальных файлов/anchors и синтаксиса внешних URL.
- `tests/skills/test_readme_grounding_block.py` — сохраняет grounding contract после переноса раздела под workflows.
- `tests/docs/test_board_provider_docs.py` — выбирает skill-секции независимо от их взаимного порядка и продолжает проверять generic board/store-first факты.
- `tests/install/test_codex_plugin_payload.py` — не меняется; его динамический inventory остаётся обязательной регрессией.

## Canonical Section Pairs

| English | Русский |
|---|---|
| `## Start here` | `## Начните здесь` |
| `## Try reviewer` | `## Попробовать reviewer` |
| `## Deploy for a team` | `## Развёртывание для команды` |
| `## Core workflows` | `## Основные сценарии` |
| `## How it works` | `## Как это работает` |
| `## Installation and configuration` | `## Установка и конфигурация` |
| `## CLI reference` | `## Справочник CLI` |
| `## Skills reference` | `## Справочник skills` |
| `## Operations, troubleshooting, and limitations` | `## Эксплуатация, диагностика и ограничения` |
| `## Development` | `## Разработка` |
| `## License` | `## Лицензия` |

---

### Task 1: Два маршрута входа и проверяемый quick start

**Files:**
- Create: `tests/docs/test_readme_onboarding.py`
- Modify: `README.md:1-483`
- Modify: `README.ru.md:1-412`

**Interfaces:**
- Consumes: публичные команды из `reviewer/entrypoints/cli.py`, `pyproject.toml` и `.venv/bin/reviewer --help`; installer contracts из `reviewer/install.py`.
- Produces: `_read(name: str) -> str`, `_assert_in_order(text: str, headings: tuple[str, ...]) -> None`, `ROOT`, `EN`, `RU`, `QUICK_START_PAIRS`; Task 2 расширяет этот модуль, не переименовывая интерфейсы.

- [ ] **Step 1: Создать failing contract для language switch и dual-track начала**

Создать `tests/docs/test_readme_onboarding.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EN = ROOT / "README.md"
RU = ROOT / "README.ru.md"

QUICK_START_PAIRS = (
    ("## Start here", "## Начните здесь"),
    ("## Try reviewer", "## Попробовать reviewer"),
    ("## Deploy for a team", "## Развёртывание для команды"),
)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _assert_in_order(text: str, headings: tuple[str, ...]) -> None:
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)


def test_readmes_link_to_each_other_near_the_top():
    english = _read("README.md")[:600]
    russian = _read("README.ru.md")[:600]

    assert "[Русский](README.ru.md)" in english
    assert "[English](README.md)" in russian


def test_readmes_start_with_matching_dual_track_routes():
    english = _read("README.md")
    russian = _read("README.ru.md")

    _assert_in_order(english, tuple(pair[0] for pair in QUICK_START_PAIRS))
    _assert_in_order(russian, tuple(pair[1] for pair in QUICK_START_PAIRS))
```

- [ ] **Step 2: Запустить новый тест и подтвердить красную фазу**

Run:

```bash
.venv/bin/pytest -q tests/docs/test_readme_onboarding.py
```

Expected: FAIL — текущие README не содержат новых парных headings и точных language links в
первых 600 символах.

- [ ] **Step 3: Переписать общий верх обеих README**

В обоих файлах заменить прежнюю презентацию, раннюю архитектуру и дублирующиеся варианты
установки на следующий Markdown-каркас:

```markdown
# rag-reviewer

[Русский](README.ru.md)  <!-- в RU: [English](README.md) -->

AI-assisted pull-request reviews grounded in whole-repository context: hybrid search,
a code graph, and inline comments anchored to changed lines.

> Requires Python 3.11–3.13 and external Voyage, PostgreSQL/ParadeDB, and Neo4j
> services; publishing reviews also requires VCS credentials.

## Start here

| If you want to… | Follow |
|---|---|
| Try reviewer and get a first result | [Try reviewer](#try-reviewer) |
| Deploy one reviewer service for a team | [Deploy for a team](#deploy-for-a-team) |

## Try reviewer

## Deploy for a team
```

Русская версия использует утверждённые парные headings и естественные формулировки. Не вставлять
архитектурный deep dive до двух маршрутов.

- [ ] **Step 4: Написать маршрут «Try reviewer / Попробовать reviewer»**

Собрать маршрут строго в порядке prerequisites → install → infrastructure → configure → connect
client → check/index → first action → expected result. Канонический command spine:

```bash
uv tool install --from rag-reviewer reviewer
docker compose up -d
reviewer init
reviewer install codex
reviewer check
reviewer index /path/to/repo --ref main
reviewer status /path/to/repo --branch main
```

В command spine использовать `reviewer install codex`; рядом дать `reviewer install --list` и один короткий link на
полные client-specific варианты ниже. Для первого действия показать синтаксис отдельно:

```text
# Claude Code
/rag-reviewer:reviewer_review-pr owner/repo#123 --dry-run

# Codex
$rag-reviewer:reviewer_review-pr owner/repo#123
```

Перед публикацией примеров сверить Claude invocation с `plugin/README.md`, а Codex invocation —
с namespaced plugin/skill metadata. Не представлять один синтаксис как универсальный для всех
клиентов.

- [ ] **Step 5: Написать маршрут «Deploy for a team / Развёртывание для команды»**

Порядок содержания:

```markdown
1. Deployment shape: one reviewer MCP service + ParadeDB/Postgres + Neo4j + Voyage.
2. Secret placement: server-side env only; never send credentials through MCP calls.
3. Repository and branch scope: DEFAULT_REPO, REVIEW_BRANCHES, per-repo .review.yml.
4. Optional task board: registered provider, project/options in .review.yml, credentials in env.
5. Client installation: reviewer install --all or one named client.
6. Health and index: reviewer check, reviewer index, reviewer status --json.
7. Operations links: gc, serve, troubleshooting and safe Docker commands later in the README.
```

В каждом пункте дать наблюдаемую проверку. Не перечислять все env-переменные здесь: маршрут
ссылается на единый справочник конфигурации.

- [ ] **Step 6: Удалить дублирующий ранний материал и прогнать quick-start contract**

Удалить прежние верхние `Why/Зачем`, ранний architecture deep dive, `One-click install` и
параллельные quick/manual инструкции, если их содержание уже покрыто новыми маршрутами. Глубокие
факты пока оставить в нижней справочной части для миграции в Task 2.

Run:

```bash
.venv/bin/pytest -q tests/docs/test_readme_onboarding.py
git diff --check -- README.md README.ru.md tests/docs/test_readme_onboarding.py
```

Expected: PASS; `git diff --check` не печатает ошибок.

- [ ] **Step 7: Проверить factual commands без сетевых действий**

Run:

```bash
.venv/bin/reviewer --help
.venv/bin/reviewer install --help
.venv/bin/reviewer check --help
.venv/bin/reviewer index --help
.venv/bin/reviewer status --help
```

Expected: каждая опубликованная команда/опция присутствует в help; команды возвращают exit 0.

- [ ] **Step 8: Закоммитить dual-track начало**

```bash
git add README.md README.ru.md tests/docs/test_readme_onboarding.py
git commit -m "docs(readme): добавить два маршрута онбординга (PRI-220)"
```

---

### Task 2: Синхронные workflows, архитектура, CLI и 12 skills

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `tests/docs/test_readme_onboarding.py`
- Modify: `tests/skills/test_readme_grounding_block.py:10-23`
- Modify: `tests/docs/test_board_provider_docs.py:210-237`

**Interfaces:**
- Consumes: `QUICK_START_PAIRS`, `_read`, `_assert_in_order` из Task 1; inventory `plugin/skills/*/SKILL.md`; факты из CLI/config/policy/board sources.
- Produces: `CONTENT_PAIRS`, `_registered_skills() -> tuple[str, ...]`, `_skill_section(text: str, skill: str) -> str`; Task 3 использует `CONTENT_PAIRS` как префикс полного каркаса.

- [ ] **Step 1: Расширить contract до content sections и отдельных skill headings**

Добавить в `tests/docs/test_readme_onboarding.py`:

```python
CONTENT_PAIRS = QUICK_START_PAIRS + (
    ("## Core workflows", "## Основные сценарии"),
    ("## How it works", "## Как это работает"),
    ("## Installation and configuration", "## Установка и конфигурация"),
    ("## CLI reference", "## Справочник CLI"),
    ("## Skills reference", "## Справочник skills"),
)


def _registered_skills() -> tuple[str, ...]:
    skills_root = ROOT / "plugin" / "skills"
    return tuple(
        sorted(
            path.name
            for path in skills_root.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        )
    )


def test_readmes_share_content_section_order():
    english = _read("README.md")
    russian = _read("README.ru.md")

    _assert_in_order(english, tuple(pair[0] for pair in CONTENT_PAIRS))
    _assert_in_order(russian, tuple(pair[1] for pair in CONTENT_PAIRS))


def test_each_registered_skill_has_its_own_heading_in_both_readmes():
    english_headings = {
        line for line in _read("README.md").splitlines() if line.startswith("### ")
    }
    russian_headings = {
        line for line in _read("README.ru.md").splitlines() if line.startswith("### ")
    }

    for skill in _registered_skills():
        marker = f"reviewer_{skill}"
        assert any(marker in heading for heading in english_headings), marker
        assert any(marker in heading for heading in russian_headings), marker
```

- [ ] **Step 2: Запустить расширенный contract и подтвердить красную фазу**

Run:

```bash
.venv/bin/pytest -q tests/docs/test_readme_onboarding.py
```

Expected: FAIL — content headings ещё старые, а `reviewer_finish-task` не имеет отдельного H3 в
обеих README.

- [ ] **Step 3: Переписать Core workflows / Основные сценарии**

Создать одинаковый порядок сценариев:

```markdown
### Review a pull request
### Solve a task
### Ask a grounded codebase question
### Walk a human reviewer through a PR
### Run focused performance or maintainability review
### Create and finish board tasks
### Reviewer grounding in plan/review phases (optional)
```

Русские headings локализовать, кроме публичных skill/command identifiers. Каждый сценарий
содержит: intent, prerequisites, invocation, observable result, material limitation, next link.
Сохранить обязательные grounding-маркеры `search_codebase`, `callers`, `drift == 0` и копируемый
fail-open блок, который проверяет `tests/skills/test_readme_grounding_block.py`.

- [ ] **Step 4: Сжать How it works / Как это работает**

Оставить одну обзорную диаграмму/последовательность и короткие subsections:

```text
PR → prepare_review → base + overlay retrieval → skill analysis
   → verify/gate/dedup/ground → inline comments + summary → cleanup
```

Объяснить `node_id = path#fqn`, `base:<branch>`, `pr:N`, SCIP/tree-sitter fallback и границы
inline commentable lines. Не дублировать внутренний module catalog: дать link на `CLAUDE.md` или
project layout в Development.

- [ ] **Step 5: Собрать единый Installation and configuration reference**

В порядке «обязательное раньше опционального»:

```markdown
### Requirements
### Installation and updates
### AI clients
### Required services and credentials
### Repositories and branches
### Per-repo .review.yml
### Task boards
### Observability and sessions
### Graph and summary tuning
```

Сохранить факты и маркеры, защищённые `tests/docs/test_board_provider_docs.py`:
`docs/board-providers.md`, `provider_options`, `key_pattern:`, `url_template:`, `sync_board`,
`get_task(key, project=...)`, `store-first`, `tasks:<type>:<board>`, migration aliases и
server-side credentials. Не возвращать forbidden legacy surface `board-mcp`, `mcp__<board>` или
закрытый выбор `yougile | youtrack`.

- [ ] **Step 6: Переписать CLI reference по пользовательским задачам**

Сверить и описать текущие команды:

```text
Setup/integrations: init, install, install-skills, update
Index lifecycle: index, status, search, migrate-branches, gc
Operations: check, serve
Server entry point: reviewer-mcp
```

Для каждой команды дать одну строку назначения и только наиболее полезный пример. Полные options
не копировать из `--help`; рядом дать форму `reviewer COMMAND --help`.

- [ ] **Step 7: Переписать Skills reference с полным динамическим inventory**

Создать отдельный H3 в формате ``### `reviewer_review-pr` — Full PR review`` (с естественным
русским назначением в `README.ru.md`) для каждого identifier:

```text
reviewer_ask
reviewer_configure-review
reviewer_create-task
reviewer_finish-task
reviewer_maintainability-review
reviewer_performance-review
reviewer_pr-walkthrough
reviewer_review-pr
reviewer_solve-task
reviewer_summarize-subsystems
reviewer_sync-codebase
reviewer_sync-tasks
```

Каждый элемент следует пятистрочному шаблону: when, invocation, prerequisites, reads/writes,
result/next step. Порядок — по workflow: review/solve/Q&A/walkthrough/focused reviews/task
lifecycle/synchronization/configuration, а не алфавитный. Факты брать из соответствующего
`SKILL.md`; `finish-task` явно описывает confirmation gate, PR backlink и idempotency.

- [ ] **Step 8: Сделать существующие README guardrails независимыми от соседства skills**

В `tests/docs/test_board_provider_docs.py` добавить helper:

```python
def _skill_section(text: str, skill: str) -> str:
    marker = f"### `{skill}`"
    section = text.split(marker, maxsplit=1)[1]
    return section.split("\n### ", maxsplit=1)[0]
```

Заменить split по конкретному следующему skill:

```python
sync_section = _skill_section(text, "reviewer_sync-tasks")
solve_section = _skill_section(text, "reviewer_solve-task")
```

В `tests/skills/test_readme_grounding_block.py` заменить только уровень утверждённых headings:

```python
assert "### Reviewer grounding in plan/review phases (optional)" in text
assert "### Грунтовка reviewer в фазах план/ревью (опционально)" in text
```

Остальные asserts не менять.

- [ ] **Step 9: Запустить content и существующие guard tests**

Run:

```bash
.venv/bin/pytest -q tests/docs/test_readme_onboarding.py \
  tests/skills/test_readme_grounding_block.py \
  tests/docs/test_board_provider_docs.py \
  tests/install/test_codex_plugin_payload.py
.venv/bin/ruff check tests/docs/test_readme_onboarding.py \
  tests/skills/test_readme_grounding_block.py \
  tests/docs/test_board_provider_docs.py
git diff --check -- README.md README.ru.md tests
```

Expected: все тесты PASS, Ruff сообщает отсутствие ошибок, `git diff --check` молчит.

- [ ] **Step 10: Закоммитить синхронное ядро документации**

```bash
git add README.md README.ru.md tests/docs/test_readme_onboarding.py \
  tests/skills/test_readme_grounding_block.py tests/docs/test_board_provider_docs.py
git commit -m "docs(readme): синхронизировать workflows и справочники (PRI-220)"
```

---

### Task 3: Эксплуатация, link/anchor validation и финальная parity-проверка

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `tests/docs/test_readme_onboarding.py`

**Interfaces:**
- Consumes: `CONTENT_PAIRS`, `_read`, `_assert_in_order`, `_registered_skills()` из Tasks 1–2.
- Produces: `SECTION_PAIRS`, `PARITY_MARKERS`, `_link_targets(text: str) -> tuple[str, ...]`, `_heading_anchors(text: str) -> set[str]`; это финальный публичный docs contract.

- [ ] **Step 1: Добавить failing contract для финальных sections и parity-маркеров**

Добавить в `tests/docs/test_readme_onboarding.py`:

```python
SECTION_PAIRS = CONTENT_PAIRS + (
    (
        "## Operations, troubleshooting, and limitations",
        "## Эксплуатация, диагностика и ограничения",
    ),
    ("## Development", "## Разработка"),
    ("## License", "## Лицензия"),
)

PARITY_MARKERS = (
    "uv tool install --from rag-reviewer reviewer",
    "docker compose up -d",
    "reviewer init",
    "reviewer install",
    "reviewer check",
    "reviewer index",
    "reviewer status",
    "reviewer serve",
    "REVIEW_BRANCHES",
    "docs/board-providers.md",
    "provider_options",
    "sync_board",
    "get_task(key, project=...)",
    "store-first",
    "tasks:<type>:<board>",
    "search_codebase",
    "callers",
    "drift == 0",
)


def test_readmes_share_the_complete_section_order():
    english = _read("README.md")
    russian = _read("README.ru.md")

    _assert_in_order(english, tuple(pair[0] for pair in SECTION_PAIRS))
    _assert_in_order(russian, tuple(pair[1] for pair in SECTION_PAIRS))


def test_readmes_share_critical_commands_and_contract_markers():
    english = _read("README.md")
    russian = _read("README.ru.md")

    for marker in PARITY_MARKERS:
        assert marker in english, ("README.md", marker)
        assert marker in russian, ("README.ru.md", marker)
```

- [ ] **Step 2: Запустить contract и подтвердить красную фазу**

Run:

```bash
.venv/bin/pytest -q tests/docs/test_readme_onboarding.py
```

Expected: FAIL — финальные canonical headings ещё не добавлены, старый tail не соответствует
новому порядку.

- [ ] **Step 3: Переписать Operations / Эксплуатация**

Создать компактные subsections:

```markdown
### Health checks
### Index freshness and recovery
### Common failures
### Web admin
### Security
### Known limitations
```

Таблица common failures связывает symptom → likely cause → exact next command/link. Обязательные
ограничения: Voyage quota/cost, внешние Postgres/Neo4j, `REVIEW_BRANCHES`, thin behavior без
base-index, tree-sitter fallback без полной точности SCIP, optional board/fail-open behavior,
headless limitation без OAuth loopback. Docker teardown показывает только:

```bash
docker compose --profile test rm -sfv paradedb-test neo4j-test
```

Не публиковать `docker compose --profile test down -v`.

- [ ] **Step 4: Переписать Development и License, удалить legacy tail**

Development содержит точные requirements и команды:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration
.venv/bin/ruff check .
```

Объяснить запрет сети/localhost в unit tests, изоляцию integration infrastructure и безопасную
очистку. Сжать project layout до одной таблицы публичных подсистем. Завершить contributing и MIT
license. Удалить остатки старых разделов, дубликаты команд и второй справочник после canonical
License.

- [ ] **Step 5: Добавить stdlib link и anchor helpers**

В начало `tests/docs/test_readme_onboarding.py` добавить imports:

```python
import re
from collections import Counter
from urllib.parse import unquote, urlsplit
```

Добавить helpers:

```python
LINK_RE = re.compile(r"(?<!!)\[[^]]*\]\(([^)]*)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _link_targets(text: str) -> tuple[str, ...]:
    targets = []
    for raw in LINK_RE.findall(text):
        raw = raw.strip()
        if raw.startswith("<") and ">" in raw:
            target = raw[1:raw.index(">")]
        else:
            target = raw.split(maxsplit=1)[0]
        targets.append(target)
    return tuple(targets)


def _base_slug(heading: str) -> str:
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = heading.replace("`", "").replace("*", "")
    lowered = heading.casefold()
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch in " _-")
    return re.sub(r"\s+", "-", kept.strip())


def _heading_anchors(text: str) -> set[str]:
    counts: Counter[str] = Counter()
    anchors = set()
    for heading in HEADING_RE.findall(text):
        base = _base_slug(heading)
        suffix = counts[base]
        counts[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors
```

- [ ] **Step 6: Добавить проверки локальных файлов/anchors и внешних URL**

Добавить:

```python
def _assert_links_resolve(source: Path) -> None:
    for target in _link_targets(source.read_text(encoding="utf-8")):
        parsed = urlsplit(target)
        if parsed.scheme:
            assert parsed.scheme in {"http", "https"}, (source.name, target)
            assert parsed.netloc, (source.name, target)
            continue

        path_text = unquote(parsed.path)
        destination = source if not path_text else source.parent / path_text
        destination = destination.resolve()
        assert destination.is_relative_to(ROOT), (source.name, target)
        assert destination.exists(), (source.name, target)

        if parsed.fragment and destination.suffix.lower() == ".md":
            anchors = _heading_anchors(destination.read_text(encoding="utf-8"))
            assert unquote(parsed.fragment).casefold() in anchors, (source.name, target)


def test_all_readme_links_and_local_anchors_resolve():
    _assert_links_resolve(EN)
    _assert_links_resolve(RU)
```

Если реальный Markdown содержит URL со скобками, не ослаблять проверку: заменить такой URL на
эквивалентную ссылку без неоднозначных скобок либо точечно улучшить parser и добавить отдельный
unit case.

- [ ] **Step 7: Запустить link contract, исправить каждую фактическую ошибку**

Run:

```bash
.venv/bin/pytest -q tests/docs/test_readme_onboarding.py -vv
```

Expected initial result: FAIL на первом stale/missing relative target, anchor или финальном
parity marker. Исправлять README или slug/link parser по фактической причине; не удалять полезную
ссылку только ради зелёного теста. Повторять команду до PASS.

- [ ] **Step 8: Прогнать все README guardrails**

Run:

```bash
.venv/bin/pytest -q tests/docs/test_readme_onboarding.py \
  tests/skills/test_readme_grounding_block.py \
  tests/docs/test_board_provider_docs.py \
  tests/install/test_codex_plugin_payload.py
.venv/bin/ruff check tests/docs/test_readme_onboarding.py \
  tests/skills/test_readme_grounding_block.py \
  tests/docs/test_board_provider_docs.py
```

Expected: все тесты PASS, Ruff сообщает отсутствие ошибок.

- [ ] **Step 9: Прогнать полный unit suite и diff hygiene**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
git status --short
```

Expected: pytest завершён без failures/errors, Ruff сообщает отсутствие ошибок,
`git diff --check` молчит. Среди изменений реализации разрешены только пять изменяемых файлов из
File Map; уже существующие посторонние локальные изменения могут оставаться в `git status`, но их
нужно зафиксировать в отчёте и не добавлять в commit.

- [ ] **Step 10: Провести ручную двуязычную приёмку**

Для каждого README отдельно пройти checklist:

```text
1. За первые два экрана понятны назначение, внешние зависимости и выбор аудитории.
2. Оба маршрута доходят до команды, проверки результата и recovery link.
3. Все commands/options подтверждены локальным --help или source-of-truth файлом.
4. Каждый benefit соседствует с material limitation.
5. Все 12 skill headings присутствуют и описаны одним шаблоном.
6. RAG, MCP, base index и overlay объяснены до первого использования без расшифровки.
7. EN/RU совпадают по SECTION_PAIRS и PARITY_MARKERS, но читаются естественно.
8. Нет остаточных повторов старых install/config/architecture sections.
```

Expected: восемь пунктов подтверждены для обоих файлов; найденные расхождения исправлены и повторно
проверены соответствующими targeted tests.

- [ ] **Step 11: Закоммитить завершённый README contract**

```bash
git add README.md README.ru.md tests/docs/test_readme_onboarding.py
git commit -m "test(docs): закрепить двуязычный контракт README (PRI-220)"
```

---

## Final Verification

После трёх task commits выполнить с чистого рабочего состояния:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git log -3 --oneline
git status --short
```

Expected:

- pytest: 0 failures, 0 errors;
- Ruff: 0 errors;
- три последних implementation commits соответствуют Tasks 1–3;
- нет незакоммиченных изменений в пяти файлах из File Map;
- посторонние пользовательские изменения, существовавшие до начала работы, сохранены и не
попали в commits.

# Общие reference-блоки промптов ревью (PRI-142) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вынести 4 дублирующихся блока инструкций (findings-schema, anti-hallucination, tool-usage, branch-selection) в `plugin/skills/_common/` как единый источник правды; перевести скилы из скоупа на runtime-include.

**Architecture:** Маркер `<!-- include: _common/X.md -->` в reference/SKILL заменяет вырезанный общий блок; оркестратор/скилл при сборке промпта субагента подставляет содержимое файла (путь от `plugin/skills/`). В `_common` идёт только инвариантное ядро — скилл-специфичные хвосты остаются в скиллах. Guard-тест резолвит include-маркеры и проверяет, что собранный промпт содержит все ключевые правила.

**Tech Stack:** Python 3.11+, pytest (unit, `-m 'not integration'` по умолчанию), ruff (line-length 100). Скилы — markdown в `plugin/skills/`.

## Global Constraints

- Язык проекта — русский: комментарии/докстринги/CLI на русском. Тела скилл-промптов — на английском (токены), но скилл инструктирует отвечать пользователю по-русски.
- Коммиты: Conventional Commits на русском, **без** self-attribution (`Co-Authored-By`/упоминаний Claude).
- Линт: `.venv/bin/ruff check .` — line-length 100, target py311.
- Тесты unit: `.venv/bin/pytest -q` (integration исключены `addopts = -m 'not integration'`).
- Include-маркер: ровно `<!-- include: _common/<file>.md -->`, путь относительно `plugin/skills/`.
- `_common/findings-schema.md` обязан по полям совпадать с `Finding` (`reviewer/vcs/base.py:30`).
- Скоуп: НЕ трогать `sync-codebase`, `sync-tasks`.

---

### Task 1: Создать `plugin/skills/_common/` (4 файла) + guard-тесты контента

**Files:**
- Create: `plugin/skills/_common/findings-schema.md`
- Create: `plugin/skills/_common/anti-hallucination.md`
- Create: `plugin/skills/_common/tool-usage.md`
- Create: `plugin/skills/_common/branch-selection.md`
- Test: `tests/skills/test_common_blocks.py`

**Interfaces:**
- Consumes: `Finding` dataclass (`reviewer/vcs/base.py:30`) — поля `category, severity, file, line, side, message, suggestion, confidence, fix_start, fix_end, replacement, code_quote`.
- Produces: 4 файла `_common/*.md`; тест-хелпер `_skills_dir()` и набор guard-проверок, на которые опираются Task 3/4.

- [ ] **Step 1: Написать падающий тест контента `_common`**

Create `tests/skills/test_common_blocks.py`:

```python
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "plugin" / "skills"
COMMON = SKILLS_DIR / "_common"


def _read(name: str) -> str:
    return (COMMON / name).read_text(encoding="utf-8")


def test_all_four_common_files_exist_nonempty():
    for name in (
        "findings-schema.md",
        "anti-hallucination.md",
        "tool-usage.md",
        "branch-selection.md",
    ):
        assert (COMMON / name).is_file(), f"нет {name}"
        assert len(_read(name).strip()) > 0, f"{name} пустой"


def test_findings_schema_matches_finding_dataclass():
    # Каждое публичное поле Finding должно присутствовать в схеме.
    from reviewer.vcs.base import Finding
    import dataclasses

    schema = _read("findings-schema.md")
    field_to_token = {
        "category": "category",
        "severity": "severity",
        "file": "file",
        "line": "line",
        "side": "side",
        "message": "message",
        "suggestion": "suggestion",
        "confidence": "confidence",
        "code_quote": "code_quote",
        # fix_start/fix_end/replacement сворачиваются в JSON-объект "fix"
        "fix_start": "fix",
        "fix_end": "fix",
        "replacement": "replacement",
    }
    for f in dataclasses.fields(Finding):
        token = field_to_token.get(f.name)
        if token is None:
            continue
        assert token in schema, f"поле {f.name} (токен {token}) отсутствует в findings-schema.md"


def test_anti_hallucination_has_core_principles():
    text = _read("anti-hallucination.md").lower()
    assert "code_quote" in text
    assert "hallucinat" in text          # «a hallucinated absence is worse…»
    assert "empty findings list" in text  # пустой список — валидный результат


def test_tool_usage_has_both_tool_families():
    text = _read("tool-usage.md")
    # PR-session
    assert "search_code" in text and "get_changed_file_diff" in text and "get_impact" in text
    # session-less
    assert "search_codebase" in text and "related_symbols" in text and "definition" in text


def test_branch_selection_has_review_branches_logic():
    text = _read("branch-selection.md")
    assert "REVIEW_BRANCHES" in text
    assert "git branch --show-current" in text
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_common_blocks.py -q`
Expected: FAIL (FileNotFoundError / каталог `_common` отсутствует).

- [ ] **Step 3: Создать `_common/findings-schema.md`**

```markdown
Findings output schema (shared). The calling skill sets `category`.

Return ONLY a JSON object (no prose around it):

```json
{"findings": [{
  "category": "<set by the calling skill>",
  "severity": "low|medium|high|critical",
  "file": "<path of the reviewed file>",
  "line": <line number in the NEW file, or null>,
  "side": "RIGHT|LEFT",
  "code_quote": "<exact line from the new file, or null when line is null>",
  "message": "<what is wrong and why it matters>",
  "suggestion": "<short advice or null>",
  "fix": {"start_line": N, "end_line": M, "replacement": "<new code>"} | null,
  "confidence": 0.0
}]}
```

Field semantics:
- `category` — set by the calling skill (see its own instructions).
- `severity` — `low|medium|high|critical`.
- `file` — path of the reviewed file.
- `line` — line number in the NEW file, or `null` (a null line lands in the summary, not inline).
- `side` — `RIGHT` (new version) or `LEFT` (old version).
- `code_quote` — exact line copied verbatim from the NEW file; it grounds the line number. `null` only when `line` is null. An inaccurate quote is worse than no quote.
- `message` / `suggestion` — written in the orchestrator's output language.
- `fix` — exact replacement for a contiguous line range in the new file (`start_line`/`end_line`/`replacement`), or `null` when unsure.
- `confidence` — float `0.0..1.0`; it feeds the publish gate, so be honest.

Write `message` and `suggestion` in the output language given by the orchestrator.
```

- [ ] **Step 4: Создать `_common/anti-hallucination.md`**

```markdown
Anti-noise / anti-hallucination rules (shared core; follow strictly):

1. Report only problems RELATED to the changed lines. Unchanged code is out of
   scope even if imperfect.
2. Before claiming "missing error handling / missing None check / missing
   validation", verify through tools (`read_file` / `search_code` /
   `search_codebase`) that the handling truly is absent — not a line above/below
   or at the call site. A hallucinated absence is worse than a missed finding.
3. Do not duplicate the same observation across multiple lines: one problem →
   one finding with the most representative line.
4. Style, naming and formatting are NOT findings unless they affect program
   behaviour (line length, single-letter variable in a comprehension, import
   order, etc.). Category `style` is only valid for real logic-readability
   problems.
5. Do not suggest refactoring for its own sake. If code works correctly and does
   not violate its contract, do not report it.
6. Do not invent issues to fill a quota; an empty findings list is a valid
   result.

Every finding MUST carry an exact `code_quote` — one line copied verbatim from
the NEW version of the file. It grounds the line number; an inaccurate quote is
worse than no quote.
```

- [ ] **Step 5: Создать `_common/tool-usage.md`**

```markdown
Tool discipline (shared):

- Use tools BEFORE claiming cross-file effects.
- Targeted search: make each tool call answer ONE specific question; do not
  browse a file as a whole. Identical calls return cached results instantly;
  still avoid redundant calls — each call should answer a new question. Stop
  calling tools once you can decide.
- If a signature or contract changes, locate all call sites and verify (via
  `read_file` / `get_changed_file_diff`) that they stay consistent.

## PR-session tools (inside `/reviewer_review-pr`)

- `search_code` — usages of a symbol/string;
- `get_related_symbols` — graph neighbours (calls / implementations / tests);
- `find_callers` — callers impacted by a change;
- `get_definition` — a symbol's definition;
- `read_file` — exact source context;
- `get_changed_file_diff` — other changed files of this PR;
- `get_impact` — callers of a changed signature that live outside the diff.

## Session-less tools (`ask` / `solve-task`, no PR session)

- `search_codebase(repo, query, branch?)` — hybrid semantic+lexical search;
- `related_symbols(repo, node_id, branch?)` — graph neighbours;
- `callers(repo, node_id, branch?)` — direct callers (impact);
- `definition(repo, symbol, branch?)` — where a symbol is defined.
```

- [ ] **Step 6: Создать `_common/branch-selection.md`**

```markdown
Branch selection for code search (shared):

- Determine the current git branch: `git branch --show-current`.
- If it is in `REVIEW_BRANCHES` (the tracked branches list), pass it as the
  `branch` parameter — the search uses that branch's index.
- If the user explicitly named a branch, use that one instead.
- Otherwise omit `branch` entirely — the server uses the primary branch (the
  first entry in `REVIEW_BRANCHES`).
- Pass the same `branch` to the graph tools (`callers` / `related_symbols` /
  `definition`) — identically (or omit it identically).
```

- [ ] **Step 7: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_common_blocks.py -q`
Expected: PASS (5 тестов).

- [ ] **Step 8: Линт + коммит**

```bash
.venv/bin/ruff check tests/skills/test_common_blocks.py
git add plugin/skills/_common tests/skills/test_common_blocks.py
git commit -m "feat(skills): _common reference-блоки промптов ревью (PRI-142)"
```

---

### Task 2: install — поддержка общего каталога `_common`

**Files:**
- Modify: `reviewer/install.py:34-37` (константа `SKILL_NAMES`)
- Test: `tests/install/test_common_shared_dir.py`

**Interfaces:**
- Consumes: `extract_skills(tar_bytes, dest)` → `list[str]`; `_skill_file_hashes(skills_dir)` → `dict[str,str]` (существующие, обходят подкаталоги без требования `SKILL.md`).
- Produces: `SKILL_NAMES` с добавленным `"_common"` (документирующая регистрация).

**Контекст:** `SKILL_NAMES` нигде не читается (grep по `reviewer/`+`tests/` — только определение); распаковка/хэширование идут по обходу подкаталогов. Поэтому код менять не требуется — задача фиксирует поддержку `_common` регресс-тестом и регистрирует его в документирующей константе.

- [ ] **Step 1: Написать падающий тест поддержки `_common`**

Create `tests/install/test_common_shared_dir.py`:

```python
import io
import tarfile

from reviewer import install as inst


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_common_dir_extracted_and_hashed(tmp_path):
    # _common не имеет SKILL.md — проверяем, что обход подкаталогов его не теряет.
    tar = _tar({
        "r/plugin/skills/review-pr/SKILL.md": b"# review",
        "r/plugin/skills/_common/findings-schema.md": b"# schema",
    })
    names = inst.extract_skills(tar, tmp_path)
    assert "_common" in names
    assert (tmp_path / "_common" / "findings-schema.md").is_file()

    hashes = inst._skill_file_hashes(tmp_path)
    assert "_common" in hashes
    assert hashes["_common"].startswith("sha256:")


def test_common_registered_in_skill_names():
    assert "_common" in inst.SKILL_NAMES
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_common_shared_dir.py -q`
Expected: `test_common_dir_extracted_and_hashed` PASS (код уже поддерживает обход), `test_common_registered_in_skill_names` FAIL (`_common` не в `SKILL_NAMES`).

- [ ] **Step 3: Зарегистрировать `_common` в `SKILL_NAMES`**

Modify `reviewer/install.py:34-37`:

```python
SKILL_NAMES = (
    "review-pr", "solve-task", "sync-codebase",
    "sync-tasks", "performance-review", "maintainability-review",
    "_common",  # общий каталог reference-блоков (без своего SKILL.md), PRI-142
)
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/install/test_common_shared_dir.py -q`
Expected: PASS (2 теста).

- [ ] **Step 5: Прогнать существующие install-тесты (регресс)**

Run: `.venv/bin/pytest tests/install -q`
Expected: PASS (все — они на tmp-фикстурах, реальный `_common` их не задевает).

- [ ] **Step 6: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/install.py tests/install/test_common_shared_dir.py
git add reviewer/install.py tests/install/test_common_shared_dir.py
git commit -m "feat(install): регистрация общего каталога _common (PRI-142)"
```

---

### Task 3: Перевести review-pipeline на include (analyze / requirements / blast-radius / review-pr SKILL)

**Files:**
- Modify: `plugin/skills/review-pr/references/analyze-prompt.md` (вырезать findings-схему стр.82-97, anti-noise ядро стр.18-33, tool-usage ядро стр.8-17 → маркеры)
- Modify: `plugin/skills/review-pr/references/requirements-prompt.md` (findings-схема стр.38-53, tool-usage упоминание стр.23-25 → маркеры)
- Modify: `plugin/skills/review-pr/references/blast-radius-prompt.md` (tool-usage → маркер; findings-схема уже ссылочная)
- Modify: `plugin/skills/review-pr/references/verify-prompt.md` (tool-usage стр.4-5 → маркер; `verdicts`-схему НЕ трогать)
- Modify: `plugin/skills/review-pr/SKILL.md` (шаги 3–5: инструкция резолвить include-маркеры)
- Test: `tests/skills/test_assembled_prompts.py`

**Interfaces:**
- Consumes: `_common/{findings-schema,anti-hallucination,tool-usage}.md` (Task 1).
- Produces: тест-хелпер `assemble(rel_path)` (резолвит `<!-- include: ... -->` от `plugin/skills/`), используемый и в Task 4.

- [ ] **Step 1: Написать падающий тест сборки review-pipeline промптов**

Create `tests/skills/test_assembled_prompts.py`:

```python
import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "plugin" / "skills"
_INCLUDE = re.compile(r"<!-- include: (\S+\.md) -->")


def assemble(rel_path: str) -> str:
    """Собрать промпт как оркестратор: подставить содержимое include-маркеров.

    Путь в маркере — относительно plugin/skills/. Резолв нерекурсивный
    (в _common-файлах маркеров нет).
    """
    text = (SKILLS_DIR / rel_path).read_text(encoding="utf-8")

    def repl(m: re.Match) -> str:
        return (SKILLS_DIR / m.group(1)).read_text(encoding="utf-8")

    out = _INCLUDE.sub(repl, text)
    assert "<!-- include:" not in out, f"неразрешённый include в {rel_path}"
    return out


def test_analyze_has_include_markers():
    raw = (SKILLS_DIR / "review-pr/references/analyze-prompt.md").read_text("utf-8")
    assert "<!-- include: _common/findings-schema.md -->" in raw
    assert "<!-- include: _common/anti-hallucination.md -->" in raw
    assert "<!-- include: _common/tool-usage.md -->" in raw


def test_analyze_assembled_has_all_rules():
    a = assemble("review-pr/references/analyze-prompt.md")
    assert "code_quote" in a                       # из findings-schema / anti-halluc
    assert '"confidence": 0.0' in a                # из findings-schema
    assert "empty findings list" in a.lower()      # из anti-hallucination
    assert "get_impact" in a                       # из tool-usage
    # analyze-специфичный хвост остался на месте
    assert "commentable_right" in a


def test_requirements_assembled_has_schema_and_category():
    r = assemble("review-pr/references/requirements-prompt.md")
    assert '"severity": "low|medium|high|critical"' in r
    assert "requirements" in r                     # фиксированная категория скилла


def test_blast_radius_assembled_has_tooling_and_confidence_tail():
    b = assemble("review-pr/references/blast-radius-prompt.md")
    assert "get_impact" in b
    assert "0.8" in b                              # confidence-scale хвост остался


def test_verify_keeps_verdicts_schema_and_tools():
    v = assemble("review-pr/references/verify-prompt.md")
    assert '{"verdicts":' in v.replace(" ", "")    # своя схема не тронута
    assert "find_callers" in v                      # tool-usage подставлен
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py -q`
Expected: FAIL (`test_*_has_include_markers` — маркеров ещё нет).

- [ ] **Step 3: Заменить блоки в `analyze-prompt.md` на маркеры**

В `plugin/skills/review-pr/references/analyze-prompt.md`:
- Строки 8-17 (tool-usage «Use tools BEFORE…» + targeted search) заменить на:
  `<!-- include: _common/tool-usage.md -->` + строку контекста
  `Use the PR-session tools above.`
- Строки 18-33 (anti-noise rules 1-6) заменить на `<!-- include: _common/anti-hallucination.md -->`.
  Строки 34-46 (line/commentable/`fix`/intent — analyze-специфика) **оставить**.
- Строки 82-97 (JSON-схема) заменить на:
  `<!-- include: _common/findings-schema.md -->` + строку
  `Set "category" to one of: correctness|security|performance|maintainability|style.`
- Блок `## Examples` (47-80) **оставить** (analyze-специфичные примеры).

- [ ] **Step 4: Заменить блоки в `requirements-prompt.md` на маркеры**

В `plugin/skills/review-pr/references/requirements-prompt.md`:
- Строки 23-25 (упоминание tool-usage) заменить на `<!-- include: _common/tool-usage.md -->` + `Use the PR-session tools above.` (правила «judge only stated requirements» стр.15-22, 26-36 **оставить**).
- Строки 38-53 (JSON-схема) заменить на `<!-- include: _common/findings-schema.md -->` + `category MUST be exactly "requirements". Set "fix" to null. "code_quote" may be null when "line" is null.`

- [ ] **Step 5: Заменить tool-usage в `blast-radius-prompt.md` и `verify-prompt.md`**

В `blast-radius-prompt.md`: после заголовка `Method:` добавить строку
`<!-- include: _common/tool-usage.md -->` + `Use the PR-session tools above (especially get_impact).`
Строки confidence-scale (20-42), anchoring (44-51) и ссылку на схему analyze (53-55) **оставить**.

В `verify-prompt.md`: строки 4-5 (`Use tools (...) to verify...`) заменить на
`<!-- include: _common/tool-usage.md -->` + `Use the PR-session tools above to verify doubtful claims — do not guess.`
Схему `{"verdicts": [...]}` (стр.42) и правила is_real (7-22), примеры (24-41) **оставить**.

- [ ] **Step 6: Обновить `review-pr/SKILL.md` — инструкция резолва include**

В `plugin/skills/review-pr/SKILL.md`, в начале шага 3 (Analyze, перед списком «the contents of references/analyze-prompt.md…») добавить абзац:

```markdown
   When you read a `references/*-prompt.md` file, it may contain
   `<!-- include: _common/<file>.md -->` markers. Before putting the prompt into
   a subagent, replace each marker with the verbatim contents of that file
   (path is relative to `plugin/skills/`). These `_common/*.md` files are the
   single source of the shared findings-schema / anti-hallucination / tool-usage
   blocks.
```

- [ ] **Step 7: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py -q`
Expected: PASS (5 тестов).

- [ ] **Step 8: Линт + коммит**

```bash
.venv/bin/ruff check tests/skills/test_assembled_prompts.py
git add plugin/skills/review-pr tests/skills/test_assembled_prompts.py
git commit -m "refactor(skills): review-pipeline промпты через _common include (PRI-142)"
```

---

### Task 4: Перевести standalone-скилы на include + финальная верификация

**Files:**
- Modify: `plugin/skills/performance-review/SKILL.md` (tool-usage стр.24-26/48-49 → маркер; findings-схема стр.68-81 → маркер)
- Modify: `plugin/skills/maintainability-review/SKILL.md` (tool-usage стр.23-26 → маркер; findings-схема стр.120-133 → маркер; «What Not To Flag» оставить)
- Modify: `plugin/skills/ask/SKILL.md` (tool-usage стр.21-31 → session-less маркер; branch-selection стр.36-49 → маркер; grounding-contract стр.73-80 оставить)
- Modify: `plugin/skills/solve-task/SKILL.md` (tool-usage + branch-selection блоки → маркеры)
- Test: `tests/skills/test_assembled_prompts.py` (расширить)

**Interfaces:**
- Consumes: `_common/*.md` (Task 1); `assemble()` (Task 3).
- Produces: финально — зелёный полный прогон `pytest -q` + `ruff check .`.

- [ ] **Step 1: Дописать падающие тесты для standalone-скилов**

Append to `tests/skills/test_assembled_prompts.py`:

```python
def test_performance_assembled_schema_and_goal():
    p = assemble("performance-review/SKILL.md")
    assert '"category": "performance"' in p or "performance" in p
    assert '"confidence": 0.0' in p                 # из findings-schema
    assert "N+1" in p                               # perf-специфичный хвост остался


def test_maintainability_assembled_schema_and_whatnot():
    m = assemble("maintainability-review/SKILL.md")
    assert '"confidence": 0.0' in m                 # из findings-schema
    assert "What Not To Flag" in m                  # maint-специфичный хвост остался


def test_ask_assembled_has_sessionless_tools_and_branch():
    a = assemble("ask/SKILL.md")
    assert "search_codebase" in a                   # session-less tool-usage
    assert "REVIEW_BRANCHES" in a                   # branch-selection
    assert "Grounding contract" in a                # ask-специфичный хвост остался


def test_solve_task_assembled_has_branch_and_tools():
    s = assemble("solve-task/SKILL.md")
    assert "REVIEW_BRANCHES" in s
    assert "search_codebase" in s
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py -q`
Expected: FAIL (4 новых теста — маркеров в standalone-скилах ещё нет).

- [ ] **Step 3: `performance-review/SKILL.md` — маркеры**

- Строки 24-26 («Call the reviewer MCP tools…») заменить на
  `<!-- include: _common/tool-usage.md -->` + `Use the PR-session tools above.`
- Строки 68-81 (JSON-схема) заменить на `<!-- include: _common/findings-schema.md -->` +
  `Set "category" to "performance"; "side" is always "RIGHT".`
- Goal/Method/Severity (perf-специфика, в т.ч. «N+1 queries») **оставить**.

- [ ] **Step 4: `maintainability-review/SKILL.md` — маркеры**

- Строки 23-26 заменить на `<!-- include: _common/tool-usage.md -->` + `Use the PR-session tools above.`
- Строки 120-133 (JSON-схема) заменить на `<!-- include: _common/findings-schema.md -->` +
  `Set "category" to "maintainability"; "side" is always "RIGHT".`
- Goal/Repository Context/Method/Simplification Heuristics/**What Not To Flag**/Severity **оставить**.

- [ ] **Step 5: `ask/SKILL.md` — маркеры**

- Раздел `## Tools` (строки 21-31): после первой строки вставить
  `<!-- include: _common/tool-usage.md -->` + `Use the session-less tools above.`
  (детальные описания session-less тулов можно заменить ссылкой на блок).
- Branch-resolution в шаге 1 (строки 36-49, часть про `branch`) заменить на
  `<!-- include: _common/branch-selection.md -->`, оставив freshness-check (warn-banner) как ask-специфику.
- `## Grounding contract` (73-80) **оставить**.

- [ ] **Step 6: `solve-task/SKILL.md` — маркеры**

- Блок «Branch selection for search_codebase» (в шаге 3) заменить на
  `<!-- include: _common/branch-selection.md -->`.
- Перечень session-less тулов (callers/related_symbols/definition в шаге 3) дополнить/заменить
  на `<!-- include: _common/tool-usage.md -->` + `Use the session-less tools above.`
- Логику пайплайна solve-task (preflight, store-first, distill) **оставить**.

- [ ] **Step 7: Запустить тесты сборки — убедиться, что проходят**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py -q`
Expected: PASS (9 тестов всего).

- [ ] **Step 8: Полный прогон unit + линт (финальная верификация)**

Run: `.venv/bin/pytest -q`
Expected: PASS (вкл. `tests/skills/`, `tests/install/`; integration исключены).

Run: `.venv/bin/ruff check .`
Expected: без новых ошибок в изменённых файлах (на main ruff не обязан быть repo-wide чист — сверять только свои файлы).

- [ ] **Step 9: Золотой слепок «поведение не изменилось» (ручная сверка)**

Для каждого скилла из скоупа собрать промпт `assemble(...)` и глазами/диффом сверить, что
итоговый набор правил совпадает с версией до рефакторинга (контент тот же, только источник —
`_common`). Зафиксировать вывод в описании PR (что собранные промпты эквивалентны по правилам).

- [ ] **Step 10: Коммит**

```bash
git add plugin/skills tests/skills/test_assembled_prompts.py
git commit -m "refactor(skills): standalone-скилы через _common include (PRI-142)"
```

---

## Self-Review (выполнено автором плана)

**Spec coverage:**
- 4 файла `_common` → Task 1. ✓
- runtime-include механизм + маркер → Task 3 (Step 6 review-pr) / Task 4. ✓
- findings-schema ↔ Finding → Task 1 (Step 1 тест). ✓
- install/`SKILL_NAMES` (+ авто-обход) → Task 2. ✓
- guard-тест собранных промптов + золотой слепок → Task 3/4 (+ Task 4 Step 9). ✓
- скоуп (review-pr/SKILL.md включён, sync-* нет; verify только tool-usage) → Task 3/4. ✓
- existing install-тесты не сломаны → Task 2 Step 5; полный прогон → Task 4 Step 8. ✓

**Placeholder scan:** код во всех code-steps реальный; нет TBD/«handle edge cases». ✓

**Type consistency:** хелпер `assemble(rel_path)` определён в Task 3 и переиспользуется в Task 4 (то же имя/сигнатура). Include-маркер единого формата `<!-- include: _common/<file>.md -->` во всех задачах и в Global Constraints. `_skill_file_hashes` возвращает `dict` с префиксом `sha256:` (сверено с `tests/install/test_skills_stamp.py`). ✓

**Примечание по номерам строк:** диапазоны строк указаны по состоянию на 89a0b7d; реализатор сверяет их перед правкой (файлы небольшие, блоки опознаются по тексту).

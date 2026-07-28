# PRI-177 Brief → Spec Traceability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Шаг 5 скилла `solve-task` просит `superpowers:brainstorming` записать в спеку ссылку на бриф и явно запрещает копировать `## Constraints` verbatim; правка защищена guard-тестом.

**Architecture:** Правка чистого промпта — заменяется первый абзац шага 5 в `plugin/skills/solve-task/SKILL.md`. Python-ядро `reviewer/` не затрагивается. Защита — один region-scoped guard-тест в существующем `tests/skills/test_solve_task_brief.py` (whole-file-ассерт был бы холостым: `Constraints` уже встречается в файле 5 раз, `docs/superpowers/briefs/` — 6 раз). Правка контента под `plugin/` меняет codex payload-digest, поэтому в тот же коммит входит пересборка манифестов.

**Tech Stack:** Markdown (`SKILL.md`), pytest, ruff, `scripts/update_codex_plugin_manifest.py`.

**Спека:** `docs/superpowers/specs/2026-07-28-pri-177-brief-spec-traceability-design.md`
**Бриф:** `docs/superpowers/briefs/2026-07-28-PRI-177-brief-spec-traceability.md`
**Ветка:** `feature/pri-177` (создана, спека+бриф закоммичены в `accbbf1`)

## Global Constraints

- ruff: `line-length 100`, `target py311` — новый тестовый код обязан укладываться в 100 символов.
- Тело `plugin/skills/**/SKILL.md` пишется **по-английски** (экономия токенов); докстринги и
  комментарии в тестах — **по-русски**, как весь проект.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`,
  никаких упоминаний Claude).
- Unit-тесты не ходят в сеть, Postgres и Neo4j; этот тест — чисто текстовый анализ файла.
- Любая правка контента под `plugin/` требует прогона `scripts/update_codex_plugin_manifest.py`
  в том же коммите, иначе падают install-тесты.
- Тест не пиннит формулировки — только стабильные маркеры (домашний стиль, зафиксирован в
  докстринге `tests/skills/test_solve_task_brief.py`).

---

### Task 1: Provenance-ссылка на бриф в шаге 5 + guard-тест

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (первый абзац шага 5, строки 266–270)
- Modify/Test: `tests/skills/test_solve_task_brief.py` (добавить константу `STEP5_RE` и один тест)
- Regenerate: `.codex-plugin/`, `plugin/.codex-plugin/`, `plugin/.claude-plugin/`, `plugin/assets/`
  (сгенерированные манифесты, правятся только скриптом)

**Interfaces:**
- Consumes: `SKILL_PATH` — уже определён в `tests/skills/test_solve_task_brief.py:14` как
  `ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"`; `import re` уже есть на строке 9.
- Produces: модульная константа `STEP5_RE: re.Pattern[str]` (regex-срез шага 5) и тест
  `test_solve_task_step5_asks_for_brief_link_not_verbatim_constraints()`. Других потребителей нет —
  задача единственная в плане.

- [ ] **Step 1: Написать падающий тест**

В `tests/skills/test_solve_task_brief.py` добавить константу сразу после `SKILL_PATH` (строка 14),
рядом с остальными модульными константами:

```python
STEP5_RE = re.compile(
    r"^5\. \*\*Hand off to development\.\*\*(.*?)^## Failure handling", re.S | re.M
)
```

И новый тест в конец файла (после `test_solve_task_uses_only_generic_board_metadata`):

```python
def test_solve_task_step5_asks_for_brief_link_not_verbatim_constraints():
    """PRI-177 (урезанный скоуп): Step 5 требует provenance-ссылку на бриф в спеке
    и явно запрещает копировать Constraints verbatim.

    Ассерты режутся по региону шага 5, а не по всему файлу: `Constraints` и
    `docs/superpowers/briefs/` встречаются в SKILL.md многократно (шаги 0, 4),
    поэтому whole-file-ассерт остался бы зелёным даже при удалённом шаге 5.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")
    m = STEP5_RE.search(text)
    assert m, "Step 5 (hand off) не найден — заголовок шага переименован"
    step5 = m.group(1)
    assert "docs/superpowers/briefs/" in step5   # путь брифа = якорь трассы
    assert "provenance" in step5.lower()         # просим записать происхождение
    assert "Do NOT ask it to copy" in step5      # явный запрет
    assert "verbatim" in step5
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run:
```bash
.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_step5_asks_for_brief_link_not_verbatim_constraints -v
```
Expected: **FAIL** на `assert "provenance" in step5.lower()` — слова `provenance` в текущем
шаге 5 нет. Регион при этом должен найтись (ассерт `m` проходит) и `docs/superpowers/briefs/`
в нём уже есть — падение ровно на одном ассерте подтверждает, что regex-срез работает.

- [ ] **Step 3: Применить правку шага 5**

В `plugin/skills/solve-task/SKILL.md` заменить ровно этот текст:

```
5. **Hand off to development.** Show the brief, state the saved file path
   (`docs/superpowers/briefs/…`), then invoke `superpowers:brainstorming` with the brief **file
   path** as the seed/context — so the brief survives compaction, not just the in-context text.
   From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.
```

на:

```
5. **Hand off to development.** Show the brief, state the saved file path
   (`docs/superpowers/briefs/…`), then invoke `superpowers:brainstorming` with the brief **file
   path** as the seed/context — so the brief survives compaction, not just the in-context text.
   **Ask brainstorming to record the brief's provenance in the spec:** one line under the spec
   heading pointing at the brief's path (`docs/superpowers/briefs/…md`), in the spec's own
   language — the path itself is the greppable anchor for the задача→бриф→спека→PR trace, so no
   dedicated marker is needed. Do NOT ask it to copy the brief's `## Constraints / open
   questions` verbatim: those are open questions brainstorming exists to RESOLVE, and a verbatim
   copy would contradict the very spec that answers them.
   From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.
```

Блоки `**After the PR is created (later in the dev cycle):**` и `**Board-less mode:**`, идущие
ниже внутри шага 5, **не трогать**.

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run:
```bash
.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_step5_asks_for_brief_link_not_verbatim_constraints -v
```
Expected: **PASS**

- [ ] **Step 5: Прогнать весь пакет guard-тестов скиллов**

Run:
```bash
.venv/bin/pytest tests/skills/ -q
```
Expected: всё зелёное. Особое внимание — `test_assembled_prompts.py::test_solve_task_assembled_has_branch_and_tools`
(собирает промпт solve-task с разворачиванием include-маркеров) и
`test_readme_grounding_block.py::test_solve_task_skill_uses_generic_board_setup_hint`
(ассертит подстроки по тому же файлу). Правка не добавляет и не удаляет include-маркеров,
поэтому оба должны остаться зелёными.

- [ ] **Step 6: Линт**

Run:
```bash
.venv/bin/ruff check tests/skills/test_solve_task_brief.py
```
Expected: `All checks passed!`

Примечание: `ruff check .` по всему репозиторию на `dev` не чист — гнаться за repo-wide clean
не нужно, проверяем только изменённый файл.

- [ ] **Step 7: Пересобрать манифесты плагина**

Правка контента под `plugin/` меняет codex payload-digest.

Run:
```bash
python scripts/update_codex_plugin_manifest.py
python scripts/update_codex_plugin_manifest.py --check
```
Expected: первая команда молча пересобирает манифесты, вторая завершается с кодом 0 и без
сообщения «Манифесты плагина рассинхронизированы с pyproject».

Затем убедиться, что install-тесты, читающие payload, зелёные:
```bash
.venv/bin/pytest tests/install/ -q
```
Expected: всё зелёное.

- [ ] **Step 8: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git add .codex-plugin plugin/.codex-plugin plugin/.claude-plugin plugin/assets
git status --short   # убедиться, что попали только эти пути
git commit -m "feat(skills): просить brainstorming ссылаться на бриф в спеке (PRI-177)

Шаг 5 solve-task теперь требует provenance-строку с путём брифа в спеке и
явно запрещает копировать ## Constraints verbatim: это открытые вопросы,
которые brainstorming обязан разрешить, а не переносить. Шаг закрыт
region-scoped guard-тестом — whole-file-ассерт был бы холостым."
```

Ожидаемо изменённых файлов: `SKILL.md`, `test_solve_task_brief.py` и сгенерированные манифесты.
Ничего под `reviewer/` меняться не должно.

---

## После плана

- **PR** в `dev` из `feature/pri-177`. CI-джобы гейтятся только на `main`, поэтому отсутствие
  checks на dev-PR — норма.
- **Доска:** описание PRI-177 требует секции `## Brief` и ассерта `"copy verbatim"`, что прямо
  противоречит реализованному решению. После PR задачу нужно переформулировать под сокращённый
  скоуп либо закрыть с комментарием о ложной посылке (см. §7 спеки) — это решается через
  `/reviewer_finish-task` или вручную, вне этого плана.

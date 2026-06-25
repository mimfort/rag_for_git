# solve-task: персист solution brief — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Скил `solve-task` сохраняет solution brief файлом в `docs/superpowers/briefs/` и ссылается на путь в хендоффе, чтобы бриф пережил компакт контекста / новую сессию (PRI-163).

**Architecture:** Правка только в markdown-скиле `plugin/skills/solve-task/SKILL.md` (инструкция персиста в конце шага 4 + ссылка на путь в шаге 5 + уточнение ноты про запись в репо). Исполняемой логики нет — персист делает LLM в рантайме скила; автоматически проверяется только структурный guard-тест по тексту SKILL.md (как в PRI-146). Папка `briefs/` коммитится (как `specs/`/`plans/`).

**Tech Stack:** Markdown (SKILL.md), Python/pytest (guard-тест в `tests/skills/`).

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Тело SKILL.md — на английском (экономия токенов), как у соседних скилов; новые guard-тесты — докстринг по-русски.
- Коммиты: Conventional Commits на русском (`feat(solve-task): …`), **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Линт: `ruff check .` (line-length 100, target py311).
- Guard-тесты — маркер-проверки текста SKILL.md, НЕ пинят точные формулировки (стиль существующих `tests/skills/`).
- Имя файла брифа: `ГГГГ-ММ-ДД-<KEY>-<slug>.md` с ключом (KEY = пользовательский ключ доски под `key_pattern`, напр. `PRI-163`, не нормализованный store-ключ `ID-163`); board-less — `ГГГГ-ММ-ДД-<slug>.md`. Идемпотентность по префиксу `<дата>-<KEY>-*`.
- Папка `docs/superpowers/briefs/` — коммитится, НЕ в gitignore.

---

### Task 1: Папка `briefs/` + шаг персиста в SKILL.md (guard-driven)

**Files:**
- Create: `docs/superpowers/briefs/.gitkeep`
- Modify: `plugin/skills/solve-task/SKILL.md` (конец шага 4 ~стр. 168; шаг 5 ~стр. 170–172; нота ~стр. 186)
- Test: `tests/skills/test_solve_task_brief.py`

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: в `plugin/skills/solve-task/SKILL.md` появляются стабильные маркеры `docs/superpowers/briefs/`, `Persist the brief`, `file path`, `Board-less`; guard-функция `test_solve_task_persists_brief()` в `tests/skills/test_solve_task_brief.py`.

- [ ] **Step 1: Создать папку `briefs/` со `.gitkeep`**

Чтобы папка существовала в репо до первого реального брифа (рантайм скила также делает `mkdir -p`).

Создать файл `docs/superpowers/briefs/.gitkeep` с пустым содержимым (0 байт).

- [ ] **Step 2: Написать падающий guard-тест**

В конец `tests/skills/test_solve_task_brief.py` (после `test_solve_task_passes_project_scope`) добавить:

```python
def test_solve_task_persists_brief():
    """PRI-163: шаг персиста брифа в docs/superpowers/briefs/ + ссылка на путь в хендоффе."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "docs/superpowers/briefs/" in text   # целевой путь персиста
    assert "Persist the brief" in text          # шаг персиста присутствует
    assert "file path" in text                  # хендофф ссылается на путь к файлу
    assert "Board-less" in text                 # сохранение и без ключа (slug)
```

- [ ] **Step 3: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_persists_brief -q`
Expected: FAIL — `assert "docs/superpowers/briefs/" in text` (маркеры ещё не добавлены в SKILL.md).

- [ ] **Step 4: Добавить абзац персиста в конец шага 4 SKILL.md**

В `plugin/skills/solve-task/SKILL.md`, сразу ПОСЛЕ строки 168 (`Cite \`path:line\` straight from the line-numbered Step 3 snippets — no re-Read (Step 3 contract).`) и ПЕРЕД пустой строкой перед `5. **Hand off to development.**`, вставить:

```markdown

   **Persist the brief (survivability).** After distilling, save the brief to a file so it
   survives context compaction / a new session and seeds the trace задача→бриф→спека→план→PR.
   - **Directory:** `docs/superpowers/briefs/` — create it if missing (`mkdir -p`). Committed like
     `specs/`/`plans/` (leave a trace, do not gitignore).
   - **Filename:** with a task key — `YYYY-MM-DD-<KEY>-<slug>.md`, where `KEY` is the board key
     matching `key_pattern` (e.g. `PRI-163`, NOT the normalized store key `ID-163`) and `slug` is a
     short ASCII kebab of the title. **Board-less** (no key): `YYYY-MM-DD-<slug>.md` (slug from the
     user's formulation). `YYYY-MM-DD` = today's date.
   - **Idempotency:** before writing, glob `docs/superpowers/briefs/<date>-<KEY>-*.md` and overwrite
     the match if any (slug drift between runs must not spawn duplicates); board-less → exact name.
   - **Content:** the distilled brief verbatim (the `# Brief — <KEY> <title>` skeleton); add the
     task `url` on the line below the heading when available, for grep-by-key.
   - **Fail-open:** a failed write (read-only FS, no permission) is non-fatal — note it and still
     hand off with the in-context brief.
```

- [ ] **Step 5: Обновить шаг 5 (Hand off) — ссылка на путь к файлу**

В `plugin/skills/solve-task/SKILL.md` заменить текущий шаг 5:

```markdown
5. **Hand off to development.** Show the brief, then invoke `superpowers:brainstorming` with the brief
   as the seed/context. From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.
```

на:

```markdown
5. **Hand off to development.** Show the brief, state the saved file path
   (`docs/superpowers/briefs/…`), then invoke `superpowers:brainstorming` with the brief **file
   path** as the seed/context — so the brief survives compaction, not just the in-context text.
   From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.
```

- [ ] **Step 6: Уточнить ноту про запись в репо (failure handling)**

В `plugin/skills/solve-task/SKILL.md` заменить последнюю строку секции «Failure handling»:

```markdown
- Read-only on the board; this skill never writes to it.
```

на:

```markdown
- Read-only on the board; this skill never writes to it. The brief file under
  `docs/superpowers/briefs/` is the only write this skill makes — to the repo, not the board.
```

- [ ] **Step 7: Запустить guard-тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`
Expected: PASS (3 теста, включая новый `test_solve_task_persists_brief`).

- [ ] **Step 8: Прогнать весь `tests/skills/` — нет регрессий**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (все guard-тесты скилов зелёные — preflight/brief/assembled_prompts/common_blocks и пр.).

- [ ] **Step 9: Линт**

Run: `.venv/bin/ruff check tests/skills/test_solve_task_brief.py`
Expected: без ошибок в изменённом файле (если ruff не чист по репо в целом — игнорировать чужие предупреждения, важен только изменённый файл).

- [ ] **Step 10: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py docs/superpowers/briefs/.gitkeep
git commit -m "feat(solve-task): персистить solution brief в docs/superpowers/briefs/ + guard-тест"
```

---

### Task 2 (опционально): упоминание `briefs/` в доках

Критериями приёмки НЕ требуется; включать только если найдётся естественное место. Reviewer может отклонить эту правку, приняв Task 1, — поэтому отдельная задача.

**Files:**
- Modify: `README.md` или `CLAUDE.md` (один из — по результату grep)

**Interfaces:**
- Consumes: Task 1 (папка `briefs/` уже существует и описана в скиле).
- Produces: ничего (документационная строка).

- [ ] **Step 1: Найти естественное место**

Run: `grep -rn "docs/superpowers" README.md CLAUDE.md`
Если есть строка, упоминающая `specs/`/`plans/` — добавить рядом `briefs/`. Если совпадений нет — **пропустить Task 2** (нет естественного дома, не плодить разрозненные упоминания).

- [ ] **Step 2: Добавить строку (только если Step 1 нашёл место)**

Рядом с найденным упоминанием specs/plans добавить (адаптировать формулировку под контекст абзаца):

```markdown
`solve-task` дополнительно персистит распиленный solution brief в `docs/superpowers/briefs/`
(`ГГГГ-ММ-ДД-<KEY>-<slug>.md`) — чтобы бриф пережил компакт контекста и достроил трассу
задача→бриф→спека→план→PR.
```

- [ ] **Step 3: Коммит (только если Step 2 выполнен)**

```bash
git add README.md CLAUDE.md
git commit -m "docs(solve-task): упомянуть docs/superpowers/briefs/ (персист брифа PRI-163)"
```

---

## Self-Review

**1. Spec coverage:**
- «Шаг персиста после шага 4, до хендоффа» → Task 1 Step 4. ✓
- «Шаг 5 ссылается на путь к файлу» → Task 1 Step 5. ✓
- «Папка `briefs/`, коммитим» → Task 1 Step 1 (`.gitkeep`) + абзац персиста. ✓
- «Имя `ГГГГ-ММ-ДД-<KEY|slug>.md`, board-less slug» → Task 1 Step 4 (Filename). ✓
- «Идемпотентность по префиксу» → Task 1 Step 4 (Idempotency). ✓
- «Fail-open персиста» → Task 1 Step 4 (Fail-open). ✓
- «Уточнить ноту read-only» → Task 1 Step 6. ✓
- «Guard-тест» → Task 1 Steps 2–3, 7. ✓
- «Весь `tests/skills/` без регрессий» → Task 1 Step 8. ✓
- «Docs (опц.)» → Task 2. ✓

**2. Placeholder scan:** Конкретный код/текст в каждом шаге; точные пути и команды; ожидаемые результаты заданы. Нет TBD/«handle edge cases». ✓

**3. Type consistency:** Маркеры теста (`docs/superpowers/briefs/`, `Persist the brief`, `file path`, `Board-less`) дословно совпадают с текстом, добавляемым в Steps 4–5. Имя guard-функции `test_solve_task_persists_brief` единообразно в Steps 2/3/7. ✓

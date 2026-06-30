# PRI-176 — solve-task: проверка существующих briefs/plans/specs по ключу

**Задача:** https://ru.yougile.com/team/686c049c8af8/#PRI-176  
**Бриф:** `docs/superpowers/briefs/2026-06-29-PRI-176-check-existing-briefs-plans-specs.md`  
**Размер:** S. **Слой:** плагин/скил `solve-task`.

## Проблема

Скил `solve-task` (PRI-163) персистит solution brief в `docs/superpowers/briefs/`. Две проблемы:

1. **Баг в glob-паттерне идемпотентности.** Текущий паттерн `docs/superpowers/briefs/<date>-<KEY>-*.md` привязан к сегодняшней дате. Если бриф создан вчера (`2026-06-28-PRI-164-...`), а сегодня (`2026-06-29`) запустить `solve-task PRI-164` — поиск `2026-06-29-PRI-164-*.md` ничего не найдёт и создаст дубликат.
2. **Нет проверки downstream-артефактов.** Для задачи, уже прошедшей полный цикл (бриф → спека → план), повторный `solve-task` молча перезаписывает бриф, не предупреждая, что спека и план уже существуют.

## Решение

Правки только в `plugin/skills/solve-task/SKILL.md` (параграф **Persist the brief**) + guard-тест в `tests/skills/test_solve_task_brief.py`. Никакой исполняемой логики — инструкции для LLM-агента, который выполняет скил.

### 1. Починить glob-паттерн

Заменить:

```markdown
- **Idempotency:** before writing, glob `docs/superpowers/briefs/<date>-<KEY>-*.md` and overwrite
  the match if any (slug drift between runs must not spawn duplicates); board-less → exact name.
```

на:

```markdown
- **Idempotency:** before writing, glob `docs/superpowers/briefs/*-<KEY>-*.md` and overwrite
  the match if any (slug drift between runs must not spawn duplicates); board-less → exact name.
```

Паттерн `*-<KEY>-*.md` не привязан к дате и находит любой бриф для этого ключа независимо от даты создания.

### 2. Pre-brief artifact check

Добавить подпункт в параграф **Persist the brief**, **сразу перед** подпунктом **Idempotency**:

```markdown
   - **Check for existing artifacts (warn, don't block).** Before writing the brief, scan the
     three artifact directories for files matching this task key (case-insensitive):
     - `docs/superpowers/briefs/*<KEY>*`
     - `docs/superpowers/specs/*<key>*-design.md`
     - `docs/superpowers/plans/*<key>*.md`
     Use case-insensitive matching (e.g., try both `PRI-176` and `pri-176` globs, or lowercase
     file names before matching). If any artifacts are found, warn the user (in Russian):
     > "⚠️ Похожие артефакты уже существуют: briefs/PRI-176-..., specs/pri-176-...-design.md,
     > plans/pri-176-....md. Продолжить? [Y/n]"
     Do **not** block — continue unless the user explicitly says no. If the user continues (or
     auto-permission mode leaves no choice), list the found artifacts under `## Constraints` with
     the tag `[existing_artifacts]`.
```

**Case-insensitive matching:** на macOS/Linux glob чувствителен к регистру. Инструкция требует явно приводить имена файлов к нижнему регистру (или перебирать оба варианта `PRI-N`/`pri-N`) перед сопоставлением.

### 3. Тег в Constraints

Если артефакты найдены и пользователь продолжил, в `## Constraints / open questions` брифа добавлять строку:

```markdown
- `[existing_artifacts] Найдены: briefs/PRI-176-..., specs/pri-176-...-design.md`
```

### 4. Guard-тест

В `tests/skills/test_solve_task_brief.py` добавить:

```python
def test_solve_task_warns_on_existing_artifacts():
    """PRI-176: solve-task проверяет существующие briefs/specs/plans и предупреждает, не блокируя."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "*-<KEY>-*.md" in text               # glob без даты
    assert "docs/superpowers/specs/" in text    # проверка спек
    assert "docs/superpowers/plans/" in text    # проверка планов
    assert "case-insensitive" in text            # insensitive matching
    assert "[Y/n]" in text                       # предупреждение с выбором
    assert "[existing_artifacts]" in text        # тег в Constraints
    assert "Do NOT block" in text or "not block" in text  # не блокировка
```

## Что НЕ делаем

- **Не модифицируем** `superpowers:brainstorming` / `superpowers:writing-plans` — они human-gated.
- **Не добавляем новый риск-тег в список #8** Constraints — `[existing_artifacts]` это информационный тег.
- **Не меняем** формат имён файлов (`YYYY-MM-DD-<KEY>-<slug>.md`) — только glob для их поиска.
- **Не делаем** server-side логику: это markdown-скил, исполняемый LLM-агентом.

## Критерии приёмки

- В `SKILL.md` glob-паттерн идемпотентности использует `*-<KEY>-*.md` (без даты).
- В `SKILL.md` описан case-insensitive поиск по `briefs/`, `specs/`, `plans/`.
- В `SKILL.md` есть текст предупреждения `[Y/n]` и явное требование не блокировать.
- В `SKILL.md` есть требование писать `[existing_artifacts]` в Constraints при продолжении.
- `tests/skills/test_solve_task_brief.py` содержит guard-тест на все маркеры выше.
- Все существующие guard-тесты `tests/skills/` продолжают проходить.

## Затрагиваемые файлы

- `plugin/skills/solve-task/SKILL.md` — параграф **Persist the brief**.
- `tests/skills/test_solve_task_brief.py` — новый guard-тест.

## Вне скоупа

- Автоматическая ротация/чистка старых брифов.
- Жёсткие перекрёстные ссылки между brief/spec/plan.
- Board-less режим (может быть затронут опционально, но не является критерием).

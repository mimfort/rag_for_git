# PRI-237 — убрать эхо fingerprint из промпта file-job'а: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать fingerprint из входа и результата file-job'а в скилле `summarize-subsystems` и закрепить guard-тестами, что авторитетные значения подставляет оркестратор из ответа `get_subsystem_summary_work`.

**Architecture:** Правка чисто промптовая: два абзаца в `plugin/skills/summarize-subsystems/SKILL.md` (шаги 5.2 и 5.3) плюс три guard-теста поверх существующего хелпера `_assembled_skill()`. Серверный контракт `index_subsystem_summary` (fragments с полем `fingerprint`, строгая optimistic-проверка) не меняется — меняется только источник значения при сборке fragments.

**Tech Stack:** Markdown (промпты скилла), pytest (guard-тесты по тексту скилла), `scripts/update_codex_plugin_manifest.py` (пересборка codex payload-digest).

Спека: `docs/superpowers/specs/2026-08-11-pri-237-drop-fingerprint-echo-from-file-job-design.md`
Бриф: `docs/superpowers/briefs/2026-08-11-PRI-237-drop-fingerprint-echo-from-file-job.md`

## Global Constraints

- Ветка работы: `feat/pri-237-drop-fingerprint-echo` (уже создана, спека и бриф в ней закоммичены).
- Правка **только промптовая**: питоновский код `reviewer/` не трогать, миграций и изменений схемы нет.
- Серверный контракт `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash, fragments=[…])` не меняется; шаг 5.4 SKILL.md остаётся дословно прежним (его строка вызова закреплена существующим тестом `test_skill_persists_new_fragments_and_defers_races`).
- Фраза `file prompt must name only its own path` должна сохраниться в шаге 5.2 дословно — её ассертит существующий `test_skill_composes_only_from_ordered_fragment_texts`.
- Легитимные упоминания fingerprint в SKILL.md на строках 45 (`no fingerprints` про сжатый listing) и 144 (`fingerprint granularity`) не трогать.
- Язык проекта — русский: комментарии и докстринги тестов по-русски, тело SKILL.md остаётся английским (так устроены все скиллы плагина).
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- Тесты гонять через `.venv/bin/pytest` (unit, без Postgres/Neo4j/сети).

---

### Task 1: Guard-тесты и правка промптов шагов 5.2/5.3

**Files:**
- Modify: `tests/skills/test_summarize_subsystems.py` (добавить хелпер и три теста в конец файла)
- Modify: `plugin/skills/summarize-subsystems/SKILL.md:84-96` (шаги 5.2 и 5.3)

**Interfaces:**
- Consumes: существующий хелпер `_assembled_skill() -> str` из `tests/skills/test_summarize_subsystems.py:9` (читает SKILL.md и разворачивает `<!-- include: _common/*.md -->` inline).
- Produces: новый хелпер `_file_job_step() -> str` — срез текста шага 5.2; используется только внутри этого файла тестов.

- [ ] **Step 1: Написать падающие guard-тесты**

Добавить в КОНЕЦ файла `tests/skills/test_summarize_subsystems.py`:

```python
_STEP_52_START = "Let pending work be exactly"
_STEP_53_START = "Build the **ordered reused/moved/new fragment texts**"


def _file_job_step() -> str:
    """Срез шага 5.2 (диспатч file-job'ов) — без соседних пунктов 5.1 и 5.3."""
    text = _assembled_skill()
    start = text.index(_STEP_52_START)
    end = text.index(_STEP_53_START, start)
    return " ".join(text[start:end].split())


def test_skill_file_job_does_not_echo_fingerprint():
    """PRI-237: job не получает и не возвращает fingerprint — это серверное значение."""
    step = _file_job_step()
    assert "`{path, summary, provenance}`" in step, (
        "шаг 5.2 не требует от job'а результат {path, summary, provenance}"
    )
    assert "never compute, guess, or return a `fingerprint`" in step, (
        "шаг 5.2 не запрещает job'у выдумывать fingerprint"
    )
    assert "plus that entry's fingerprint" not in step, (
        "шаг 5.2 снова передаёт fingerprint во вход job'а"
    )
    assert "{path, fingerprint" not in step, (
        "шаг 5.2 снова требует fingerprint в результате job'а"
    )


def test_skill_orchestrator_supplies_fingerprints():
    """PRI-237: fingerprint берётся только из ответа get_subsystem_summary_work."""
    normalized = " ".join(_assembled_skill().split())
    assert (
        "The orchestrator attaches each new fragment's `fingerprint` by joining on `path`"
        in normalized
    ), "шаг 5.3 не предписывает оркестратору подстановку fingerprint по path"
    assert (
        "authoritative `added_files` / `changed_files` entries of the "
        "`get_subsystem_summary_work` response"
        in normalized
    ), "шаг 5.3 не называет авторитетный источник fingerprint"
    assert "never from a job's answer" in normalized, (
        "шаг 5.3 не запрещает брать fingerprint из ответа субагента"
    )


def test_skill_rejects_file_job_path_mismatch():
    """PRI-237: path — единственное поле job'а, участвующее в join; рассинхрон не персистится."""
    step = _file_job_step()
    assert "returns a `path` outside the pending list" in step, (
        "шаг 5.2 не описывает рассинхрон path"
    )
    assert "discard that result and re-dispatch the job" in step, (
        "шаг 5.2 не предписывает отбросить результат и перевызвать job"
    )
    assert "on a second mismatch count the cluster as deferred" in step, (
        "шаг 5.2 не переводит кластер в deferred при повторном рассинхроне"
    )
```

- [ ] **Step 2: Прогнать тесты — убедиться, что они падают**

Run: `.venv/bin/pytest tests/skills/test_summarize_subsystems.py -q -k "fingerprint or path_mismatch"`
Expected: FAIL — `test_skill_file_job_does_not_echo_fingerprint` (нет `{path, summary, provenance}`), `test_skill_orchestrator_supplies_fingerprints` (нет фразы про join), `test_skill_rejects_file_job_path_mismatch` (нет описания рассинхрона).

- [ ] **Step 3: Правка шага 5.2 в SKILL.md**

В `plugin/skills/summarize-subsystems/SKILL.md` заменить блок (строки 84-89):

```
   2. Let pending work be exactly `added_files + changed_files`. Dispatch exactly one file-summary job
      on the chosen model for each pending entry, and no other source-reading jobs. Each file prompt must name only its own path
      (plus that entry's fingerprint), tell the job to `Read` exactly that path, and require one Russian result:
      `{path, fingerprint, summary, provenance}`. The orchestrator and every job must not read unchanged
      source files. If per-subagent model override is unavailable, generate the same
      per-file result inline and note that fallback in the report.
```

на:

```
   2. Let pending work be exactly `added_files + changed_files`. Dispatch exactly one file-summary job
      on the chosen model for each pending entry, and no other source-reading jobs. Each file prompt must name only its own path,
      tell the job to `Read` exactly that path, and require one Russian result:
      `{path, summary, provenance}`. A job must never compute, guess, or return a `fingerprint`: that
      value is server-side and the orchestrator supplies it. If a job returns a `path` outside the
      pending list, discard that result and re-dispatch the job for that pending entry once; on a
      second mismatch count the cluster as deferred and persist nothing for it. The orchestrator and
      every job must not read unchanged source files. If per-subagent model override is unavailable,
      generate the same per-file result inline and note that fallback in the report.
```

- [ ] **Step 4: Правка шага 5.3 в SKILL.md**

В том же файле заменить первое предложение пункта 3 (строки 90-91):

```
   3. Build the **ordered reused/moved/new fragment texts** by merging `reused_fragments`,
      `moved_files`, and the new file results, then sorting by `path`. Dispatch exactly one cluster
```

на:

```
   3. Build the **ordered reused/moved/new fragment texts** by merging `reused_fragments`,
      `moved_files`, and the new file results, then sorting by `path`. The orchestrator attaches each
      new fragment's `fingerprint` by joining on `path` with the authoritative `added_files` /
      `changed_files` entries of the `get_subsystem_summary_work` response — never from a job's
      answer. Dispatch exactly one cluster
```

Остаток пункта 3 (начиная с `composer on the chosen model with only those ordered fragment records;`) не менять.

- [ ] **Step 5: Прогнать весь файл тестов скилла**

Run: `.venv/bin/pytest tests/skills/test_summarize_subsystems.py -q`
Expected: PASS — и три новых теста, и все существующие (в частности `test_skill_composes_only_from_ordered_fragment_texts`, ассертящий `file prompt must name only its own path`, и `test_skill_persists_new_fragments_and_defers_races`).

Если `test_skill_composes_only_from_ordered_fragment_texts` покраснел — значит фраза `file prompt must name only its own path` при правке шага 5.2 была изменена; восстановить её дословно, тест не трогать.

- [ ] **Step 6: Коммит**

```bash
git add tests/skills/test_summarize_subsystems.py plugin/skills/summarize-subsystems/SKILL.md
git commit -m "fix(skills): summarize-subsystems — fingerprint подставляет оркестратор, а не file-job"
```

---

### Task 2: Пересборка манифестов плагина и полный прогон

**Files:**
- Modify: артефакты, которые перезапишет `scripts/update_codex_plugin_manifest.py` (манифесты codex-плагина; конкретные пути определяет сам скрипт)

**Interfaces:**
- Consumes: изменённый `plugin/skills/summarize-subsystems/SKILL.md` из Task 1 — его контент входит в codex payload-digest.
- Produces: обновлённые манифесты, на которых `--check` возвращает exit 0 (это же гоняет CI: `.github/workflows/tests.yml:30` и `.github/workflows/codex-plugin.yml:32`).

- [ ] **Step 1: Убедиться, что проверка манифестов краснеет до пересборки**

Run: `python scripts/update_codex_plugin_manifest.py --check; echo "exit=$?"`
Expected: ненулевой exit — digest не совпадает, потому что контент под `plugin/` изменён в Task 1.

Если exit уже 0 — пересборка не нужна (контент digest не затронул); зафиксировать это в отчёте и перейти к Step 3.

- [ ] **Step 2: Пересобрать манифесты**

Run: `python scripts/update_codex_plugin_manifest.py`
Expected: скрипт отработал без ошибок и перезаписал манифесты.

- [ ] **Step 3: Проверить, что digest сошёлся**

Run: `python scripts/update_codex_plugin_manifest.py --check; echo "exit=$?"`
Expected: `exit=0`.

- [ ] **Step 4: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration-тесты исключены по умолчанию через `addopts = -m 'not integration'`).

- [ ] **Step 5: Линт изменённых python-файлов**

Run: `.venv/bin/ruff check tests/skills/test_summarize_subsystems.py`
Expected: `All checks passed!`

Замечание: repo-wide `ruff` на этом репозитории не чист — проверять только изменённый файл.

- [ ] **Step 6: Коммит**

```bash
git add -A
git commit -m "chore(plugin): пересобрать манифесты codex после правки summarize-subsystems"
```

Если Step 1 показал exit=0 и `git status` пуст — коммит пропустить и отметить это в отчёте.

---

## Проверка критериев приёмки

После Task 2 сверить со спекой:

1. `grep -n fingerprint plugin/skills/summarize-subsystems/SKILL.md` → упоминания остались только на строках сжатого listing (`no fingerprints`), в новых фразах шага 5.2 (запрет) и 5.3 (подстановка оркестратором) и в описании гранулярности инвариантов.
2. Шаг 5.3 называет `get_subsystem_summary_work` единственным источником fingerprint.
3. `.venv/bin/pytest tests/skills/ -q` зелёный; откат любой из правок Task 1 краснит новые тесты.
4. `python scripts/update_codex_plugin_manifest.py --check` → exit 0; `.venv/bin/pytest -q` зелёный.
5. `git diff --stat dev...HEAD` не содержит файлов из `reviewer/` — серверный контракт не тронут.

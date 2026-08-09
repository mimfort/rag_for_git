# PRI-219 — поле `summaries` в `reviewer status` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перенести факт «сводки подсистем построены» в отчёт `reviewer status`, чтобы preflight скилла `solve-task` перестал тянуть в контекст полный дамп всех сводок (~8k токенов на прогон).

**Architecture:** `BranchStatus` получает поле `summaries: int | None`; `build_status_report` считает его через keyword-only `summary_store` с тем же fail-soft приёмом, что уже применён к `graph_nodes`. CLI-команда `status` конструирует `SummaryStore` рядом с `ChunkStore`/`GraphStore` и закрывает его в том же `finally`. Скилл `solve-task` читает готовое число из payload Step 0.1, а старый деплой (ключа нет) и сбой стора (`null`) схлопываются в одну фолбэк-ветку.

**Tech Stack:** Python 3.11+, Click (CLI), psycopg/psycopg_pool (Postgres), pytest, ruff.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения CLI. `plugin/skills/**/SKILL.md` пишется по-английски (экономия токенов), но инструктирует отвечать пользователю по-русски.
- Коммиты — Conventional Commits на русском (`feat(cli): …`, `test(services): …`), **без self-attribution** (никаких `Co-Authored-By` и упоминаний Claude).
- `ruff check` — line-length 100, target py311.
- Unit-тесты не открывают внешних и localhost-сокетов; всё, что ходит в Postgres/Neo4j, мокается.
- Ветка `feature/pri-219` от `origin/dev` **уже создана**, бриф и спека закоммичены (`57c1e56`). Upstream снят намеренно — при первом пуше нужен `git push -u origin feature/pri-219`.
- Версия: `0.4.1 → 0.4.2` (правится только в `pyproject.toml`; манифесты плагина синхронизирует скрипт).
- `README.md` и `README.ru.md` правятся синхронно.
- Спека: `docs/superpowers/specs/2026-07-28-pri-219-status-summaries-design.md`.

---

## File Structure

| Файл | Ответственность | Задачи |
|---|---|---|
| `reviewer/services/status.py` | модель отчёта + сбор + оба рендера | 1, 2 |
| `reviewer/entrypoints/cli.py` | проводка стора в команду `status` | 3 |
| `tests/services/test_status.py` | unit-тесты сбора, рендеров и CLI-команды | 1, 2, 3 |
| `plugin/skills/solve-task/SKILL.md` | Step 0.4 «Summary warmth» — потребитель поля | 4 |
| `tests/skills/test_preflight_guardrail.py` | guard на формулировку Step 0.4 | 4 |
| `README.md`, `README.ru.md` | таблица CLI + описание потока `solve-task` | 5 |
| `pyproject.toml` + манифесты плагина | версия и codex payload-digest | 5 |

---

### Task 1: Поле `summaries` в модели и сборщике отчёта

**Files:**
- Modify: `reviewer/services/status.py:16-24` (dataclass `BranchStatus`), `reviewer/services/status.py:49-66` (`build_status_report`)
- Test: `tests/services/test_status.py`

**Interfaces:**
- Consumes: существующие `FakeStore` (`tests/services/test_status.py:11-22`), `FakeGraph` (:25-32).
- Produces: `BranchStatus.summaries: int | None` (последнее поле dataclass, дефолт `None`); `build_status_report(store, graph, repo, branches, repo_path, *, summary_store=None)`. Контракт стора — единственный метод `count_summaries(repo: str, branch: str) -> int`.

**Почему поле последнее и с дефолтом:** `BranchStatus` конструируется **позиционно** семью аргументами в четырёх местах тестов (`tests/services/test_status.py:42-44, 83-86, 104, 120-123, 144`). Поле без дефолта или в середине списка сломает их на арности.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/services/test_status.py` после класса `FakeGraph` (после строки 32):

```python
class FakeSummaryStore:
    def __init__(self, counts, fail=False):
        self._counts, self._fail = counts, fail

    def count_summaries(self, repo, branch):
        if self._fail:
            raise RuntimeError("postgres down")
        return self._counts.get(branch, 0)
```

И три теста в конец файла:

```python
def test_build_status_report_counts_summaries(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(
        meta={"base:main": ("abc1234", dt), "base:dev": ("def5678", dt)},
        chunks={"base:main": 1843, "base:dev": 1850},
        refs=["base:main", "base:dev"])
    graph = FakeGraph(nodes={"main": 1207, "dev": 1190})
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: 0)
    rep = build_status_report(store, graph, "a/x", ["main", "dev"], "/tmp/repo",
                              summary_store=FakeSummaryStore({"main": 26, "dev": 14}))
    assert rep.branches[0].summaries == 26
    assert rep.branches[1].summaries == 14


def test_build_status_report_without_summary_store(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(meta={"base:main": ("abc1234", dt)},
                      chunks={"base:main": 5}, refs=["base:main"])
    graph = FakeGraph(nodes={"main": 3})
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: 0)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo")
    assert rep.branches[0].summaries is None      # обратная совместимость вызовов


def test_build_status_report_summary_store_down(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(meta={"base:main": ("abc1234", dt)},
                      chunks={"base:main": 5}, refs=["base:main"])
    graph = FakeGraph(nodes={"main": 3})
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: 0)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo",
                              summary_store=FakeSummaryStore({}, fail=True))
    assert rep.branches[0].summaries is None      # fail-soft, как у graph_nodes
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/services/test_status.py -q -k summaries`
Expected: FAIL — `TypeError: build_status_report() got an unexpected keyword argument 'summary_store'` (и `AttributeError: 'BranchStatus' object has no attribute 'summaries'`).

- [ ] **Step 3: Добавить поле в dataclass**

В `reviewer/services/status.py` заменить тело `BranchStatus` (строки 17-24):

```python
@dataclass
class BranchStatus:
    branch: str
    ref: str
    indexed_sha: str | None
    updated_at: datetime | None
    chunks: int
    graph_nodes: int | None
    drift: int | None
    summaries: int | None = None
```

- [ ] **Step 4: Считать сводки в `build_status_report`**

Заменить сигнатуру (строки 49-50) на:

```python
def build_status_report(store, graph, repo: str, branches: list[str],
                        repo_path: str, *, summary_store=None) -> RepoStatus:
    """Собрать RepoStatus по веткам. Neo4j и стор сводок fail-soft (поле=None при сбое)."""
```

В цикле по веткам, сразу после блока `try/except` для `graph_nodes` (после строки 62), добавить:

```python
        try:
            summaries = summary_store.count_summaries(repo, branch) if summary_store else None
        except Exception:  # noqa: BLE001 — стор сводок недоступен
            summaries = None
```

И передать значение в конструктор (строки 64-66):

```python
        branch_statuses.append(BranchStatus(
            branch=branch, ref=ref, indexed_sha=sha, updated_at=updated_at,
            chunks=chunks, graph_nodes=graph_nodes, drift=drift, summaries=summaries))
```

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: PASS — все тесты файла, включая три новых и все прежние.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/services/status.py tests/services/test_status.py
git commit -m "feat(services): считать сводки подсистем в отчёте status (PRI-219)"
```

---

### Task 2: Сводки в обоих рендерах

**Files:**
- Modify: `reviewer/services/status.py:75-98` (`render_status_json`), `reviewer/services/status.py:105-133` (`render_status`)
- Test: `tests/services/test_status.py`

**Interfaces:**
- Consumes: `BranchStatus.summaries: int | None` из Task 1.
- Produces: ключ `"summaries"` в per-branch объекте JSON; подстрока `Сводки: <N>` (или `Сводки: —`) в текстовом отчёте.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/services/test_status.py`:

```python
def test_render_status_json_includes_summaries():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567def", dt, 1843, 1207, 0, 26),
            BranchStatus("dev", "base:dev", "def5678901abc", dt, 1850, None, 12, None),
        ],
        overlays=[])
    by = {b["branch"]: b for b in json.loads(render_status_json(rep))["branches"]}
    assert by["main"]["summaries"] == 26
    assert by["dev"]["summaries"] is None       # стор недоступен → null


def test_render_status_shows_summaries():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567", dt, 1843, 1207, 0, 26),
            BranchStatus("dev", "base:dev", "def5678901", dt, 1850, None, 12, None),
        ],
        overlays=[])
    out = render_status(rep, "tree-sitter (fallback)")
    assert "Сводки: 26" in out
    assert "Сводки: —" in out                   # неизвестно
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/services/test_status.py -q -k "render_status_json_includes or render_status_shows"`
Expected: FAIL — `KeyError: 'summaries'` в первом тесте, `assert "Сводки: 26" in out` во втором.

- [ ] **Step 3: Добавить ключ в JSON-рендер**

В `reviewer/services/status.py`, в словаре per-branch (строки 84-92), после `"drift": b.drift,` добавить:

```python
                "summaries": b.summaries,
```

- [ ] **Step 4: Добавить счётчик в текстовый рендер**

Заменить строки 126-127:

```python
        nodes = "—  (Neo4j недоступен)" if b.graph_nodes is None else str(b.graph_nodes)
        summ = "—" if b.summaries is None else str(b.summaries)
        lines.append(f"  Чанки:  {b.chunks}   Узлы графа: {nodes}   Сводки: {summ}")
```

Ветка `indexed_sha is None` делает `continue` выше (строки 114-117), поэтому для непроиндексированной ветки эта строка не печатается — как сейчас не печатаются чанки и узлы графа. Это существующее поведение, менять его не нужно.

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: PASS — включая прежний `test_render_status_shapes_output`, который конструирует `BranchStatus` семью позиционными аргументами.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/services/status.py tests/services/test_status.py
git commit -m "feat(services): показать число сводок в обоих рендерах status (PRI-219)"
```

---

### Task 3: Проводка `SummaryStore` в CLI-команду `status`

**Files:**
- Modify: `reviewer/entrypoints/cli.py:24` (импорт), `reviewer/entrypoints/cli.py:592-620` (команда `status`)
- Test: `tests/services/test_status.py:100-113` и `:140-155` (существующие CLI-тесты) + новый тест

**Interfaces:**
- Consumes: `build_status_report(..., *, summary_store=...)` из Task 1; `SummaryStore(dsn, *, min_size, max_size)` и `SummaryStore.close()` из `reviewer/index/summary_store.py:18-41`.
- Produces: команда `reviewer status` передаёт живой `SummaryStore` в сборщик отчёта и гарантированно закрывает его.

**Почему конструирование безопасно:** `SummaryStore.__init__` только сохраняет параметры, пул открывается лениво в `_ensure_pool` (`summary_store.py:25-32`). Существующая обработка `psycopg.OperationalError` остаётся валидной: при лежащем Postgres команда падает на `count_chunks` раньше, чем дойдёт до сводок.

- [ ] **Step 1: Написать падающий тест и обновить существующие CLI-тесты**

Добавить в конец `tests/services/test_status.py`:

```python
def test_status_command_passes_and_closes_summary_store(monkeypatch, status_report):
    captured = {}

    def fake_build(*a, **k):
        captured.update(k)
        return status_report

    summary_store_cls = MagicMock()
    monkeypatch.setattr(cli_mod, "build_status_report", fake_build)
    monkeypatch.setattr(cli_mod, "ChunkStore", MagicMock())
    monkeypatch.setattr(cli_mod, "GraphStore", MagicMock())
    monkeypatch.setattr(cli_mod, "SummaryStore", summary_store_cls)
    res = CliRunner().invoke(cli_mod.cli, ["status", ".", "--repo", "a/x", "--json"])
    assert res.exit_code == 0, res.output
    assert captured["summary_store"] is summary_store_cls.return_value
    summary_store_cls.return_value.close.assert_called_once()
```

В двух существующих CLI-тестах — `test_status_command_smoke` (строка 100) и `test_status_command_json` (строка 140) — добавить строку рядом с уже мокнутыми сторами, сразу после `monkeypatch.setattr(cli_mod, "GraphStore", MagicMock())`:

```python
    monkeypatch.setattr(cli_mod, "SummaryStore", MagicMock())
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/services/test_status.py -q -k status_command`
Expected: FAIL — `AttributeError: <module 'reviewer.entrypoints.cli'> does not have the attribute 'SummaryStore'` (у `monkeypatch.setattr` нет цели).

- [ ] **Step 3: Импортировать стор в CLI**

В `reviewer/entrypoints/cli.py` после строки 24 (`from reviewer.index.store import ChunkStore`) добавить:

```python
from reviewer.index.summary_store import SummaryStore
```

Порядок импортов — по алфавиту внутри блока `reviewer.index.*`; проверит `ruff`.

- [ ] **Step 4: Подключить стор в команде `status`**

В теле команды `status` (строки 604-614) заменить конструирование и try/finally на:

```python
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    summary_store = SummaryStore(s.pg_dsn, min_size=s.pg_pool_min_size,
                                 max_size=s.pg_pool_max_size)
    try:
        report = build_status_report(store, graph, repo, branches, path,
                                     summary_store=summary_store)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    finally:
        store.close()
        graph.close()
        summary_store.close()
```

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/services/test_status.py tests/entrypoints -q`
Expected: PASS — включая CLI-тесты команд, которые не должны открывать сокеты.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/services/test_status.py
git commit -m "feat(cli): передать стор сводок в команду status (PRI-219)"
```

---

### Task 4: Step 0.4 скилла `solve-task` читает поле вместо дампа

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md:56-71` (пункт `4. **Summary warmth.**`)
- Test: `tests/skills/test_preflight_guardrail.py`

**Interfaces:**
- Consumes: ключ `summaries` в per-branch объекте `reviewer status --json` (Task 2).
- Produces: инструкцию Step 0.4 с тремя состояниями (`> 0` / `== 0` / `null`-или-нет-ключа) и ровно одним упоминанием `get_subsystem_summaries` — в фолбэк-ветке.

**Почему guard именно счётчиком:** сейчас `get_subsystem_summaries` упоминается в секции трижды (основной вызов, ветка «Прогрею сам», fail-open). Ассерт `count(...) == 1` ловит регресс «вернули дамп обратно» надёжнее, чем поиск подстроки.

- [ ] **Step 1: Написать падающий guard-тест**

Добавить в конец `tests/skills/test_preflight_guardrail.py`:

```python
def _summary_warmth_section() -> str:
    """Вырезать пункт 4 preflight'а solve-task — от заголовка до блока Decisions."""
    text = SOLVE.read_text(encoding="utf-8")
    start = text.index("4. **Summary warmth.**")
    return text[start:text.index("Decisions:", start)]


def test_solve_task_reads_summaries_from_status():
    # теплота сводок берётся из payload'а status, полученного в Step 0.1
    assert "summaries" in _summary_warmth_section()


def test_solve_task_probes_summaries_only_as_fallback():
    # единственное упоминание тула — фолбэк для деплоя старше поля summaries
    assert _summary_warmth_section().count("get_subsystem_summaries") == 1


def test_solve_task_keeps_three_warmth_options():
    section = _summary_warmth_section()
    assert "Прогреть сейчас" in section
    assert "Прогрею сам" in section
    assert "Пропустить" in section
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/skills/test_preflight_guardrail.py -q`
Expected: FAIL — `test_solve_task_reads_summaries_from_status` (в секции нет слова `summaries`) и `test_solve_task_probes_summaries_only_as_fallback` (`assert 3 == 1`). Третий тест проходит уже сейчас — он фиксирует то, что менять нельзя.

- [ ] **Step 3: Переписать пункт 4 в SKILL.md**

Заменить в `plugin/skills/solve-task/SKILL.md` весь блок со строки 56 (`   4. **Summary warmth.** Call ...`) по строку 71 (`        same options, but include the error detail in option 3's Constraints note.`) на:

```markdown
   4. **Summary warmth.** Read `summaries` from the branch object of the Step 0.1 status payload —
      do NOT probe the summaries tool here. Skip this check if `drift == null` (no index at all —
      summaries can't exist).
      - `summaries > 0` → silently continue (no message needed — summaries are warm).
      - `summaries == 0` (summaries not built yet) → tell the user (in Russian): «Сводки подсистем
        не построены — архитектурный приор будет пустым. Как поступим?» and present **three
        options**:
        1. «Прогреть сейчас» → delegate to `/reviewer_summarize-subsystems`, wait for it to
           complete, then continue. (Good if using the default model.)
        2. «Прогрею сам» → **PAUSE HERE** and wait for the user to write something like «готово»,
           «прогрел», «done» or any confirmation that they have run their own tool (e.g. an
           external CLI with a cheaper model). Once confirmed, re-run
           `uvx --from rag-reviewer reviewer status <path> --branch <branch> --json` and verify
           `summaries > 0`, then continue.
        3. «Пропустить» → note in brief under **Constraints**: «сводки подсистем не построены;
           `/reviewer_summarize-subsystems` не запускался». Continue without them.
      - `summaries` is `null`, or the key is absent (deploy older than this field) → fall back to
        the legacy probe: call `get_subsystem_summaries(repo, branch)` and use the returned count
        with the same three options; an error from that call counts as 0 and adds the error detail
        to option 3's Constraints note.
```

- [ ] **Step 4: Обновить строку `Decisions:` под пунктом 4**

Сразу под пунктом 4 идёт абзац `Decisions: …`. Заменить в нём фрагмент `summaries missing → three-way choice (build now / build yourself / skip).` на:

```markdown
summaries missing → three-way choice (build now / build yourself / skip), read from the status
payload instead of dumping every summary into context.
```

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/skills -q`
Expected: PASS — новые guard-тесты плюс прежние (`test_solve_task_has_preflight` и др.: `reviewer status`, `--json`, `drift`, `sync_board(`, `reviewer_sync-codebase` остались на месте).

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_preflight_guardrail.py
git commit -m "feat(skills): читать теплоту сводок из status в preflight solve-task (PRI-219)"
```

---

### Task 5: Документация, версия и манифесты плагина

**Files:**
- Modify: `README.md:652` (строка `status` в таблице команд), `README.md:722` (буллет `- **Flow:**` секции `reviewer_solve-task`)
- Modify: `README.ru.md:553` (та же строка таблицы), `README.ru.md:621` (буллет `- **Поток:**`)
- Modify: `pyproject.toml:3` (версия)
- Regenerate: `plugin/.claude-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json`, `.codex-plugin/plugin.json` — **скриптом, не руками**

**Interfaces:**
- Consumes: поведение из Task 2 (JSON/текстовый рендер) и Task 4 (preflight скилла).
- Produces: синхронные README и валидные манифесты плагина (`scripts/update_codex_plugin_manifest.py --check` без ошибок).

**Про версии:** `sync_plugin_metadata` (`reviewer/install_codex.py:153-178`) сам проставляет версию из `pyproject.toml` в оба манифеста плагина и пересчитывает payload-digest. Руками правится только `pyproject.toml`.

- [ ] **Step 1: Обновить таблицу команд в обоих README**

`README.md:652` — заменить последнюю ячейку строки `status` на:

```
Index health / freshness vs the clone's HEAD, including per-branch chunk, graph-node and subsystem-summary counts. Spends no Voyage quota.
```

`README.ru.md:553` — заменить последнюю ячейку строки `status` на:

```
Здоровье/свежесть индекса vs HEAD клона, включая число чанков, узлов графа и сводок подсистем по каждой ветке. Квоту Voyage не тратит.
```

- [ ] **Step 2: Обновить описание потока `solve-task` в обоих README**

`README.md:722` — заменить начало буллета `- **Flow:** resolve generic board config →` на:

```
- **Flow:** preflight (index freshness and subsystem-summary warmth, both read from one
  `reviewer status --json` payload) → resolve generic board config →
```

`README.ru.md:621` — заменить начало буллета `- **Поток:** резолв generic board config →` на:

```
- **Поток:** preflight (свежесть индекса и теплота сводок подсистем — оба факта из одного
  payload'а `reviewer status --json`) → резолв generic board config →
```

- [ ] **Step 3: Бампнуть версию**

В `pyproject.toml:3` заменить `version = "0.4.1"` на `version = "0.4.2"`.

- [ ] **Step 4: Пересобрать манифесты плагина**

Run:
```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
```
Expected: первая команда молча перезаписывает три манифеста, вторая ничего не печатает и выходит с кодом 0.

- [ ] **Step 5: Полная верификация**

Run:
```bash
.venv/bin/pytest -q
.venv/bin/ruff check reviewer/services/status.py reviewer/entrypoints/cli.py \
    tests/services/test_status.py tests/skills/test_preflight_guardrail.py
```
Expected: pytest зелёный (в том числе `tests/install/test_codex_plugin_payload.py`, который краснеет при несобранном digest); ruff — `All checks passed!` по перечисленным файлам. `ruff check .` по всему репозиторию чистым не был и до этой задачи — гнаться за этим не нужно.

- [ ] **Step 6: Коммит**

```bash
git add README.md README.ru.md pyproject.toml plugin/.claude-plugin/plugin.json \
        plugin/.codex-plugin/plugin.json .codex-plugin/plugin.json
git commit -m "docs: описать поле summaries в status и бампнуть версию 0.4.2 (PRI-219)"
```

---

## Проверка критериев приёмки

| № | Критерий | Где закрыт |
|---|---|---|
| 1 | `summaries` в каждом branch-объекте JSON: число / `null` | Task 2, `test_render_status_json_includes_summaries` |
| 2 | `build_status_report` без `summary_store` → `summaries=None` | Task 1, `test_build_status_report_without_summary_store` |
| 3 | Текстовый вывод показывает число сводок, «—» при неизвестном | Task 2, `test_render_status_shows_summaries` |
| 4 | Preflight не зовёт `get_subsystem_summaries`, когда поле пришло; при `null`/отсутствии — прежнее поведение и те же три опции | Task 4, `test_solve_task_probes_summaries_only_as_fallback` + `test_solve_task_keeps_three_warmth_options` |
| 5 | Unit-тесты: happy path, `None`, исключение стора, оба рендера, guard на Step 0.4 | Tasks 1, 2, 4 |
| 6 | `pytest -q` зелёный, `ruff check` чист по изменённым файлам | Task 5, Step 5 |

## После реализации

- Первый пуш: `git push -u origin feature/pri-219` (upstream снят намеренно, чтобы `git push` не ушёл в защищённую `dev`).
- PR в `dev`. Конфликт с незамерженной `feature/pri-177` возможен только в digest-строке `plugin/.codex-plugin/plugin.json` (SKILL.md правится в разных разделах — Step 0.4 против Step 5); разрешается повторным прогоном `scripts/update_codex_plugin_manifest.py` после мержа.
- После мержа и публикации — закрыть задачу скиллом `/reviewer_finish-task`.

# PRI-265 — Эвал-харнесс перестаёт молча терять свидетельства: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Прогон офлайн-харнесса перестаёт стирать ручные разделы отчётов, а сбой обхода графа перестаёт маскироваться под честно пустое контекстное ядро.

**Architecture:** Новый чистый модуль `eval/solve_task_metrics/report_merge.py` разделяет генерируемую и ручную части отчёта явным маркером и сливает их при записи; проверка пригодности файла стоит ДО дорогого прогона. В `replay.py` появляется отдельный статус `context_retrieval_failed` и новый ключ снимка `context_statuses`, строго аддитивно к существующим числам; `replay_report.py` печатает их отдельным разделом. Отдельным треком в промпте плагина `solve-task` появляется дословный шаблон стартовой панели с запретами и guard-тестом.

**Tech Stack:** Python 3.12, pytest (unit, без Postgres/Neo4j/сети), стандартная библиотека (`random` для property-теста — `hypothesis` в проекте нет).

**Spec:** `docs/superpowers/specs/2026-08-21-pri-265-eval-harness-evidence-loss-design.md`

## Global Constraints

- Ветка работы: `feat/pri-265-eval-evidence-loss` (уже создана, не переключаться).
- Язык проекта — русский: комментарии, докстринги, сообщения CLI.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- Все новые тесты — unit: без Postgres, Neo4j, Voyage и localhost-сокетов. Провайдер и git-раннер инъектируются (образцы — `tests/eval/test_replay.py`).
- `reviewer/metrics/brief_quality/**` не меняется ни одной строкой: ни I/O, ни новой логики. `tests/metrics/test_reexport_guard.py` обязан остаться зелёным.
- Существующие числа агрегата (`aggregate`) и существующий кортеж `STATUSES` не меняются — только добавление новых ключей.
- Правка `plugin/**` меняет payload-digest → обязателен прогон `scripts/update_codex_plugin_manifest.py`, иначе install-тесты краснеют.
- Полный прогон перед завершением: `.venv/bin/pytest -q` (baseline — всё зелёное; любое падение = регрессия).

---

### Task 1: Модуль слияния отчёта `report_merge.py`

Чистое ядро механизма: маркер границы, побайтовый хвост, слияние, проверка пригодности файла. Ничего не пишет на диск (читает — да, пишет — нет).

**Files:**
- Create: `eval/solve_task_metrics/report_merge.py`
- Test: `tests/eval/test_report_merge.py`

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: `MARKER: str`, `class MarkerMissing(Exception)`, `manual_tail(existing: str) -> str`, `merge(generated: str, existing: str) -> str`, `ensure_mergeable(path: pathlib.Path) -> None`, `merge_file(path: pathlib.Path, generated: str) -> str`. Задача 2 зовёт `ensure_mergeable` и `merge_file`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/eval/test_report_merge.py`:

```python
"""Слияние генерируемой и ручной частей отчёта (PRI-265)."""
from __future__ import annotations

import pathlib

import pytest

from eval.solve_task_metrics import report_merge

MANUAL = "## Приёмка PRI-262\n\nЧисла, оговорки, ручной разбор.\n"


def _with_marker(generated: str, manual: str) -> str:
    return f"{generated}\n{report_merge.MARKER}\n\n{manual}"


def test_manual_tail_starts_at_marker_and_is_byte_exact():
    existing = _with_marker("# Отчёт\n\nстарые числа\n", MANUAL)
    tail = report_merge.manual_tail(existing)
    assert tail.startswith(report_merge.MARKER)
    assert tail.endswith(MANUAL)
    # хвост — срез исходного текста, а не пересборка
    assert tail == existing[existing.index(report_merge.MARKER):]


def test_manual_tail_of_empty_text_is_empty():
    assert report_merge.manual_tail("") == ""


def test_merge_keeps_manual_tail_byte_for_byte():
    existing = _with_marker("# Отчёт\n\nстарые числа\n", MANUAL)
    merged = report_merge.merge("# Отчёт\n\nНОВЫЕ числа\n", existing)
    assert "НОВЫЕ числа" in merged
    assert "старые числа" not in merged
    assert merged.endswith(existing[existing.index(report_merge.MARKER):])


def test_merge_without_existing_file_appends_marker():
    merged = report_merge.merge("# Отчёт\n\nчисла\n", "")
    assert merged.count(report_merge.MARKER) == 1
    assert merged.rstrip().endswith(report_merge.MARKER)


def test_merge_is_idempotent_and_never_duplicates_marker():
    first = report_merge.merge("# Отчёт\n\nA\n", "")
    second = report_merge.merge("# Отчёт\n\nB\n", first)
    third = report_merge.merge("# Отчёт\n\nC\n", second)
    assert third.count(report_merge.MARKER) == 1


def test_ensure_mergeable_accepts_missing_file(tmp_path: pathlib.Path):
    report_merge.ensure_mergeable(tmp_path / "нет-такого.md")


def test_ensure_mergeable_accepts_file_with_marker(tmp_path: pathlib.Path):
    path = tmp_path / "r.md"
    path.write_text(_with_marker("# Отчёт\n", MANUAL), encoding="utf-8")
    report_merge.ensure_mergeable(path)


def test_ensure_mergeable_rejects_file_without_marker(tmp_path: pathlib.Path):
    """Догадка о границе запрещена: файл без маркера отвергается, а не режется."""
    path = tmp_path / "r.md"
    path.write_text("# Отчёт\n\n## Приёмка PRI-262\n\nручное\n", encoding="utf-8")
    with pytest.raises(report_merge.MarkerMissing) as error:
        report_merge.ensure_mergeable(path)
    assert str(path) in str(error.value)
    assert report_merge.MARKER in str(error.value)


def test_merge_file_reads_existing_and_returns_without_writing(tmp_path: pathlib.Path):
    path = tmp_path / "r.md"
    original = _with_marker("# Отчёт\n\nстарое\n", MANUAL)
    path.write_text(original, encoding="utf-8")
    merged = report_merge.merge_file(path, "# Отчёт\n\nновое\n")
    assert "новое" in merged and MANUAL in merged
    assert path.read_text(encoding="utf-8") == original  # запись — дело вызывающего
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/eval/test_report_merge.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.report_merge'`

- [ ] **Step 3: Написать модуль**

Создать `eval/solve_task_metrics/report_merge.py`:

```python
"""Слияние генерируемой и ручной частей markdown-отчёта (PRI-265).

Прогон харнесса писал отчёт целиком и уничтожал накопленный ручной разбор
приёмок — единственное место, где живут числа и оговорки прошлых замеров.
Граница проводится ЯВНЫМ маркером: догадка о том, где кончается генерируемое,
— тот же класс молчаливой потери, который этот модуль чинит.
"""
from __future__ import annotations

import pathlib

MARKER = "<!-- generated:end — ниже ручные разделы, прогон их не трогает -->"


class MarkerMissing(Exception):
    """В существующем отчёте нет маркера границы: сливать не с чем."""


def manual_tail(existing: str) -> str:
    """Ручной хвост отчёта — срезом от маркера включительно.

    Побайтовость обеспечена конструкцией (срез исходной строки), а не
    аккуратностью вызывающего: пересобранный текст рано или поздно разойдётся
    с исходным на пустой строке или переносе.
    """
    if not existing:
        return ""
    index = existing.find(MARKER)
    return "" if index < 0 else existing[index:]


def merge(generated: str, existing: str) -> str:
    """Генерируемая часть + маркер + ручной хвост дословно."""
    tail = manual_tail(existing)
    head = generated.rstrip("\n")
    if not tail:
        return f"{head}\n\n{MARKER}\n"
    return f"{head}\n\n{tail}"


def ensure_mergeable(path: pathlib.Path) -> None:
    """Проверить, что отчёт можно перезаписать без потери ручной части.

    Вызывать ДО прогона, а не при записи: запись стоит после дорогого прогона
    (Voyage, обход графа), и отказ на записи означал бы «прогон отработал,
    результат записать нельзя» — потеря того же класса.
    """
    if not path.exists():
        return
    if MARKER in path.read_text(encoding="utf-8"):
        return
    raise MarkerMissing(
        f"В отчёте {path} нет маркера границы, прогон бы стёр ручные разделы.\n"
        f"Вставьте строку-маркер между генерируемой частью и первым ручным\n"
        f"разделом и повторите прогон:\n{MARKER}"
    )


def merge_file(path: pathlib.Path, generated: str) -> str:
    """Итоговый текст отчёта с сохранённым хвостом. Ничего не пишет."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    return merge(generated, existing)
```

- [ ] **Step 4: Прогнать тесты — зелено**

Run: `.venv/bin/pytest -q tests/eval/test_report_merge.py`
Expected: PASS (9 тестов)

- [ ] **Step 5: Коммит**

```bash
git add eval/solve_task_metrics/report_merge.py tests/eval/test_report_merge.py
git commit -m "feat(eval): маркер границы и слияние ручной части отчёта"
```

---

### Task 2: Подключить слияние к обеим командам и разметить существующие отчёты

Механизм становится действующим: обе команды проверяют пригодность отчёта до прогона и пишут через слияние. В существующие отчёты маркер вставляется вручную — граница известна точно.

**Files:**
- Modify: `eval/solve_task_metrics/__main__.py` (импорт, `cmd_snapshot` ~строка 44-65, `cmd_replay` ~строка 231-280)
- Modify: `eval/replay_report.md` (вставить маркер между строкой 107 и `## Приёмка PRI-255`)
- Modify: `eval/solve_task_metrics_report.md` (маркер в конец файла)
- Test: `tests/eval/test_replay_cli.py` (дополнить)

**Interfaces:**
- Consumes: `report_merge.ensure_mergeable`, `report_merge.merge_file`, `report_merge.MarkerMissing`, `report_merge.MARKER` из Task 1.
- Produces: гарантия «прогон не пишет отчёт без маркера»; поведение `cmd_replay`/`cmd_snapshot` → `1` с сообщением.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `tests/eval/test_replay_cli.py` (в конец файла; импорты `pathlib` и `report_merge` добавить сверху):

```python
def test_replay_refuses_report_without_marker_before_any_run(tmp_path, monkeypatch, capsys):
    """Fail-closed стоит ДО прогона: инфраструктура не трогается вовсе.

    Тест проходит без Postgres/Neo4j именно потому, что отказ случается раньше
    открытия живых зависимостей: любой выход в сеть здесь уронил бы тест.
    """
    report = tmp_path / "replay_report.md"
    report.write_text("# Отчёт\n\n## Приёмка PRI-262\n\nручное\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPLAY_REPORT_PATH", report)
    args = cli.build_parser().parse_args(["replay"])
    assert cli.cmd_replay(args) == 1
    out = capsys.readouterr().out
    assert str(report) in out
    assert report_merge.MARKER in out
    # ручной текст на месте: отказ ничего не переписал
    assert "## Приёмка PRI-262" in report.read_text(encoding="utf-8")


def test_snapshot_refuses_report_without_marker_before_any_run(tmp_path, monkeypatch, capsys):
    report = tmp_path / "solve_task_metrics_report.md"
    report.write_text("# Метрики\n\n## Ручное\n\nтекст\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPORT_PATH", report)
    args = cli.build_parser().parse_args(["snapshot"])
    assert cli.cmd_snapshot(args) == 1
    assert report_merge.MARKER in capsys.readouterr().out
```

И убедиться, что оба отчёта репозитория размечены:

```python
def test_repository_reports_carry_the_marker():
    """Оба отчёта репозитория пригодны к слиянию — иначе первый же прогон откажет."""
    for path in (cli.REPLAY_REPORT_PATH, cli.REPORT_PATH):
        assert report_merge.MARKER in path.read_text(encoding="utf-8"), path
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/eval/test_replay_cli.py`
Expected: FAIL — `cmd_replay` возвращает не `1` (уходит в `live.open_live`) и маркера в отчётах нет.

- [ ] **Step 3: Разметить существующие отчёты**

В `eval/replay_report.md` вставить ровно две строки — пустую и маркер — между концом генерируемой части (последняя строка блока `## Оговорка`, строка 107) и заголовком `## Приёмка PRI-255` (строка 108). Проверить глазами: выше маркера — только `# Replay-метрики…`, `## Идентичность прогона`, `## Агрегат`, `## Статусы задач`, `## Задачи`, `## Оговорка`; ниже — все ручные приёмки.

В `eval/solve_task_metrics_report.md` дописать в конец файла пустую строку и маркер (ручных разделов там сейчас нет — маркер ставится превентивно).

Проверка после правки:

```bash
grep -n 'generated:end' eval/replay_report.md eval/solve_task_metrics_report.md
sed -n '104,112p' eval/replay_report.md
```

- [ ] **Step 4: Подключить модуль в CLI**

В `eval/solve_task_metrics/__main__.py` добавить `report_merge` в общий импорт пакета (рядом с `report`, `replay_report`).

В `cmd_snapshot` — проверка первым делом, до `ground_truth.git_runner` и построения снимка:

```python
def cmd_snapshot(_args) -> int:
    try:
        report_merge.ensure_mergeable(REPORT_PATH)
    except report_merge.MarkerMissing as error:
        print(str(error))
        return 1
    run_git = ground_truth.git_runner(REPO_ROOT)
```

и запись через слияние вместо полной перезаписи:

```python
    REPORT_PATH.write_text(
        report_merge.merge_file(REPORT_PATH, report.render(snap, rows)),
        encoding="utf-8",
    )
```

В `cmd_replay` — проверка после валидации варианта/оверрайдов, но ДО `from . import live` и `live.open_live`:

```python
    try:
        report_merge.ensure_mergeable(REPLAY_REPORT_PATH)
    except report_merge.MarkerMissing as error:
        print(str(error))
        return 1

    from . import live  # ленивый импорт: живые зависимости только здесь
```

и запись:

```python
    REPLAY_REPORT_PATH.write_text(
        report_merge.merge_file(
            REPLAY_REPORT_PATH, replay_report.render(snap, baseline_snap)
        ),
        encoding="utf-8",
    )
```

- [ ] **Step 5: Прогнать тесты — зелено**

Run: `.venv/bin/pytest -q tests/eval/`
Expected: PASS, включая три новых теста и все существующие.

- [ ] **Step 6: Коммит**

```bash
git add eval/solve_task_metrics/__main__.py eval/replay_report.md eval/solve_task_metrics_report.md tests/eval/test_replay_cli.py
git commit -m "fix(eval): прогон больше не стирает ручные разделы отчётов"
```

---

### Task 3: Статус сбоя обхода и его видимость в отчёте

Сбой обхода графа получает собственное имя и собственный счётчик, не смешиваясь с честно пустым знаменателем. Данные и рендер идут одной задачей: статус, который никуда не выводится, требования тикета не закрывает.

**Files:**
- Modify: `eval/solve_task_metrics/replay.py` (константы 20-33, `_evaluate` 80-120, цикл 168-183, снимок 220-258)
- Modify: `eval/solve_task_metrics/replay_report.py` (раздел после строки 130)
- Test: `tests/eval/test_replay.py` (дополнить), `tests/eval/test_replay_report.py` (дополнить)

**Interfaces:**
- Consumes: ничего из Task 1-2.
- Produces: `replay.STATUS_CONTEXT_FAILED`, `replay.CONTEXT_STATUSES`, `replay.CONTEXT_EVALUATED_STATUSES`, ключ снимка `context_statuses: dict[str, int]`, раздел отчёта `## Статусы контекста`.

- [ ] **Step 1: Написать падающие тесты снимка**

Дописать в `tests/eval/test_replay.py`:

```python
class FailingNeighbours:
    """Провайдер, у которого падает ровно обход графа (Neo4j недоступен)."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def neighbors(self, repo, branch, node_ids):
        raise RuntimeError("граф недоступен")


def _run_with_failing_graph(tmp_path, keys, *, tasks, changed, predicted):
    provider = FailingNeighbours(FakeProvider(tasks, predicted))
    return replay.run_replay(
        provider=provider,
        run_git=FakeGit(changed),
        briefs_dir=_corpus(tmp_path, keys),
        target=_target(),
        variant_name="baseline",
        commit="deadbee",
        taken_at="2026-08-17T00:00:00+00:00",
    )


def test_graph_failure_is_named_not_confused_with_empty_core(tmp_path):
    """Сбой обхода отличим от честно пустого ядра — иначе тонет в штатном шуме."""
    snap = _run_with_failing_graph(
        tmp_path,
        ["PRI-21"],
        tasks={"PRI-21": {"title": "PRI-21", "description": ""}},
        changed={"PRI-21": ["reviewer/a.py"]},
        predicted={"PRI-21": ["reviewer/a.py"]},
    )
    row = snap["tasks"][0]
    assert row["context_status"] == replay.STATUS_CONTEXT_FAILED
    assert row["context_status"] != replay.STATUS_EMPTY_CONTEXT
    assert row["context_recall"] is None
    # прогон корпуса не прерван: задача измерена по core как обычно
    assert row["status"] == replay.STATUS_MEASURED


def test_graph_failure_is_counted_in_context_statuses(tmp_path):
    snap = _run_with_failing_graph(
        tmp_path,
        ["PRI-22"],
        tasks={"PRI-22": {"title": "PRI-22", "description": ""}},
        changed={"PRI-22": ["reviewer/a.py"]},
        predicted={"PRI-22": ["reviewer/a.py"]},
    )
    assert snap["context_statuses"][replay.STATUS_CONTEXT_FAILED] == 1
    assert snap["context_statuses"][replay.STATUS_EMPTY_CONTEXT] == 0


def test_context_statuses_count_only_tasks_that_reached_the_traversal(tmp_path):
    """Задача без ground truth до обхода не доходит и счётчик не подкрашивает."""
    snap = _run(
        tmp_path,
        ["PRI-23"],
        tasks={"PRI-23": {"title": "PRI-23", "description": ""}},
        changed={},
        predicted={},
        missing=["PRI-23"],
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_NO_GROUND_TRUTH
    assert sum(snap["context_statuses"].values()) == 0


def test_context_failure_does_not_move_any_existing_number(tmp_path):
    """Свойство аддитивности на случайных входах (критерий 3 PRI-265).

    Сторона «до» — обход, честно вернувший пусто; сторона «после» — упавший
    обход. Ни одно существующее число агрегата и ни один счётчик STATUSES не
    имеет права разойтись: новый статус добавляет знание, а не меняет метрику.
    """
    import random

    rnd = random.Random(20265)
    for index in range(25):
        keys = [f"PRI-{i}" for i in range(1, rnd.randrange(2, 6))]
        tasks = {k: {"title": k, "description": ""} for k in keys}
        changed = {k: ["reviewer/a.py"] for k in keys if rnd.random() < 0.8}
        predicted = {k: ["reviewer/a.py"] for k in keys if rnd.random() < 0.7}
        empty_dir = tmp_path / f"e{index}"
        failed_dir = tmp_path / f"f{index}"
        empty_dir.mkdir()
        failed_dir.mkdir()
        empty = _run(empty_dir, keys, tasks=tasks, changed=changed,
                     predicted=predicted, neighbors=set())
        failed = _run_with_failing_graph(failed_dir, keys, tasks=tasks,
                                         changed=changed, predicted=predicted)
        assert failed["aggregate"] == empty["aggregate"]
        assert failed["statuses"] == empty["statuses"]
        assert failed["context_statuses"] != empty["context_statuses"]
```

Примечание для исполнителя: `_corpus` делает `(dir / "briefs").mkdir()` без `parents=True` и падает на уже существующем каталоге, поэтому каждой итерации property-теста даётся свой заранее созданный подкаталог. Один и тот же `tmp_path` дважды подряд в `_run` передавать нельзя.

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/eval/test_replay.py`
Expected: FAIL — `AttributeError: module 'eval.solve_task_metrics.replay' has no attribute 'STATUS_CONTEXT_FAILED'`

- [ ] **Step 3: Реализовать статус в `replay.py`**

Константы (после `STATUS_EMPTY_CONTEXT`):

```python
STATUS_CONTEXT_FAILED = "context_retrieval_failed"

CONTEXT_STATUSES = (
    STATUS_MEASURED,
    STATUS_EMPTY_CONTEXT,
    STATUS_CONTEXT_FAILED,
)

CONTEXT_EVALUATED_STATUSES = (
    STATUS_MEASURED,
    STATUS_EMPTY_CORE,
    STATUS_NO_TASK,
)
"""Статусы задач, у которых обход графа вообще выполнялся.

Задача без ground truth или с упавшим ретривом до обхода не доходит, и её
дефолтный context_status не должен подмешиваться в счётчики контекста: иначе
новый блок отчёта врёт с первого дня.
"""
```

`_evaluate` получает флаг сбоя и перестаёт выдавать пустое множество за факт:

```python
def _evaluate(key: str, predicted: set, truth, run_git, context_core_paths: set,
              context_failed: bool = False) -> dict:
```

и внутри, вместо прежней строки `context_status = ...`:

```python
    if context_failed:
        context_status = STATUS_CONTEXT_FAILED
    else:
        context_status = STATUS_MEASURED if context_core_paths else STATUS_EMPTY_CONTEXT
```

В цикле `run_replay` — поднять флаг вместо молчаливого пустого множества:

```python
        context_failed = False
        try:
            seeds = context_seeds.collect_seeds(truth, run_git)
            core_now = context_core.derive_context_core(
                seeds.symbols,
                {p for p in truth.changed if classify.is_core_production_path(p)},
                lambda ids: provider.neighbors(target.repo, target.branch, ids),
                allowed_names=seeds.called_names,
            )
        except Exception:  # noqa: BLE001 — недоступный граф не роняет прогон корпуса
            core_now = set()
            context_failed = True
        row = _evaluate(key, predicted, truth, run_git, core_now,
                        context_failed=context_failed)
```

В возвращаемый снимок добавить ключ рядом с `statuses` (существующий `statuses` не трогать):

```python
        "context_statuses": {
            status: sum(
                1 for r in rows
                if r["status"] in CONTEXT_EVALUATED_STATUSES
                and r["context_status"] == status
            )
            for status in CONTEXT_STATUSES
        },
```

- [ ] **Step 4: Прогнать тесты снимка — зелено**

Run: `.venv/bin/pytest -q tests/eval/test_replay.py`
Expected: PASS (все прежние + 4 новых)

- [ ] **Step 5: Написать падающий тест рендера**

Дописать в `tests/eval/test_replay_report.py`:

```python
def test_report_renders_context_statuses_section():
    snap = _snap("baseline", [_task("PRI-1", 0.5, ["reviewer/a.py"])], 0.5)
    snap["context_statuses"] = {
        "measured": 3,
        "empty_context_denominator": 16,
        "context_retrieval_failed": 2,
    }
    text = replay_report.render(snap)
    assert "## Статусы контекста" in text
    assert "| context_retrieval_failed | 2 |" in text


def test_report_without_context_statuses_key_still_renders():
    """Снимок старого формата ключа не имеет — A/B со старой историей не ломается."""
    snap = _snap("baseline", [_task("PRI-1", 0.5, ["reviewer/a.py"])], 0.5)
    assert "context_statuses" not in snap
    text = replay_report.render(snap)
    assert "## Статусы контекста" not in text
    assert "## Статусы задач" in text
```

- [ ] **Step 6: Прогнать и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/eval/test_replay_report.py`
Expected: FAIL — раздела `## Статусы контекста` в выводе нет.

- [ ] **Step 7: Реализовать раздел в `replay_report.py`**

Сразу после блока `## Статусы задач` (после `lines.append("")` на строке ~130):

```python
    context_statuses = new.get("context_statuses")
    if context_statuses:
        # Отдельный блок, а не колонка в «Статусах задач»: это другая шкала —
        # статус ОБХОДА задачи, а не статус её измерения по ядру.
        lines += ["## Статусы контекста", "", "| Статус | Задач |", "|---|---|"]
        for status, count in context_statuses.items():
            lines.append(f"| {status} | {count} |")
        lines.append("")
```

- [ ] **Step 8: Прогнать тесты — зелено**

Run: `.venv/bin/pytest -q tests/eval/`
Expected: PASS

- [ ] **Step 9: Коммит**

```bash
git add eval/solve_task_metrics/replay.py eval/solve_task_metrics/replay_report.py tests/eval/test_replay.py tests/eval/test_replay_report.py
git commit -m "feat(eval): отдельный статус сбоя обхода вместо пустого знаменателя"
```

---

### Task 4: Принуждение штатной стартовой панели `solve-task`

Дефект того же рода, но в промпте: текст «one panel» присутствовал, а панель всё равно была задана двумя своими вопросами. Лечится дословным шаблоном и запретами, закреплёнными guard-тестом.

**Files:**
- Modify: `plugin/skills/solve-task/references/preflight.md` (пункт 0 «Startup survey», строки 1-30)
- Modify: `tests/skills/test_solve_task_modes.py` (дополнить)
- Modify: манифесты codex (результат прогона скрипта)

**Interfaces:**
- Consumes: ничего.
- Produces: ничего для других задач (терминальная).

- [ ] **Step 1: Написать падающие guard-тесты**

Дописать в `tests/skills/test_solve_task_modes.py`:

```python
def test_survey_carries_a_verbatim_template():
    """Шаблон панели дан дословно: пересказ своими словами — то, что и сломалось."""
    section = _survey_section()
    assert "verbatim" in section
    # три header'а панели названы как значения, а не как темы
    for header in ("Brief model tier", "Interaction mode", "Execution strategy"):
        assert f"`{header}`" in section


def test_survey_forbids_reformulating_and_splitting():
    section = _survey_section()
    lowered = section.lower()
    for ban in ("do not reformulate", "do not split", "do not substitute", "do not omit"):
        assert ban in lowered, ban


def test_survey_requires_self_check_for_missing_questions():
    """Задал не все три — доспроси немедленно, до предполётных проверок."""
    section = _survey_section()
    lowered = section.lower()
    assert "self-check" in lowered
    assert "immediately" in lowered
    assert "before" in lowered
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/skills/test_solve_task_modes.py`
Expected: FAIL — трёх новых тестов нет в тексте промпта.

- [ ] **Step 3: Усилить промпт**

В `plugin/skills/solve-task/references/preflight.md`, внутри пункта «0. **Startup survey.**», после перечисления трёх вопросов и перед блоком «Defaults (fail-open)», добавить:

```markdown
      **The panel is fixed, not a theme to improvise on.** Ask these three questions,
      in one `AskUserQuestion` call, using the headers **verbatim**: `Brief model tier`,
      `Interaction mode`, `Execution strategy`. Do not reformulate the questions in your
      own words, do not split them across several panels, do not substitute your own
      questions for them, and do not omit any of the three — a panel that asks two of
      them is a violation, not a shortcut. Option wording may be phrased in the user's
      language, but each option must still state what that value means, per the option
      texts given above.

      **Self-check.** After the panel returns, verify you actually asked all three. If
      any is missing, say so plainly and ask the missing ones immediately, in a new
      panel, before any preflight check runs — the answers govern the preflight
      questions below, so a late answer governs nothing.
```

- [ ] **Step 4: Прогнать guard-тесты — зелено**

Run: `.venv/bin/pytest -q tests/skills/`
Expected: PASS

- [ ] **Step 5: Пересобрать манифесты плагина**

Правка `plugin/**` меняет payload-digest, install-тесты сверяют его.

```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py
.venv/bin/pytest -q tests/install/
```
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/solve-task/references/preflight.md tests/skills/test_solve_task_modes.py
git add -A  # манифесты, пересобранные скриптом
git commit -m "fix(plugin): дословный шаблон и запреты для стартовой панели solve-task"
```

---

## Финальная верификация

- [ ] **Полный прогон unit-тестов**

Run: `.venv/bin/pytest -q`
Expected: PASS. Baseline репозитория — всё зелёное; любое падение считать регрессией, а не «известным».

- [ ] **Линт**

Run: `.venv/bin/ruff check eval/ tests/ plugin/`
Expected: без замечаний.

- [ ] **Ре-экспорт цел (критерий 4 тикета)**

Run: `.venv/bin/pytest -q tests/metrics/test_reexport_guard.py`
Expected: PASS. `reviewer/metrics/brief_quality/**` не должен появиться в `git diff --stat`:

```bash
git diff --stat 5093069..HEAD -- reviewer/
```
Expected: пусто.

- [ ] **Ручные разделы отчёта целы**

```bash
git diff --stat 5093069..HEAD -- eval/replay_report.md
```
Expected: только добавленные строки маркера (`2 insertions`), ни одного удаления.

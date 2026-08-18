# PRI-259 — Свод рычагов секции `code` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Свести замеры всех рычагов секции `code` на одном свежем индексе, поднять медиану bulk core-recall обменом ширины бюджета на глубину и закрыть guard-тестами критерии изоляции, метрики и токенов.

**Architecture:** Сначала измерительная фаза без единой правки продакшна — лестница вариантов и кандидаты бюджета гоняются через существующий CLI `eval.solve_task_metrics replay` с оверрайдами `--set`. Затем ровно одна точечная правка продакшна: дефолты `CodeSectionLimits`. Затем guard-тесты, фиксирующие то, что сейчас верно лишь архитектурно. Отчёт пишется последним, потому что каждый прогон перезаписывает его целиком.

**Tech Stack:** Python 3, pytest, Postgres/ParadeDB (pgvector + pg_search), Neo4j, Voyage (эмбеддинги/реранк), собственный эвал-харнесс `eval/solve_task_metrics`.

**Spec:** `docs/superpowers/specs/2026-08-18-pri-259-lever-consolidation-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- Ветка работы: `feat/pri-259-lever-consolidation` (уже создана, спека и бриф в ней закоммичены).
- Все замеры — на одном `indexed_sha = 308b86b` (ветка `dev`, репозиторий `mimfort/rag_for_git`). Индекс уже построен; переиндексация в ходе работы **запрещена** — она обнулит сравнимость прогонов.
- Не трогать: `Retriever.search_base` (`reviewer/retrieval/retriever.py:155-226`), публичный `search_codebase`, `CodebaseLimits`, `reviewer/metrics/brief_quality/`, `reviewer/services/brief_quality.py`, реестр вариантов `eval/solve_task_metrics/variants.py`.
- Unit-тестам запрещены внешние и localhost-сокеты. Любой тест с реальной сетью/БД обязан иметь `@pytest.mark.integration`.
- Прогон замера — десятки минут (Voyage free tier 3 RPM / 10K TPM). Запускать в фоне, последовательно, по одному: параллельные прогоны делят одну квоту и растянут оба.
- Операционный бюджет секции = `max_files × max_chunks_per_file × chars_per_file`. Базовое значение — `12 × 1 × 1300 = 15 600`; в задачах 1–4 оно неизменно.
- **Пол по глубине: `chars_per_file` ≥ 975.** Метрика core-recall считает пути и слепа к файлам «прочитать, не менять» (контракт, соседний адаптер, образец), полезность которых лежит в содержимом. Сокращение глубины бьёт именно по ним, и замер этой потери не покажет ни одним числом — поэтому пол взят из требования «в блоке читаются сигнатура символа и несколько строк тела», а не из метрики. См. раздел спеки «Чего метрика не видит».
- **Рост precision не является целью.** Файл-контекст входит в `predicted`, но не в `expected`, поэтому измеренная precision систематически занижена; оптимизация под неё выбрасывала бы ровно тот контекст, ради которого секция существует.

---

### Task 1: Лестница вариантов на свежем индексе

Полная лестница даёт дельту каждого смерженного рычага и их сумму (критерий приёмки 2). Рычаги вложены друг в друга (`similar_paths` = `multiquery` + подмешивание), поэтому лестница заменяет декартову матрицу комбинаций.

**Files:**
- Modify: `eval/replay_history.jsonl` (дописывается прогонами, руками не редактируется)
- Create: `.superpowers/sdd/pri-259-measurements.md` (рабочие числа между задачами; git-ignored)

**Interfaces:**
- Consumes: существующий CLI `eval.solve_task_metrics replay`, индекс `dev@308b86b`.
- Produces: четыре снимка в `eval/replay_history.jsonl` с `variant` ∈ {`baseline`, `limits`, `multiquery`, `similar_paths`} и одинаковым `indexed_sha`; таблицу их агрегатов в рабочем файле.

- [ ] **Step 1: Убедиться, что индекс тот самый**

```bash
uvx --from rag-reviewer reviewer status /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git --branch dev --json
```

Ожидается: `"drift": 0` и `indexed_sha`, начинающийся на `308b86b`. Если `drift > 0` — **остановиться и сообщить**: кто-то закоммитил в `dev`, и переиндексация сломает сравнимость с уже снятыми числами этой задачи.

- [ ] **Step 2: Прогнать baseline (в фоне)**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant baseline \
  --repo mimfort/rag_for_git --branch dev
```

Десятки минут. Троттлинг Voyage с retry/backoff — норма, не сбой.

- [ ] **Step 3: Прогнать limits (в фоне, после завершения Step 2)**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant limits \
  --set code_section.max_files=12 --set code_section.chars_per_file=1300 \
  --repo mimfort/rag_for_git --branch dev --baseline baseline
```

Вариант `limits` требует хотя бы один `--set` (иначе CLI откажет). Значения здесь — текущие дефолты: это точка «файловый бюджет включён, но не изменён».

- [ ] **Step 4: Прогнать multiquery (в фоне, после Step 3)**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant multiquery \
  --repo mimfort/rag_for_git --branch dev --baseline limits
```

- [ ] **Step 5: Прогнать similar_paths (в фоне, после Step 4)**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --repo mimfort/rag_for_git --branch dev --baseline multiquery
```

- [ ] **Step 6: Свести числа лестницы в рабочий файл**

```bash
mkdir -p .superpowers/sdd
.venv/bin/python - <<'EOF' | tee .superpowers/sdd/pri-259-measurements.md
import json
rows=[json.loads(l) for l in open('eval/replay_history.jsonl')]
fresh=[r for r in rows if (r.get('indexed_sha') or '').startswith('308b86b')]
print("| Вариант | медиана | bulk | bulk_n | precision | предсказано |")
print("|---|---|---|---|---|---|")
for r in fresh:
    a=r['aggregate']
    print(f"| {r['variant']} | {a['core_recall_median']} | {a['bulk_core_recall_median']} "
          f"| {a['bulk_n_measured']} | {a.get('precision_median')} | {a.get('denominator_median')} |")
print()
print("### Построчно по bulk-задачам (последний прогон)")
last=fresh[-1]
for t in last['tasks']:
    if t.get('expected_core') and t['expected_core']>=10:
        print(f"- {t['key']}: ядро={t['expected_core']} предсказано={t['predicted']} "
              f"попало={t['hit_core']} recall={t['core_recall']}")
EOF
```

Построчные значения bulk-задач обязательны: медиана по 5 точкам шумная, и без строк число не читается.

- [ ] **Step 7: Зафиксировать рабочие числа**

```bash
git add .superpowers/sdd/pri-259-measurements.md 2>/dev/null || true
git status --short
```

`.superpowers/` git-ignored — коммита не будет, и это ожидаемо: файл живёт между задачами, в PR не уходит. Числа лестницы к этому моменту должны быть в нём; они понадобятся в задаче 6.

---

### Task 2: Замер кандидатов обмена ширины на глубину и выбор точки

Пол по глубине (`chars_per_file` ≥ 975, Global Constraints) вместе с неизменным произведением бюджета оставляет ровно одну свободную точку — `16 × 975`. Поэтому замер идёт двумя ярусами: ярус A (бюджет неизменен) и ярус B (бюджет растёт — размен критерия 5, включается только если ярус A не берёт порог 0.55). Точка выбирается по наибольшей медиане bulk core-recall **без падения общей медианы по корпусу**.

**Files:**
- Modify: `eval/replay_history.jsonl` (дописывается прогонами)
- Modify: `.superpowers/sdd/pri-259-measurements.md`

**Interfaces:**
- Consumes: снимок `similar_paths` из задачи 1 как сторона «до».
- Produces: выбранную пару `(max_files=W, chars_per_file=C)` и её агрегаты; строку вердикта в рабочем файле.

- [ ] **Step 1: Ярус A — прогнать `16 × 975` (в фоне)**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=16 --set code_section.chars_per_file=975 \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths
```

`16 × 1 × 975 = 15 600` — произведение бюджета то же, что у дефолта. Это единственная точка,
не требующая размена критерия 5.

- [ ] **Step 2: Ярус B — прогнать `20 × 975` и `24 × 975` (в фоне, последовательно, после Step 1)**

Выполняется **только если** ярус A дал bulk < 0.55. Бюджет здесь растёт: `20 × 975 = 19 500`
(+25 %), `24 × 975 = 23 400` (+50 %) — это осознанный размен критерия 5, и его цена называется
в отчёте числом.

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=20 --set code_section.chars_per_file=975 \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths

.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=24 --set code_section.chars_per_file=975 \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths
```

- [ ] **Step 3: Справочные прогоны ниже пола глубины (в фоне, после Step 2)**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=20 --set code_section.chars_per_file=780 \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths

.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=24 --set code_section.chars_per_file=650 \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths
```

Обе точки **не могут быть приняты**: 780 и 650 ниже пола 975 (Global Constraints). Меряются,
чтобы в отчёте была видна форма кривой «ширина против глубины» при постоянном бюджете — сколько
recall покупает ширина, если платить за неё глубиной. Если такая точка окажется лучшей по bulk,
это записывается в отчёт как факт, но точкой не становится: потерю, которой она платит, метрика
не измеряет вовсе.

- [ ] **Step 4: Свести кандидатов и выбрать точку**

```bash
.venv/bin/python - <<'EOF' | tee -a .superpowers/sdd/pri-259-measurements.md
import json
rows=[json.loads(l) for l in open('eval/replay_history.jsonl')]
fresh=[r for r in rows if (r.get('indexed_sha') or '').startswith('308b86b')
       and r['variant']=='similar_paths']
print("\n### Кандидаты обмена ширины на глубину\n")
print("| max_files | chars_per_file | произведение | медиана | bulk | bulk_n |")
print("|---|---|---|---|---|---|")
for r in fresh:
    p=r.get('variant_params') or {}
    cs=(p.get('code_section') or {})
    mf=cs.get('max_files',12); cpf=cs.get('chars_per_file',1300)
    a=r['aggregate']
    print(f"| {mf} | {cpf} | {mf*cpf} | {a['core_recall_median']} "
          f"| {a['bulk_core_recall_median']} | {a['bulk_n_measured']} |")
EOF
```

Правило выбора, применить буквально:
1. кандидаты с `chars_per_file < 975` исключаются независимо от их чисел (пол глубины);
2. если ярус A (`16 × 975`) берёт порог 0.55 — он и есть точка, ярус B не принимается даже при лучших числах: бюджет дороже не платим, если дешевле уже хватило;
3. иначе из яруса B берётся **минимальный** `max_files`, взявший порог, — размен критерия 5 покупается ровно в том объёме, в каком он нужен;
4. если порог не взят нигде — точкой становится кандидат с максимальной `bulk_core_recall_median` среди допустимых, и обязательна задача 7 (дожим). Кандидат, у которого `core_recall_median` **ниже**, чем у дефолтной точки, отбрасывается независимо от bulk — критерий 1 требует роста bulk *без падения общей медианы*.

- [ ] **Step 5: Записать вердикт в рабочий файл**

Дописать в `.superpowers/sdd/pri-259-measurements.md` одну строку вида
`Выбрана точка: ярус A, max_files=16, chars_per_file=975 — bulk 0.XXXX (было 0.3889), медиана 0.XX (было 0.75).`
Числа — фактические из Step 4, не выдуманные. Если ни один кандидат не берёт порог 0.55 — записать это отдельной строкой; задача 7 (дожим) тогда обязательна.

---

### Task 3: Новые дефолты `CodeSectionLimits` и инвариант бюджета (критерий 5)

Единственная правка продакшн-кода во всей задаче. Значения берутся из вердикта задачи 2 — в примерах ниже подставлена точка яруса A `16 × 975`; если замер выбрал другую пару, подставить её везде.

**Files:**
- Modify: `reviewer/policy/context_limits.py:44-47` (поля `max_files`, `chars_per_file` в `CodeSectionLimits`)
- Modify: `tests/policy/test_context_limits.py:30-56` (тесты жёстко проверяют дефолты 12/1300)
- Test: `tests/policy/test_context_limits.py`

**Interfaces:**
- Consumes: пару `(W, C)` из задачи 2.
- Produces: `CodeSectionLimits(max_files=W, chars_per_file=C)` как дефолт политики; тест-инвариант `test_code_section_operational_budget_did_not_grow`.

- [ ] **Step 1: Написать падающий тест-инвариант бюджета**

Добавить в `tests/policy/test_context_limits.py`:

```python
def test_code_section_operational_budget_did_not_grow():
    """Критерий 5 PRI-259: операционный бюджет символов секции не вырос.

    Обмен ширины на глубину меняет форму бюджета (больше файлов, короче
    фрагмент), но не его размер: расход токенов сборщика брифа определяется
    произведением, а не числом файлов.
    """
    lim = CodeSectionLimits()
    budget = lim.max_files * lim.max_chunks_per_file * lim.chars_per_file
    assert budget <= 15_600, (
        f"операционный бюджет секции вырос до {budget} против 15600 "
        "(12 × 1 × 1300 до PRI-259)"
    )
```

- [ ] **Step 2: Прогнать тест — он должен пройти на старых дефолтах**

```bash
.venv/bin/pytest tests/policy/test_context_limits.py::test_code_section_operational_budget_did_not_grow -q
```

Ожидается: PASS (12 × 1 × 1300 = 15 600 ≤ 15 600). Тест — сторож против будущего роста, а не красный тест TDD: он обязан быть зелёным и до, и после правки дефолтов. Именно это и проверяется на этом шаге.

- [ ] **Step 3: Поменять дефолты**

В `reviewer/policy/context_limits.py`, в `CodeSectionLimits`:

```python
    max_files: int = 16          # различных файлов в секции (PRI-259: обмен ширины на глубину)
    max_chunks_per_file: int = 1  # чанков на один файл
    chars_per_file: int = 975    # доля символов на файл (операционный бюджет — на исходный текст блока)
    max_augmented_files: int = 3  # сколько файлов секции может занять подмешанный сигнал (PRI-257)
```

Дописать в докстринг класса абзац (по-русски): ширина против глубины при неизменном произведении, ссылка на PRI-259, и **пол глубины** — `chars_per_file` не опускается ниже 975, потому что метрика считает пути, а не тела символов, и слепа к файлам «прочитать, не менять», полезность которых лежит в содержимом; величина пола взята из требования «в блоке читаются сигнатура символа и несколько строк тела», а не из замера. Если замер выбрал точку яруса B — тем же абзацем назвать выросшее произведение бюджета и его цену.

- [ ] **Step 4: Обновить тесты дефолтов**

В `tests/policy/test_context_limits.py` заменить в `test_code_section_defaults` ожидания `max_files == 12` → `== 16` и `chars_per_file == 1300` → `== 975`. В `test_code_section_partial_block_keeps_other_defaults` (строка ~54) ожидание сохранённого дефолта `chars_per_file == 1300` → `== 975`. Проверить `test_code_section_max_chars_is_derived` и `test_code_section_max_chars_accounts_for_chunks_per_file`: если в них зашиты числа, пересчитать по формуле `max_files × max_chunks_per_file × chars_per_file × 3 // 2`.

- [ ] **Step 5: Прогнать тесты политики и весь unit-набор**

```bash
.venv/bin/pytest tests/policy/ -q
.venv/bin/pytest -q
```

Ожидается: всё зелёное. Если падают тесты `tests/retrieval/test_multiquery.py` — читать их внимательно: они могли зашить 12 слотов как литерал; правка допустима только там, где число — ожидание дефолта политики, а не проверка механики бюджета.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/policy/context_limits.py tests/policy/test_context_limits.py
git commit -m "feat(policy): обмен ширины бюджета секции code на глубину фрагмента"
```

---

### Task 4: Guard-тесты изоляции solve-task-пути (критерий 3)

Изоляция сейчас верна архитектурно и ничем не защищена: ничто не мешает будущей правке протащить `CodeSectionLimits` в `search_base` или пустить публичный `search_codebase` через `search_multi`, и ревью PR с `/ask` тихо поменяют поведение.

**Files:**
- Create: `tests/retrieval/test_solve_task_isolation.py`
- Test: `tests/retrieval/test_solve_task_isolation.py`

**Interfaces:**
- Consumes: `reviewer.retrieval.retriever.Retriever.search_base`, `reviewer.retrieval.multiquery.search_multi`.
- Produces: два структурных guard-теста; новых продакшн-символов не появляется.

- [ ] **Step 1: Написать падающие guard-тесты**

Создать `tests/retrieval/test_solve_task_isolation.py`:

```python
"""Guard-тесты изоляции пути solve-task от общего ретрива (PRI-259, критерий 3).

Секция code брифа живёт на своём бюджете (CodeSectionLimits) и своём
мультизапросном пути (search_multi). Общий Retriever.search_base обслуживает
/ask, грунтовку и ревью PR — он обязан остаться незатронутым. Оба теста
структурные: они ловят не поведение, а протечку зависимости.
"""
import inspect
import pathlib

from reviewer.retrieval import multiquery, retriever


def test_search_base_does_not_read_code_section_limits():
    """search_base не знает про файловый бюджет секции code."""
    source = inspect.getsource(retriever.Retriever.search_base)
    assert "CodeSectionLimits" not in source
    assert "section_limits" not in source
    assert "section_limits" not in inspect.signature(
        retriever.Retriever.search_base).parameters


def test_search_multi_is_called_only_from_the_task_context_path():
    """Единственный продакшн-вызов search_multi — приватный _search_codebase_multi."""
    root = pathlib.Path(multiquery.__file__).parent.parent  # пакет reviewer/
    callers = set()
    for path in root.rglob("*.py"):
        if path.name == "multiquery.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "search_multi(" in text:
            callers.add(path.relative_to(root).as_posix())
    assert callers == {"mcp/service.py"}, (
        f"search_multi вызывается из {sorted(callers)}; путь solve-task должен "
        "оставаться единственным потребителем — иначе /ask и ревью PR меняют поведение"
    )
```

- [ ] **Step 2: Прогнать тесты**

```bash
.venv/bin/pytest tests/retrieval/test_solve_task_isolation.py -q
```

Ожидается: PASS на текущем коде — тесты фиксируют уже верное состояние. Если хоть один падает, это находка: изоляция уже нарушена, и её надо чинить до продолжения.

- [ ] **Step 3: Проверить, что guard действительно ловит регрессию**

Временно добавить в `reviewer/retrieval/retriever.py` внутрь `search_base` строку `section_limits = None  # временная проверка guard`, прогнать тест, убедиться, что он **падает**, затем строку удалить и прогнать снова — зелено.

```bash
.venv/bin/pytest tests/retrieval/test_solve_task_isolation.py::test_search_base_does_not_read_code_section_limits -q
git diff --stat reviewer/retrieval/retriever.py   # должно быть пусто после удаления
```

Guard, который не падает ни на чём, бесполезен; этот шаг — единственное доказательство, что он работает.

- [ ] **Step 4: Коммит**

```bash
git add tests/retrieval/test_solve_task_isolation.py
git commit -m "test(retrieval): guard-тесты изоляции пути solve-task от общего ретрива"
```

---

### Task 5: Guard-тест сопоставимости онлайн-метрики (критерий 4)

`brief_quality` не трогается этой задачей, но её сопоставимость с точкой «до» держится на двух свойствах, которые ничем не закреплены как требование: строка хранит **множества путей**, а не только счётчики, и чтение делает **union по задаче**. Существующее покрытие есть (`tests/web/test_history.py:608` — integration-роундтрип, `tests/services/test_brief_quality.py` — форма измерения), поэтому задача не дублирует их, а добавляет один unit-guard.

**Files:**
- Modify: `tests/services/test_brief_quality.py`
- Test: `tests/services/test_brief_quality.py`

**Interfaces:**
- Consumes: `reviewer.services.brief_quality` (форма `measure()`), `reviewer.web.history` (union на чтении).
- Produces: guard-тест `test_measurement_carries_path_sets_not_only_counters`.

- [ ] **Step 1: Прочитать существующее покрытие, чтобы не задваивать**

```bash
grep -n "def test" tests/services/test_brief_quality.py
sed -n '600,660p' tests/web/test_history.py
```

Роундтрип с union уже покрыт integration-тестом `test_brief_quality_roundtrip_and_task_union`. Новый тест — про **форму измерения**: что `measure()` отдаёт множества путей, из которых union вообще возможен.

- [ ] **Step 2: Написать guard-тест**

Дописать в `tests/services/test_brief_quality.py` (использовать те же фикстуры/хелперы, что и соседние тесты файла — их имена взять из Step 1, здесь показана суть проверки):

```python
def test_measurement_carries_path_sets_not_only_counters(tmp_path):
    """Критерий 4 PRI-259: строка метрики хранит множества путей.

    Онлайн видит один PR, офлайн-baseline считался по задаче (union всех её
    PR). Без множеств путей union на чтении невозможен, и task-level число
    считалось бы другой линейкой, чем точка «до» (bulk_core_recall_median
    ≈ 0.373, bulk_n_measured = 4) — то есть сопоставимость молча исчезла бы.
    """
    measurement = _measure_fixture(tmp_path)      # хелпер соседних тестов файла
    assert measurement.expected_core_paths        # не пусто
    assert measurement.hit_core_paths is not None
    for value in (measurement.expected_core_paths, measurement.hit_core_paths):
        assert not isinstance(value, int), "счётчик вместо множества путей ломает union"
        assert all(isinstance(path, str) for path in value)
    assert set(measurement.hit_core_paths) <= set(measurement.expected_core_paths)
```

- [ ] **Step 3: Прогнать тест**

```bash
.venv/bin/pytest tests/services/test_brief_quality.py -q
```

Ожидается: PASS. Красный тест здесь означает, что форма метрики уже разошлась с точкой «до» — тогда остановиться и сообщить, это отдельная находка.

- [ ] **Step 4: Прогнать полный unit-набор**

```bash
.venv/bin/pytest -q
```

- [ ] **Step 5: Коммит**

```bash
git add tests/services/test_brief_quality.py
git commit -m "test(services): guard сопоставимости онлайн-метрики brief_quality с точкой «до»"
```

---

### Task 6: Контрольный прогон на новых дефолтах и раздел «Приёмка PRI-259»

Отчёт пишется последним: каждый прогон `replay` перезаписывает `eval/replay_report.md` целиком, поэтому разделы приёмки прошлых задач восстанавливаются вручную после последнего прогона.

**Files:**
- Modify: `eval/replay_report.md`
- Modify: `CLAUDE.md` (раздел «Неочевидные факты» — абзац про новую форму бюджета)
- Modify: `eval/replay_history.jsonl` (дописывается контрольным прогоном)

**Interfaces:**
- Consumes: числа из `.superpowers/sdd/pri-259-measurements.md` (задачи 1–2), новые дефолты (задача 3).
- Produces: раздел «Приёмка PRI-259» в отчёте; абзац в `CLAUDE.md`.

- [ ] **Step 1: Сохранить текущие разделы приёмки перед перезаписью**

```bash
cp eval/replay_report.md /tmp/replay_report_before_pri259.md
grep -n '^## Приёмка' /tmp/replay_report_before_pri259.md
```

- [ ] **Step 2: Контрольный прогон на новых дефолтах**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths
```

Без `--set`: теперь дефолты политики и есть выбранная точка. Числа обязаны совпасть с соответствующим кандидатом из задачи 2 — расхождение означает, что оверрайд и дефолт идут разными путями, и это находка, а не шум.

- [ ] **Step 3: Восстановить прошлые разделы приёмки**

Перенести из `/tmp/replay_report_before_pri259.md` разделы «Приёмка PRI-255», «Приёмка PRI-256», «Приёмка PRI-257», «Приёмка PRI-258» в конец свежесгенерированного `eval/replay_report.md` — дословно, включая их оговорки про `indexed_sha`.

- [ ] **Step 4: Написать раздел «Приёмка PRI-259»**

Дописать в `eval/replay_report.md` раздел со всем перечисленным:

1. **Лестница рычагов** (таблица из задачи 1): вариант → медиана, bulk, bulk_n, precision; дельта каждой ступени к предыдущей. Одна строка вывода: рычаги вложены, поэтому лестница даёт и дельту каждого, и сумму.
2. **Кандидаты обмена ширины на глубину** (таблица из задачи 2) и правило выбора точки.
3. **Вердикт по каждому рычагу с числом**: `multiquery` — смержен (+Δ), файловый бюджет — смержен (+Δ), `similar-diffs` — смержен (+Δ), `co-change` — снят (12 % точности, ссылка на приёмку PRI-257), разворот кластеров subsystems — снят (2 % точности, ссылка на приёмку PRI-258), обмен ширины на глубину — смержен/снят по факту замера.
4. **Арифметика потолка**: построчная таблица bulk-задач (ядро / предсказано / попало / recall / потолок при бюджете). Без неё число «медиана bulk» не читается.
5. **Чего метрика не видит**: знаменатель — только изменённые файлы, поэтому файлы «прочитать,
   не менять» recall не приносят, а precision штрафует; измеренная precision смещена вниз, и её
   рост целью не является (в приёмке PRI-257 он читался как хороший знак — здесь это отмечается
   как индикатор со смещением). Отсюда пол `chars_per_file ≥ 975` и вывод: замеры отвечают на
   вопрос «доезжают ли файлы, которые придётся менять», и не отвечают на вопрос «хватает ли
   брифу контекста, чтобы новый код писался правильно».
6. **Три оговорки**: `bulk_n = 5` — медиана шумная, ±1 задача двигает её на 0.1–0.2; все числа PRI-259 сняты на `308b86b`, прошлые приёмки — на `951e791`, дельты между разными индексами не сравниваются; произведение бюджета не изменилось (критерий 5 закрыт арифметикой, тест `test_code_section_operational_budget_did_not_grow`).
7. **Процедура воспроизведения** — точные команды всех прогонов.

- [ ] **Step 5: Дописать абзац в `CLAUDE.md`**

В раздел «Неочевидные факты» добавить абзац (по-русски, в стиле соседних): бюджет секции `code` меняет форму, а не размер — `max_files × max_chunks_per_file × chars_per_file` остаётся 15 600; причина в том, что метрика core-recall считает пути, а секция уходит в бриф строками `path:line`; нижняя граница `chars_per_file` взята не из метрики, потому что деградацию объяснений она не ловит.

- [ ] **Step 6: Коммит**

```bash
git add eval/replay_report.md eval/replay_history.jsonl CLAUDE.md
git commit -m "docs(eval): приёмка PRI-259 — свод рычагов секции code на одном индексе"
```

---

### Task 7 (условная): Дожим порога 0.55

Выполняется **только** если после задачи 6 медиана bulk core-recall < 0.55. Предел — три итерации; после третьей работа закрывается фиксацией достигнутого числа и рекомендацией пересмотреть форму критерия.

**Files:**
- Modify: `reviewer/policy/context_limits.py` (итерация 1) либо `reviewer/mcp/service.py:1809-1824` (итерация 2) либо `reviewer/retrieval/augment.py` (итерация 3)
- Modify: `eval/replay_report.md`, `.superpowers/sdd/pri-259-measurements.md`

**Interfaces:**
- Consumes: достигнутое число из задачи 6.
- Produces: либо взятый порог с зафиксированной ценой, либо документированный отказ.

- [ ] **Step 1: Итерация 1 — расширять `max_files` за пределы яруса B**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=32 --set code_section.chars_per_file=975 \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths
```

Глубина остаётся на полу 975; произведение бюджета **растёт** (32 × 975 = 31 200 против 15 600, +100 %) — это осознанный размен критерия 5. Если итерация принимается, тест `test_code_section_operational_budget_did_not_grow` придётся ослабить: заменить порог на новое значение и переписать докстринг так, чтобы он называл размен и его цену в токенах, а не делал вид, что бюджет не рос.

- [ ] **Step 2: Итерация 2 — graph-expansion**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set graph.hops=2 --set code_section.max_files=16 --set code_section.chars_per_file=975 \
  --repo mimfort/rag_for_git --branch dev --baseline similar_paths
```

- [ ] **Step 3: Итерация 3 — новый источник подмешивания**

Прежде чем писать код источника, проверить механику бюджета по прецеденту PRI-257: три независимых механизма (квота как потолок на остаток, известность по сырому пулу, квота по кандидатам вместо найденных) там **по отдельности обнуляли рычаг**, и ноль был свойством бюджета, а не сигнала. Нота видимости в тексте секции (`— подмешано N файлов: <источник> (квота Q)`) — единственный способ увидеть, доехал ли сигнал.

- [ ] **Step 4: Зафиксировать исход**

Каждая итерация дописывает в раздел «Приёмка PRI-259» строку с числом — включая отрицательные. Если после третьей порог не взят: записать достигнутое число, арифметический потолок при выбранном бюджете и рекомендацию заменить абсолютный порог долей от потолка. Порог, не взятый после трёх итераций, — результат замера, а не провал исполнения; молча оставлять его без записи нельзя.

- [ ] **Step 5: Коммит**

```bash
git add -A
git commit -m "docs(eval): дожим порога bulk core-recall — итерации и вердикт"
```

---

## Self-Review

**Покрытие спеки:** измерительная фаза → задачи 1–2; рычаг обмена ширины на глубину → задача 3; guard-тесты критериев 3/4/5 → задачи 4/5/3 соответственно; отчёт → задача 6; дожим с пределом в три итерации → задача 7. Раздел спеки «Что НЕ делается» отражён в Global Constraints. Гэпов нет.

**Плейсхолдеры:** пара `(W, C)` в задаче 3 — не плейсхолдер, а выход задачи 2; подставлена конкретная точка яруса A `16 × 975` с указанием заменить её фактической. Имя хелпера `_measure_fixture` в задаче 5 берётся из Step 1 той же задачи — шаг чтения предшествует шагу написания.

**Согласованность типов:** `max_files` / `chars_per_file` / `max_chunks_per_file` — имена полей `CodeSectionLimits` из `reviewer/policy/context_limits.py:44-47`; `expected_core_paths` / `hit_core_paths` — имена из `reviewer/web/history.py:545-572`; `search_multi` / `_search_codebase_multi` — из `reviewer/retrieval/multiquery.py` и `reviewer/mcp/service.py:1809-1824`. Все проверены по коду перед написанием плана.

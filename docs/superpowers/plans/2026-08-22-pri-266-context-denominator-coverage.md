# PRI-266 — покрытие знаменателя context-recall: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** вернуть покрытие офлайн-метрики context-recall, не потеряв точность, и отличить «знаменатель неопределим» от «знаменатель пуст».

**Architecture:** два независимых рычага целиком внутри `eval/solve_task_metrics/`. Первый — второй источник разрешённых имён: «шапка» (сигнатура) символов, уже ставших сидами; включается режимом сидов, поэтому сторона «до» снимается тем же кодом. Второй — новый статус прогона `undefined_context_denominator`, отделяющий задачу, у которой сидов не могло быть в принципе (нет Python среди изменённых core-путей), от задачи с содержательно пустым обходом. `reviewer/metrics/brief_quality/` не меняется ни одной строкой.

**Tech Stack:** Python 3, tree-sitter (`tree_sitter_python`), pytest. Инфраструктура для финального замера: ParadeDB (:5433), Neo4j, Voyage.

**Spec:** `docs/superpowers/specs/2026-08-22-pri-266-context-denominator-coverage-design.md`

## Global Constraints

- Язык проекта — русский: докстринги, комментарии, сообщения CLI, тексты отчётов.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- `reviewer/metrics/brief_quality/**` не изменяется в задачах 1–4 ни одной строкой. `is_core_production_path` не трогается вовсе — она общая с онлайн-метрикой, её правка сдвинула бы опубликованные core-recall.
- `tests/metrics/test_reexport_guard.py` обязан оставаться зелёным без правок.
- Unit-тесты идут без Postgres, Neo4j и сети: `.venv/bin/pytest -q`. Тесты с реальной сетью обязаны нести `@pytest.mark.integration`.
- Предзарегистрация (Task 1) коммитится ДО любого кода механизмов — это критерий 1 задачи.
- Все команды замера — с явным `--branch dev`. Без флага молча берётся индекс `main`.
- Обе стороны замера — на одном `indexed_sha`. Сторона «до» пересчитывается, а не берётся из отчёта PRI-262 (тот снят на `a07405c`).
- Пол шума харнесса — ±1 файл на задачу; пер-задачные дельты внутри него сигналом не считаются.
- Базовая линия тестов: `.venv/bin/pytest -q` зелёный целиком и `ruff` чист. Падение = регрессия, а не «известное падение».

---

### Task 1: Предзарегистрировать гейт, выборку и условия замера

**Files:**
- Create: `eval/pri266_preregistration.md`

**Interfaces:**
- Consumes: ничего.
- Produces: журнал, на который ссылаются Task 5 (замер) и Task 6 (документация). Ни одна цифра из него задним числом не двигается.

Задача обязана быть выполнена и закоммичена ДО реализации механизмов: критерий 1 требует, чтобы порог был назван до просмотра данных. Образец формы — `eval/pri262_preregistration.md`.

- [ ] **Step 1: Написать файл предзарегистрации**

Создать `eval/pri266_preregistration.md` ровно с этим содержимым:

```markdown
# PRI-266 pre-registration

**Написано и закоммичено ДО реализации механизмов и ДО просмотра любых данных нового
механизма.** Критерий 1 задачи требует, чтобы порог и выборка были названы заранее и задним
числом не двигались. Этот файл — та самая запись.

## Проверяемая гипотеза

PRI-262 взяла гейт точности (64.0 % осмысленных путей при пороге ≥ 50 %), но сузила покрытие:
задач с измеренным контекстным знаменателем 42 → 31 из 63, медиана размера ядра 4 → 0.

PRI-266 проверяет две независимые гипотезы о причинах сужения.

1. **Сигнатурная.** Фильтр `allowed_names` пропускает соседа, только если его имя прозвучало
   на изменённой строке, и потому слеп к зависимости, названной в «шапке» изменённого
   символа — в декораторе, аннотации параметра, дефолте, аннотации возврата, базе класса.
   Расширение источника имён шапкой символа-сида обязано вернуть часть потерянных путей, не
   вернув мусор god-модулей.
2. **Не-Python.** У задачи, чьё изменённое ядро целиком не-Python, сидов не может возникнуть
   ни при какой настройке фильтра. Её знаменатель НЕОПРЕДЕЛИМ, а не пуст, и статус обязан это
   различать.

## Предзарегистрированный гейт

Гейт двойной; обе половины обязаны выполниться одновременно.

- **Покрытие.** Число задач с измеренным контекстным знаменателем строго больше, чем на
  стороне «до», снятой на том же `indexed_sha` тем же кодом в режиме сидов `lines`.
- **Точность.** Доля осмысленных путей при ручной сверке ≥ 50 %.

Порог точности держится на значении PRI-261/262 и не корректируется под выборку.

## Предзарегистрированная выборка ручной сверки

Та же шестёрка, что у PRI-262 — сторона «до» тогда сравнима с опубликованными 64.0 %
напрямую.

| Задача | Роль в выборке |
|---|---|
| PRI-227 | правка только докстринга |
| PRI-236 | правка только help-текста + нетронутые соседние вызовы |
| PRI-215 | plumbing-задача |
| PRI-221 | заражение god-модуля |
| PRI-251 | контроль: настоящие рёбра фичи, на ней потеряны три пути |
| PRI-249 | контроль: настоящие рёбра пайплайна публикации |

## Предзарегистрированные фальсифицируемые предсказания (негейтящие)

1. **PRI-227 обязана остаться с нулём сидов.** Её дифф — переименование докстринга;
   сигнатурный рычаг не имеет права её воскресить. Воскресила — значит рычаг пропускает
   незначимые хунки, и это дефект независимо от агрегата.
2. **PRI-251 обязана вернуть хотя бы один из трёх поимённо потерянных путей**
   (`reviewer/index/chunker.py`, `reviewer/index/models.py`, `reviewer/gitutil.py`). Не
   вернула ни одного — сигнатурная гипотеза не подтвердилась независимо от агрегата.
3. **PRI-177, PRI-237, PRI-243 обязаны получить статус «неопределим», а не «пусто».**

## Предзарегистрированные условия замера

- Один `indexed_sha` на обе стороны; его значение печатается прогоном и записывается в отчёт.
- Каждая команда `replay` — с явным `--branch dev`.
- Пер-задачные дельты в пределах пола шума харнесса (±1 файл; 6 из 62 задач нестабильны между
  идентичными прогонами) сигналом не считаются.
- Вклад в покрытие и вклад в точность замеряются отдельно: рычаг не имеет права ухудшить ни
  одно из двух.

## Предзарегистрированное стоп-правило

Если покрытие не восстанавливается без потери точности, PRI-266 закрывается отрицательным
результатом: число фиксируется, область применимости метрики называется явно, третий рычаг
не тянется.
```

- [ ] **Step 2: Проверить, что файл читается и не содержит плейсхолдеров**

Run: `grep -n "TBD\|TODO\|XXX" eval/pri266_preregistration.md; wc -l eval/pri266_preregistration.md`
Expected: grep ничего не находит; файл непустой.

- [ ] **Step 3: Коммит**

```bash
git add eval/pri266_preregistration.md
git commit -m "docs(eval): предзарегистрировать гейт, выборку и условия замера PRI-266"
```

---

### Task 2: Имена из сигнатуры символа-сида

**Files:**
- Modify: `eval/solve_task_metrics/context_seeds.py` (дописать `signature_names`, расширить `SeedSet`, наполнить поле в `seeds_for_merge`)
- Test: `tests/eval/test_context_seeds.py`

**Interfaces:**
- Consumes: `Chunk` из `chunk_python` (поля `symbol_fqn`, `start_line`, `end_line`), `_simple_name(node) -> str`, `_innermost_symbols(path, source, lines) -> set[str]`.
- Produces:
  - `signature_names(source: str, symbols: set[str]) -> set[str]` — простые имена из шапок символов, заданных как `"path#fqn"`; неизвестные `fqn` игнорируются.
  - `SeedSet` с третьим полем `signature_names: set` (дефолт — пустое множество), участвующим в `__or__`.
  - `seeds_for_merge` / `collect_seeds` возвращают `SeedSet` с наполненным `signature_names`.

Сиды НЕ расширяются: множество `symbols` остаётся строчным ровно таким, каким его сделала PRI-262. Расширяется только набор разрешённых имён, и отдельным полем, чтобы вклад мерился отдельно.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/eval/test_context_seeds.py`:

```python
SIG_SOURCE = '''import functools

@functools.cache
def f(a: Chunk, b=make_default()) -> Report:
    helper()
    return None


class C(Base):
    def m(self, x: Other) -> None:
        inner()
'''


def test_signature_names_takes_decorator_annotations_defaults_and_return():
    """Шапка символа: декоратор, аннотация параметра, дефолт, тип возврата."""
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#f"})
    assert {"cache", "Chunk", "make_default", "Report"} <= names


def test_signature_names_ignores_the_body():
    """Имя из ТЕЛА символа в шапку не попадает — иначе рычаг возвращает
    мусор god-модулей, ровно тот, который чинила PRI-262."""
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#f"})
    assert "helper" not in names


def test_signature_names_takes_class_bases():
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#C"})
    assert "Base" in names
    assert "inner" not in names


def test_signature_names_reads_method_signature_by_fqn():
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#C.m"})
    assert "Other" in names
    assert "inner" not in names


def test_signature_names_folds_dotted_names_to_last_segment():
    source = "def f(a: mod.pkg.Thing) -> None:\n    pass\n"
    assert context_seeds.signature_names(source, {"reviewer/a.py#f"}) == {"Thing"}


def test_signature_names_unknown_symbol_is_empty_not_error():
    assert context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#nope"}) == set()


def test_signature_names_broken_source_is_empty_not_error():
    """Не-Python или битый файл не роняет прогон корпуса."""
    assert context_seeds.signature_names("{ not python", {"reviewer/a.py#f"}) == set()
    assert context_seeds.signature_names("", set()) == set()


def test_seed_set_union_merges_signature_names():
    left = context_seeds.SeedSet(symbols={"a#f"}, signature_names={"X"})
    right = context_seeds.SeedSet(called_names={"g"}, signature_names={"Y"})
    merged = left | right
    assert merged.symbols == {"a#f"}
    assert merged.called_names == {"g"}
    assert merged.signature_names == {"X", "Y"}
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/eval/test_context_seeds.py -q`
Expected: FAIL — `AttributeError: module 'eval.solve_task_metrics.context_seeds' has no attribute 'signature_names'` и `TypeError` про неизвестное поле `signature_names` у `SeedSet`.

- [ ] **Step 3: Расширить `SeedSet`**

В `eval/solve_task_metrics/context_seeds.py` заменить класс `SeedSet` на:

```python
@dataclass(frozen=True)
class SeedSet:
    """Сиды задачи: символы обхода и имена, разрешающие соседа.

    Имена разделены по источнику, а не слиты в одно поле: вклад шапки символа
    в покрытие и в точность обязан быть измерен отдельно (критерий 3 PRI-266),
    и обе стороны замера обязаны сниматься одним и тем же кодом.
    """
    symbols: set = field(default_factory=set)
    called_names: set = field(default_factory=set)
    signature_names: set = field(default_factory=set)

    def __or__(self, other: "SeedSet") -> "SeedSet":
        return SeedSet(self.symbols | other.symbols,
                       self.called_names | other.called_names,
                       self.signature_names | other.signature_names)
```

- [ ] **Step 4: Реализовать `signature_names`**

Дописать в `eval/solve_task_metrics/context_seeds.py` перед `_lines_of`:

```python
def _def_node_at(root, start_line: int):
    """Узел определения (`def`/`class`), начинающийся на данной строке.

    Сопоставление по строке начала тела определения, а не по имени: имя может
    повторяться в файле, а `chunk_python` уже выбрал нужный символ.
    """
    found = []

    def visit(node) -> None:
        if node.type in ("function_definition", "class_definition") and \
                node.start_point[0] + 1 == start_line:
            found.append(node)
        for child in node.children:
            visit(child)

    visit(root)
    return found[0] if found else None


def _names_in(node) -> set:
    """Простые имена всех идентификаторов и атрибутов поддерева.

    В шапке символа идентификатор — это тип, дефолт или декоратор, то есть
    ровно та зависимость, которую граф выражает ребром. Запрет `called_names`
    на голые идентификаторы здесь не нужен: локальных переменных в шапке нет.
    """
    names: set = set()

    def visit(child) -> None:
        if child.type in ("identifier", "attribute"):
            name = _simple_name(child)
            if name:
                names.add(name)
            return
        for sub in child.children:
            visit(sub)

    visit(node)
    return names


def _signature_parts(node) -> list:
    """Части шапки определения: декораторы, параметры, тип возврата, базы."""
    parts = []
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        parts.extend(c for c in parent.children if c.type == "decorator")
    for field_name in ("parameters", "return_type", "superclasses", "type_parameters"):
        child = node.child_by_field_name(field_name)
        if child is not None:
            parts.append(child)
    return parts


def signature_names(source: str, symbols: set) -> set:
    """Имена из шапок символов-сидов: декораторы, аннотации, дефолты, базы.

    Второй источник разрешённых имён (PRI-266). Фильтр по именам с изменённых
    строк не видит зависимость, которую изменённые строки не называют: PRI-251
    потеряла chunker/models/gitutil, потому что ведущие к ним вызовы лежат на
    нетронутых строках. Шапка изменённого символа такую зависимость называет.

    Тело символа не читается: оттуда и приходил мусор god-модулей, который
    чинила PRI-262. Сиды этой функцией не расширяются — только фильтр.
    """
    if not symbols or not source:
        return set()
    names: set = set()
    try:
        chunks = chunk_python("x.py", source.encode("utf-8", "replace"))
        root = _PARSER.parse(source.encode("utf-8", "replace")).root_node
    except Exception:  # noqa: BLE001 — не-Python или битый файл не роняет прогон
        return set()
    wanted = {sym.split("#", 1)[1] for sym in symbols if "#" in sym}
    for chunk in chunks:
        if chunk.symbol_fqn not in wanted:
            continue
        node = _def_node_at(root, chunk.start_line)
        if node is None:
            continue
        for part in _signature_parts(node):
            names |= _names_in(part)
    return names
```

- [ ] **Step 5: Наполнить поле в `seeds_for_merge`**

В `eval/solve_task_metrics/context_seeds.py` заменить хвост `seeds_for_merge` (сборку `result`) на:

```python
        symbols = _innermost_symbols(path, after, right_lines)
        result = result | SeedSet(
            symbols=symbols,
            called_names=called_names(after, right_lines)
            | called_names(before, left_lines),
            signature_names=signature_names(after, symbols),
        )
    return result
```

- [ ] **Step 6: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/eval/test_context_seeds.py -q`
Expected: PASS, все тесты файла.

- [ ] **Step 7: Прогнать весь юнит-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check eval reviewer tests`
Expected: PASS, ruff чист. `signature_names` пока никем не потребляется, поэтому существующие числа сдвинуться не могут.

- [ ] **Step 8: Коммит**

```bash
git add eval/solve_task_metrics/context_seeds.py tests/eval/test_context_seeds.py
git commit -m "feat(eval): имена из шапки символа-сида как второй источник фильтра"
```

---

### Task 3: Режим сидов в прогоне и флаг CLI

**Files:**
- Modify: `eval/solve_task_metrics/replay.py` (константы режимов, параметр `seed_mode` у `run_replay`, сборка `allowed_names`, поле снимка)
- Modify: `eval/solve_task_metrics/__main__.py` (`_replay_side`, аргумент `--context-seeds`)
- Test: `tests/eval/test_replay.py`, `tests/eval/test_replay_cli.py`

**Interfaces:**
- Consumes: `SeedSet.symbols`, `SeedSet.called_names`, `SeedSet.signature_names` (Task 2); `derive_context_core(seed_ids, changed_core, traverse, allowed_names=...)`.
- Produces:
  - `replay.SEED_MODE_LINES = "lines"`, `replay.SEED_MODE_LINES_SIGNATURE = "lines+signature"`, `replay.SEED_MODES = (SEED_MODE_LINES, SEED_MODE_LINES_SIGNATURE)`.
  - `run_replay(..., seed_mode: str = SEED_MODE_LINES)` — дефолт тождественен поведению до PRI-266.
  - Ключ снимка `"seed_mode"` со значением режима прогона.
  - CLI: `python -m eval.solve_task_metrics replay --context-seeds lines|lines+signature`.

Дефолт `lines` намеренно: сторона «до» обязана сниматься тем же кодом, что и сторона «после», и до вердикта Task 5 поведение по умолчанию не меняется.

- [ ] **Step 1: Написать падающие тесты прогона**

Дописать в конец `tests/eval/test_replay.py`:

```python
def test_seed_mode_lines_is_the_default_and_ignores_signature_names(monkeypatch, tmp_path):
    """Дефолт тождественен поведению до PRI-266: шапка не участвует."""
    seen = {}

    def fake_collect(truth, run_git):
        return replay.context_seeds.SeedSet(
            symbols={"reviewer/a.py#g"},
            called_names={"g"},
            signature_names={"Sig"},
        )

    def fake_derive(seed_ids, changed_core, traverse, allowed_names=None):
        seen["allowed"] = allowed_names
        return set()

    monkeypatch.setattr(replay.context_seeds, "collect_seeds", fake_collect)
    monkeypatch.setattr(replay.context_core, "derive_context_core", fake_derive)
    _run(
        tmp_path,
        ["PRI-31"],
        tasks={"PRI-31": {"title": "PRI-31", "description": ""}},
        changed={"PRI-31": ["reviewer/a.py"]},
        predicted={"PRI-31": ["reviewer/a.py"]},
        neighbors=set(),
    )
    assert seen["allowed"] == {"g"}


def test_seed_mode_signature_unions_both_name_sources(monkeypatch, tmp_path):
    seen = {}

    def fake_collect(truth, run_git):
        return replay.context_seeds.SeedSet(
            symbols={"reviewer/a.py#g"},
            called_names={"g"},
            signature_names={"Sig"},
        )

    def fake_derive(seed_ids, changed_core, traverse, allowed_names=None):
        seen["allowed"] = allowed_names
        return set()

    monkeypatch.setattr(replay.context_seeds, "collect_seeds", fake_collect)
    monkeypatch.setattr(replay.context_core, "derive_context_core", fake_derive)
    _run(
        tmp_path,
        ["PRI-32"],
        tasks={"PRI-32": {"title": "PRI-32", "description": ""}},
        changed={"PRI-32": ["reviewer/a.py"]},
        predicted={"PRI-32": ["reviewer/a.py"]},
        neighbors=set(),
        seed_mode=replay.SEED_MODE_LINES_SIGNATURE,
    )
    assert seen["allowed"] == {"g", "Sig"}


def test_lines_mode_moves_no_existing_number(tmp_path):
    """Аддитивность: режим по умолчанию оставляет строку прогона побайтово
    той же, какой она была до PRI-266. Иначе числа приёмок PRI-255…262
    перестают быть сравнимыми без пересчёта."""
    kwargs = dict(
        tasks={"PRI-34": {"title": "PRI-34", "description": ""}},
        changed={"PRI-34": ["reviewer/a.py"]},
        predicted={"PRI-34": ["reviewer/a.py"]},
        neighbors={"reviewer/b.py#g"},
    )
    default_row = _run(tmp_path / "a", ["PRI-34"], **kwargs)["tasks"][0]
    explicit_row = _run(
        tmp_path / "b", ["PRI-34"], seed_mode=replay.SEED_MODE_LINES, **kwargs
    )["tasks"][0]
    assert default_row == explicit_row


def test_snapshot_records_the_seed_mode(tmp_path):
    """Режим сидов виден в снимке: без него две стороны A/B неразличимы."""
    snap = _run(
        tmp_path,
        ["PRI-33"],
        tasks={"PRI-33": {"title": "PRI-33", "description": ""}},
        changed={"PRI-33": ["reviewer/a.py"]},
        predicted={"PRI-33": ["reviewer/a.py"]},
        neighbors=set(),
        seed_mode=replay.SEED_MODE_LINES_SIGNATURE,
    )
    assert snap["seed_mode"] == replay.SEED_MODE_LINES_SIGNATURE
```

Хелпер `_run` в этом файле обязан пробрасывать новый параметр: найти его определение и добавить `seed_mode=replay.SEED_MODE_LINES` в сигнатуру и в вызов `replay.run_replay(...)`.

- [ ] **Step 2: Прогнать и убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/eval/test_replay.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'SEED_MODE_LINES_SIGNATURE'`.

- [ ] **Step 3: Реализовать режим в `run_replay`**

В `eval/solve_task_metrics/replay.py` дописать рядом с константами статусов:

```python
SEED_MODE_LINES = "lines"
SEED_MODE_LINES_SIGNATURE = "lines+signature"

SEED_MODES = (SEED_MODE_LINES, SEED_MODE_LINES_SIGNATURE)
"""Источник разрешённых имён фильтра (PRI-266).

`lines` — только имена с изменённых строк (поведение PRI-262, дефолт).
`lines+signature` — плюс имена из шапок символов-сидов. Режим, а не правка
кода между прогонами: обе стороны A/B обязаны сниматься одним исходником.
"""


def allowed_names_for(seeds, seed_mode: str) -> set:
    """Разрешённые имена фильтра для выбранного режима сидов."""
    if seed_mode == SEED_MODE_LINES_SIGNATURE:
        return seeds.called_names | seeds.signature_names
    return seeds.called_names
```

В сигнатуру `run_replay` добавить параметр `seed_mode: str = SEED_MODE_LINES` (после `limit`), заменить вызов вывода ядра на:

```python
            seeds = context_seeds.collect_seeds(truth, run_git)
            core_now = context_core.derive_context_core(
                seeds.symbols,
                {p for p in truth.changed if classify.is_core_production_path(p)},
                lambda ids: provider.neighbors(target.repo, target.branch, ids),
                allowed_names=allowed_names_for(seeds, seed_mode),
            )
```

и добавить в возвращаемый словарь снимка, рядом с `"variant"`:

```python
        "seed_mode": seed_mode,
```

- [ ] **Step 4: Прогнать тесты прогона**

Run: `.venv/bin/pytest tests/eval/test_replay.py -q`
Expected: PASS.

- [ ] **Step 5: Написать падающий тест CLI**

Дописать в конец `tests/eval/test_replay_cli.py`:

```python
def test_replay_cli_accepts_context_seeds_flag():
    """Режим сидов задаётся флагом: правка исходника между сторонами A/B
    сделала бы сравнение невалидным."""
    from eval.solve_task_metrics import __main__ as main_mod

    parser = main_mod.build_parser()
    args = parser.parse_args(["replay", "--context-seeds", "lines+signature"])
    assert args.context_seeds == "lines+signature"

    default_args = parser.parse_args(["replay"])
    assert default_args.context_seeds == "lines"
```

- [ ] **Step 6: Прогнать и убедиться, что тест падает**

Run: `.venv/bin/pytest tests/eval/test_replay_cli.py -q`
Expected: FAIL — неизвестный аргумент `--context-seeds`.

- [ ] **Step 7: Добавить флаг и проброс**

В `eval/solve_task_metrics/__main__.py` дописать к `replay_parser`:

```python
    replay_parser.add_argument(
        "--context-seeds",
        default=replay_mod.SEED_MODE_LINES,
        choices=list(replay_mod.SEED_MODES),
        help="источник разрешённых имён контекстного ядра: "
             "lines (по изменённым строкам) или lines+signature (плюс шапки сидов)",
    )
```

и пробросить его в `_replay_side`:

```python
    snap = replay_mod.run_replay(
        provider=provider, run_git=run_git, briefs_dir=BRIEFS_DIR,
        target=target, variant_name=variant_name, commit=commit,
        taken_at=taken_at, limit=args.limit, seed_mode=args.context_seeds,
    )
```

- [ ] **Step 8: Прогнать весь юнит-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check eval reviewer tests`
Expected: PASS, ruff чист. Дефолт `lines` означает, что ни одно существующее число не сдвинулось.

- [ ] **Step 9: Коммит**

```bash
git add eval/solve_task_metrics/replay.py eval/solve_task_metrics/__main__.py tests/eval/test_replay.py tests/eval/test_replay_cli.py
git commit -m "feat(eval): режим сидов контекстного ядра как флаг прогона"
```

---

### Task 4: Статус «знаменатель неопределим»

**Files:**
- Modify: `eval/solve_task_metrics/replay.py` (константа статуса, реестр, правило в `_evaluate`)
- Test: `tests/eval/test_replay.py`, `tests/eval/test_replay_report.py`

**Interfaces:**
- Consumes: `truth.changed`, `classify.is_core_production_path`.
- Produces:
  - `replay.STATUS_UNDEFINED_CONTEXT = "undefined_context_denominator"`, включён в `CONTEXT_STATUSES`.
  - `replay.context_denominator_defined(changed_paths) -> bool` — есть ли среди изменённых core-путей хотя бы один Python-файл.
  - Строка задачи: `context_status == STATUS_UNDEFINED_CONTEXT` при неопределимом знаменателе; `context_recall` при этом `None`, как и раньше.

Разделение проходит по тому, мог ли обход быть засеян в принципе, а не по результату обхода: иначе «неопределим» маскируется пустой выдачей графа.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/eval/test_replay.py`:

```python
def test_non_python_core_is_undefined_not_empty(tmp_path):
    """Ядро целиком не-Python: сидов не могло возникнуть ни при какой
    настройке фильтра, поэтому знаменатель неопределим, а не пуст."""
    snap = _run(
        tmp_path,
        ["PRI-41"],
        tasks={"PRI-41": {"title": "PRI-41", "description": ""}},
        changed={"PRI-41": ["plugin/manifest.json"]},
        predicted={"PRI-41": ["plugin/manifest.json"]},
        neighbors=set(),
    )
    row = snap["tasks"][0]
    assert row["context_status"] == replay.STATUS_UNDEFINED_CONTEXT
    assert row["context_status"] != replay.STATUS_EMPTY_CONTEXT
    assert row["context_recall"] is None


def test_no_core_paths_at_all_is_undefined(tmp_path):
    """Дифф из тестов и доков: контекстного знаменателя тоже нет."""
    snap = _run(
        tmp_path,
        ["PRI-42"],
        tasks={"PRI-42": {"title": "PRI-42", "description": ""}},
        changed={"PRI-42": ["tests/test_x.py", "docs/readme.md"]},
        predicted={"PRI-42": ["tests/test_x.py"]},
        neighbors=set(),
    )
    assert snap["tasks"][0]["context_status"] == replay.STATUS_UNDEFINED_CONTEXT


def test_python_core_with_empty_traversal_stays_empty(tmp_path):
    """Содержательный ноль остаётся нолём: сиды были, читать нечего."""
    snap = _run(
        tmp_path,
        ["PRI-43"],
        tasks={"PRI-43": {"title": "PRI-43", "description": ""}},
        changed={"PRI-43": ["reviewer/a.py"]},
        predicted={"PRI-43": ["reviewer/a.py"]},
        neighbors=set(),
    )
    assert snap["tasks"][0]["context_status"] == replay.STATUS_EMPTY_CONTEXT


def test_undefined_context_is_counted_in_context_statuses(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-44"],
        tasks={"PRI-44": {"title": "PRI-44", "description": ""}},
        changed={"PRI-44": ["plugin/manifest.json"]},
        predicted={"PRI-44": ["plugin/manifest.json"]},
        neighbors=set(),
    )
    assert snap["context_statuses"][replay.STATUS_UNDEFINED_CONTEXT] == 1
    assert snap["context_statuses"][replay.STATUS_EMPTY_CONTEXT] == 0


def test_graph_failure_wins_over_undefined(tmp_path):
    """Сбой обхода называется сбоем: молчаливая подмена его на «неопределим»
    прятала бы недоступный Neo4j в штатном статусе."""
    snap = _run_with_failing_graph(
        tmp_path,
        ["PRI-45"],
        tasks={"PRI-45": {"title": "PRI-45", "description": ""}},
        changed={"PRI-45": ["reviewer/a.py"]},
        predicted={"PRI-45": ["reviewer/a.py"]},
    )
    assert snap["tasks"][0]["context_status"] == replay.STATUS_CONTEXT_FAILED
```

Дописать в конец `tests/eval/test_replay_report.py`:

```python
def test_report_renders_undefined_context_status():
    from eval.solve_task_metrics import replay

    snap = _snapshot_with()
    snap["context_statuses"] = {
        replay.STATUS_MEASURED: 2,
        replay.STATUS_EMPTY_CONTEXT: 1,
        replay.STATUS_UNDEFINED_CONTEXT: 3,
        replay.STATUS_CONTEXT_FAILED: 0,
    }
    text = replay_report.render(snap, None)
    assert replay.STATUS_UNDEFINED_CONTEXT in text
```

Хелпер `_snapshot_with()` уже есть в этом файле (`tests/eval/test_replay_report.py:68`).

- [ ] **Step 2: Прогнать и убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/eval/test_replay.py tests/eval/test_replay_report.py -q`
Expected: FAIL — `AttributeError: ... 'STATUS_UNDEFINED_CONTEXT'`.

- [ ] **Step 3: Добавить статус и правило**

В `eval/solve_task_metrics/replay.py` дописать константу рядом с остальными статусами и включить её в реестр:

```python
STATUS_UNDEFINED_CONTEXT = "undefined_context_denominator"

CONTEXT_STATUSES = (
    STATUS_MEASURED,
    STATUS_EMPTY_CONTEXT,
    STATUS_UNDEFINED_CONTEXT,
    STATUS_CONTEXT_FAILED,
)
```

Дописать предикат рядом с `allowed_names_for`:

```python
def context_denominator_defined(changed_paths) -> bool:
    """Мог ли обход быть засеян в принципе.

    Сиды строятся через `chunk_python`, а граф хранит символы только для
    Python. У задачи, чьё изменённое ядро целиком не-Python (или ядра нет
    вовсе), знаменатель контекста НЕОПРЕДЕЛИМ, а не пуст: «пусто» — это
    высказывание «читать нечего», и смешивать с ним «мерить нечем» значит
    считать метрику там, где точки измерения не существует.

    Считается по фактическим путям задачи, а не по результату обхода: иначе
    неопределимость маскируется пустой выдачей графа.
    """
    return any(
        classify.is_core_production_path(path) and path.endswith(".py")
        for path in changed_paths
    )
```

Заменить ветку вычисления `context_status` в `_evaluate` на:

```python
    if context_failed:
        context_status = STATUS_CONTEXT_FAILED
    elif context_core_paths:
        context_status = STATUS_MEASURED
    elif not context_denominator_defined(truth.changed):
        context_status = STATUS_UNDEFINED_CONTEXT
    else:
        context_status = STATUS_EMPTY_CONTEXT
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/eval/test_replay.py tests/eval/test_replay_report.py -q`
Expected: PASS.

- [ ] **Step 5: Прогнать весь юнит-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check eval reviewer tests`
Expected: PASS, ruff чист. Числа core-recall не двигаются: статус контекста в них не участвует.

- [ ] **Step 6: Коммит**

```bash
git add eval/solve_task_metrics/replay.py tests/eval/test_replay.py tests/eval/test_replay_report.py
git commit -m "feat(eval): отдельный статус для неопределимого знаменателя контекста"
```

---

### Task 5: Замер, ручная сверка и вердикт по гейту

**Files:**
- Create: `eval/pri266_eye_check.md`
- Modify: `eval/replay_report.md` (раздел «Приёмка PRI-266» — только внутри ручной части, ниже маркера слияния)
- Modify: `eval/replay_history.jsonl` (пишется прогоном автоматически)

**Interfaces:**
- Consumes: CLI `--context-seeds` (Task 3), статусы контекста (Task 4), журнал `eval/pri266_preregistration.md` (Task 1).
- Produces: числа приёмки, на которые ссылается Task 6.

Задача требует живой инфраструктуры: ParadeDB (:5433), Neo4j, Voyage. Обе стороны — на одном `indexed_sha`, обе команды — с явным `--branch dev`.

- [ ] **Step 1: Убедиться, что индекс свеж и запомнить sha**

Run: `uvx --from rag-reviewer reviewer status . --branch dev --json`
Expected: `drift == 0`. Если нет — `uvx --from rag-reviewer reviewer index . --ref dev` и повторить. Записать `indexed_sha` — он обязан совпасть у обеих сторон.

- [ ] **Step 2: Снять сторону «до» (режим lines)**

Run: `.venv/bin/python -m eval.solve_task_metrics replay --branch dev --context-seeds lines`
Expected: прогон завершается, печатает `Индекс: … sha=<indexed_sha>`, снимок дописан в `eval/replay_history.jsonl`. Записать: `context_n_measured`, `no_context_measurement`, `context_recall_median`, число путей контекстного ядра, медиану размера ядра, счётчики `context_statuses`.

- [ ] **Step 3: Снять сторону «после» (режим lines+signature)**

Run: `.venv/bin/python -m eval.solve_task_metrics replay --branch dev --context-seeds lines+signature --baseline last`
Expected: тот же `indexed_sha` в выводе. Записать те же величины.

- [ ] **Step 4: Проверить фальсифицируемые предсказания поимённо**

Из обоих снимков `eval/replay_history.jsonl` достать строки задач PRI-227, PRI-251, PRI-177, PRI-237, PRI-243 и проверить:

```bash
.venv/bin/python - <<'PY'
import json, pathlib
rows = [json.loads(line) for line in pathlib.Path("eval/replay_history.jsonl").read_text().splitlines()]
after = rows[-1]
for key in ("PRI-227", "PRI-251", "PRI-177", "PRI-237", "PRI-243"):
    task = next((t for t in after["tasks"] if t["key"] == key), None)
    print(key, task["context_status"], task["context_core"], task["context_core_paths"] if task else None)
PY
```

Expected: PRI-227 — ядро пустое (0 сидов, предсказание 1); PRI-251 — среди `context_core_paths` есть хотя бы один из `reviewer/index/chunker.py`, `reviewer/index/models.py`, `reviewer/gitutil.py` (предсказание 2); PRI-177 / PRI-237 / PRI-243 — `context_status == "undefined_context_denominator"` (предсказание 3). Результат каждого предсказания записать как есть, включая неподтверждённые.

- [ ] **Step 5: Ручная сверка точности по предзарегистрированной выборке**

Для шести задач выборки (PRI-227, PRI-236, PRI-215, PRI-221, PRI-251, PRI-249) просмотреть `context_core_paths` стороны «после» и по каждому пути вынести решение «осмысленный / мусорный»: осмысленный — файл, который реально надо прочитать, чтобы сделать эту задачу. Посчитать долю осмысленных по выборке. Ту же процедуру провести для стороны «до» на тех же задачах — обе колонки обязаны быть получены одинаково.

- [ ] **Step 6: Написать `eval/pri266_eye_check.md`**

Файл строится по форме `eval/pri262_eye_check.md` и содержит разделы:
- **Метод** — что сравнивалось, на каком `indexed_sha`, какими командами, кто выносил решение по каждому пути.
- **Результат** — таблица по шести задачам: осмысленных / всего, до и после; итоговая доля.
- **Фальсифицируемые предсказания** — три предсказания и что с каждым случилось, включая неподтверждённые.
- **Цена** — какие пути потеряны и какие приобретены, поимённо; сюда же попадают мусорные пути, если рычаг их вернул.
- **Эффект на корпусе** — таблица: путей контекстного ядра, медиана размера ядра, задач с измеренным знаменателем, задач со статусом «неопределим», задач без измерения, плюс `core_recall_median`, `bulk_core_recall_median`, `precision_median` (последние три обязаны быть побайтово теми же — доказательство аддитивности).
- **Вердикт** — обе половины гейта названы числом, и сказано, взят гейт или нет.

- [ ] **Step 7: Дописать раздел приёмки в отчёт**

В `eval/replay_report.md` дописать раздел «Приёмка PRI-266» — **ниже маркера слияния**, в ручной части (машинная часть перезаписывается прогоном; PRI-265). Раздел: условия замера, обе половины гейта числом, вердикт, ссылка на `eval/pri266_eye_check.md`.

- [ ] **Step 8: Проверить, что ручная часть отчёта пережила прогон**

Run: `git diff --stat eval/replay_report.md`
Expected: раздел «Приёмка PRI-266» на месте, ранее написанные ручные разделы не стёрты.

- [ ] **Step 9: Коммит**

```bash
git add eval/pri266_eye_check.md eval/replay_report.md eval/replay_history.jsonl
git commit -m "docs(eval): приёмка PRI-266 — замер покрытия и ручная сверка точности"
```

---

### Task 6: Зафиксировать вердикт в документации и выбрать дефолт

**Files:**
- Modify: `CLAUDE.md` (абзац про офлайн-only `context-recall`)
- Modify: `eval/solve_task_metrics/replay.py` (дефолт `seed_mode` — только если гейт взят)

**Interfaces:**
- Consumes: числа Task 5.
- Produces: документированный факт и дефолтный режим сидов.

Дефолт меняется ТОЛЬКО при взятом гейте. Гейт не взят — дефолт остаётся `lines`, а документация называет отрицательный результат и область применимости метрики: это предзарегистрированное стоп-правило, а не поражение.

- [ ] **Step 1: Если гейт взят — переключить дефолт**

В `eval/solve_task_metrics/replay.py` заменить дефолт параметра на `seed_mode: str = SEED_MODE_LINES_SIGNATURE` и дефолт CLI-флага `--context-seeds` в `__main__.py` на `replay_mod.SEED_MODE_LINES_SIGNATURE`. Если гейт не взят — шаг пропускается целиком, и это фиксируется в тексте документации.

- [ ] **Step 2: Прогнать юнит-набор**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check eval reviewer tests`
Expected: PASS. Тесты Task 3 проверяют оба режима явно, поэтому смена дефолта их не ломает; если ломает — тест зависел от дефолта неявно, и это его дефект, который надо починить явным аргументом.

- [ ] **Step 3: Обновить абзац в `CLAUDE.md`**

В разделе «Неочевидные факты» дописать к абзацу про офлайн-only `context-recall` итог PRI-266: два механизма (шапка символа-сида как второй источник имён; отдельный статус `undefined_context_denominator` для не-Python ядра), обе половины гейта числом, действующий дефолт режима сидов, и — при непройденном гейте — явно названная область применимости метрики. Числа берутся из `eval/pri266_eye_check.md`, ссылки на файлы приводятся путями.

- [ ] **Step 4: Проверить, что факт не противоречит соседним абзацам**

Run: `grep -n "context-recall\|PRI-261\|PRI-262\|PRI-266" CLAUDE.md`
Expected: абзацы PRI-261 / PRI-262 не переписаны и не противоречат новому — PRI-266 дополняет их, а не отменяет.

- [ ] **Step 5: Коммит**

```bash
git add CLAUDE.md eval/solve_task_metrics/replay.py eval/solve_task_metrics/__main__.py
git commit -m "docs: зафиксировать итог PRI-266 в неочевидных фактах"
```

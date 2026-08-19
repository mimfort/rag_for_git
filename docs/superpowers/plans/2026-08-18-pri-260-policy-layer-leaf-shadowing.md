# PRI-260 — слияние слоёв политики по листу и видимость дрейфа рабочего дерева

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** слой политики, не высказавшийся о подсекции, перестаёт её стирать; диагностика `config show` называет затенённый лист, а не верхний ключ, и сообщает о расхождении рабочего дерева с коммиченным `.review.yml`.

**Architecture:** новый чистый модуль `reviewer/config/deepmerge.py` держит рекурсивное слияние и листовую диагностику; `resolve_policy_data` и `_simulated_repo_layer` в `layers.py` зовут его вместо двух своих копий логики; `build_config_report` отдаёт листовые `sources`/`shadowed`, а `_render_config_report` сворачивает их обратно в компактный вывод; проверка дрейфа рабочего дерева живёт чистой функцией в `committed.py` и вызывается только из `config show`.

**Tech Stack:** Python 3.11+, pytest, click, PyYAML. Внешних зависимостей задача не добавляет.

**Spec:** `docs/superpowers/specs/2026-08-18-pri-260-policy-layer-leaf-shadowing-design.md`

## Global Constraints

- Порядок слоёв не меняется: `home:review.yml` → коммиченный `.review.yml` → `home:repos/<repo>.yml` (`reviewer/config/layers.py:446-449`).
- Рабочее дерево клона НЕ становится источником политики: дрейф — только предупреждение.
- Рекурсия входит в ключ только когда **оба** значения — `Mapping`; списки, скаляры и смена типа заменяются целиком.
- `task_board` — единственный атомарный mapping-ключ (`ATOMIC_KEYS`), заменяется целиком.
- Ключи `sources`/`shadowed` — dotted-путь до листа; для скаляра верхнего уровня путь совпадает с прежним плоским ключом.
- Все тесты задачи — unit, без Postgres/Neo4j/сети. Тесты, создающие git-клон через `subprocess`, следуют уже существующему в репозитории образцу (`tests/config/test_committed_layer.py::_make_clone`) и остаются unit.
- Язык кода проекта — русский: комментарии и докстринги на русском.
- Коммиты — Conventional Commits на русском, без self-attribution.
- Прогон тестов: `.venv/bin/pytest -q <путь>`.

---

### Task 1: Модуль рекурсивного слияния `reviewer/config/deepmerge.py`

Чистая функция без зависимостей от резолвера — её потребят и `resolve_policy_data`, и `_simulated_repo_layer` (Task 2).

**Files:**
- Create: `reviewer/config/deepmerge.py`
- Test: `tests/config/test_deepmerge.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `ATOMIC_KEYS: frozenset[str]` — `frozenset({"task_board"})`
  - `leaf_paths(value: object, prefix: str = "") -> list[str]`
  - `merge_layer(merged: dict[str, object], sources: dict[str, str], shadowed: dict[str, list[str]], data: Mapping[str, object], source: str) -> None` — мутирует первые три аргумента на месте.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/config/test_deepmerge.py`:

```python
"""Рекурсивное слияние слоёв политики с листовой диагностикой (PRI-260)."""
from __future__ import annotations

from reviewer.config.deepmerge import leaf_paths, merge_layer


def _merge(*layers: tuple[dict, str]):
    merged: dict[str, object] = {}
    sources: dict[str, str] = {}
    shadowed: dict[str, list[str]] = {}
    for data, source in layers:
        merge_layer(merged, sources, shadowed, data, source)
    return merged, sources, shadowed


def test_partial_mapping_layer_keeps_untouched_subsections():
    """Слой, не высказавшийся о подсекции, её не стирает."""
    merged, sources, shadowed = _merge(
        ({"context_limits": {"code_section": {"max_files": 12}}}, ".review.yml"),
        ({"context_limits": {"graph": {"hops": 2}}}, "home:repos/o/r.yml"),
    )

    assert merged["context_limits"] == {
        "code_section": {"max_files": 12},
        "graph": {"hops": 2},
    }
    assert sources["context_limits.code_section.max_files"] == ".review.yml"
    assert sources["context_limits.graph.hops"] == "home:repos/o/r.yml"
    assert shadowed == {}


def test_shadowing_names_the_leaf_not_the_top_key():
    merged, sources, shadowed = _merge(
        ({"context_limits": {"code_section": {"max_files": 12, "chars_per_file": 1300}}},
         ".review.yml"),
        ({"context_limits": {"code_section": {"max_files": 20}}}, "home:repos/o/r.yml"),
    )

    assert merged["context_limits"]["code_section"] == {
        "max_files": 20, "chars_per_file": 1300,
    }
    assert shadowed == {"context_limits.code_section.max_files": [".review.yml"]}


def test_lists_and_scalars_are_replaced_whole():
    merged, sources, shadowed = _merge(
        ({"paths": {"ignore": ["a", "b"]}, "max_comments": 5}, "home:review.yml"),
        ({"paths": {"ignore": ["c"]}, "max_comments": 7}, ".review.yml"),
    )

    assert merged["paths"] == {"ignore": ["c"]}          # без слияния по элементам
    assert merged["max_comments"] == 7
    assert sources["paths.ignore"] == ".review.yml"
    assert sources["max_comments"] == ".review.yml"      # скаляр: путь = прежний плоский ключ
    assert shadowed["paths.ignore"] == ["home:review.yml"]
    assert shadowed["max_comments"] == ["home:review.yml"]


def test_task_board_is_atomic():
    """Связный контракт доски не смешивается между слоями."""
    merged, sources, shadowed = _merge(
        ({"task_board": {"type": "yougile", "project": "PRI", "done_target": "Готово"}},
         ".review.yml"),
        ({"task_board": {"type": "jira"}}, "home:repos/o/r.yml"),
    )

    assert merged["task_board"] == {"type": "jira"}
    assert sources["task_board"] == "home:repos/o/r.yml"
    assert shadowed["task_board"] == [".review.yml"]


def test_categories_merge_per_flag():
    merged, _sources, shadowed = _merge(
        ({"categories": {"sql": True, "security": True}}, ".review.yml"),
        ({"categories": {"sql": False}}, "home:repos/o/r.yml"),
    )

    assert merged["categories"] == {"sql": False, "security": True}
    assert shadowed == {"categories.sql": [".review.yml"]}


def test_type_change_clears_stale_leaf_records():
    """Mapping, заменённый скаляром, не оставляет следов своих листьев."""
    merged, sources, shadowed = _merge(
        ({"context_limits": {"graph": {"hops": 2}}}, "home:review.yml"),
        ({"context_limits": None}, ".review.yml"),
    )

    assert merged["context_limits"] is None
    assert "context_limits.graph.hops" not in sources
    assert sources["context_limits"] == ".review.yml"
    assert shadowed["context_limits.graph.hops"] == ["home:review.yml"]


def test_leaf_paths_of_empty_mapping_is_the_mapping_itself():
    assert leaf_paths({"a": {"b": 1}}, "top") == ["top.a.b"]
    assert leaf_paths({}, "top") == ["top"]
    assert leaf_paths(5, "top") == ["top"]
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/config/test_deepmerge.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.config.deepmerge'`

- [ ] **Step 3: Написать модуль**

Создать `reviewer/config/deepmerge.py`:

```python
"""Рекурсивное слияние слоёв политики с листовой диагностикой (PRI-260).

Слой, не высказавшийся о подсекции, не должен её стирать: до PRI-260 слияние
шло по верхнему ключу, и домашний per-repo слой с частичной `context_limits`
уносил коммиченные подсекции целиком. Диагностика затенения здесь той же
гранулярности — путь до листа, иначе по отчёту `config show` неотличимо
«подсекция потеряна» от «её и не было».

Модуль намеренно чистый (никаких путей, файлов и VCS): его зовут и резолвер
`resolve_policy_data`, и симуляция публикации домашнего слоя в `config migrate`.
Одна копия логики на обоих — иначе `migrate` и `show` разошлись бы в вердикте
о затенении.
"""
from __future__ import annotations

from collections.abc import Mapping

# Единственный атомарный mapping-ключ: task_board — связный контракт
# (type + project + create_target/done_target + options), а не набор
# независимых настроек. Частичный домашний `task_board: {type: jira}` поверх
# коммиченного yougile дал бы при рекурсии конфиг доски, которого не писал ни
# один слой: тип от одного, проект и целевые колонки — от другого.
ATOMIC_KEYS = frozenset({"task_board"})


def leaf_paths(value: object, prefix: str = "") -> list[str]:
    """Пути до листьев значения. Пустой mapping — сам себе лист."""
    if isinstance(value, Mapping) and value:
        paths: list[str] = []
        for key, child in value.items():
            nested = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(leaf_paths(child, nested))
        return paths
    return [prefix]


def merge_layer(
    merged: dict[str, object],
    sources: dict[str, str],
    shadowed: dict[str, list[str]],
    data: Mapping[str, object],
    source: str,
) -> None:
    """Наложить слой ``data`` на ``merged``, обновив листовую диагностику."""
    _merge_mapping(merged, sources, shadowed, data, source, "")


def _merge_mapping(
    merged: dict[str, object],
    sources: dict[str, str],
    shadowed: dict[str, list[str]],
    data: Mapping[str, object],
    source: str,
    prefix: str,
) -> None:
    for raw_key, value in data.items():
        key = str(raw_key)
        path = f"{prefix}.{key}" if prefix else key
        top = path.split(".", 1)[0]
        current = merged.get(raw_key)
        if (
            isinstance(value, Mapping)
            and isinstance(current, Mapping)
            and top not in ATOMIC_KEYS
        ):
            child = dict(current)
            _merge_mapping(child, sources, shadowed, value, source, path)
            merged[raw_key] = child
            continue
        _replace(merged, sources, shadowed, raw_key, path, value, source, top)


def _replace(
    merged: dict[str, object],
    sources: dict[str, str],
    shadowed: dict[str, list[str]],
    raw_key: object,
    path: str,
    value: object,
    source: str,
    top: str,
) -> None:
    """Заменить значение целиком, сняв прежние записи под этим путём.

    Снятие обязательно: mapping, заменённый скаляром, иначе оставил бы в
    отчёте источники листьев, которых в эффективной политике больше нет.
    """
    for stale in [k for k in sources if k == path or k.startswith(f"{path}.")]:
        shadowed.setdefault(stale, []).append(sources.pop(stale))
    merged[raw_key] = value
    if top in ATOMIC_KEYS or not isinstance(value, Mapping):
        sources[path] = source
        return
    for leaf in leaf_paths(value, path):
        sources[leaf] = source
```

- [ ] **Step 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest -q tests/config/test_deepmerge.py`
Expected: PASS (8 тестов)

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/config/deepmerge.py tests/config/test_deepmerge.py
git add reviewer/config/deepmerge.py tests/config/test_deepmerge.py
git commit -m "feat(config): рекурсивное слияние слоёв политики с листовой диагностикой"
```

---

### Task 2: Подключить рекурсивное слияние к резолверу и отчёту

Обе копии логики слияния в `layers.py` заменяются вызовом `merge_layer`, а `build_config_report` начинает отдавать листовые `sources`.

**Files:**
- Modify: `reviewer/config/layers.py:355-360` (замыкание `merge` в `resolve_policy_data`), `reviewer/config/layers.py:1015-1032` (`_simulated_repo_layer`), `reviewer/config/layers.py:585-615` (`build_config_report`)
- Test: `tests/config/test_layers.py`

**Interfaces:**
- Consumes: `merge_layer` из Task 1.
- Produces: `ResolutionMeta.sources` / `.shadowed` с ключами-путями до листа (типы полей не меняются); `build_config_report(...)["sources"]` — `dict[str, str]` с теми же путями (только листья, чей верхний ключ присутствует в `effective`).

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/config/test_layers.py`:

```python
def test_pri259_committed_code_section_pin_survives_partial_home_layer(tmp_path: Path) -> None:
    """Регресс PRI-259: домашний context_limits без code_section не уносит коммиченный пин."""
    _write(
        tmp_path / "repos/o/r.yml",
        "context_limits: {graph: {hops: 1}, search_codebase: {ceiling: 15}}\n",
    )
    committed = (
        "context_limits:\n"
        "  code_section: {max_files: 12, chars_per_file: 1300}\n"
    )

    data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: committed, config_root=tmp_path
    )

    limits = data["context_limits"]
    assert limits["code_section"] == {"max_files": 12, "chars_per_file": 1300}
    assert limits["graph"] == {"hops": 1}
    assert meta.sources["context_limits.code_section.max_files"] == ".review.yml"
    assert meta.sources["context_limits.graph.hops"] == "home:repos/o/r.yml"
    assert "context_limits" not in meta.shadowed        # верхний ключ больше не затеняется целиком


def test_shadowed_names_the_leaf_when_subsection_is_overridden(tmp_path: Path) -> None:
    _write(tmp_path / "repos/o/r.yml", "context_limits: {code_section: {max_files: 20}}\n")
    committed = "context_limits: {code_section: {max_files: 12, chars_per_file: 1300}}\n"

    _data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: committed, config_root=tmp_path
    )

    assert meta.shadowed["context_limits.code_section.max_files"] == (".review.yml",)
    assert "context_limits" not in meta.shadowed


def test_layer_order_is_unchanged(tmp_path: Path) -> None:
    """Критерий 4: приоритет источников прежний — домашний per-repo слой последний."""
    _write(tmp_path / "review.yml", "max_comments: 1\n")
    _write(tmp_path / "repos/o/r.yml", "max_comments: 3\n")

    data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: "max_comments: 2\n", config_root=tmp_path
    )

    assert data["max_comments"] == 3
    assert meta.sources["max_comments"] == "home:repos/o/r.yml"
    assert meta.shadowed["max_comments"] == ("home:review.yml", ".review.yml")
```

Обновить существующий `test_layers_replace_top_level_values_and_report_sources` (`tests/config/test_layers.py:25-52`) — три ассерта меняются, потому что меняется само поведение:

```python
    assert data["paths"] == {"ignore": ["home-repo"]}
    # context_limits теперь сливается по подсекциям: слой, не высказавшийся о
    # search_codebase, её не стирает.
    assert data["context_limits"] == {
        "graph": {"hops": 2},
        "search_codebase": {"ceiling": 25},
    }
    assert data["task_board"] is None
    assert meta.sources["paths.ignore"] == "home:repos/o/r.yml"
    assert meta.sources["max_comments"] == ".review.yml"
    assert meta.shadowed["paths.ignore"] == ("home:review.yml", ".review.yml")
    assert meta.shadowed["max_comments"] == ("home:review.yml",)
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/config/test_layers.py`
Expected: FAIL — новые тесты падают на `KeyError: 'context_limits.code_section.max_files'`, обновлённый — на `assert data["context_limits"] == {...}`.

- [ ] **Step 3: Заменить обе копии слияния и собрать листовые sources**

В `reviewer/config/layers.py` добавить импорт рядом с прочими импортами модуля:

```python
from reviewer.config.deepmerge import merge_layer
```

Заменить замыкание `merge` внутри `resolve_policy_data` (строки 355-360) на:

```python
    def merge(data: Mapping[str, object], source: str) -> None:
        merge_layer(merged, sources, shadowed, data, source)
```

Заменить тело `_simulated_repo_layer` (строки 1015-1032) на общий вызов — ручная копия логики здесь и была тем, что разошлось бы с резолвером:

```python
def _simulated_repo_layer(
    data: Mapping[str, object], meta: ResolutionMeta, candidate: Mapping[str, object], source: str
) -> tuple[dict[str, object], ResolutionMeta]:
    """Вычислить результат repository-home layer до его атомарной публикации."""
    merged = dict(data)
    sources = dict(meta.sources)
    shadowed = {key: list(value) for key, value in meta.shadowed.items()}
    merge_layer(merged, sources, shadowed, candidate, source)
    return merged, ResolutionMeta(
        sources,
        {key: tuple(value) for key, value in shadowed.items()},
        meta.warnings,
        skipped=meta.skipped,
    )
```

В `build_config_report` заменить сборку `sources` (строка 605) на листовую:

```python
    sources = {
        path: layer
        for path, layer in meta.sources.items()
        if path.split(".", 1)[0] in effective
    }
    sources.update({
        key: "env"
        for key in effective
        if not any(
            path == key or path.startswith(f"{key}.") for path in meta.sources
        )
    })
```

и заодно поправить докстринг `build_config_report`, назвав листовую гранулярность
`sources`/`shadowed`.

- [ ] **Step 4: Прогнать тесты пакета config и убедиться, что они проходят**

Run: `.venv/bin/pytest -q tests/config/`
Expected: PASS. Если падают тесты `config migrate` — сверить, что `_simulated_repo_layer` вызывается с тем же порядком аргументов; изменения поведения `migrate` план не предполагает, кроме гранулярности ключей `shadowed`.

- [ ] **Step 5: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS. Падения ожидаются только в `tests/entrypoints/test_config_commands.py` (форма отчёта) — их чинит Task 3; если падает что-то ещё, разбираться здесь.

- [ ] **Step 6: Коммит**

```bash
.venv/bin/ruff check reviewer/config/layers.py tests/config/test_layers.py
git add reviewer/config/layers.py tests/config/test_layers.py
git commit -m "fix(config): слой политики не стирает подсекции, о которых молчит"
```

---

### Task 3: Свёртка листовой диагностики в выводе `config show`

**Files:**
- Modify: `reviewer/entrypoints/cli.py:150-189` (`_render_config_report`)
- Test: `tests/entrypoints/test_config_commands.py`

**Interfaces:**
- Consumes: листовые `report["sources"]` / `report["shadowed"]` из Task 2.
- Produces: текстовый вывод `config show`; форма JSON не меняется относительно Task 2.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/entrypoints/test_config_commands.py`:

```python
def test_config_show_text_collapses_single_source_key(monkeypatch, tmp_path):
    """Ключ из одного слоя печатается одной строкой, как до PRI-260."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    assert result.exit_code == 0, result.output
    assert "max_comments: 7" in result.output
    assert "  source: .review.yml" in result.output
    assert "mixed" not in result.output


def test_config_show_text_reports_mixed_sources_per_leaf(monkeypatch, tmp_path):
    """Ключ, собранный из двух слоёв, называет слой каждого расходящегося листа."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home_repo = tmp_path / "rag-reviewer/repos/o/r.yml"
    home_repo.parent.mkdir(parents=True, exist_ok=True)
    home_repo.write_text("context_limits: {graph: {hops: 3}}\n", encoding="utf-8")
    _install_fake_vcs(
        monkeypatch, "context_limits: {code_section: {max_files: 12}}\n"
    )

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    assert result.exit_code == 0, result.output
    assert "  source: mixed" in result.output
    assert "    context_limits.code_section.max_files: .review.yml" in result.output
    assert "    context_limits.graph.hops: home:repos/o/r.yml" in result.output


def test_config_show_text_shadowing_names_the_leaf(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home_repo = tmp_path / "rag-reviewer/repos/o/r.yml"
    home_repo.parent.mkdir(parents=True, exist_ok=True)
    home_repo.write_text(
        "context_limits: {code_section: {max_files: 20}}\n", encoding="utf-8"
    )
    _install_fake_vcs(
        monkeypatch,
        "context_limits: {code_section: {max_files: 12, chars_per_file: 1300}}\n",
    )

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    assert result.exit_code == 0, result.output
    assert "  shadowed:" in result.output
    assert (
        "    context_limits.code_section.max_files: .review.yml" in result.output
    )
```

Существующий `test_config_show_json_reports_effective_sources_and_shadowing`
(`tests/entrypoints/test_config_commands.py:35`) обновить под путь-ключи: там, где
ассерты обращаются к `payload["sources"]["paths"]` / `payload["shadowed"]["paths"]`,
использовать `paths.ignore`; ассерты на скалярные ключи (`max_comments`) не меняются.

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/entrypoints/test_config_commands.py`
Expected: FAIL — вывод печатает `source: home:repos/o/r.yml` вместо `mixed`, подстрок по листьям нет.

- [ ] **Step 3: Реализовать свёртку в рендере**

В `reviewer/entrypoints/cli.py` заменить цикл вывода ключей в `_render_config_report`
(строки 175-181) на:

```python
    for key in sorted(effective):
        click.echo(
            f"{key}: {json.dumps(effective[key], ensure_ascii=False, sort_keys=True)}"
        )
        _echo_leaf_block("source", _leaves_of(sources, key))
        _echo_leaf_block(
            "shadowed",
            {path: ", ".join(layers) for path, layers in _leaves_of(shadowed, key).items()},
        )
```

и добавить два хелпера рядом с `_render_config_report`:

```python
def _leaves_of(mapping: Mapping[str, object], key: str) -> dict[str, str]:
    """Записи диагностики, относящиеся к верхнему ключу ``key``."""
    prefix = f"{key}."
    return {
        path: value                                    # type: ignore[misc]
        for path, value in mapping.items()
        if path == key or path.startswith(prefix)
    }


def _echo_leaf_block(label: str, leaves: Mapping[str, str]) -> None:
    """Одна строка, если все листья согласны; иначе — строка на лист.

    Свёртка обязательна: без неё вывод одних только context_limits занял бы
    десятки строк, а ради скалярных ключей формат менять незачем.
    """
    if not leaves:
        return
    values = set(leaves.values())
    if len(values) == 1:
        click.echo(f"  {label}: {values.pop()}")
        return
    click.echo(f"  {label}: mixed" if label == "source" else f"  {label}:")
    for path in sorted(leaves):
        click.echo(f"    {path}: {leaves[path]}")
```

- [ ] **Step 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest -q tests/entrypoints/test_config_commands.py tests/config/`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/entrypoints/cli.py tests/entrypoints/test_config_commands.py
git add reviewer/entrypoints/cli.py tests/entrypoints/test_config_commands.py
git commit -m "feat(cli): config show называет затенённый лист и смешанные источники ключа"
```

---

### Task 4: Дрейф рабочего дерева и метка `git-blob`

**Files:**
- Modify: `reviewer/config/committed.py:23` (метка), `reviewer/config/committed.py:53-104` (публичный доступ к корню клона), конец модуля (новая функция), `reviewer/entrypoints/cli.py:163-167` и `reviewer/entrypoints/cli.py:274-292` (вызов и вывод)
- Test: `tests/config/test_committed_layer.py`, `tests/entrypoints/test_config_commands.py`

**Interfaces:**
- Consumes: `leaf_paths` из Task 1.
- Produces:
  - `SOURCE_LOCAL = "git-blob"`
  - `CommittedLayerFetcher.clone_root -> str | None` (свойство)
  - `DriftReport(status: str, keys: tuple[str, ...])` — статусы `"clean" | "drifted" | "absent_in_worktree" | "absent_in_blob" | "ref_not_head" | "unknown"`
  - `worktree_drift(root: str, ref: str, policy_file: str = POLICY_FILE) -> DriftReport`
  - `report["worktree_drift"]` — `{"status": str, "keys": list[str]}` или отсутствует, если клона нет.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/config/test_committed_layer.py`:

```python
from reviewer.config.committed import DriftReport, worktree_drift


def test_clean_worktree_reports_no_drift(tmp_path):
    root = _make_clone(tmp_path)
    assert worktree_drift(str(root), "main") == DriftReport("clean", ())


def test_worktree_edit_names_diverging_leaf_keys_without_values(tmp_path):
    """Правка в рабочем дереве видна по ключам; значения в отчёт не попадают."""
    root = _make_clone(tmp_path, content="max_comments: 5\ncontext_limits: {graph: {hops: 1}}\n")
    (root / ".review.yml").write_text(
        "max_comments: 9\ncontext_limits: {graph: {hops: 1}}\n", encoding="utf-8"
    )

    report = worktree_drift(str(root), "main")

    assert report.status == "drifted"
    assert report.keys == ("max_comments",)
    assert "9" not in " ".join(report.keys)


def test_missing_worktree_file_is_its_own_status(tmp_path):
    root = _make_clone(tmp_path)
    (root / ".review.yml").unlink()

    assert worktree_drift(str(root), "main").status == "absent_in_worktree"


def test_untracked_worktree_file_is_its_own_status(tmp_path):
    root = _make_clone(tmp_path)
    _git(root, "rm", "-q", ".review.yml")
    _git(root, "commit", "-qm", "drop policy")
    (root / ".review.yml").write_text("max_comments: 3\n", encoding="utf-8")

    assert worktree_drift(str(root), "main").status == "absent_in_blob"


def test_ref_other_than_head_is_not_compared(tmp_path):
    """Сравнение с рабочим деревом осмысленно только когда ref == HEAD клона."""
    root = _make_clone(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature")
    (root / ".review.yml").write_text("max_comments: 42\n", encoding="utf-8")
    _git(root, "commit", "-qam", "feature policy")

    assert worktree_drift(str(root), "main").status == "ref_not_head"


def test_broken_yaml_in_worktree_is_fail_soft(tmp_path):
    root = _make_clone(tmp_path)
    (root / ".review.yml").write_text("max_comments: [unclosed\n", encoding="utf-8")

    assert worktree_drift(str(root), "main").status == "unknown"


def test_committed_source_label_names_the_blob(tmp_path):
    root = _make_clone(tmp_path)
    fetcher = CommittedLayerFetcher("owner/name", clone_path=str(root))

    assert fetcher("main") == "summary_cluster_depth: 3\n"
    assert fetcher.source == "git-blob"
    assert fetcher.clone_root == str(root)
```

Заменить в том же файле два существующих ассерта `fetcher.source == "local"`
(`tests/config/test_committed_layer.py:75` и в `test_missing_file_locally_is_absence_not_fallback`)
на `"git-blob"`.

Добавить в `tests/entrypoints/test_config_commands.py`:

```python
def test_config_show_warns_about_worktree_drift(monkeypatch, tmp_path):
    """Правка .review.yml в рабочем дереве названа явно; политика — из блоба."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home"))
    clone = _make_local_clone(
        tmp_path, remote="https://github.com/o/r.git", content="max_comments: 11\n"
    )
    (clone / ".review.yml").write_text("max_comments: 22\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "show", "--repo", "o/r", "--branch", "main",
         "--path", str(clone), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["effective"]["max_comments"] == 11          # источник прежний — блоб
    assert payload["committed_source"] == "git-blob"
    assert payload["worktree_drift"]["status"] == "drifted"
    assert payload["worktree_drift"]["keys"] == ["max_comments"]


def test_config_show_text_prints_drift_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home"))
    clone = _make_local_clone(
        tmp_path, remote="https://github.com/o/r.git", content="max_comments: 11\n"
    )
    (clone / ".review.yml").write_text("max_comments: 22\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "show", "--repo", "o/r", "--branch", "main", "--path", str(clone)],
    )

    assert result.exit_code == 0, result.output
    assert "committed: git-blob" in result.output
    assert "worktree_drift: drifted" in result.output
    assert "max_comments" in result.output
```

Обновить `payload["committed_source"] == "local"` в
`test_config_show_reads_committed_layer_from_local_clone`
(`tests/entrypoints/test_config_commands.py:352`) на `"git-blob"`.

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/config/test_committed_layer.py tests/entrypoints/test_config_commands.py`
Expected: FAIL — `ImportError: cannot import name 'DriftReport'`, метка всё ещё `local`.

- [ ] **Step 3: Реализовать дрейф и переименовать метку**

В `reviewer/config/committed.py`:

```python
SOURCE_LOCAL = "git-blob"
```

и обновить комментарий над константами: метка называет прочитанный объект (git-блоб на ref),
а не файл в рабочем дереве — разница с рабочим деревом должна быть видна из самой метки.

Добавить свойство рядом с `clone_available`:

```python
    @property
    def clone_root(self) -> str | None:
        """Корень пригодного клона или None. Нужен `config show` для дрейфа."""
        return self._clone_root
```

Добавить в конец модуля:

```python
@dataclass(frozen=True)
class DriftReport:
    """Расхождение рабочего дерева клона с коммиченным `.review.yml` (PRI-260).

    Диагностика, а не источник данных: эффективная политика по-прежнему целиком
    из коммиченного слоя — иначе PR ослаблял бы собственное ревью правкой
    своего же конфига.
    """

    status: str
    keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "keys": list(self.keys)}


def worktree_drift(root: str, ref: str, policy_file: str = POLICY_FILE) -> DriftReport:
    """Сравнить рабочее дерево клона с коммиченным блобом на ``ref``.

    Сравнение проводится только когда ``ref`` резолвится в тот же коммит, что
    HEAD клона: при `config show --branch dev` из ветки `feat/...` разница
    была бы про другую ветку, а не про несохранённую правку. Возвращаются
    только ключи — значения политики в диагностику не попадают.
    """
    try:
        head = rev_parse(root, "HEAD")
        target = rev_parse(root, ref)
        if not head or not target:
            return DriftReport("unknown")
        if head != target:
            return DriftReport("ref_not_head")
        blob = file_at_ref(root, policy_file, ref)
        path = os.path.join(root, policy_file)
        working = None
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                working = handle.read()
        if working is None:
            return DriftReport("absent_in_worktree" if blob is not None else "clean")
        if blob is None:
            return DriftReport("absent_in_blob")
        if working == blob:
            return DriftReport("clean")
        keys = _diverging_keys(blob, working)
    except Exception:  # noqa: BLE001 — диагностика не должна ронять config show
        log.debug("дрейф рабочего дерева не вычислен для %s", root, exc_info=True)
        return DriftReport("unknown")
    return DriftReport("drifted", keys)


def _diverging_keys(blob: str, working: str) -> tuple[str, ...]:
    """Листовые пути, значения которых различаются между блобом и деревом."""
    left = yaml.safe_load(blob) or {}
    right = yaml.safe_load(working) or {}
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise ValueError("policy layer is not a mapping")
    paths = sorted(set(leaf_paths(left)) | set(leaf_paths(right)))
    return tuple(path for path in paths if _at(left, path) != _at(right, path))


_MISSING = object()


def _at(data: object, path: str) -> object:
    for key in path.split("."):
        if not isinstance(data, Mapping) or key not in data:
            return _MISSING
        data = data[key]
    return data
```

и импорты в шапке модуля:

```python
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import yaml

from reviewer.config.deepmerge import leaf_paths
```

В `reviewer/entrypoints/cli.py::config_show` после успешного `build_config_report`
(строка 289) добавить:

```python
                clone_root = fetch_committed.clone_root
                if clone_root is not None:
                    payload["worktree_drift"] = worktree_drift(
                        clone_root, ref
                    ).as_dict()
```

и импортировать `worktree_drift` рядом с `CommittedLayerFetcher`.

В `_render_config_report` после строки `committed: ...` (строка 167) добавить:

```python
    drift = report.get("worktree_drift")
    if isinstance(drift, Mapping) and drift["status"] != "clean":
        # «Правка есть, но её не видно» — исходный дефект PRI-260, поэтому
        # молчания здесь быть не должно ни в одном статусе, кроме чистого.
        click.echo(f"worktree_drift: {drift['status']}")
        for key in drift["keys"]:
            click.echo(f"  {key}")
```

- [ ] **Step 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest -q tests/config/ tests/entrypoints/test_config_commands.py`
Expected: PASS

- [ ] **Step 5: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/config/committed.py reviewer/entrypoints/cli.py tests/config/test_committed_layer.py tests/entrypoints/test_config_commands.py`
Expected: PASS, ruff чист по перечисленным файлам.

- [ ] **Step 6: Обновить CLAUDE.md**

В разделе «Неочевидные факты» дополнить абзац «Сбой чтения коммиченного `.review.yml` не обнуляет
домашние слои»: слияние слоёв рекурсивно по mapping-значениям (лист затеняется листом, `task_board` —
единственный атомарный ключ, списки и скаляры заменяются целиком); `sources`/`shadowed` в
`config show` называют путь до листа; метка способа чтения — `git-blob` (коммиченный объект git),
а расхождение рабочего дерева с ним печатается отдельной строкой `worktree_drift` и на эффективную
политику не влияет — сравнение проводится только при `ref == HEAD` клона.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/config/committed.py reviewer/entrypoints/cli.py tests/config/test_committed_layer.py tests/entrypoints/test_config_commands.py CLAUDE.md
git commit -m "feat(config): предупреждение о дрейфе рабочего дерева и метка git-blob"
```

---

## Приёмка

Все шесть критериев задачи закрыты так:

1. Частичный домашний `context_limits` не стирает коммиченный `code_section` — Task 2, `test_pri259_committed_code_section_pin_survives_partial_home_layer`.
2. `shadowed` называет лист в тексте и JSON — Task 2 (`test_shadowed_names_the_leaf_when_subsection_is_overridden`) и Task 3 (`test_config_show_text_shadowing_names_the_leaf`).
3. Предупреждение о дрейфе с перечислением ключей, эффективная политика остаётся коммиченной — Task 4, `test_config_show_warns_about_worktree_drift`.
4. Приоритет источников не изменён — Task 2, `test_layer_order_is_unchanged`.
5. Регресс на конфигурацию PRI-259 — Task 2, тот же `test_pri259_...`: при возврате к слиянию по верхнему ключу он краснеет.
6. Списки и скаляры заменяются целиком — Task 1, `test_lists_and_scalars_are_replaced_whole`.

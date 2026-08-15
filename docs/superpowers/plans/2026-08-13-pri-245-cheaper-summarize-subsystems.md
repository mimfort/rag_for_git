# PRI-245 — Удешевление summarize-subsystems: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Снизить стоимость полного прохода `/rag-reviewer:summarize-subsystems` с >1M токенов, согласовав вход файлового job'а с тем, что инвалидирует его результат, исключив тестовые деревья из кластеризации сводок и сбатчив файловые job'ы.

**Architecture:** Файловый job перестаёт читать исходники и получает AST-скелет, собранный сервером из уже проиндексированных чанков — ровно того материала, из которого считается `skeleton_hash`. Новый session-less MCP-тул `get_file_skeletons` принимает список путей, поэтому один job обслуживает порцию файлов. Новый слой политики `summary_paths.ignore` фильтрует members до кластеризации, не трогая ревью-индекс, и входит в `layout_token`, поэтому его включение запускает штатный полный пересбор с pruning'ом.

**Tech Stack:** Python 3.11+, tree-sitter (`reviewer/index/chunker.py`), FastMCP (`reviewer/entrypoints/mcp_server.py`), psycopg/ParadeDB, pytest.

**Spec:** `docs/superpowers/specs/2026-08-13-pri-245-cheaper-summarize-subsystems-design.md`

## Global Constraints

- Ветка работы: `feat/pri-245-cheaper-summarize-subsystems` (уже создана от `dev`, спека закоммичена). Не создавать новых веток, не пушить, не открывать PR.
- Коммиты — Conventional Commits **на русском** (`feat(policy): …`, `fix(mcp): …`). **Без self-attribution**: никаких `Co-Authored-By`, никаких упоминаний Claude в теле коммита.
- Язык проекта — русский: комментарии, докстринги, сообщения. Тела скиллов (`plugin/skills/**/*.md`) — по-английски (это осознанная экономия токенов), но инструктируют отвечать пользователю по-русски.
- Unit-тестам запрещены Postgres, Neo4j, localhost-сокеты и внешняя сеть. Всё, что ходит в сеть, обязано иметь `@pytest.mark.integration`. Прогон: `.venv/bin/pytest -q` (по умолчанию исключает integration).
- Любая правка чего угодно под `plugin/` меняет codex payload-digest → обязателен прогон `.venv/bin/python scripts/update_codex_plugin_manifest.py`, иначе install-тесты краснеют.
- `ruff` **не чист** на `dev` — не гнаться за repo-wide clean; следить только за файлами, которые трогаешь.
- Существующие guard-тесты в `tests/skills/` привязаны к дословным фразам `SKILL.md`. Менять текст скилла и тест **в одном коммите**; якорную фразу среза `Let pending work be exactly` (`_STEP_52_START`) сохранять дословно.

---

### Task 1: Слой политики `summary_paths.ignore` и его identity в `layout_token`

Чистое ядро: новый ключ политики, его нормализация и включение в canonical `layout_token`. Ничего не применяет — применение в Task 2.

**Files:**
- Modify: `reviewer/graph/summaries.py:80-100` (`canonicalize_layout`, `compute_layout_token`)
- Modify: `reviewer/policy/policy.py:16-33,42-68,90-129`
- Modify: `reviewer/config/layers.py:255-264` (валидация домашнего слоя), `:441-457` (`policy_to_public_data`), `:481-522` (`_validate_public_policy_data`)
- Modify: `.review.yml`
- Test: `tests/graph/test_summaries_layout.py` (создать)
- Test: `tests/policy/test_policy_summary_paths.py` (создать)
- Test: `tests/test_review_yml_example.py:7-15`

**Interfaces:**
- Produces: `reviewer.graph.summaries.normalize_summary_paths_ignore(patterns: Sequence[str] | None) -> list[str]`
- Produces: `canonicalize_layout(default_depth: int, overrides: dict[str, int], summary_paths_ignore: Sequence[str] | None = None) -> tuple[dict[str, int], list[str], str]` — **тройка**, третий элемент token (было: пара)
- Produces: `compute_layout_token(default_depth: int, overrides: dict[str, int], summary_paths_ignore: Sequence[str] | None = None) -> str`
- Produces: `reviewer.policy.policy.DEFAULT_SUMMARY_PATHS_IGNORE: tuple[str, ...]`
- Produces: `ReviewPolicy.summary_paths_ignore: list[str]`
- Produces: публичный ключ политики `"summary_paths": {"ignore": [...]}`

- [ ] **Step 1: Написать падающие тесты layout-identity**

Создать `tests/graph/test_summaries_layout.py`:

```python
from reviewer.graph.summaries import (
    canonicalize_layout,
    compute_layout_token,
    normalize_summary_paths_ignore,
)


def test_normalize_strips_slashes_dedupes_and_sorts():
    assert normalize_summary_paths_ignore(["/tests/", "test", "tests", "", None]) == [
        "test", "tests",
    ]


def test_normalize_none_is_empty():
    assert normalize_summary_paths_ignore(None) == []


def test_canonicalize_returns_normalized_ignore_and_token():
    overrides, ignore, token = canonicalize_layout(2, {"reviewer/index": 3}, ["/tests/"])
    assert overrides == {"reviewer/index": 3}
    assert ignore == ["tests"]
    assert len(token) == 64


def test_ignore_order_and_slashes_do_not_change_token():
    assert compute_layout_token(2, {}, ["tests", "docs"]) == compute_layout_token(
        2, {}, ["/docs/", "tests"]
    )


def test_different_ignore_changes_token():
    assert compute_layout_token(2, {}, ["tests"]) != compute_layout_token(2, {}, [])


def test_empty_ignore_differs_from_default_ignore_token():
    """Выключение фильтра — тоже смена layout: token обязан отличаться."""
    assert compute_layout_token(2, {}, []) != compute_layout_token(2, {}, ["tests", "test"])


def test_ignore_is_independent_of_depth_component():
    assert compute_layout_token(2, {}, ["tests"]) != compute_layout_token(3, {}, ["tests"])
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -q tests/graph/test_summaries_layout.py`
Expected: FAIL — `ImportError: cannot import name 'normalize_summary_paths_ignore'`

- [ ] **Step 3: Реализовать в `reviewer/graph/summaries.py`**

Добавить `Sequence` в импорт typing (`from typing import Callable, Sequence`) и рядом с `normalize_depth_overrides`:

```python
def normalize_summary_paths_ignore(patterns: Sequence[str] | None) -> list[str]:
    """Канонизировать ignore-паттерны кластеризации сводок.

    strip пробелов и '/', отбрасывание пустых, дедуп, сортировка — чтобы
    порядок и написание в .review.yml не меняли layout_token.
    """
    cleaned = {str(p).strip().strip("/") for p in (patterns or []) if p is not None}
    return sorted(p for p in cleaned if p)
```

Заменить `canonicalize_layout` и `compute_layout_token`:

```python
def canonicalize_layout(
    default_depth: int,
    overrides: dict[str, int],
    summary_paths_ignore: Sequence[str] | None = None,
) -> tuple[dict[str, int], list[str], str]:
    """Вернуть единые normalized overrides, normalized ignore и token.

    ``summary_paths_ignore`` входит в payload token'а намеренно (PRI-245):
    смена состава кластеров обязана инвалидировать layout, иначе штатный
    prune не соберёт осиротевшие сводки.
    """
    normalized = normalize_depth_overrides(overrides)
    ignore = normalize_summary_paths_ignore(summary_paths_ignore)
    payload = json.dumps(
        {
            "default_depth": int(default_depth),
            "overrides": list(normalized.items()),
            "summary_paths_ignore": ignore,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    token = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return normalized, ignore, token


def compute_layout_token(
    default_depth: int,
    overrides: dict[str, int],
    summary_paths_ignore: Sequence[str] | None = None,
) -> str:
    """Вернуть canonical identity effective layout policy."""
    return canonicalize_layout(default_depth, overrides, summary_paths_ignore)[2]
```

- [ ] **Step 4: Прогнать — тесты layout зелёные**

Run: `.venv/bin/pytest -q tests/graph/test_summaries_layout.py`
Expected: PASS

- [ ] **Step 5: Написать падающие тесты политики**

Создать `tests/policy/test_policy_summary_paths.py`:

```python
from reviewer.policy.policy import DEFAULT_SUMMARY_PATHS_IGNORE, ReviewPolicy


class _Settings:
    """Минимальные настройки: from_settings читает только эти поля."""
    review_severity_threshold = "low"
    review_max_comments = 25
    review_min_confidence = 0.5
    review_output_language = "ru"
    review_grounding_max_distance = 5
    summary_cluster_depth = 2
    summary_topk_threshold = 20
    review_bug_reports = True

    def review_categories_list(self):
        return []

    def task_board_default(self):
        return None


def test_default_ignores_test_trees():
    assert ReviewPolicy.from_settings(_Settings()).summary_paths_ignore == list(
        DEFAULT_SUMMARY_PATHS_IGNORE
    )
    assert "tests" in DEFAULT_SUMMARY_PATHS_IGNORE


def test_missing_key_keeps_default():
    policy = ReviewPolicy.load_data(_Settings(), {"summary_cluster_depth": 3})
    assert policy.summary_paths_ignore == list(DEFAULT_SUMMARY_PATHS_IGNORE)


def test_explicit_value_replaces_default():
    policy = ReviewPolicy.load_data(
        _Settings(), {"summary_paths": {"ignore": ["tests", "eval"]}}
    )
    assert policy.summary_paths_ignore == ["tests", "eval"]


def test_explicit_empty_list_disables_filter():
    """Явный [] выключает фильтр, а не откатывается на дефолт."""
    policy = ReviewPolicy.load_data(_Settings(), {"summary_paths": {"ignore": []}})
    assert policy.summary_paths_ignore == []


def test_from_yaml_reads_summary_paths():
    policy = ReviewPolicy.from_yaml("summary_paths:\n  ignore:\n    - tests\n")
    assert policy.summary_paths_ignore == ["tests"]


def test_from_yaml_without_key_keeps_default():
    policy = ReviewPolicy.from_yaml("summary_cluster_depth: 2\n")
    assert policy.summary_paths_ignore == list(DEFAULT_SUMMARY_PATHS_IGNORE)


def test_summary_paths_ignore_does_not_touch_review_ignore():
    """Фильтр сводок и ignore ревью-индекса — независимые слои."""
    policy = ReviewPolicy.load_data(
        _Settings(), {"summary_paths": {"ignore": ["tests"]}}
    )
    assert policy.ignore == []
```

- [ ] **Step 6: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -q tests/policy/test_policy_summary_paths.py`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_SUMMARY_PATHS_IGNORE'`

- [ ] **Step 7: Реализовать поле политики**

В `reviewer/policy/policy.py` после `_SEV`:

```python
# Дефолтный фильтр кластеризации сводок (PRI-245): тестовые деревья бесполезны
# как высокоуровневый приор, но дают около двух третей объёма работы. Голые
# имена — is_ignored ловит и сам каталог, и всё поддерево, но не reviewer/testing.py.
DEFAULT_SUMMARY_PATHS_IGNORE: tuple[str, ...] = ("tests", "test")
```

В `@dataclass class ReviewPolicy` рядом с `summary_cluster_depth_overrides`:

```python
    summary_paths_ignore: list[str] = field(
        default_factory=lambda: list(DEFAULT_SUMMARY_PATHS_IGNORE)
    )   # фильтр кластеризации сводок; НЕ влияет на индекс ревью (PRI-245)
```

Добавить статический хелпер в класс:

```python
    @staticmethod
    def _summary_paths_ignore(data: Mapping[str, object]) -> list[str] | None:
        """Явный список из слоя или None, если ключ не задан (→ дефолт).

        Присутствующий ключ заменяет дефолт целиком, поэтому `ignore: []`
        выключает фильтр, а не откатывается на дефолт.
        """
        raw = data.get("summary_paths")
        if not isinstance(raw, Mapping) or "ignore" not in raw:
            return None
        return [str(item) for item in (raw["ignore"] or [])]
```

В `from_yaml` — перед `return cls(...)` вычислить один раз:

```python
        summary_paths_ignore = cls._summary_paths_ignore(data)
```

и в конструктор `cls(...)` добавить:

```python
            summary_paths_ignore=(
                list(DEFAULT_SUMMARY_PATHS_IGNORE)
                if summary_paths_ignore is None
                else summary_paths_ignore
            ),
```

В `load_data` после блока `summary_cluster_depth_overrides`:

```python
        summary_paths_ignore = cls._summary_paths_ignore(data)
        if summary_paths_ignore is not None:
            policy.summary_paths_ignore = summary_paths_ignore
```

`from_settings` не трогать: `default_factory` уже даёт дефолт-константу (env-слоя у ключа нет — единообразно с `context_limits`).

- [ ] **Step 8: Прогнать — тесты политики зелёные**

Run: `.venv/bin/pytest -q tests/policy/`
Expected: PASS

- [ ] **Step 9: Написать падающие тесты публичной формы и примера конфига**

Дописать в `tests/policy/test_policy_summary_paths.py`:

```python
def test_public_data_exposes_summary_paths():
    from reviewer.config.layers import policy_to_public_data

    policy = ReviewPolicy.load_data(
        _Settings(), {"summary_paths": {"ignore": ["tests"]}}
    )
    public = policy_to_public_data(policy)
    assert public["summary_paths"] == {"ignore": ["tests"]}
    assert public["paths"] == {"ignore": []}
```

Дописать в `tests/test_review_yml_example.py::test_example_review_yml_documents_new_keys`:

```python
    assert "ignore" in (data.get("summary_paths") or {}), (
        "summary_paths.ignore должен быть в примере"
    )
```

- [ ] **Step 10: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -q tests/policy/test_policy_summary_paths.py::test_public_data_exposes_summary_paths tests/test_review_yml_example.py`
Expected: FAIL — `KeyError: 'summary_paths'` и `AssertionError: summary_paths.ignore должен быть в примере`

- [ ] **Step 11: Провести ключ через `layers.py`**

В `policy_to_public_data` (после `"paths"`):

```python
        "summary_paths": {"ignore": list(policy.summary_paths_ignore)},
```

В `_validate_public_policy_data` добавить `"summary_paths"` в множество `expected` и рядом с валидацией `paths`:

```python
    _validate_mapping_shape(effective["summary_paths"], {"ignore": lambda value: (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )})
```

В `_validate_known_policy_data` после блока `paths`:

```python
        summary_paths = data.get("summary_paths", _MISSING)
        if summary_paths is not _MISSING:
            if summary_paths is not None and not isinstance(summary_paths, Mapping):
                raise TypeError
            if isinstance(summary_paths, Mapping) and "ignore" in summary_paths:
                ignore = summary_paths["ignore"]
                if ignore is not None and (
                    not isinstance(ignore, list)
                    or not all(isinstance(item, str) for item in ignore)
                ):
                    raise TypeError
```

`reviewer/entrypoints/cli.py` править **не нужно**: `_render_config_report` (`:169`) печатает generic'ом по всем ключам `effective`, поэтому новый ключ и его `source:` появляются в `reviewer config show` сами.

- [ ] **Step 12: Добавить секцию в `.review.yml`**

После секции `paths:` (перед `summary_cluster_depth`):

```yaml
# Фильтр КЛАСТЕРИЗАЦИИ СВОДОК (PRI-245) — независим от paths.ignore выше.
# paths.ignore управляет индексацией и ревью; этот ключ — только сводками подсистем.
# Тесты обязаны остаться в ревью-индексе (находиться через search_codebase,
# комментироваться в PR) и обязаны исчезнуть из сводок: «подсистема tests/skills»
# бесполезна как высокоуровневый приор, но даёт около двух третей объёма работы.
# Дефолт (без ключа) — ["tests", "test"]. Явный пустой список ВЫКЛЮЧАЕТ фильтр.
# Входит в layout_token: любая правка запускает полный пересбор сводок.
summary_paths:
  ignore:
    - tests
```

- [ ] **Step 13: Прогнать полный набор**

Run: `.venv/bin/pytest -q`
Expected: PASS (все тесты; если падает что-то в `tests/config/` из-за новой публичной формы — починить, добавив ключ в ожидаемые множества там же)

- [ ] **Step 14: Коммит**

```bash
git add reviewer/graph/summaries.py reviewer/policy/policy.py reviewer/config/layers.py .review.yml tests/graph/test_summaries_layout.py tests/policy/test_policy_summary_paths.py tests/test_review_yml_example.py
git commit -m "feat(policy): слой summary_paths.ignore и его identity в layout_token

Отдельный от paths.ignore фильтр кластеризации сводок: индекс ревью не
затрагивается, тесты по-прежнему ищутся и комментируются. Фильтр входит в
payload layout_token, поэтому его смена инвалидирует layout и осиротевшие
сводки собирает штатный prune."
```

---

### Task 2: Применение фильтра при сборке members

Фильтр применяется **до** `build_clusters`, в обеих точках построения кластеров. Вторая точка обязательна: она считает `stale` для `get_subsystem_summaries`, и расхождение наборов сделало бы каждую сводку вечно stale.

**Files:**
- Modify: `reviewer/mcp/service.py:1646-1658` (`_resolve_summary_depth` → `_resolve_summary_layout`), `:1802-1900` (`_summary_state`), `:2379-2412` (`_current_subsystem_hashes`)
- Test: `tests/mcp/test_summary_paths_filter.py` (создать)

**Interfaces:**
- Consumes: `ReviewPolicy.summary_paths_ignore`, `canonicalize_layout(...) -> (overrides, ignore, token)`, `compute_layout_token(depth, overrides, ignore)` (Task 1)
- Produces: `MCPReviewService._resolve_summary_layout(repo, branch) -> tuple[int, dict[str, int], list[str], str]` — `(depth, overrides, summary_paths_ignore, depth_source)`
- Produces: инвариант — members, отфильтрованные `is_ignored(path, summary_paths_ignore)`, не образуют кластеров ни в `_summary_state`, ни в `_current_subsystem_hashes`

- [ ] **Step 1: Написать падающий тест фильтрации**

Создать `tests/mcp/test_summary_paths_filter.py`:

```python
"""PRI-245: фильтр кластеризации сводок применяется до build_clusters."""
from reviewer.graph.summaries import Member, build_clusters
from reviewer.index.pathfilter import is_ignored


def _members():
    return [
        Member("reviewer/index/store.py#A", "reviewer/index/store.py", "h1", "s1", 1),
        Member("tests/mcp/test_x.py#B", "tests/mcp/test_x.py", "h2", "s2", 1),
        Member("tests/skills/test_y.py#C", "tests/skills/test_y.py", "h3", "s3", 1),
    ]


def filter_members(members, ignore):
    """Ровно та фильтрация, которую применяет сервис (ссылочная реализация теста)."""
    return [m for m in members if not is_ignored(m.path, ignore)]


def test_test_trees_form_no_clusters_under_default_filter():
    kept = filter_members(_members(), ["tests", "test"])
    keys = {c.key for c in build_clusters(kept, None, depth=2)}
    assert keys == {"reviewer/index"}


def test_empty_filter_keeps_test_clusters():
    kept = filter_members(_members(), [])
    keys = {c.key for c in build_clusters(kept, None, depth=2)}
    assert "tests/mcp" in keys and "tests/skills" in keys


def test_filter_does_not_match_similarly_named_production_paths():
    members = [
        Member("reviewer/testing.py#A", "reviewer/testing.py", "h", "s", 1),
        Member("reviewer/test_utils.py#B", "reviewer/test_utils.py", "h", "s", 1),
    ]
    assert filter_members(members, ["tests", "test"]) == members
```

Затем — тест самого сервиса. `MCPReviewService(settings, components)` собирается с `MagicMock`-компонентами по образцу `tests/mcp/test_server.py:24-45`; дописать в тот же файл:

```python
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _service(raw_members):
    settings = Settings()
    settings.voyage_api_key = "test"
    settings.github_token = "test"
    components = MagicMock()
    components.store.list_base_members.return_value = raw_members
    components.summary_store.get_fragments.return_value = []
    components.summary_store.get_completed_depth.return_value = None
    components.summary_store.get_completed_layout.return_value = None
    components.graph = None
    return MCPReviewService(settings, components)


def test_service_filters_members_in_both_cluster_paths(monkeypatch):
    """_summary_state и _current_subsystem_hashes видят ОДИН набор кластеров.

    Расхождение сделало бы каждую сводку вечно stale: source_hash из
    _summary_state не совпал бы с эталоном из _current_subsystem_hashes.
    """
    raw = [
        ("reviewer/index/store.py", "A", "h1", 1, "s1"),
        ("tests/mcp/test_x.py", "B", "h2", 1, "s2"),
    ]
    service = _service(raw)
    monkeypatch.setattr(
        service, "_resolve_summary_layout",
        lambda repo, branch: (2, {}, ["tests"], ".review.yml"),
    )
    state = service._summary_state("owner/name", "dev")
    hashes = service._current_subsystem_hashes("owner/name", "dev")
    assert {c.key for c in state.clusters} == {"reviewer/index"}
    assert set(hashes) == {"reviewer/index"}
    assert hashes["reviewer/index"] == next(
        c.source_hash for c in state.clusters if c.key == "reviewer/index"
    )
```

Порядок кортежа `list_base_members` — `(path, symbol_fqn, content_hash, start_line, skeleton_hash)` (`reviewer/index/store.py:299`); он отличается от порядка полей `Member`, поэтому копировать вслепую нельзя.

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -q tests/mcp/test_summary_paths_filter.py`
Expected: FAIL — `AttributeError: 'MCPReviewService' object has no attribute '_resolve_summary_layout'`

- [ ] **Step 3: Заменить `_resolve_summary_depth` на `_resolve_summary_layout`**

В `reviewer/mcp/service.py` заменить `_resolve_summary_depth` (`:1646`):

```python
    def _resolve_summary_layout(
        self, repo: str, branch: str
    ) -> tuple[int, dict[str, int], list[str], str]:
        """Резолв layout-политики сводок: глубина, overrides, ignore-фильтр, источник.

        Fail-soft: при сбое резолва политики — env-глубина и ДЕФОЛТНЫЙ фильтр,
        а не пустой. Пустой фильтр при сбое молча вернул бы тестовые кластеры
        и сделал бы стоимость прохода недетерминированной.
        """
        from reviewer.policy.policy import DEFAULT_SUMMARY_PATHS_IGNORE

        default = self.settings.summary_cluster_depth
        try:
            policy, meta = self._resolve_policy(repo, branch)
            source = meta.sources.get(
                "summary_cluster_depth",
                meta.sources.get("summary_cluster_depth_overrides", "env"),
            )
            return (
                policy.summary_cluster_depth,
                policy.summary_cluster_depth_overrides,
                list(policy.summary_paths_ignore),
                source,
            )
        except Exception:
            log.warning("_resolve_summary_layout: fail-soft → env-дефолт")
            return default, {}, list(DEFAULT_SUMMARY_PATHS_IGNORE), "env"
```

- [ ] **Step 4: Применить фильтр в `_summary_state`**

В `_summary_state` (`:1802`) добавить импорт `is_ignored` и переписать резолв/сборку members.

Резолв политики выполняется **всегда**, даже при явном `depth`-аргументе: фильтр не связан с глубиной, и `depth`-override не должен молча возвращать тестовые кластеры. Заменить блок `:1838-1857`:

```python
        resolved_depth, policy_overrides, ignore, policy_source = (
            self._resolve_summary_layout(repo, branch)
        )
        if depth is None:
            overrides, depth_source = policy_overrides, policy_source
        else:
            resolved_depth, overrides, depth_source = depth, {}, "arg"
        overrides, ignore, layout_token = canonicalize_layout(
            resolved_depth,
            overrides,
            ignore,
        )
        members = [
            Member(
                node_id=f"{path}#{symbol}",
                path=path,
                content_hash=content_hash,
                start_line=start_line,
                skeleton_hash=skeleton_hash,
            )
            for path, symbol, content_hash, start_line, skeleton_hash in raw
            if not is_ignored(path, ignore)
        ]
```

Ранний возврат при пустом индексе (`:1821-1837`) тоже должен нести согласованный token — заменить его тело:

```python
        if not raw:
            resolved_depth, _, ignore, _ = self._resolve_summary_layout(repo, branch)
            if depth is not None:
                resolved_depth = depth
            return _SummaryState(
                depth=resolved_depth,
                layout_token=compute_layout_token(resolved_depth, {}, ignore),
                depth_source="env" if depth is None else "arg",
                members=[],
                clusters=[],
                file_fingerprints={},
                fragments=[],
                completed_depth=None,
                completed_layout=None,
            )
```

Импорт `is_ignored` — рядом с локальным импортом `reviewer.graph.summaries`:

```python
        from reviewer.index.pathfilter import is_ignored
```

`build_clusters` **не менять**: фильтрация остаётся слоем выше, чистая функция остаётся чистой.

- [ ] **Step 5: Применить фильтр в `_current_subsystem_hashes`**

В `_current_subsystem_hashes` (`:2379`) заменить сборку members и резолв:

```python
        from reviewer.graph.summaries import Member, build_clusters
        from reviewer.index.pathfilter import is_ignored

        try:
            raw = self.components.store.list_base_members(repo, branch)
            if not raw:
                return None
            depth, overrides, ignore, _ = self._resolve_summary_layout(repo, branch)
            members = [
                Member(
                    node_id=f"{path}#{symbol}",
                    path=path,
                    content_hash=content_hash,
                    start_line=start_line,
                    skeleton_hash=skeleton_hash,
                )
                for path, symbol, content_hash, start_line, skeleton_hash in raw
                if not is_ignored(path, ignore)
            ]
            clusters = build_clusters(
                members,
                None,
                depth=depth,
                min_size=1,
                depth_overrides=overrides,
            )
            return {cluster.key: cluster.source_hash for cluster in clusters}
```

- [ ] **Step 6: Найти и починить остальных вызывающих**

Run: `grep -rn "_resolve_summary_depth\|canonicalize_layout\|compute_layout_token" reviewer/ tests/`
Ожидание: не осталось вызовов `_resolve_summary_depth`; все вызовы `canonicalize_layout` распакованы в тройку. Починить каждое найденное место.

- [ ] **Step 7: Прогнать полный набор**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 8: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_summary_paths_filter.py
git commit -m "feat(mcp): применить summary_paths.ignore до кластеризации сводок

Фильтр применяется в обеих точках построения кластеров — _summary_state и
_current_subsystem_hashes. Вторая обязательна: она считает stale, и
расхождение наборов сделало бы каждую сводку вечно устаревшей."
```

---

### Task 3: Session-less тул `get_file_skeletons`

Источник скелета — проиндексированные чанки, а не файл на диске: `skeleton_hash` считается по тексту символа-чанка, поэтому только чтение из чанков даёт «job читает ровно то, что инвалидирует его результат».

**Files:**
- Modify: `reviewer/index/chunker.py` (после `symbol_skeleton_hash`)
- Modify: `reviewer/index/store.py:299-311` (рядом с `list_base_members`)
- Modify: `reviewer/mcp/service.py` (рядом с session-less тулами — `definition`, `implementations`)
- Modify: `reviewer/entrypoints/mcp_server.py:365-378` (session-less блок), `:19` (докстринг «38 тулов»)
- Test: `tests/index/test_file_skeleton.py` (создать)
- Test: `tests/mcp/test_get_file_skeletons.py` (создать)

**Interfaces:**
- Produces: `reviewer.index.chunker.file_skeleton_lines(chunks: list[tuple[int, str]]) -> list[tuple[int, str]]`
- Produces: `IndexStore.fetch_chunks_at_paths(repo: str, branch: str, paths: list[str]) -> list[tuple[str, int, str]]` — `(path, start_line, text)`
- Produces: `MCPReviewService.get_file_skeletons(repo: str, paths: list[str], branch: str | None = None) -> dict[str, str]`
- Produces: MCP-тул `get_file_skeletons(repo, paths, branch)` — плоский `{path: skeleton_text}`; отброшенный по капу путь получает **строку-ноту вместо скелета**, поэтому усечение видно в ответе (no silent caps)

- [ ] **Step 1: Написать падающий тест чистой сборки скелета**

Создать `tests/index/test_file_skeleton.py`:

```python
"""PRI-245: скелет файла как объединение скелетов его символов-чанков."""
from reviewer.index.chunker import chunk_python, file_skeleton_lines

SRC = '''"""Модульный докстринг."""
import os


class A:
    """Класс A."""

    def m(self, x):
        """Метод m."""
        y = x + 1
        return y


def f(a, b):
    """Функция f."""
    return a + b
'''


def _chunks(src: str) -> list[tuple[int, str]]:
    return [(c.start_line, c.text) for c in chunk_python("x.py", src.encode("utf-8"))]


def test_skeleton_carries_signatures_and_first_docstring_lines():
    rendered = {n: text for n, text in file_skeleton_lines(_chunks(SRC))}
    assert "class A:" in rendered[5]
    assert '"""Класс A."""' in rendered[6]
    assert "def m(self, x):" in rendered[8]
    assert "def f(a, b):" in rendered[15]


def test_skeleton_omits_bodies():
    rendered = "\\n".join(text for _, text in file_skeleton_lines(_chunks(SRC)))
    assert "y = x + 1" not in rendered
    assert "return a + b" not in rendered


def test_nested_symbols_are_deduplicated():
    """Скелет класса уже содержит сигнатуры методов — строки не задваиваются."""
    numbers = [n for n, _ in file_skeleton_lines(_chunks(SRC))]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers))


def test_line_numbers_are_absolute():
    """Номера строк — абсолютные в файле, а не относительные внутри чанка."""
    for number, text in file_skeleton_lines(_chunks(SRC)):
        assert SRC.splitlines()[number - 1] == text


def test_empty_input_is_empty():
    assert file_skeleton_lines([]) == []
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -q tests/index/test_file_skeleton.py`
Expected: FAIL — `ImportError: cannot import name 'file_skeleton_lines'`

- [ ] **Step 3: Реализовать чистую сборку**

В `reviewer/index/chunker.py` после `symbol_skeleton_hash`:

```python
def file_skeleton_lines(chunks: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Скелет файла как объединение скелетов его символов-чанков (PRI-245).

    ``chunks`` — пары (start_line, text) чанков ОДНОГО файла. Возвращает
    отсортированные (абсолютный номер строки, текст строки).

    Источник намеренно тот же, из которого считается ``symbol_skeleton_hash``:
    так вход файлового job'а совпадает с тем, что инвалидирует его результат.
    Цена — module-level docstring и код вне символов в чанки не попадают.
    Вложенные символы схлопываются множеством: скелет класса уже содержит
    сигнатуры своих методов.
    """
    out: dict[int, str] = {}
    for start_line, text in chunks:
        lines = text.splitlines()
        for rel in python_skeleton(text.encode("utf-8")):
            if 1 <= rel <= len(lines):
                out[start_line + rel - 1] = lines[rel - 1]
    return sorted(out.items())
```

- [ ] **Step 4: Прогнать — зелёные**

Run: `.venv/bin/pytest -q tests/index/test_file_skeleton.py`
Expected: PASS

- [ ] **Step 5: Добавить store-метод**

В `reviewer/index/store.py` после `list_base_members`:

```python
    def fetch_chunks_at_paths(self, repo: str, branch: str, paths: list[str]
                              ) -> list[tuple[str, int, str]]:
        """(path, start_line, text) чанков base-индекса ветки для заданных путей.

        Дешевле list_base_members: не тянет весь индекс и не считает
        skeleton_hash — нужен только текст запрошенных файлов (PRI-245).
        """
        from reviewer.index.refs import base_ref
        if not paths:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path, start_line, text FROM chunks "
                "WHERE repo=%s AND ref=%s AND path = ANY(%s) "
                "ORDER BY path, start_line",
                (repo, base_ref(branch), list(paths))).fetchall()
        return [(p, sl, t) for p, sl, t in rows]
```

- [ ] **Step 6: Написать падающий тест сервисного контракта**

Создать `tests/mcp/test_get_file_skeletons.py`:

```python
"""PRI-245: контракт session-less тула get_file_skeletons."""
from unittest.mock import MagicMock

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.service import _MAX_SKELETON_PATHS, MCPReviewService

SRC_A = '''class A:
    """Класс A."""

    def m(self):
        return 1
'''


def _fetch(repo, branch, paths):
    rows = {"a.py": [(1, SRC_A)]}
    return [
        (path, start, text)
        for path in paths
        for start, text in rows.get(path, [])
    ]


@pytest.fixture
def service(monkeypatch):
    settings = Settings()
    settings.voyage_api_key = "test"
    settings.github_token = "test"
    components = MagicMock()
    components.store.fetch_chunks_at_paths.side_effect = _fetch
    svc = MCPReviewService(settings, components)
    monkeypatch.setattr(
        svc, "_resolve_repo_branch", lambda repo, branch: ("owner/name", "dev")
    )
    return svc


def test_returns_line_numbered_skeleton(service):
    out = service.get_file_skeletons("owner/name", ["a.py"])
    assert "1|class A:" in out["a.py"]
    assert "4|    def m(self):" in out["a.py"]
    assert "return 1" not in out["a.py"]


def test_missing_path_gets_note_not_exception(service):
    out = service.get_file_skeletons("owner/name", ["nope.py"])
    assert out["nope.py"] == "(файл не найден в индексе: nope.py)"


def test_batch_returns_every_requested_path(service):
    out = service.get_file_skeletons("owner/name", ["a.py", "nope.py"])
    assert set(out) == {"a.py", "nope.py"}


def test_paths_over_cap_are_reported_not_dropped(service):
    paths = [f"f{i}.py" for i in range(_MAX_SKELETON_PATHS + 3)]
    out = service.get_file_skeletons("owner/name", paths)
    assert set(out) == set(paths), "усечение не должно быть молчаливым"
    assert out[paths[-1]].startswith("(превышен лимит путей на вызов")


def test_empty_paths_returns_empty(service):
    assert service.get_file_skeletons("owner/name", []) == {}
```

- [ ] **Step 7: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -q tests/mcp/test_get_file_skeletons.py`
Expected: FAIL — `ImportError: cannot import name '_MAX_SKELETON_PATHS'`

- [ ] **Step 8: Реализовать сервисный метод**

В `reviewer/mcp/service.py` рядом с session-less тулами добавить константы модуля:

```python
_MAX_SKELETON_PATHS = 25      # путей на один вызов get_file_skeletons
_MAX_SKELETON_LINES = 400     # строк скелета на файл — как у read_file
```

и метод:

```python
    def get_file_skeletons(
        self, repo: str, paths: list[str], branch: str | None = None
    ) -> dict[str, str]:
        """AST-скелеты проиндексированных файлов пачкой (PRI-245), без PR-сессии.

        Источник — чанки base-индекса ветки, то есть ровно тот материал, из
        которого считается skeleton_hash: файловый job сводок читает то же, что
        инвалидирует его результат. Ключ ответа есть у КАЖДОГО запрошенного
        пути — отсутствие, пустой скелет и превышение капа возвращаются нотой,
        а не молчаливым пропуском.
        """
        from reviewer.index.chunker import file_skeleton_lines

        requested = [str(p) for p in (paths or []) if p]
        if not requested:
            return {}
        accepted = requested[:_MAX_SKELETON_PATHS]
        overflow = requested[_MAX_SKELETON_PATHS:]
        out = {
            path: f"(превышен лимит путей на вызов: {_MAX_SKELETON_PATHS})"
            for path in overflow
        }
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {**out, **{path: f"({rb})" for path in accepted}}
        repo, resolved = rb
        try:
            rows = self.components.store.fetch_chunks_at_paths(
                repo, resolved, accepted
            )
        except Exception:
            log.warning("get_file_skeletons: сбой чтения чанков", exc_info=True)
            return {**out, **{p: "(чтение скелета недоступно)" for p in accepted}}
        grouped: dict[str, list[tuple[int, str]]] = {}
        for path, start_line, text in rows:
            grouped.setdefault(path, []).append((start_line, text))
        for path in accepted:
            chunks = grouped.get(path)
            if not chunks:
                out[path] = f"(файл не найден в индексе: {path})"
                continue
            skeleton = file_skeleton_lines(chunks)
            if not skeleton:
                out[path] = "(нет определений для скелета)"
                continue
            capped = len(skeleton) > _MAX_SKELETON_LINES
            body = "\n".join(
                f"{n}|{text}" for n, text in skeleton[:_MAX_SKELETON_LINES]
            )
            out[path] = body + "\n(…усечено)" if capped else body
        return out
```

- [ ] **Step 9: Зарегистрировать MCP-тул**

В `reviewer/entrypoints/mcp_server.py` после `definition` (`:375-378`):

```python
    @mcp.tool()
    def get_file_skeletons(repo: str, paths: list[str],
                           branch: str | None = None) -> dict:
        """AST skeletons (def/class signatures + first docstring line) of indexed
        files, no PR session. Takes a BATCH of paths and returns {path: skeleton},
        line-numbered as "N|code". Built from the indexed chunks — exactly the
        material a subsystem summary's freshness hash is computed from, so a
        summary job reads what invalidates it. Every requested path is a key:
        missing files, empty skeletons and over-cap paths come back as a note
        rather than silently dropped. branch defaults to the primary tracked branch."""
        return service.get_file_skeletons(repo, paths, branch)
```

Обновить докстринг `create_server` (`:19`): «38 тулов» → «39 тулов».

- [ ] **Step 10: Прогнать полный набор**

Run: `.venv/bin/pytest -q`
Expected: PASS (тест на число тулов, если он есть, обновится вместе с докстрингом)

- [ ] **Step 11: Коммит**

```bash
git add reviewer/index/chunker.py reviewer/index/store.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/index/test_file_skeleton.py tests/mcp/test_get_file_skeletons.py
git commit -m "feat(mcp): session-less тул get_file_skeletons

Скелеты пачкой из проиндексированных чанков — того же материала, из которого
считается skeleton_hash. Существующий read_file остаётся тулом PR-сессии."
```

---

### Task 4: Скилл — скелет вместо чтения исходника, батчинг job'ов, bump поколения

Правка протокола и инвалидация содержания фрагментов идут одним коммитом: способ чтения меняется, а `skeleton_hash` — нет, поэтому без bump'а `_GENERATION` существующие фрагменты остались бы «свежими» навсегда с текстом от полного `Read`.

**Files:**
- Modify: `plugin/skills/summarize-subsystems/SKILL.md:16-21` (Tools), `:52-69` (шаг 3 preflight), `:84-93` (шаг 5.2)
- Modify: `plugin/skills/_common/tool-usage.md` (session-less раздел)
- Modify: `reviewer/services/summary_fragments.py:10`
- Modify: `tests/skills/test_summarize_subsystems.py`
- Test: те же guard-тесты + новые в `tests/skills/test_summarize_subsystems.py`

**Interfaces:**
- Consumes: MCP-тул `get_file_skeletons(repo, paths, branch)` (Task 3); ключ `summary_paths.ignore` как компонент layout policy (Task 1)
- Produces: `_GENERATION = "summary-fragment-v2"`

- [ ] **Step 1: Написать падающие guard-тесты**

Дописать в `tests/skills/test_summarize_subsystems.py`:

```python
def test_skill_file_job_reads_skeletons_not_source():
    """PRI-245: вход фрагмента — скелет, а не исходник через harness-Read."""
    step = _file_job_step()
    assert "get_file_skeletons(repo, paths, branch)" in step, (
        "шаг 5.2 не переведён на скелеты"
    )
    assert "no harness `Read` of a source file" in step, (
        "шаг 5.2 не запрещает harness-Read по исходнику"
    )
    assert "no `read_file`" in step, "шаг 5.2 не запрещает PR-сессионный read_file"


def test_skill_batches_file_jobs():
    """PRI-245: один субагент на порцию путей, а не на файл."""
    step = _file_job_step()
    assert "batches of at most 15 paths" in step, "порция не описана или без потолка"
    assert "one file-summary job per batch" in step
    assert "a **single** `get_file_skeletons(repo, paths, branch)` call" in step, (
        "не сказано, что скелеты берутся одним вызовом на порцию"
    )


def test_skill_batch_keeps_path_mismatch_rejection():
    """Отбраковка чужого path переживает батчинг — единицей становится порция."""
    step = _file_job_step()
    assert "outside its batch" in step
    assert "re-dispatch that batch" in step
    assert "increment `raced`" in step


def test_skill_preflight_names_clustering_filter_as_layout_component():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "summary_paths.ignore" in normalized, (
        "preflight не называет фильтр кластеризации компонентом layout policy"
    )


def test_common_tool_usage_lists_get_file_skeletons():
    text = _assembled_skill()
    assert "get_file_skeletons(repo, paths, branch?)" in text
```

Обновить существующий `test_skill_composes_only_from_ordered_fragment_texts`: фраза `"file prompt must name only its own path"` → `"batch prompt must name only its own paths"`.

Добавить тест поколения в `tests/services/` (или туда, где уже тестируется `summary_fragments`; если модуля нет — создать `tests/services/test_summary_fragment_generation.py`):

```python
def test_generation_is_v2_after_read_mode_change():
    """PRI-245: смена способа чтения обязана инвалидировать старые фрагменты."""
    from reviewer.services.summary_fragments import _GENERATION

    assert _GENERATION == "summary-fragment-v2"
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -q tests/skills/ tests/services/`
Expected: FAIL — новые guard-тесты не находят фраз; поколение всё ещё `v1`

- [ ] **Step 3: Переписать шаг 5.2 `SKILL.md`**

Заменить пункт 2 шага 5 (`plugin/skills/summarize-subsystems/SKILL.md:84-93`) целиком. Якорную фразу `Let pending work be exactly` сохранить дословно — на ней стоит срез `_file_job_step()`:

```markdown
   2. Let pending work be exactly `added_files + changed_files`. Split it into **batches of at
      most 15 paths**, preserving order, and dispatch exactly one file-summary job per batch on
      the chosen model — and no other source-reading jobs. Each batch prompt must name only its
      own paths and tell the job to fetch their skeletons with a **single**
      `get_file_skeletons(repo, paths, branch)` call. A job must read nothing else: no harness
      `Read` of a source file, no `read_file`. The skeleton is deliberately the whole input —
      it is exactly the material a fragment's freshness hash is computed from, so a summary
      derived from it cannot silently go stale. The job returns one Russian result per path:
      `{path, summary, provenance}`. A job must never compute, guess, or return a `fingerprint`:
      that value is server-side and the orchestrator supplies it. If a job returns a `path`
      outside its batch, or omits a path of its batch, discard that batch's results and
      re-dispatch that batch once; on a second mismatch count the cluster as deferred, increment
      `raced`, and persist nothing for it. The orchestrator and every job must not read
      unchanged source files. If per-subagent model override is unavailable, generate the same
      per-file results inline and note that fallback in the report.
```

- [ ] **Step 4: Обновить Tools и preflight в `SKILL.md`**

В секции `## Tools` (`:19-21`) заменить хвост:

```markdown
Plus `list_subsystem_clusters`, `get_subsystem_summary_work`, `get_file_skeletons`,
`index_subsystem_summary`, `prune_subsystem_summaries` and `backfill_summary_embeddings`
(reviewer MCP). File-summary jobs read source ONLY through `get_file_skeletons`; the harness
`Read` is not used on source files in this skill.
```

В шаге 3 (preflight) заменить формулировку инварианта (`:66-68`):

```markdown
   - State the invariant explicitly: `cluster_key` and the layout identity depend on the whole
     layout policy — the default depth, every depth override, **and the `summary_paths.ignore`
     clustering filter (which keeps test trees out of summaries without touching the review
     index)** — so changing any of them triggers a full rebuild of every summary (old-layout
     summaries orphan and get pruned).
```

- [ ] **Step 5: Добавить тул в общий блок `tool-usage.md`**

В `plugin/skills/_common/tool-usage.md`, в конец раздела `## Session-less tools`:

```markdown
- `get_file_skeletons(repo, paths, branch?)` — AST skeletons of indexed files, a batch of paths
  per call; built from indexed chunks, so it matches what a summary's freshness hash sees.
```

- [ ] **Step 6: Поднять поколение фрагментов**

В `reviewer/services/summary_fragments.py:10`:

```python
_GENERATION = "summary-fragment-v2"   # PRI-245: вход фрагмента сменился на скелет
```

- [ ] **Step 7: Пересобрать манифесты плагина**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: манифесты обновлены (payload-digest пересчитан); без этого install-тесты краснеют.

- [ ] **Step 8: Прогнать полный набор**

Run: `.venv/bin/pytest -q`
Expected: PASS. Правка `_common/tool-usage.md` меняет собранный текст **всех** скиллов, которые её включают — если упал guard-тест другого скилла, чинить его там же.

- [ ] **Step 9: Коммит**

```bash
git add plugin/ reviewer/services/summary_fragments.py tests/skills/test_summarize_subsystems.py tests/services/
git commit -m "feat(skills): скелет и батчинг файловых job'ов summarize-subsystems

Файловый job больше не читает исходник: вход — скелет через get_file_skeletons,
то есть ровно то, что инвалидирует фрагмент. Один субагент на порцию до 15
путей вместо одного на файл. Поколение фрагментов поднято до v2, иначе
сохранённые фрагменты остались бы свежими навсегда с текстом от полного Read."
```

---

### Task 5: Документация

**Files:**
- Modify: `CLAUDE.md` (блок «Неочевидные факты», пункты про сводки)
- Modify: `README.md`, `README.ru.md`

- [ ] **Step 1: Обновить `CLAUDE.md`**

В разделе «Неочевидные факты» дополнить пункт про инкрементальные fragments и добавить факт про фильтр:

```markdown
- **Фильтр кластеризации сводок (`summary_paths.ignore`, PRI-245).** Отдельный от `paths.ignore`
  слой: `paths.ignore` управляет индексацией и ревью, а этот ключ — только кластеризацией сводок.
  Дефолт `("tests", "test")`; env-слоя нет (как у `context_limits`), явный пустой список
  выключает фильтр. Применяется при сборке members ДО `build_clusters` в обеих точках —
  `_summary_state` и `_current_subsystem_hashes`; расхождение наборов сделало бы каждую сводку
  вечно stale. Входит в payload `layout_token`, поэтому включение/выключение запускает полный
  пересбор и штатный `prune_subsystem_summaries` собирает осиротевшие `tests/*`.
- **Вход файлового job сводок — скелет, а не исходник (PRI-245).** `skeleton_hash` считается по
  тексту символа-чанка, поэтому «читать ровно то, что инвалидирует» достижимо только чтением из
  чанков: session-less тул `get_file_skeletons(repo, paths, branch)` собирает скелет файла как
  объединение скелетов его чанков. Цена — module-level docstring в чанки не попадает и в скелет
  не войдёт. `read_file` остаётся тулом PR-сессии и session-less аналога не имеет. Job'ы
  батчатся по 15 путей — один субагент на порцию, а не на файл.
```

- [ ] **Step 2: Обновить оба README**

В `README.md` (EN) и `README.ru.md` (RU) — в разделах про `.review.yml` и про сводки подсистем: добавить ключ `summary_paths.ignore` с дефолтом и пометкой «не влияет на индекс ревью»; в описании `summarize-subsystems` заменить «читает файлы» на «читает скелеты через `get_file_skeletons`, job'ы батчатся». Держать оба файла синхронными: расхождение EN/RU — самостоятельный дефект.

- [ ] **Step 3: Проверить, что doc-тесты зелёные**

Run: `.venv/bin/pytest -q tests/docs/ tests/`
Expected: PASS

- [ ] **Step 4: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md
git commit -m "docs: фильтр кластеризации сводок и скелет-вход файловых job'ов"
```

---

## Приёмка на живом деплое (вне автотестов)

Выполняется после мержа и передеплоя; результат вписывается в PRI-245 (критерий 10).

- [ ] Первый проход после апгрейда помечает кластеры `bootstrap`, а не «всё свежо»: фрагменты предыдущей версии не переиспользуются.
- [ ] До успешной замены bundle `get_subsystem_summaries` продолжает отдавать старые сводки (не пустоту в середине миграции).
- [ ] Полный uncapped проход завершается `deferred == 0`, `raced == 0`, `completed=true`; кластеры `tests/*` уходят через `prune_subsystem_summaries` без ручного SQL.
- [ ] Прогон при выставленном `SUMMARY_REBUILD_CAP` пропускает pruning и оставляет незаменённые сводки на месте.
- [ ] Число диспатчей файловых job'ов меньше числа pending-файлов.
- [ ] Замер стоимости полного прохода «до и после» записан в задачу.

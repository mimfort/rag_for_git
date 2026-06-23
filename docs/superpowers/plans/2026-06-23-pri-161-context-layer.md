# PRI-161 (расширенная) — настраиваемый контекст-слой: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать контекст-слой настраиваемым: ignore-папок на входе индексации (экономия Voyage), per-prefix глубина кластеризации сводок, и приор сводок подсистем в скилле solve-task.

**Architecture:** Три независимых по коду части, одна ветка, всё конфигурируется через `.review.yml` целевой/индексируемой ветки. Ignore переиспользует существующий ключ `paths.ignore` через новый единый матчер `is_ignored`, применяемый в `freshness` (overlay+base), в `reviewer index` (один фильтр `files` — чанки+граф+чистка через существующий `delete_paths_except`) и в `prepare`. Per-prefix depth — карта `summary_cluster_depth_overrides` + longest-prefix `depth_for`, прокинутая в `build_clusters` и единую depth-логику service (list/index/prune). Приор — вызов `get_subsystem_summaries(query=…)` в solve-task + секция brief.

**Tech Stack:** Python 3.11–3.13, fnmatch (stdlib), pgvector/ParadeDB, Neo4j, tree-sitter/SCIP, FastMCP, Voyage, Click, pytest, ruff.

## Global Constraints

- Язык проекта русский: комментарии/докстринги/CLI-сообщения. Тела `SKILL.md` и докстринги MCP-тулов — английские (документированное исключение); текст плана и комментарии в коде — русские.
- ruff: line-length 100, target py311. Прогон: `.venv/bin/ruff check <files>`.
- Тесты: `.venv/bin/pytest -q` (unit; ВСЕГДА с `--ignore=tests/web` — там предсуществующий `ModuleNotFoundError: fastapi`, к задаче не относится). Integration: `.venv/bin/pytest -m integration` (нужны Postgres :5433 / Neo4j).
- Conventional Commits на русском, БЕЗ self-attribution (никаких `Co-Authored-By`/упоминаний Claude). Суффикс задачи `(PRI-161)`.
- Voyage free tier 3 RPM / 10K TPM — ignore-на-входе исключает эмбеддинги игнор-файлов; дедуп эмбеддингов сводок по `source_hash` сохраняется (PRI-167).
- `.review.yml` берётся из целевой/индексируемой ветки (PR не ослабляет своё ревью) — ignore и depth-overrides тоже оттуда.
- Инвариант `node_id="path#fqn"` един для чанков и графа — ignore применяется к обоим одинаково. `cluster_key`/`source_hash` зависят от depth → `list_subsystem_clusters`/`index_subsystem_summary`/`prune_subsystem_summaries` используют ОДНУ per-path depth-логику.

---

### Task 1: Единый матчер `is_ignored` + перевод гейта находок

**Files:**
- Create: `reviewer/index/pathfilter.py`
- Modify: `reviewer/policy/policy.py` (импорт + `gate` :108)
- Test: `tests/index/test_pathfilter.py`

**Interfaces:**
- Produces: `reviewer.index.pathfilter.is_ignored(path: str, patterns: list[str]) -> bool`

- [ ] **Step 1: Написать падающий тест**

`tests/index/test_pathfilter.py`:
```python
from reviewer.index.pathfilter import is_ignored


def test_bare_dir_matches_subtree():
    assert is_ignored("vendor/lib/x.py", ["vendor"])
    assert is_ignored("vendor/x.py", ["vendor"])


def test_bare_dir_does_not_match_sibling_prefix():
    assert not is_ignored("vendored/x.py", ["vendor"])


def test_glob_pattern_matched_as_is():
    assert is_ignored("pkg/a.gen.py", ["*.gen.py"])
    assert is_ignored("migrations/0001.py", ["migrations/*"])


def test_no_match_returns_false():
    assert not is_ignored("reviewer/index/store.py", ["vendor", "migrations/*"])


def test_empty_patterns_and_blank():
    assert not is_ignored("a/b.py", [])
    assert not is_ignored("a/b.py", [""])
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_pathfilter.py -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.index.pathfilter`)

- [ ] **Step 3: Реализовать `is_ignored`**

`reviewer/index/pathfilter.py`:
```python
from __future__ import annotations

from fnmatch import fnmatch

_GLOB_CHARS = ("*", "?", "[")


def is_ignored(path: str, patterns: list[str]) -> bool:
    """Путь под одним из ignore-паттернов (fnmatch).

    Нормализация: голый паттерн без glob-метасимволов (``* ? [``) — ``"dir"`` или
    ``"a/b"`` — матчит и сам путь, и его поддерево (эквивалент fnmatch против ``pat``
    и ``pat + "/*"``), чтобы «папка и всё внутри» работало без явного ``/*``.
    Паттерны с glob (``"vendor/*"``, ``"*.gen.py"``) матчатся fnmatch как есть.
    """
    for pat in patterns:
        if not pat:
            continue
        if fnmatch(path, pat):
            return True
        if not any(ch in pat for ch in _GLOB_CHARS):
            prefix = pat.rstrip("/")
            if fnmatch(path, f"{prefix}/*"):
                return True
    return False
```

- [ ] **Step 4: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/index/test_pathfilter.py -q`
Expected: PASS (5 тестов)

- [ ] **Step 5: Перевести гейт находок на `is_ignored`**

`reviewer/policy/policy.py` — добавить импорт после строки 6:
```python
from reviewer.index.pathfilter import is_ignored
```
Заменить тело проверки в `gate` (строка 108):
```python
        if is_ignored(finding.file, self.ignore):
            return False
```
Удалить ставший ненужным `from fnmatch import fnmatch` (строка 3), если `fnmatch` больше не используется в файле.

- [ ] **Step 6: Прогнать тесты гейта — поведение не должно измениться**

Run: `.venv/bin/pytest tests/policy/ tests/index/test_pathfilter.py -q`
Expected: PASS (все существующие тесты политики зелёные)

- [ ] **Step 7: Линт**

Run: `.venv/bin/ruff check reviewer/index/pathfilter.py reviewer/policy/policy.py tests/index/test_pathfilter.py`
Expected: All checks passed!

- [ ] **Step 8: Коммит**

```bash
git add reviewer/index/pathfilter.py reviewer/policy/policy.py tests/index/test_pathfilter.py
git commit -m "feat(index): единый матчер is_ignored + перевод гейта находок на него (PRI-161)"
```

---

### Task 2: Ignore-фильтр в `freshness` (overlay + base)

**Files:**
- Modify: `reviewer/index/freshness.py` (`build_overlay`, `update_base`)
- Test: `tests/index/test_freshness_ignore.py`

**Interfaces:**
- Consumes: `is_ignored` (Task 1)
- Produces:
  - `build_overlay(store, embedder, repo, pr_number, changed_files, head_sources, ignore: list[str] = ())`
  - `update_base(store, embedder, repo, target_ref, changed_files, read, removed_files=(), ignore: list[str] = ())`

- [ ] **Step 1: Написать падающий тест**

`tests/index/test_freshness_ignore.py`:
```python
from reviewer.index.freshness import build_overlay, update_base


class _FakeStore:
    def __init__(self):
        self.upserted = []
        self.deleted = []

    def existing_hashes(self, repo, ref):
        return set()

    def find_embeddings_by_hashes(self, repo, hashes):
        return {}

    def upsert(self, rows):
        self.upserted.extend(rows)

    def delete_paths(self, repo, ref, paths):
        self.deleted.extend(paths)

    def delete_missing_symbols(self, repo, ref, path, keep_fqns):
        pass


class _FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.0] * 1024 for _ in texts]


def test_build_overlay_skips_ignored_paths():
    store, emb = _FakeStore(), _FakeEmbedder()
    build_overlay(
        store, emb, "o/r", 1,
        ["vendor/x.py", "reviewer/a.py"],
        head_sources={"vendor/x.py": "def a():\n    pass\n",
                      "reviewer/a.py": "def b():\n    pass\n"},
        ignore=["vendor"],
    )
    paths = {r.path for r in store.upserted}
    assert "vendor/x.py" not in paths
    assert "reviewer/a.py" in paths


def test_update_base_skips_and_purges_newly_ignored():
    store, emb = _FakeStore(), _FakeEmbedder()
    sources = {"vendor/x.py": "def a():\n    pass\n",
               "reviewer/a.py": "def b():\n    pass\n"}
    update_base(
        store, emb, "o/r", "main",
        ["vendor/x.py", "reviewer/a.py"],
        read=lambda p: sources.get(p),
        ignore=["vendor"],
    )
    paths = {r.path for r in store.upserted}
    assert "vendor/x.py" not in paths
    assert "reviewer/a.py" in paths
    assert "vendor/x.py" in store.deleted   # ставший игнор-путь вычищается из base
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_freshness_ignore.py -q`
Expected: FAIL (`build_overlay() got an unexpected keyword argument 'ignore'`)

- [ ] **Step 3: Добавить ignore в `build_overlay`**

`reviewer/index/freshness.py` — импорт после строки 5:
```python
from reviewer.index.pathfilter import is_ignored
```
Сигнатура `build_overlay` (строка 33-34) → добавить параметр:
```python
def build_overlay(store, embedder, repo: str, pr_number: int, changed_files: list[str],
                  head_sources: dict[str, str], ignore: list[str] = ()) -> None:
```
В цикле (после `if not path.endswith(".py"): continue`, строка 45-46) добавить:
```python
        if is_ignored(path, list(ignore)):
            continue
```

- [ ] **Step 4: Добавить ignore в `update_base`**

Сигнатура `update_base` (строка 57-60) → добавить параметр:
```python
def update_base(store, embedder, repo: str, target_ref: str,
                changed_files: list[str],
                read: Callable[[str], str | None],
                removed_files: list[str] | tuple[str, ...] = (),
                ignore: list[str] = ()) -> None:
```
В цикле (после `if not path.endswith(".py"): continue`, строка 73-74) добавить:
```python
        if is_ignored(path, list(ignore)):
            store.delete_paths(repo, ref, [path])   # путь стал игнор — вычищаем из base
            continue
```

- [ ] **Step 5: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/index/test_freshness_ignore.py -q`
Expected: PASS (2 теста)

- [ ] **Step 6: Линт**

Run: `.venv/bin/ruff check reviewer/index/freshness.py tests/index/test_freshness_ignore.py`
Expected: All checks passed!

- [ ] **Step 7: Коммит**

```bash
git add reviewer/index/freshness.py tests/index/test_freshness_ignore.py
git commit -m "feat(index): ignore-фильтр на входе freshness (overlay + base) (PRI-161)"
```

---

### Task 3: Применить ignore в `reviewer index` (CLI)

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (команда `index`, строки 146-179)
- Test: `tests/entrypoints/test_index_ignore.py`

**Interfaces:**
- Consumes: `is_ignored` (Task 1), `update_base(..., ignore=)` (Task 2), `ReviewPolicy.from_yaml` (существует)

- [ ] **Step 1: Написать падающий тест**

`tests/entrypoints/test_index_ignore.py` (проводочный тест через CliRunner + monkeypatch — БД не нужна):
```python
from unittest.mock import MagicMock

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod


def test_index_filters_ignored_files(monkeypatch):
    captured = {}

    def fake_update_base(store, embedder, repo, branch, files, **kwargs):
        captured["files"] = list(files)
        captured["ignore"] = list(kwargs.get("ignore", []))

    fake_components = MagicMock()
    monkeypatch.setattr(cli_mod, "build_components", lambda s: fake_components)
    monkeypatch.setattr(cli_mod, "_resolve_repo", lambda *a: "o/r")
    monkeypatch.setattr(cli_mod, "list_python_files",
                        lambda repo, ref: ["vendor/x.py", "reviewer/a.py"])
    monkeypatch.setattr(cli_mod, "rev_parse", lambda repo, ref: "deadbeef")
    monkeypatch.setattr(cli_mod, "build_code_graph",
                        lambda *a, **k: (set(), [], "tree-sitter"))

    def fake_file_at_ref(repo, path, ref):
        if path == ".review.yml":
            return "paths:\n  ignore:\n    - vendor\n"
        return "def f():\n    pass\n"

    monkeypatch.setattr(cli_mod, "file_at_ref", fake_file_at_ref)
    monkeypatch.setattr(cli_mod, "update_base", fake_update_base)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])
    assert result.exit_code == 0, result.output
    assert captured["files"] == ["reviewer/a.py"]      # vendor/x.py отфильтрован
    assert "vendor" in captured["ignore"]
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/entrypoints/test_index_ignore.py -q`
Expected: FAIL (`captured["files"]` содержит `vendor/x.py` — фильтра ещё нет)

- [ ] **Step 3: Добавить чтение `.review.yml` и фильтр**

`reviewer/entrypoints/cli.py` — добавить импорты в шапку (рядом со строкой 15):
```python
from reviewer.index.pathfilter import is_ignored
from reviewer.policy.policy import ReviewPolicy
```
В теле `index` заменить блок строк 156-158 на:
```python
        files = list_python_files(repo, ref)
        review_yml = file_at_ref(repo, ".review.yml", ref)
        ignore = ReviewPolicy.from_yaml(review_yml).ignore if review_yml else []
        if ignore:
            files = [f for f in files if not is_ignored(f, ignore)]
        update_base(c.store, c.embedder, repo_id, branch, files,
                    read=lambda p: file_at_ref(repo, p, ref), ignore=ignore)
```
(`delete_paths_except(repo_id, bref, files)` на строке 159 теперь чистит игнор-чанки автоматически — `files` уже без игнор-путей; граф на строках 163-171 строится из тех же `files` → игнор-узлы не попадут.)

- [ ] **Step 4: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/entrypoints/test_index_ignore.py -q`
Expected: PASS

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/entrypoints/cli.py tests/entrypoints/test_index_ignore.py`
Expected: All checks passed!

- [ ] **Step 6: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_index_ignore.py
git commit -m "feat(cli): reviewer index фильтрует paths.ignore из .review.yml ветки (чанки+граф+чистка) (PRI-161)"
```

---

### Task 4: Применить ignore в `prepare` (overlay + base-досинк)

**Files:**
- Modify: `reviewer/services/review_service.py` (`prepare`, строки 168-252)
- Test: `tests/services/test_prepare_ignore.py`

**Interfaces:**
- Consumes: `update_base(..., ignore=)`, `build_overlay(..., ignore=)` (Task 2), `ReviewPolicy.from_yaml` (импортирован :21)

- [ ] **Step 1: Написать падающий тест**

`tests/services/test_prepare_ignore.py` (мокаем `build_overlay`, проверяем проброс ignore):
```python
from unittest.mock import MagicMock

import reviewer.services.review_service as rs


def test_prepare_passes_ignore_to_build_overlay(monkeypatch):
    captured = {}
    monkeypatch.setattr(rs, "build_overlay",
                        lambda *a, **k: captured.update(ignore=k.get("ignore")))
    monkeypatch.setattr(rs, "chunk_python", lambda path, src: [])
    monkeypatch.setattr(rs, "_structural_summary", lambda *a, **k: "")

    vcs = MagicMock()
    pr = MagicMock(number=7, base_sha="b", head_sha="h", base_ref="main", draft=False)
    vcs.get_pull_request.return_value = pr
    vcs.get_changed_files.return_value = [MagicMock(path="reviewer/a.py",
                                                    status="modified", patch="@@")]
    vcs.get_file_at_ref.side_effect = (
        lambda path, sha: "paths:\n  ignore:\n    - vendor\n"
        if path == ".review.yml" else "def f():\n    pass\n")

    settings = MagicMock()
    settings.review_branches_list.return_value = ["main"]
    settings.review_max_files = 50
    components = MagicMock()
    components.store.get_index_meta.return_value = None     # без base-досинка

    svc = rs.ReviewService(settings, components)
    svc.prepare("o", "r", 7, vcs_provider=vcs)
    assert captured.get("ignore") == ["vendor"]
```
(Сигнатуру конструктора `ReviewService(settings, components)` сверить с фактической в начале `review_service.py`; при иной — подогнать инстанцирование, поведение теста не менять.)

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_prepare_ignore.py -q`
Expected: FAIL (`captured.get("ignore")` is None — ignore не прокинут)

- [ ] **Step 3: Резолвить ignore и прокинуть**

`reviewer/services/review_service.py` — после branch-check (после строки 170, до base-sync) добавить:
```python
            # paths.ignore из .review.yml целевой ветки — общий для base-досинка и overlay.
            review_yml = vcs.get_file_at_ref(".review.yml", prq.base_sha)
            ignore = ReviewPolicy.from_yaml(review_yml).ignore if review_yml else []
```
В вызов `update_base` (строки 185-193) добавить аргумент `ignore=ignore`:
```python
                    update_base(
                        self.components.store,
                        self.components.embedder,
                        repo,
                        prq.base_ref,
                        [f.path for f in diff_files if f.status != "removed"],
                        read=lambda p: vcs.get_file_at_ref(p, prq.base_sha),
                        removed_files=[f.path for f in diff_files if f.status == "removed"],
                        ignore=ignore,
                    )
```
В вызов `build_overlay` (строки 245-252) добавить `ignore=ignore`:
```python
            build_overlay(
                self.components.store,
                self.components.embedder,
                repo,
                pr_number,
                changed,
                head_sources=head_sources,
                ignore=ignore,
            )
```

- [ ] **Step 4: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/services/test_prepare_ignore.py -q`
Expected: PASS

- [ ] **Step 5: Регресс prepare**

Run: `.venv/bin/pytest tests/services/ tests/mcp/ -q --ignore=tests/web`
Expected: PASS (существующие тесты prepare/MCP зелёные)

- [ ] **Step 6: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/services/review_service.py tests/services/test_prepare_ignore.py`
```bash
git add reviewer/services/review_service.py tests/services/test_prepare_ignore.py
git commit -m "feat(services): prepare прокидывает paths.ignore в base-досинк и overlay (PRI-161)"
```

---

### Task 5: `depth_for` + `build_clusters(depth_overrides)` + поле политики

**Files:**
- Modify: `reviewer/graph/summaries.py` (`depth_for`, `build_clusters`)
- Modify: `reviewer/policy/policy.py` (поле + `from_yaml`/`load`)
- Test: `tests/graph/test_summaries_depth.py`, `tests/policy/test_policy_depth_overrides.py`

**Interfaces:**
- Produces:
  - `reviewer.graph.summaries.depth_for(path: str, default: int, overrides: dict[str, int]) -> int`
  - `build_clusters(members, in_degree_fn, *, depth=2, min_size=1, top_n=10, depth_overrides: dict[str, int] | None = None)`
  - `ReviewPolicy.summary_cluster_depth_overrides: dict[str, int]`

- [ ] **Step 1: Написать падающий тест (summaries)**

`tests/graph/test_summaries_depth.py`:
```python
from reviewer.graph.summaries import Member, build_clusters, depth_for


def test_depth_for_no_overrides_returns_default():
    assert depth_for("a/b/c.py", 2, {}) == 2


def test_depth_for_longest_prefix_wins():
    ov = {"reviewer": 1, "reviewer/index": 3}
    assert depth_for("reviewer/index/store.py", 2, ov) == 3
    assert depth_for("reviewer/mcp/service.py", 2, ov) == 1


def test_depth_for_sibling_prefix_not_matched():
    assert depth_for("reviewer/indexer/x.py", 2, {"reviewer/index": 3}) == 2


def test_depth_for_root_file_uses_default():
    assert depth_for("setup.py", 2, {"reviewer": 1}) == 2


def test_build_clusters_applies_per_prefix_depth():
    members = [
        Member("reviewer/index/store.py#A", "reviewer/index/store.py", "h1", "sk1", 1),
        Member("reviewer/index/sub/x.py#B", "reviewer/index/sub/x.py", "h2", "sk2", 1),
        Member("reviewer/mcp/service.py#C", "reviewer/mcp/service.py", "h3", "sk3", 1),
    ]
    clusters = build_clusters(members, None, depth=2,
                              depth_overrides={"reviewer/index": 3})
    keys = {c.key for c in clusters}
    assert "reviewer/index" in keys          # store.py: depth 3, директория 2 сегмента
    assert "reviewer/index/sub" in keys      # sub/x.py: depth 3 → 3 сегмента
    assert "reviewer/mcp" in keys            # depth 2 (дефолт)
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/graph/test_summaries_depth.py -q`
Expected: FAIL (`ImportError: cannot import name 'depth_for'`)

- [ ] **Step 3: Реализовать `depth_for` и расширить `build_clusters`**

`reviewer/graph/summaries.py` — добавить функцию после `cluster_key` (после строки 42):
```python
def depth_for(path: str, default: int, overrides: dict[str, int]) -> int:
    """Глубина для пути: depth самого длинного ключа-префикса ``overrides``
    (совпадение по сегментам ДИРЕКТОРИИ), иначе ``default``.

    Ключ ``"reviewer/index"`` матчит ``"reviewer/index/store.py"``, но НЕ
    ``"reviewer/indexer/x.py"`` (сравнение посегментно, не по строке-префиксу).
    """
    if not overrides:
        return default
    dir_parts = path.split("/")[:-1]
    best_depth, best_len = default, -1
    for key, d in overrides.items():
        kparts = key.strip("/").split("/")
        if dir_parts[:len(kparts)] == kparts and len(kparts) > best_len:
            best_depth, best_len = d, len(kparts)
    return best_depth
```
Сигнатуру `build_clusters` (строки 55-62) → добавить параметр `depth_overrides`:
```python
def build_clusters(
    members: list[Member],
    in_degree_fn: Callable[[list[str]], dict[str, int]] | None,
    *,
    depth: int = 2,
    min_size: int = 1,
    top_n: int = 10,
    depth_overrides: dict[str, int] | None = None,
) -> list[Cluster]:
```
Заменить группировку (строки 64-66) на per-path depth:
```python
    overrides = depth_overrides or {}
    groups: dict[str, list[Member]] = {}
    for m in members:
        groups.setdefault(cluster_key(m.path, depth_for(m.path, depth, overrides)), []).append(m)
```

- [ ] **Step 4: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/graph/test_summaries_depth.py -q`
Expected: PASS (5 тестов)

- [ ] **Step 5: Написать падающий тест (policy)**

`tests/policy/test_policy_depth_overrides.py`:
```python
from reviewer.policy.policy import ReviewPolicy


def test_from_yaml_reads_depth_overrides():
    text = "summary_cluster_depth: 2\nsummary_cluster_depth_overrides:\n  reviewer/index: 3\n"
    pol = ReviewPolicy.from_yaml(text)
    assert pol.summary_cluster_depth_overrides == {"reviewer/index": 3}


def test_load_overrides_only_when_present(monkeypatch):
    class S:
        def review_categories_list(self): return []
        review_severity_threshold = "low"
        review_max_comments = 25
        review_min_confidence = 0.5
        review_output_language = "ru"
        def task_board_default(self): return None
        review_grounding_max_distance = 5
        summary_cluster_depth = 2
        summary_topk_threshold = 20
    pol = ReviewPolicy.load(S(), "summary_cluster_depth_overrides:\n  vendor: 1\n")
    assert pol.summary_cluster_depth_overrides == {"vendor": 1}
    pol2 = ReviewPolicy.load(S(), "max_comments: 10\n")
    assert pol2.summary_cluster_depth_overrides == {}      # дефолт, ключа нет
```

- [ ] **Step 6: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/policy/test_policy_depth_overrides.py -q`
Expected: FAIL (`AttributeError: ... summary_cluster_depth_overrides`)

- [ ] **Step 7: Добавить поле в политику**

`reviewer/policy/policy.py` — после строки 23 (поле `summary_topk_threshold`):
```python
    summary_cluster_depth_overrides: dict[str, int] = field(
        default_factory=dict)             # per-prefix depth из .review.yml (PRI-161)
```
В `from_yaml` (в конструкторе `cls(...)`, после `summary_topk_threshold=...` строка 43):
```python
            summary_cluster_depth_overrides=dict(
                data.get("summary_cluster_depth_overrides", {}) or {}),
```
В `load` (после блока `summary_topk_threshold`, строка 91):
```python
        if "summary_cluster_depth_overrides" in data:
            policy.summary_cluster_depth_overrides = dict(
                data["summary_cluster_depth_overrides"] or {})
```
(`from_settings` не трогаем — overrides задаются только в `.review.yml`.)

- [ ] **Step 8: Прогнать — убедиться, что проходит + регресс политики**

Run: `.venv/bin/pytest tests/policy/ tests/graph/test_summaries_depth.py -q`
Expected: PASS

- [ ] **Step 9: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/graph/summaries.py reviewer/policy/policy.py tests/graph/test_summaries_depth.py tests/policy/test_policy_depth_overrides.py`
```bash
git add reviewer/graph/summaries.py reviewer/policy/policy.py tests/graph/test_summaries_depth.py tests/policy/test_policy_depth_overrides.py
git commit -m "feat(graph): per-prefix depth кластеризации сводок (depth_for + build_clusters + policy) (PRI-161)"
```

---

### Task 6: Резолв overrides в service (list/index/prune — единая depth-логика)

**Files:**
- Modify: `reviewer/mcp/service.py` (`_resolve_summary_depth` :331-360; callsites :504-509, :547-550, :613-617)
- Test: `tests/mcp/test_summary_depth_overrides.py`

**Interfaces:**
- Consumes: `depth_for` (Task 5), `build_clusters(depth_overrides=)` (Task 5)
- Produces: `_resolve_summary_depth(repo, branch) -> tuple[int, dict[str, int], str]`

- [ ] **Step 1: Написать падающий тест**

`tests/mcp/test_summary_depth_overrides.py`:
```python
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _svc(review_yml: str):
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    c = MagicMock()
    # base-состав: один файл в reviewer/index/sub, один в reviewer/mcp
    c.store.list_base_members.return_value = [
        ("reviewer/index/sub/x.py", "A", "h1", 1, "sk1"),
        ("reviewer/mcp/service.py", "B", "h2", 1, "sk2"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    c.summary_store.get_updated_ats.return_value = {}
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = review_yml
    svc = MCPReviewService(s, c, vcs_factory=lambda o, n: vcs)
    return svc


def test_list_clusters_honors_depth_override():
    svc = _svc("summary_cluster_depth: 2\n"
               "summary_cluster_depth_overrides:\n  reviewer/index: 3\n")
    out = svc.list_subsystem_clusters("o/n", "main", cap=0)
    keys = {c["cluster_key"] for c in out["clusters"]}
    assert "reviewer/index/sub" in keys     # override depth 3
    assert "reviewer/mcp" in keys           # дефолт depth 2
```
(`MCPReviewService(settings, components, vcs_factory=...)` — сверить конструктор с `tests/mcp/test_server.py`; `review_branches_list()` должен включать "main" — Settings по умолчанию; при необходимости задать `s.review_branches`.)

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_summary_depth_overrides.py -q`
Expected: FAIL (`cluster_key` = `reviewer/index` без учёта override → нет `reviewer/index/sub`)

- [ ] **Step 3: Расширить `_resolve_summary_depth`**

`reviewer/mcp/service.py` — заменить тело `_resolve_summary_depth` (строки 331-360) так, чтобы возвращать `(depth, overrides, source)`:
```python
    def _resolve_summary_depth(self, repo: str, branch: str) -> tuple[int, dict[str, int], str]:
        """Резолв глубины кластеризации сводок: env-дефолт → override из .review.yml ветки.

        Возвращает (depth, depth_overrides, source). depth_overrides — карта
        префикс→depth из .review.yml (PRI-161); пусто, если ключа нет. Fail-soft:
        нет токена/ветки/файла/кривой yml → (settings.summary_cluster_depth, {}, "env").
        source = ".review.yml", если файл задаёт summary_cluster_depth или _overrides."""
        import yaml
        from reviewer.policy.policy import ReviewPolicy
        default = self.settings.summary_cluster_depth
        owner, name = repo.split("/", 1)
        vcs = None
        try:
            vcs = (self._vcs_factory(owner, name) if self._vcs_factory
                   else self._review_service._create_vcs_provider(owner, name))
            text = vcs.get_file_at_ref(".review.yml", branch)
            if not text:
                return default, {}, "env"
            data = yaml.safe_load(text) or {}
            pol = ReviewPolicy.load(self.settings, text)
            keyed = ("summary_cluster_depth" in data
                     or "summary_cluster_depth_overrides" in data)
            return (pol.summary_cluster_depth, pol.summary_cluster_depth_overrides,
                    ".review.yml" if keyed else "env")
        except Exception:
            log.warning("_resolve_summary_depth: fail-soft → env-дефолт", exc_info=True)
            return default, {}, "env"
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("_resolve_summary_depth: не удалось закрыть VCS", exc_info=True)
```

- [ ] **Step 4: Обновить callsite `list_subsystem_clusters`**

`reviewer/mcp/service.py` строки 504-509 → :
```python
        if depth is None:
            resolved_depth, overrides, depth_source = self._resolve_summary_depth(repo, resolved)
        else:
            resolved_depth, overrides, depth_source = depth, {}, "arg"
        clusters = build_clusters(
            members, in_degree_fn, depth=resolved_depth, min_size=min_size or 1,
            depth_overrides=overrides)
```

- [ ] **Step 5: Обновить callsite `index_subsystem_summary`**

`reviewer/mcp/service.py` — импорт в начале метода (строка 538) → добавить `depth_for`:
```python
        from reviewer.graph.summaries import (cluster_key as cluster_key_of,
                                              compute_source_hash, depth_for)
```
Строки 547-550 → :
```python
        depth, overrides, _ = self._resolve_summary_depth(repo, resolved)
        raw = self.components.store.list_base_members(repo, resolved)
        members = [(f"{p}#{s}", sk) for p, s, _h, _sl, sk in raw
                   if cluster_key_of(p, depth_for(p, depth, overrides)) == cluster_key]
```

- [ ] **Step 6: Обновить callsite `prune_subsystem_summaries`**

`reviewer/mcp/service.py` — импорт (строка 608) → добавить `depth_for`:
```python
        from reviewer.graph.summaries import cluster_key as cluster_key_of, depth_for
```
Строки 613-617 → :
```python
        depth, overrides, _ = self._resolve_summary_depth(repo, resolved)
        raw = self.components.store.list_base_members(repo, resolved)
        if not raw:
            return {"pruned": 0, "kept": 0, "note": "(base-индекс пуст — purge пропущен)"}
        keep_keys = sorted({cluster_key_of(p, depth_for(p, depth, overrides))
                            for p, _s, _h, _sl, _sk in raw})
```

- [ ] **Step 7: Прогнать новый тест + регресс сводок**

Run: `.venv/bin/pytest tests/mcp/test_summary_depth_overrides.py tests/mcp/test_subsystem_summaries.py -q`
Expected: PASS (новый тест зелёный; существующие сводок-тесты не сломаны — `_resolve_summary_depth` теперь 3-кортеж, проверь, что моки в test_subsystem_summaries обновлены, если они мокали этот метод напрямую)

- [ ] **Step 8: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_summary_depth_overrides.py`
```bash
git add reviewer/mcp/service.py tests/mcp/test_summary_depth_overrides.py
git commit -m "feat(mcp): per-path depth в list/index/prune сводок (резолв overrides из .review.yml) (PRI-161)"
```

---

### Task 7: Приор сводок подсистем в solve-task

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (шаг 3 «Gather context», шаг 4 скелет brief)
- Test: `tests/skills/test_ask_uses_summaries.py` (добавить guard для solve-task)

**Interfaces:**
- Consumes: MCP-тул `get_subsystem_summaries(repo, branch, query, top_k)` (PRI-167, готов)

- [ ] **Step 1: Написать падающий guard-тест**

`tests/skills/test_ask_uses_summaries.py` — добавить в конец файла:
```python
SOLVE = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_solve_task_passes_query_to_summaries():
    text = SOLVE.read_text(encoding="utf-8")
    assert "get_subsystem_summaries(repo, branch, query=" in text


def test_solve_task_has_subsystems_brief_section():
    text = SOLVE.read_text(encoding="utf-8")
    assert "## Subsystems" in text
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_ask_uses_summaries.py -q`
Expected: FAIL (2 новых теста — подстрок ещё нет в solve-task SKILL.md)

- [ ] **Step 3: Добавить вызов приора в шаг 3**

`plugin/skills/solve-task/SKILL.md` — в шаге 3 «Gather context», ПЕРВЫМ пунктом перед `get_task_context`, вставить (английский текст тела скилла):
```markdown
   - **Subsystem prior (architectural map).** Call
     `get_subsystem_summaries(repo, branch, query="<task title>. <first lines of description>")`
     → top-k relevant subsystems by proximity (top-k vs all is server-side; PRI-167).
     Use the same `branch` as `search_codebase`. Fail-open: an empty list / a `(… недоступно)`
     note / an error is non-fatal — omit the `## Subsystems` brief section and note the gap.
```

- [ ] **Step 4: Добавить секцию в скелет brief (шаг 4)**

`plugin/skills/solve-task/SKILL.md` — в скелете brief (шаг 4, блок ```# Brief — …```), ПЕРЕД строкой `## Relevant code`, добавить:
```markdown
   ## Subsystems — ≤8 relevant subsystems, one line: «cluster_key — gist of summary». (omit if prior empty)
```

- [ ] **Step 5: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_ask_uses_summaries.py -q`
Expected: PASS (все, включая 2 новых)

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_ask_uses_summaries.py
git commit -m "feat(skills): solve-task подмешивает приор сводок подсистем (секция Subsystems) (PRI-161)"
```

---

### Task 8: Deliverable — пример `.review.yml` + README

**Files:**
- Modify: `.review.yml` (корень репозитория)
- Modify: `README.md` (раздел про `.review.yml`/политику)
- Test: `tests/test_review_yml_example.py`

**Interfaces:** — (документация; функциональных интерфейсов нет)

- [ ] **Step 1: Написать падающий guard-тест**

`tests/test_review_yml_example.py`:
```python
import pathlib

import yaml


def test_example_review_yml_documents_new_keys():
    root = pathlib.Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / ".review.yml").read_text(encoding="utf-8")) or {}
    assert "ignore" in (data.get("paths") or {}), "paths.ignore должен быть в примере"
    assert "summary_cluster_depth_overrides" in data
    assert "summary_topk_threshold" in data
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/test_review_yml_example.py -q`
Expected: FAIL (ключей ещё нет в `.review.yml`)

- [ ] **Step 3: Дополнить `.review.yml`**

`.review.yml` — добавить после блока `task_board` (русские комментарии):
```yaml

# --- Политика контекст-слоя (PRI-161) ---

# Игнорируемые пути: НЕ индексируются (вектора + граф) и не комментируются.
# Паттерны fnmatch. Голое имя папки ловит всё поддерево; '<dir>/*' — то же явно.
# Берётся из целевой/индексируемой ветки (PR не может ослабить своё ревью).
paths:
  ignore:
    - vendor            # папка и всё внутри
    - migrations/*
    - "*.gen.py"

# Глубина кластеризации сводок подсистем (число сегментов пути). Дефолт из env (2).
summary_cluster_depth: 2

# Точечные переопределения глубины по поддеревьям (longest-prefix-match по сегментам):
summary_cluster_depth_overrides:
  reviewer/index: 3     # эту подсистему дробить глубже
  vendor: 1             # держать крупно

# Порог масштаба приора сводок: число сводок > порога → ANN top-k по близости, иначе все.
summary_topk_threshold: 20
```

- [ ] **Step 4: Дополнить README**

`README.md` — найти раздел, описывающий `.review.yml`/политику ревью (поиск по строке `.review.yml`). Добавить в него подраздел:
```markdown
#### Контекст-слой (PRI-161)

- `paths.ignore` — список fnmatch-паттернов; перечисленные пути **не индексируются**
  (вектора и граф) и не комментируются. Голое имя папки (`vendor`) ловит всё поддерево;
  `vendor/*` — то же явно. Экономит квоту Voyage и убирает шум.
- `summary_cluster_depth_overrides` — карта `префикс → depth` для точечной глубины
  кластеризации сводок (longest-prefix-match по сегментам пути); дополняет глобальный
  `summary_cluster_depth`. Смена глубины пересобирает затронутые сводки.

Все ключи берутся из `.review.yml` целевой ветки. Пример — в корневом `.review.yml`.
```

- [ ] **Step 5: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/test_review_yml_example.py -q`
Expected: PASS

- [ ] **Step 6: Финальная проверка всего набора**

Run: `.venv/bin/pytest -q --ignore=tests/web`
Expected: PASS (вся unit-сюита)
Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: чисто на затронутых файлах (repo-wide clean не требуется)

- [ ] **Step 7: Коммит**

```bash
git add .review.yml README.md tests/test_review_yml_example.py
git commit -m "docs: пример .review.yml + README для ignore-на-входе и per-prefix depth (PRI-161)"
```

---

## Примечания для исполнителя

- **Порядок обязателен:** Task 1 (матчер) → 2 (freshness) → 3 (cli) → 4 (prepare) → 5 (summaries+policy) → 6 (service) → 7 (solve-task) → 8 (deliverable). Tasks 1-4 — ignore-цепочка; 5-6 — depth; 7 — приор; 8 — финал.
- **tests/web** всегда исключать (`--ignore=tests/web`) — предсуществующий `fastapi` ImportError.
- Если интеграционные тесты сводок (`tests/index/test_summary_store.py`) понадобятся — это `-m integration`, нужны Postgres :5433 / Neo4j.
- Сигнатуры конструкторов (`ReviewService`, `MCPReviewService`) в тестах Task 4/6 свериться с фактическими (`tests/mcp/test_server.py`, `tests/mcp/test_service.py`) — подгонять инстанцирование, не меняя проверяемого поведения.

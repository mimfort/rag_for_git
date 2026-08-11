# PRI-229 — ограничение объёма ответа `list_subsystem_clusters`: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сократить ответ MCP-тула `list_subsystem_clusters` со 107 КБ до единиц килобайт — сжатым режимом без file-level payload, детерминированной пагинацией и устранением дублирования путей в полном формате — и перевести скилл `summarize-subsystems` на новый контракт.

**Architecture:** Все изменения кода локализованы в двух местах: сервисный метод `MCPReviewService.list_subsystem_clusters` (`reviewer/mcp/service.py:1717-1832`) и его тул-обёртка (`reviewer/entrypoints/mcp_server.py:308-327`). Порядок вычислений внутри метода становится явным конвейером: резолв → кластеризация → дельты → `cap`/`deferred` → сортировка → срез страницы → сериализация. Всё, что считается по полному множеству кластеров (`deferred`, `deferred_files`, `orphans`, `layout_token`, `depth`, `total_clusters`), вычисляется **до** среза. Потребитель — промпт-скилл `plugin/skills/summarize-subsystems/SKILL.md` — обновляется последним.

**Tech Stack:** Python 3.11+, FastMCP (`reviewer/entrypoints/mcp_server.py`), pytest с `unittest.mock.MagicMock` (без Postgres/Neo4j — все тесты этой области юнитовые на фейках), ruff.

**Спека:** `docs/superpowers/specs/2026-08-11-pri-229-list-subsystem-clusters-payload-design.md`
**Бриф:** `docs/superpowers/briefs/2026-08-11-PRI-229-list-subsystem-clusters-payload-limit.md`
**Ветка:** `feat/pri-229-list-clusters-payload` (уже создана, спека и бриф закоммичены)

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения. Докстринги тулов MCP — тоже русские (как у соседних тулов сводок в `mcp_server.py`).
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, никаких упоминаний Claude).
- Тесты запускать через `.venv/bin/pytest` (не голый `pytest`).
- Все тесты этого плана — **unit**, без Postgres/Neo4j/сети. Маркер `@pytest.mark.integration` не ставить.
- Обязательный порядок задач: Task 1–2 (PRI-230 + PRI-231, аддитивно) → Task 3 (PRI-232, намеренное изменение полного формата) → Task 4–5 (PRI-233).
- Полный формат остаётся поведением по умолчанию: `compact=False`, `offset=0`, `limit=None`.
- Счётчики сжатого формата называются `added`/`changed`/`removed`/`moved` — **без суффикса `_files`**. Это защита от того, чтобы один ключ был то списком, то числом.
- Правка любого файла под `plugin/` меняет payload-digest манифестов → после Task 4 обязательно прогнать `.venv/bin/python scripts/update_codex_plugin_manifest.py`, иначе тесты установки станут красными.
- Ruff на `dev` не чист repo-wide; проверять только затронутые файлы: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp tests/skills`.

---

## File Structure

| Файл | Ответственность | Задачи |
|---|---|---|
| `reviewer/mcp/service.py` | `list_subsystem_clusters`: конвейер вычислений, сериализация кластера в двух форматах, срез страницы | 1, 2, 3 |
| `reviewer/entrypoints/mcp_server.py` | тул-обёртка: сигнатура + докстринг-контракт | 1, 2, 3 |
| `tests/mcp/test_subsystem_summaries.py` | unit-тесты обоих форматов, пагинации, дедупа путей | 1, 2, 3 |
| `plugin/skills/summarize-subsystems/SKILL.md` | промпт скилла: сжатое перечисление + цикл пагинации | 4 |
| `tests/skills/test_summarize_subsystems.py` | guard-тесты скилла | 4 |
| `CLAUDE.md`, `README.md`, `README.ru.md` | документация контракта | 5 |

Новых файлов не создаётся.

---

### Task 1: Сжатый режим ответа (PRI-230)

**Files:**
- Modify: `reviewer/mcp/service.py:1717-1832`
- Modify: `reviewer/entrypoints/mcp_server.py:308-327`
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: существующие `MCPReviewService._summary_state`, `_cluster_generation_flags`, `_summary_delta`, `_serialize_summary_delta`, `_resolve_repo_branch`; `reviewer.graph.summaries.Cluster` с полями `key`, `num_members`, `files`, `top_symbols`, `source_hash`.
- Produces: приватный статический метод
  `MCPReviewService._compact_cluster_record(cluster, *, stale: bool, bootstrap: bool, full_rebuild: bool, delta: FragmentDelta) -> dict`,
  возвращающий ровно ключи
  `{"cluster_key": str, "num_members": int, "source_hash": str, "stale": bool, "bootstrap": bool, "full_rebuild": bool, "reused_files": int, "added": int, "changed": int, "removed": int, "moved": int}`.
  Публичный параметр `compact: bool = False` у `MCPReviewService.list_subsystem_clusters` и у тула `list_subsystem_clusters`.

- [ ] **Step 1: Написать падающий тест на форму сжатой записи**

Добавить в конец `tests/mcp/test_subsystem_summaries.py`:

```python
def _one_cluster_components():
    """Фейк с одним кластером reviewer/index из двух файлов: один свежий (fragment
    сохранён), второй новый → delta.added непустая, delta.reused непустая."""
    from reviewer.graph.summaries import (
        Member,
        compute_file_fingerprints,
        compute_layout_token,
        compute_source_hash,
    )

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 1, "sk2"),
    ]
    c.graph = None
    members = [
        Member("reviewer/index/a.py#A", "reviewer/index/a.py", "h1", "sk1", 1),
        Member("reviewer/index/b.py#B", "reviewer/index/b.py", "h2", "sk2", 1),
    ]
    fingerprints = compute_file_fingerprints(members)
    source_hash = compute_source_hash(
        [("reviewer/index/a.py#A", "sk1"), ("reviewer/index/b.py#B", "sk2")]
    )
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": source_hash}
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_completed_layout.return_value = compute_layout_token(2, {})
    c.summary_store.get_fragments.return_value = [
        {
            "cluster_key": "reviewer/index",
            "path": "reviewer/index/a.py",
            "fingerprint": fingerprints["reviewer/index/a.py"],
            "summary": "A",
            "provenance": {},
        }
    ]
    return c


def test_compact_cluster_record_has_exact_keys():
    out = _svc(_one_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    [cluster] = out["clusters"]
    assert set(cluster) == {
        "cluster_key",
        "num_members",
        "source_hash",
        "stale",
        "bootstrap",
        "full_rebuild",
        "reused_files",
        "added",
        "changed",
        "removed",
        "moved",
    }
    assert cluster["cluster_key"] == "reviewer/index"
    assert cluster["num_members"] == 2
    assert cluster["added"] == 1
    assert cluster["reused_files"] == 1
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py::test_compact_cluster_record_has_exact_keys -v`
Expected: FAIL — `TypeError: list_subsystem_clusters() got an unexpected keyword argument 'compact'`

- [ ] **Step 3: Добавить `_compact_cluster_record` и параметр `compact` в сервис**

В `reviewer/mcp/service.py` сразу после `_serialize_summary_delta` (строка ~1715) добавить:

```python
    @staticmethod
    def _compact_cluster_record(
        cluster: "Cluster",
        *,
        stale: bool,
        bootstrap: bool,
        full_rebuild: bool,
        delta: FragmentDelta,
    ) -> dict:
        """Запись кластера без file-level payload: только метаданные и счётчики.

        Счётчики намеренно названы ``added``/``changed``/``removed``/``moved``
        (без суффикса ``_files``): одноимённые ключи полного формата — списки, и
        совпадение имён при разных типах ломало бы клиента, не проверившего режим.
        """
        return {
            "cluster_key": cluster.key,
            "num_members": cluster.num_members,
            "source_hash": cluster.source_hash,
            "stale": stale,
            "bootstrap": bootstrap,
            "full_rebuild": full_rebuild,
            "reused_files": len(delta.reused),
            "added": len(delta.added),
            "changed": len(delta.changed),
            "removed": len(delta.removed),
            "moved": len(delta.moved),
        }
```

Изменить сигнатуру `list_subsystem_clusters` (строка 1717):

```python
    def list_subsystem_clusters(self, repo: str, branch: str | None = None,
                                depth: int | None = None, min_size: int | None = None,
                                cap: int | None = None,
                                compact: bool = False) -> dict:
```

и в цикле сериализации (строки 1798-1822) заменить тело на:

```python
        clusters = []
        for cluster in state.clusters:
            if cluster.key in deferred_keys:
                continue
            delta = deltas[cluster.key]
            bootstrap, full_rebuild = generation_flags[cluster.key]
            if compact:
                clusters.append(
                    self._compact_cluster_record(
                        cluster,
                        stale=stale[cluster.key],
                        bootstrap=bootstrap,
                        full_rebuild=full_rebuild,
                        delta=delta,
                    )
                )
                continue
            serialized = self._serialize_summary_delta(
                delta, include_reused_content=False
            )
            clusters.append(
                {
                    "cluster_key": cluster.key,
                    "num_members": cluster.num_members,
                    "files": cluster.files,
                    "top_symbols": cluster.top_symbols,
                    "source_hash": cluster.source_hash,
                    "stale": stale[cluster.key],
                    "added_files": serialized["added_files"],
                    "changed_files": serialized["changed_files"],
                    "removed_files": serialized["removed_files"],
                    "moved_files": serialized["moved_files"],
                    "reused_files": len(delta.reused),
                    "bootstrap": bootstrap,
                    "full_rebuild": full_rebuild,
                }
            )
```

Убедиться, что `FragmentDelta` и `Cluster` доступны в области видимости файла (`FragmentDelta` уже используется в аннотации `_summary_delta`, `Cluster` — в строковой аннотации `"Cluster"`).

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py::test_compact_cluster_record_has_exact_keys -v`
Expected: PASS

- [ ] **Step 5: Написать падающий тест на отсутствие путей и fingerprint'ов**

Добавить в тот же файл:

```python
def test_compact_response_carries_no_paths_or_fingerprints():
    """Сжатая запись не должна содержать ни путей, ни 64-символьных hex-хешей,
    кроме разрешённых cluster_key и source_hash."""
    import re

    out = _svc(_one_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    [cluster] = out["clusters"]
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    for key, value in cluster.items():
        if key in ("cluster_key", "source_hash"):
            continue
        assert not isinstance(value, (list, dict)), f"{key} остался структурой"
        if isinstance(value, str):
            assert "/" not in value, f"{key} похоже на путь: {value}"
            assert not hex64.match(value), f"{key} похоже на fingerprint"
```

- [ ] **Step 6: Запустить — убедиться, что проходит (реализация уже готова)**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py::test_compact_response_carries_no_paths_or_fingerprints -v`
Expected: PASS

- [ ] **Step 7: Написать тест на равенство счётчиков длинам списков полного формата**

```python
def test_compact_counters_match_full_format_list_lengths():
    svc_full = _svc(_one_cluster_components())
    svc_compact = _svc(_one_cluster_components())
    full = svc_full.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    compact = svc_compact.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    by_key_full = {c["cluster_key"]: c for c in full["clusters"]}
    by_key_compact = {c["cluster_key"]: c for c in compact["clusters"]}
    assert by_key_full.keys() == by_key_compact.keys()
    for key, cf in by_key_full.items():
        cc = by_key_compact[key]
        assert cc["added"] == len(cf["added_files"])
        assert cc["changed"] == len(cf["changed_files"])
        assert cc["removed"] == len(cf["removed_files"])
        assert cc["moved"] == len(cf["moved_files"])
        assert cc["reused_files"] == cf["reused_files"]
        assert cc["num_members"] == cf["num_members"]
        assert cc["source_hash"] == cf["source_hash"]
        assert cc["stale"] == cf["stale"]
        assert cc["bootstrap"] == cf["bootstrap"]
        assert cc["full_rebuild"] == cf["full_rebuild"]


def test_full_format_is_default_and_top_level_fields_match_both_modes():
    svc_full = _svc(_one_cluster_components())
    svc_compact = _svc(_one_cluster_components())
    full = svc_full.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    compact = svc_compact.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    assert "files" in full["clusters"][0]           # дефолт — полный формат
    for field in ("branch", "depth", "layout_token", "depth_source",
                  "deferred", "deferred_files", "orphans"):
        assert full[field] == compact[field], field
```

- [ ] **Step 8: Запустить оба теста**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k "compact or full_format_is_default" -v`
Expected: PASS (4 теста)

- [ ] **Step 9: Пробросить `compact` в тул и обновить его докстринг**

В `reviewer/entrypoints/mcp_server.py` заменить блок строк 308-327 на:

```python
    @mcp.tool()
    def list_subsystem_clusters(repo: str, branch: str | None = None,
                                depth: int | None = None,
                                min_size: int | None = None,
                                cap: int | None = None,
                                compact: bool = False) -> dict:
        """Кластеризовать base-граф кода по путям модулей для скилла
        rag-reviewer:summarize-subsystems. Возвращает
        {branch, depth, layout_token, depth_source, deferred, deferred_files,
        orphans, clusters:[...]}, где layout_token — обязательная canonical
        identity default depth + normalized overrides для последующего verified
        prune. Без PR-сессии; branch по умолчанию — первичная отслеживаемая ветка.

        compact=False (по умолчанию) — полный формат: кластер содержит
        cluster_key, num_members, files, top_symbols (по центральности),
        source_hash, stale, bootstrap, full_rebuild, reused_files и file-level
        delta (added_files / changed_files / moved_files — объекты
        {path, fingerprint}; removed_files — пути).

        compact=True — сжатый формат без file-level payload: кластер содержит
        только cluster_key, num_members, source_hash, stale, bootstrap,
        full_rebuild, reused_files и числовые счётчики added / changed /
        removed / moved (без суффикса _files — это числа, а не списки).
        Ни путей, ни fingerprint'ов, ни top_symbols. Размер ответа растёт по
        числу кластеров, а не файлов; детализация по кластеру — через
        get_subsystem_summary_work.

        cap (по умолчанию — env SUMMARY_REBUILD_CAP; None/0 = без ограничений)
        отбрасывает наименее приоритетные stale-кластеры за один проход: сначала
        кластеры без сводки, затем с наиболее старым updated_at. Отложенные
        кластеры не попадают в clusters, но учитываются в deferred — количестве
        задержанных этим проходом кластеров."""
        return service.list_subsystem_clusters(
            repo, branch, depth, min_size, cap, compact
        )
```

- [ ] **Step 10: Прогнать весь файл тестов сводок и линт**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py tests/mcp/test_summary_depth_overrides.py tests/mcp/test_server.py -q`
Expected: PASS, регрессий нет

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp`
Expected: чисто

- [ ] **Step 11: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): сжатый режим ответа list_subsystem_clusters без file-level payload"
```

---

### Task 2: Детерминированная пагинация (PRI-231)

**Files:**
- Modify: `reviewer/mcp/service.py` (`list_subsystem_clusters`, включая два ранних возврата на строках ~1728 и ~1744)
- Modify: `reviewer/entrypoints/mcp_server.py` (тул `list_subsystem_clusters`)
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: `MCPReviewService._compact_cluster_record` и параметр `compact` из Task 1.
- Produces: параметры `offset: int = 0` и `limit: int | None = None` у сервисного метода и тула; новые верхнеуровневые поля ответа `total_clusters: int`, `offset: int`, `limit: int | None`, `has_more: bool` — присутствуют в обоих форматах и в обоих ранних возвратах с `note`.

**Важно:** `reviewer/graph/summaries.py:153` уже строит кластеры через `sorted(groups.items())`, то есть порядок по `cluster_key` детерминирован сегодня. Задача — зафиксировать инвариант явной сортировкой на сериализации и тестом, не меняя наблюдаемый порядок.

- [ ] **Step 1: Написать падающий тест на полный обход страницами**

Добавить в `tests/mcp/test_subsystem_summaries.py`:

```python
def _four_cluster_components():
    """Фейк с четырьмя кластерами (d/x, b/x, a/x, c/x) — порядок членов намеренно
    не отсортирован, чтобы поймать зависимость выдачи от порядка обхода."""
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("d/x/d.py", "D", "h4", 1, "sk4"),
        ("b/x/b.py", "B", "h2", 1, "sk2"),
        ("a/x/a.py", "A", "h1", 1, "sk1"),
        ("c/x/c.py", "C", "h3", 1, "sk3"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []
    return c


def test_pagination_full_walk_equals_unpaginated_call():
    unpaged = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True
    )
    expected = [c["cluster_key"] for c in unpaged["clusters"]]

    walked, offset, pages = [], 0, 0
    while True:
        page = _svc(_four_cluster_components()).list_subsystem_clusters(
            "o/n", "dev", depth=2, min_size=1, cap=0, compact=True,
            offset=offset, limit=2,
        )
        walked.extend(c["cluster_key"] for c in page["clusters"])
        pages += 1
        if not page["has_more"]:
            break
        offset += 2
        assert pages < 10, "пагинация не сходится"

    assert walked == expected
    assert len(walked) == len(set(walked)), "дубли при обходе страницами"
    assert unpaged["total_clusters"] == len(expected)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py::test_pagination_full_walk_equals_unpaginated_call -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'offset'`

- [ ] **Step 3: Реализовать пагинацию в сервисе**

В `reviewer/mcp/service.py` изменить сигнатуру:

```python
    def list_subsystem_clusters(self, repo: str, branch: str | None = None,
                                depth: int | None = None, min_size: int | None = None,
                                cap: int | None = None,
                                compact: bool = False,
                                offset: int = 0,
                                limit: int | None = None) -> dict:
```

В начале тела метода, до резолва, нормализовать аргументы:

```python
        # Мягкая нормализация: тул вызывает LLM, падение на кривом аргументе
        # хуже, чем предсказуемое приведение. limit<=0 == «без лимита».
        page_offset = max(0, int(offset))
        page_limit = int(limit) if limit is not None and int(limit) > 0 else None
```

Оба ранних возврата дополнить полями пагинации. Возврат при нерезолвленной ветке (строка ~1728):

```python
            return {
                "branch": branch or "",
                "deferred": 0,
                "deferred_files": 0,
                "total_clusters": 0,
                "offset": page_offset,
                "limit": page_limit,
                "has_more": False,
                "clusters": [],
                "note": rb,
            }
```

Возврат при пустом индексе (строка ~1744) — аналогично, с `"branch": resolved` и прежним `note`.

Заменить цикл сериализации на конвейер «полный набор → сортировка → срез → сериализация»:

```python
        selected = sorted(
            (cluster for cluster in state.clusters
             if cluster.key not in deferred_keys),
            key=lambda cluster: cluster.key,
        )
        total_clusters = len(selected)
        page_end = (
            total_clusters if page_limit is None else page_offset + page_limit
        )
        page = selected[page_offset:page_end]
        has_more = page_end < total_clusters

        clusters = []
        for cluster in page:
            ...   # тело из Task 1 без строки `if cluster.key in deferred_keys: continue`
```

и верхнеуровневый возврат:

```python
        return {
            "branch": resolved,
            "depth": state.depth,
            "layout_token": state.layout_token,
            "depth_source": state.depth_source,
            "deferred": len(deferred_keys),
            "deferred_files": deferred_files,
            "orphans": orphans,
            "total_clusters": total_clusters,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": has_more,
            "clusters": clusters,
        }
```

`deferred_keys`, `deferred_files`, `orphans`, `state.layout_token` считаются выше по коду по полному множеству — не переносить их вычисление внутрь среза.

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py::test_pagination_full_walk_equals_unpaginated_call -v`
Expected: PASS

- [ ] **Step 5: Написать тесты границ и инвариантов страниц**

```python
def test_pagination_order_is_reproducible_and_sorted_by_cluster_key():
    first = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True
    )
    second = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True
    )
    keys = [c["cluster_key"] for c in first["clusters"]]
    assert keys == [c["cluster_key"] for c in second["clusters"]]
    assert keys == sorted(keys)


def test_pagination_offset_beyond_set_returns_empty_page():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True,
        offset=99, limit=2,
    )
    assert out["clusters"] == []
    assert out["has_more"] is False
    assert out["total_clusters"] == 4


def test_pagination_limit_larger_than_set_returns_single_page():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True, limit=100
    )
    assert len(out["clusters"]) == 4
    assert out["has_more"] is False


def test_pagination_normalizes_negative_offset_and_nonpositive_limit():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True,
        offset=-5, limit=0,
    )
    assert out["offset"] == 0
    assert out["limit"] is None
    assert len(out["clusters"]) == 4


def test_global_fields_identical_on_every_page():
    """deferred / deferred_files / orphans / layout_token / total_clusters
    считаются по полному множеству и не зависят от страницы."""
    globals_fields = ("deferred", "deferred_files", "orphans",
                      "layout_token", "depth", "depth_source", "total_clusters")
    unpaged = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=2, compact=True
    )
    for offset in (0, 1, 2):
        page = _svc(_four_cluster_components()).list_subsystem_clusters(
            "o/n", "dev", depth=2, min_size=1, cap=2, compact=True,
            offset=offset, limit=1,
        )
        for field in globals_fields:
            assert page[field] == unpaged[field], f"{field} на offset={offset}"


def test_pagination_works_in_full_format_too():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, offset=1, limit=2
    )
    assert [c["cluster_key"] for c in out["clusters"]] == ["b/x", "c/x"]
    assert out["has_more"] is True
    assert "files" in out["clusters"][0]


def test_empty_index_note_carries_pagination_fields():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    c.graph = None
    out = _svc(c).list_subsystem_clusters("o/n", "dev")
    assert out["clusters"] == []
    assert out["total_clusters"] == 0
    assert out["has_more"] is False
    assert "note" in out
```

- [ ] **Step 6: Запустить тесты пагинации**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k "pagination or global_fields or empty_index" -v`
Expected: PASS

- [ ] **Step 7: Пробросить `offset`/`limit` в тул**

В `reviewer/entrypoints/mcp_server.py` расширить сигнатуру тула:

```python
    def list_subsystem_clusters(repo: str, branch: str | None = None,
                                depth: int | None = None,
                                min_size: int | None = None,
                                cap: int | None = None,
                                compact: bool = False,
                                offset: int = 0,
                                limit: int | None = None) -> dict:
```

и вызов: `return service.list_subsystem_clusters(repo, branch, depth, min_size, cap, compact, offset, limit)`.

В докстринг тула добавить абзац перед абзацем про `cap`:

```
        Пагинация: offset (по умолчанию 0) и limit (по умолчанию None = все
        кластеры). Порядок кластеров детерминирован — сортировка по cluster_key.
        Ответ содержит total_clusters (число не-deferred кластеров во всём
        наборе), применённые offset / limit и has_more. Поля branch, depth,
        layout_token, depth_source, deferred, deferred_files, orphans и
        total_clusters считаются по полному множеству кластеров и одинаковы на
        каждой странице. offset за границей набора возвращает пустой clusters
        без ошибки; offset<0 приводится к 0, limit<=0 трактуется как «без
        лимита». Пагинация не является override'ом и не делает проход частичным.
```

- [ ] **Step 8: Прогнать всю область и линт**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp`
Expected: чисто

- [ ] **Step 9: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): детерминированная пагинация кластеров в list_subsystem_clusters"
```

---

### Task 3: Дедупликация путей в полном формате (PRI-232)

**Files:**
- Modify: `reviewer/mcp/service.py` (ветка полного формата в `list_subsystem_clusters`)
- Modify: `reviewer/entrypoints/mcp_server.py` (докстринг тула)
- Test: `tests/mcp/test_subsystem_summaries.py` (в т.ч. правка существующего `test_list_subsystem_clusters_adds_file_delta_without_changing_old_fields`)

**Interfaces:**
- Consumes: `_serialize_summary_delta` (структура `added_files`/`changed_files`/`moved_files` — списки объектов с ключом `path`; `removed_files` — список строк).
- Produces: изменённая семантика поля `files` полного формата — только пути, отсутствующие в `added_files`/`changed_files`/`moved_files`. Инвариант: полный состав кластера = `files ∪ added_files ∪ changed_files ∪ moved_files`.

**Это намеренное изменение контракта полного формата.** Оно допустимо только после Task 1 и Task 2, которые обязаны быть чисто аддитивными.

- [ ] **Step 1: Написать падающий тест на отсутствие пересечения**

```python
def test_full_format_files_do_not_repeat_delta_paths():
    out = _svc(_one_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1
    )
    [cluster] = out["clusters"]
    delta_paths = {
        item["path"]
        for key in ("added_files", "changed_files", "moved_files")
        for item in cluster[key]
    }
    assert delta_paths, "фикстура должна давать непустую дельту"
    assert not (set(cluster["files"]) & delta_paths), "путь продублирован"
    # полный состав кластера восстановим из ответа
    assert set(cluster["files"]) | delta_paths == {
        "reviewer/index/a.py",
        "reviewer/index/b.py",
    }


def test_files_and_delta_never_overlap_across_all_clusters():
    """Инвариант держится на всех кластерах, включая те, где дельта покрывает
    весь состав (bootstrap/full_rebuild) — там files оказывается пустым."""
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0
    )
    assert out["clusters"], "фикстура должна давать кластеры"
    for cluster in out["clusters"]:
        delta_paths = {
            item["path"]
            for key in ("added_files", "changed_files", "moved_files")
            for item in cluster[key]
        }
        assert not (set(cluster["files"]) & delta_paths)
        # состав кластера восстановим целиком
        assert len(set(cluster["files"]) | delta_paths) == cluster["num_members"]
```

Примечание для исполнителя: в этой фикстуре сохранённых сводок нет
(`get_source_hashes` → `{}`), поэтому все файлы попадают в дельту и `files`
становится пустым. Если фактические флаги `bootstrap`/`full_rebuild` окажутся
иными — тест это не проверяет и не сломается: он утверждает только инвариант
непересечения и восстановимости состава.

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k "files_do_not_repeat or never_overlap" -v`
Expected: FAIL — `assert not (set(cluster["files"]) & delta_paths)` не выполняется (сейчас `files` содержит все пути)

- [ ] **Step 3: Реализовать дедуп в ветке полного формата**

В `reviewer/mcp/service.py`, в ветке полного формата цикла сериализации, перед сборкой словаря добавить:

```python
            serialized = self._serialize_summary_delta(
                delta, include_reused_content=False
            )
            # PRI-232: пути delta-списков не дублируются в files — там остаются
            # только неизменённые файлы. Полный состав кластера восстановим как
            # files ∪ added_files ∪ changed_files ∪ moved_files (removed_files в
            # состав уже не входят).
            delta_paths = {
                item["path"]
                for key in ("added_files", "changed_files", "moved_files")
                for item in serialized[key]
            }
            unchanged_files = [
                path for path in cluster.files if path not in delta_paths
            ]
```

и в словаре заменить `"files": cluster.files,` на `"files": unchanged_files,`.

Обновить докстринг сервисного метода `list_subsystem_clusters`, добавив предложение:

```
        В полном формате ``files`` содержит только неизменённые файлы: пути из
        added_files/changed_files/moved_files в нём не повторяются, полный
        состав кластера = files ∪ этих трёх списков (PRI-232).
```

- [ ] **Step 4: Запустить новые тесты**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k "files_do_not_repeat or never_overlap" -v`
Expected: PASS

- [ ] **Step 5: Обновить существующий guard-тест полного формата**

В `tests/mcp/test_subsystem_summaries.py::test_list_subsystem_clusters_adds_file_delta_without_changing_old_fields` фикстура даёт единственный файл, у которого fragment сохранён и совпадает → он попадает в `reused`, а не в дельту, поэтому `files` остаётся `["reviewer/index/a.py"]`. Проверить прогоном; если тест упал, привести ожидание к новому контракту (`files` = неизменённые файлы) и добавить в его конец строку:

```python
    assert not (
        set(cluster["files"])
        & {
            item["path"]
            for key in ("added_files", "changed_files", "moved_files")
            for item in cluster[key]
        }
    )
```

Прочие тесты файла, читающие `files`, найти командой:

Run: `grep -n '"files"' tests/mcp/test_subsystem_summaries.py tests/mcp/test_summary_depth_overrides.py`

и привести к новому контракту.

- [ ] **Step 6: Обновить докстринг тула**

В `reviewer/entrypoints/mcp_server.py` в абзаце про `compact=False` заменить описание `files` на:

```
        compact=False (по умолчанию) — полный формат: кластер содержит
        cluster_key, num_members, files, top_symbols (по центральности),
        source_hash, stale, bootstrap, full_rebuild, reused_files и file-level
        delta (added_files / changed_files / moved_files — объекты
        {path, fingerprint}; removed_files — пути). files содержит ТОЛЬКО
        неизменённые файлы: пути из added_files / changed_files / moved_files в
        нём не повторяются. Полный состав кластера восстановим как
        files ∪ added_files ∪ changed_files ∪ moved_files (removed_files в
        состав уже не входят).
```

- [ ] **Step 7: Прогнать всю область и линт**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp`
Expected: чисто

- [ ] **Step 8: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp
git commit -m "fix(mcp): не дублировать пути кластера в files и delta-списках"
```

---

### Task 4: Перевод скилла summarize-subsystems на сжатое перечисление (PRI-233, часть 1)

**Files:**
- Modify: `plugin/skills/summarize-subsystems/SKILL.md:29-39` (шаг 2), `:41-48` (шаг 3), `:93-104` (шаг 6)
- Modify: `tests/skills/test_summarize_subsystems.py`
- Modify: манифесты плагина (генерируются скриптом, не руками)

**Interfaces:**
- Consumes: контракт тула из Task 1–3 — `compact: bool`, `offset: int`, `limit: int | None`, поля ответа `total_clusters`, `offset`, `limit`, `has_more`.
- Produces: обновлённый промпт скилла. Guard-тесты `tests/skills/test_summarize_subsystems.py` продолжают требовать наличия существующих уникальных фраз — их **нельзя удалять**: `"Ask the user which model tier to use for writing summaries"`, `"dispatch a subagent on the chosen"`, `"stale == true OR bootstrap == true"`, `"added_files + changed_files"`, `"one file-summary job"`, `"reused_fragments"`, `"ordered reused/moved/new fragment texts"`, `"composer must not call \`Read\`"`, `` `prune_subsystem_summaries(repo, branch, layout_token, expected_source_hashes)` ``, `"count the prune as raced/partial"`, `"SUMMARY_REBUILD_CAP"`, `"depth_source"`, `"SUMMARY_CLUSTER_DEPTH"`.

- [ ] **Step 1: Написать падающие guard-тесты**

Добавить в `tests/skills/test_summarize_subsystems.py`:

```python
def test_skill_lists_clusters_in_compact_mode_with_pagination():
    """Шаг 2 должен перечислять кластеры в сжатом режиме и обходить страницы."""
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "compact=True" in normalized, "перечисление не в сжатом режиме"
    assert "has_more" in text, "скилл не обходит страницы до конца"
    assert "offset" in text, "скилл не использует offset"
    assert "total_clusters" in text


def test_skill_gets_file_level_detail_from_summary_work_not_from_listing():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "get_subsystem_summary_work" in text
    assert "added_files + changed_files" in text
    assert "does not carry file-level" in normalized or \
        "no file-level" in normalized, (
            "скилл не объясняет, что file-level данные берутся не из перечисления"
        )


def test_skill_full_pass_requires_walking_every_page():
    """Пагинация не override: полный проход требует has_more == false."""
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "`has_more == false`" in normalized, (
        "определение полного прохода не учитывает обход всех страниц"
    )
    assert "pagination is not an override" in normalized.lower() or \
        "Pagination is not an override" in text
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/skills/test_summarize_subsystems.py -v`
Expected: 3 новых теста FAIL, остальные PASS

- [ ] **Step 3: Переписать шаг 2 SKILL.md**

Заменить пункт 2 (строки 29-39) на:

```markdown
2. **List clusters (compact, paginated).** Walk every page:
   call `list_subsystem_clusters(repo, branch, compact=True, limit=100)`, then repeat with
   `offset += 100` while the response has `has_more == true`. Stop only when `has_more == false` —
   a partial walk is not a full pass. Empty / `note` about an empty
   index → tell the user (in Russian) to run `rag-reviewer:sync-codebase` first, then stop.

   Every page carries the same whole-set fields: `depth` (the applied cluster depth),
   `layout_token` (server-owned identity of the effective default depth plus sorted per-prefix
   overrides), `depth_source` (`env` | `.review.yml` | `arg`), `deferred` (stale clusters held
   back this pass under the cost cap, env `SUMMARY_REBUILD_CAP`), `deferred_files` (their pending
   file jobs), `orphans` (stored summaries whose `cluster_key` is no longer a current cluster),
   and `total_clusters` (non-deferred clusters across the whole set, page-independent).

   The compact listing deliberately does **not carry file-level** data: each cluster gives
   `cluster_key`, `num_members`, `source_hash`, `stale`, `bootstrap`, `full_rebuild`,
   `reused_files` and the numeric counters `added`, `changed`, `removed`, `moved` — no paths and
   no fingerprints. That keeps the listing O(clusters) instead of O(files). Per-cluster file
   detail comes from `get_subsystem_summary_work` in step 5.

   Accumulate across pages: `expected_source_hashes = {cluster_key: source_hash}` from every
   returned cluster, and the counter totals for the preflight. Save the `layout_token`; these
   exact list-snapshot values are required for finalization.
```

- [ ] **Step 4: Обновить шаг 3 (preflight) SKILL.md**

В пункте 3 заменить строку

```
   - how many clusters there are and at what path level — e.g. «depth=2 → 15 кластеров уровня
     `reviewer/index`» — sampling a few `cluster_key`s from `clusters`;
```

на

```
   - how many clusters there are (`total_clusters`) and at what path level — e.g. «depth=2 → 15
     кластеров уровня `reviewer/index`» — sampling a few `cluster_key`s from the accumulated pages;
```

и строку

```
   - how many are `stale` vs fresh, how many require `bootstrap`, plus `deferred` clusters and
     `deferred_files` (held back by the cap).
```

на

```
   - how many are `stale` vs fresh, how many require `bootstrap`, plus `deferred` clusters and
     `deferred_files` (held back by the cap). Compute these from the accumulated compact records —
     the `added`/`changed`/`removed`/`moved` counters are enough; never re-list in full format.
```

- [ ] **Step 5: Обновить шаг 6 (prune) SKILL.md**

В пункте 6 заменить первое предложение

```
6. **Prune orphaned summaries (only on a full pass).** If the pass was full — `deferred == 0`, you
   have `raced == 0`, and you did NOT pass an explicit `depth`/`cap` override (so `clusters` covered
   every current cluster) — call
```

на

```
6. **Prune orphaned summaries (only on a full pass).** If the pass was full — you walked every page
   to `has_more == false`, `deferred == 0`, you have `raced == 0`, and you did NOT pass an explicit
   `depth`/`cap` override (so the accumulated clusters covered every current cluster) — call
```

и в конце пункта, после предложения про partial pass, добавить:

```
   **Pagination is not an override:** paging through the listing with `offset`/`limit` still yields
   a full pass, as long as you walked to `has_more == false`.
```

- [ ] **Step 6: Запустить guard-тесты скилла**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (все, включая старые фразовые guard-тесты)

- [ ] **Step 7: Пересобрать манифесты плагина**

Правка любого файла под `plugin/` меняет payload-digest манифестов Codex.

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Then: `.venv/bin/pytest tests/test_ci_gates.py -q` и `.venv/bin/pytest -q -k "install or manifest"`
Expected: PASS

- [ ] **Step 8: Коммит**

```bash
git add plugin/ tests/skills/test_summarize_subsystems.py
git add -A   # манифесты, изменённые скриптом
git commit -m "feat(skills): summarize-subsystems перечисляет кластеры в сжатом режиме с пагинацией"
```

---

### Task 5: Документация и замер (PRI-233, часть 2)

**Files:**
- Modify: `CLAUDE.md` (раздел «Инкрементальные fragments сводок»)
- Modify: `README.md` (раздел `summarize-subsystems`, ~строка 630)
- Modify: `README.ru.md` (раздел `summarize-subsystems`, ~строка 636)

**Interfaces:**
- Consumes: финальный контракт тула из Task 1–3 и обновлённый скилл из Task 4.
- Produces: документация без изменений API. Кода не трогает — регрессий не создаёт.

- [ ] **Step 1: Снять замер «после»**

Run:

```bash
.venv/bin/python -c "
import json
from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.mcp.service import MCPReviewService
s=Settings(); c=build_components(s)
svc=MCPReviewService(s,c)
full=svc.list_subsystem_clusters('mimfort/rag_for_git','dev',cap=0)
comp=svc.list_subsystem_clusters('mimfort/rag_for_git','dev',cap=0,compact=True)
print('FULL',len(json.dumps(full,ensure_ascii=False).encode()))
print('COMPACT',len(json.dumps(comp,ensure_ascii=False).encode()))
print('CLUSTERS',len(comp['clusters']))
" 2>&1 | grep -E '^(FULL|COMPACT|CLUSTERS)'
```

Записать три числа — они идут в документацию и в комментарий к задаче PRI-233. Базовая точка «до»: **106 878 байт, 40 кластеров**.

- [ ] **Step 2: Обновить CLAUDE.md**

В разделе «Инкрементальные fragments сводок (`/summarize-subsystems`)» добавить предложение в конец абзаца:

```
  Перечисление кластеров идёт в **сжатом режиме с пагинацией** (`list_subsystem_clusters(...,
  compact=True, limit=N, offset=M)`): по кластеру только метаданные и числовые счётчики
  `added`/`changed`/`removed`/`moved` — без путей и fingerprint'ов, размер O(числа кластеров), а
  не файлов (на этом репозитории <ЗАМЕР_COMPACT> Б против 106 878 Б в полном формате). File-level
  детализация — через `get_subsystem_summary_work`. В полном формате `files` содержит только
  неизменённые файлы: пути delta-списков в нём не дублируются, полный состав =
  `files ∪ added_files ∪ changed_files ∪ moved_files`. Пагинация не считается override'ом —
  полный проход требует лишь дойти до `has_more == false`.
```

Подставить фактическое число из шага 1 вместо `<ЗАМЕР_COMPACT>`.

- [ ] **Step 3: Обновить README.md**

В разделе `### summarize-subsystems — GraphRAG subsystem summaries` добавить строку в список после `- **Result:** …`:

```
- **Payload:** the cluster listing runs in compact, paginated mode
  (`compact=True`, `offset`/`limit`): metadata plus `added`/`changed`/`removed`/`moved` counters,
  no paths and no fingerprints, so its size grows with the number of clusters rather than files
  (<ЗАМЕР_COMPACT> B vs 106 878 B in full format on this repository). Per-cluster file detail
  comes from `get_subsystem_summary_work`. In full format `files` lists only unchanged files —
  the delta lists are not repeated there.
```

- [ ] **Step 4: Обновить README.ru.md**

В разделе `### summarize-subsystems — GraphRAG summaries подсистем` добавить аналогичную строку по-русски:

```
- **Объём ответа:** перечисление кластеров идёт в сжатом режиме с пагинацией
  (`compact=True`, `offset`/`limit`): метаданные и счётчики `added`/`changed`/`removed`/`moved`,
  без путей и fingerprint'ов — размер растёт по числу кластеров, а не файлов
  (<ЗАМЕР_COMPACT> Б против 106 878 Б в полном формате на этом репозитории). Детализация по
  кластеру — через `get_subsystem_summary_work`. В полном формате `files` перечисляет только
  неизменённые файлы: пути delta-списков в нём не дублируются.
```

- [ ] **Step 5: Прогнать все unit-тесты и линт**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены дефолтным `addopts`)

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp tests/skills`
Expected: чисто

- [ ] **Step 6: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md
git commit -m "docs(mcp): контракт сжатого перечисления кластеров и дедупа путей"
```

---

## Приёмка

Сверка с критериями задач:

| Критерий | Задача плана |
|---|---|
| PRI-230.1 сжатый ответ без путей и fingerprint'ов | Task 1, Step 5 |
| PRI-230.2 размер по числу кластеров | Task 1, Step 3 + Task 5, Step 1 (замер) |
| PRI-230.3 полный формат — дефолт, не меняется (до PRI-232) | Task 1, Step 7 |
| PRI-230.4 верхнеуровневые поля неизменны в обоих форматах | Task 1, Step 7 |
| PRI-230.5 unit-тесты на оба формата | Task 1, Steps 1/5/7 |
| PRI-231.1 воспроизводимый порядок | Task 2, Step 5 |
| PRI-231.2 полный обход == одиночный вызов | Task 2, Step 1 |
| PRI-231.3 глобальные поля одинаковы на страницах | Task 2, Step 5 |
| PRI-231.4 offset за границей → пусто, без ошибки | Task 2, Step 5 |
| PRI-231.5 unit-тесты границ | Task 2, Step 5 |
| PRI-232.1 ни один путь не встречается дважды | Task 3, Step 1 |
| PRI-232.2 состав восстановим из ответа | Task 3, Step 1 |
| PRI-232.3 контракт в докстринге | Task 3, Steps 3/6 |
| PRI-232.4 потребители обновлены | Task 3, Step 5 |
| PRI-233.1 скилл проходит цикл без переполнения | Task 4 |
| PRI-233.2 замер до/после зафиксирован | Task 5, Step 1 |
| PRI-233.3 документация актуальна | Task 5, Steps 2-4 |
| PRI-233.4 guard-тесты зелёные | Task 4, Step 6 |

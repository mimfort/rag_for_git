# PRI-166 — depth кластеризации в .env + per-repo override + preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать глубину кластеризации сводок подсистем (`summary_cluster_depth`) видимой и управляемой: задокументировать env-настройку, дать per-repo override через `.review.yml`, показывать применяемый depth + превью кластеров с подтверждением в скилле `summarize-subsystems`, и вычищать осиротевшие при смене depth сводки.

**Architecture:** depth резолвится **server-side** единым хелпером `MCPReviewService._resolve_summary_depth` (env-дефолт → override из `.review.yml` ветки через `ReviewPolicy.load`, fail-soft). Оба тула (`list_subsystem_clusters`, `index_subsystem_summary`) используют его — это сохраняет инвариант: `cluster_key`/`source_hash` считаются на одном depth. Скилл эхо-ит depth/число/уровень кластеров из ответа `list_subsystem_clusters`, спрашивает подтверждение, и на полном прогоне зовёт новый тул `prune_subsystem_summaries`, который через `SummaryStore.delete_summaries_except` удаляет сводки вне текущего множества кластеров.

**Tech Stack:** Python 3.11–3.13, pydantic-settings (`Settings`), psycopg/psycopg_pool (Postgres/ParadeDB), FastMCP (MCP-сервер), pytest (+ маркер `integration`), PyYAML.

## Global Constraints

- Язык кода/комментариев/докстрингов/CLI — **русский**; код-идентификаторы и `path:line` — verbatim.
- Тело скиллов (`SKILL.md`) — на английском (токены), но скилл инструктирует отвечать пользователю по-русски.
- Коммиты — **Conventional Commits на русском, без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Пример: `feat(mcp): ...`, `fix(skills): ...`.
- Unit-тесты не дёргают внешние сервисы (GitHub/Voyage/Postgres/Neo4j) — всё на фейках/MagicMock. Тесты, требующие Postgres, помечаются `pytest.mark.integration`.
- Линт: `ruff check .`, line-length 100, target py311.
- Инвариант (НЕ нарушать): `list_subsystem_clusters` и `index_subsystem_summary` обязаны резолвить depth одинаково — иначе `cluster_key`/`source_hash` разойдутся и `member_node_ids` не сохранятся (`reviewer/mcp/service.py:477-484`).

---

### Task 1: ReviewPolicy.summary_cluster_depth + документация env

**Files:**
- Modify: `reviewer/policy/policy.py` (поле + `from_yaml`/`from_settings`/`load`)
- Modify: `reviewer/config/settings.py:71` (обогатить комментарий поля)
- Modify: `.env.example` (добавить `SUMMARY_CLUSTER_DEPTH`)
- Modify: `CLAUDE.md` (буллет в «Неочевидные факты»)
- Test: `tests/policy/test_policy.py`

**Interfaces:**
- Consumes: `Settings.summary_cluster_depth: int` (уже существует, дефолт 2).
- Produces: `ReviewPolicy.summary_cluster_depth: int`; `ReviewPolicy.load(settings, yaml_text)` и `ReviewPolicy.from_yaml(text)` читают ключ `summary_cluster_depth` из `.review.yml`; `ReviewPolicy.from_settings(settings)` берёт его из env.

- [ ] **Step 1: Написать падающий тест**

В `tests/policy/test_policy.py` добавить в конец файла:

```python
def test_summary_cluster_depth_env_default_and_yaml_override():
    s = Settings(_env_file=None)                 # env-дефолт summary_cluster_depth = 2
    p = ReviewPolicy.load(s, None)
    assert p.summary_cluster_depth == 2          # из env, YAML отсутствует

    p2 = ReviewPolicy.load(s, "summary_cluster_depth: 1")
    assert p2.summary_cluster_depth == 1         # .review.yml переопределяет env

    p3 = ReviewPolicy.load(s, "severity_threshold: high")
    assert p3.summary_cluster_depth == 2         # YAML без ключа не трогает env-дефолт


def test_summary_cluster_depth_from_yaml():
    p = ReviewPolicy.from_yaml("summary_cluster_depth: 3")
    assert p.summary_cluster_depth == 3
    assert ReviewPolicy.from_yaml(None).summary_cluster_depth == 2
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/policy/test_policy.py::test_summary_cluster_depth_env_default_and_yaml_override tests/policy/test_policy.py::test_summary_cluster_depth_from_yaml -q`
Expected: FAIL — `AttributeError: 'ReviewPolicy' object has no attribute 'summary_cluster_depth'` (или `TypeError` в конструкторе).

- [ ] **Step 3: Добавить поле в ReviewPolicy**

В `reviewer/policy/policy.py` после поля `grounding_max_distance` (строка 21) добавить:

```python
    summary_cluster_depth: int = 2                               # глубина пути кластера подсистемы; per-repo override .review.yml (PRI-166)
```

В `from_yaml` (внутри `return cls(...)`, после `grounding_max_distance=...`) добавить:

```python
            summary_cluster_depth=int(data.get("summary_cluster_depth", 2)),
```

В `from_settings` (внутри `return cls(...)`, после `grounding_max_distance=...`) добавить:

```python
            summary_cluster_depth=settings.summary_cluster_depth,
```

В `load` перед `return policy` добавить:

```python
        if "summary_cluster_depth" in data:
            policy.summary_cluster_depth = int(data["summary_cluster_depth"])
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/policy/test_policy.py -q`
Expected: PASS (все тесты policy, включая два новых).

- [ ] **Step 5: Обогатить комментарий поля Settings**

В `reviewer/config/settings.py` заменить строку 71:

```python
    summary_cluster_depth: int = 2   # глубина пути для кластера подсистемы (PRI-159)
```

на:

```python
    summary_cluster_depth: int = 2   # глубина пути кластера подсистемы (PRI-159); env
    # SUMMARY_CLUSTER_DEPTH; per-repo override в .review.yml; смена = полный пересбор сводок (PRI-166)
```

- [ ] **Step 6: Задокументировать в .env.example**

В `.env.example` после строки `GRAPH_BACKEND=auto ...` (в секции «Граф кода») добавить:

```
SUMMARY_CLUSTER_DEPTH=2            # глубина пути кластера подсистемы для /summarize-subsystems; per-repo override в .review.yml; смена = полный пересбор сводок
```

- [ ] **Step 7: Добавить буллет в CLAUDE.md**

В `CLAUDE.md` (секция «Неочевидные факты…») сразу после буллета:

```
- **`reviewer check`** проверяет готовность окружения (ключи, Postgres, Neo4j, GitHub) без трат квот Voyage.
```

добавить:

```
- **Глубина кластеризации сводок (`SUMMARY_CLUSTER_DEPTH`, дефолт 2)** — env-настройка деплоя для `/summarize-subsystems`: до скольких сегментов пути обрезается `cluster_key` подсистемы. Per-repo override — ключ `summary_cluster_depth` в `.review.yml` целевой ветки (резолвится server-side в `list_subsystem_clusters`/`index_subsystem_summary`). Смена depth меняет `cluster_key` → **полный пересбор всех сводок**; осиротевшие сводки старого depth вычищаются `prune_subsystem_summaries` на полном (uncapped) прогоне скилла (PRI-166).
```

- [ ] **Step 8: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/policy/policy.py reviewer/config/settings.py && .venv/bin/pytest tests/policy/test_policy.py -q`
Expected: ruff чисто по этим файлам; тесты PASS.

```bash
git add reviewer/policy/policy.py reviewer/config/settings.py .env.example CLAUDE.md tests/policy/test_policy.py
git commit -m "feat(policy): summary_cluster_depth — env-настройка + per-repo override через .review.yml (PRI-166)"
```

---

### Task 2: SummaryStore.delete_summaries_except

**Files:**
- Modify: `reviewer/index/summary_store.py` (новый метод)
- Test: `tests/index/test_summary_store.py` (integration)

**Interfaces:**
- Consumes: таблица `subsystem_summaries` (PK `(repo, branch, cluster_key)`).
- Produces: `SummaryStore.delete_summaries_except(repo: str, branch: str, keep_keys: list[str]) -> int` — удаляет строки repo/branch, чей `cluster_key` НЕ в `keep_keys`; возвращает число удалённых. Пустой `keep_keys` → удаляет все строки repo/branch.

- [ ] **Step 1: Написать падающий тест**

В `tests/index/test_summary_store.py` добавить в конец файла:

```python
def test_delete_summaries_except_prunes_orphans(store):
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "s", [], "h1")
    store.upsert_summary("t/t", "dev", "reviewer/graph", "B", "s", [], "h2")
    store.upsert_summary("t/t", "dev", "reviewer/old", "C", "s", [], "h3")
    pruned = store.delete_summaries_except("t/t", "dev", ["reviewer/index", "reviewer/graph"])
    assert pruned == 1                                   # удалён только reviewer/old
    assert set(store.get_source_hashes("t/t", "dev")) == {"reviewer/index", "reviewer/graph"}


def test_delete_summaries_except_empty_keep_deletes_all(store):
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "s", [], "h1")
    pruned = store.delete_summaries_except("t/t", "dev", [])
    assert pruned == 1
    assert store.get_source_hashes("t/t", "dev") == {}
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest -m integration tests/index/test_summary_store.py::test_delete_summaries_except_prunes_orphans -q`
Expected: FAIL — `AttributeError: 'SummaryStore' object has no attribute 'delete_summaries_except'`.
(Требуется поднятый Postgres: `docker compose up -d`.)

- [ ] **Step 3: Реализовать метод**

В `reviewer/index/summary_store.py` после `upsert_summary` (после строки 56) добавить:

```python
    def delete_summaries_except(self, repo: str, branch: str, keep_keys: list[str]) -> int:
        """Удалить сводки repo/branch, чей cluster_key НЕ в keep_keys; вернуть число удалённых.

        Пустой keep_keys → удаляет все сводки repo/branch (вызывающий гейтит на непустой base).
        Используется prune_subsystem_summaries для чистки осиротевших при смене depth сводок."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM subsystem_summaries "
                "WHERE repo=%s AND branch=%s AND NOT (cluster_key = ANY(%s))",
                (repo, branch, list(keep_keys)))
            conn.commit()
            return cur.rowcount
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest -m integration tests/index/test_summary_store.py -q`
Expected: PASS (включая два новых теста).

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/index/summary_store.py`
Expected: чисто.

```bash
git add reviewer/index/summary_store.py tests/index/test_summary_store.py
git commit -m "feat(index): SummaryStore.delete_summaries_except — чистка осиротевших сводок (PRI-166)"
```

---

### Task 3: MCP — _resolve_summary_depth + проброс в list/index + поля ответа

**Files:**
- Modify: `reviewer/mcp/service.py` (новый хелпер; правки `list_subsystem_clusters`, `index_subsystem_summary`)
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: `ReviewPolicy.load(settings, yaml_text)` → `.summary_cluster_depth` (Task 1); `self._review_service._create_vcs_provider(owner, name)` и `vcs.get_file_at_ref(path, ref) -> str | None`; `self._vcs_factory` (test-only).
- Produces: `MCPReviewService._resolve_summary_depth(repo: str, branch: str) -> tuple[int, str]` — `(depth, source)`, где `source ∈ {"env", ".review.yml"}`, fail-soft → `(settings.summary_cluster_depth, "env")`. `list_subsystem_clusters` success-ответ дополнен полями `depth: int`, `depth_source: str` (`env`|`.review.yml`|`arg`), `orphans: int`. `index_subsystem_summary` резолвит depth тем же хелпером.

- [ ] **Step 1: Написать падающие тесты**

В `tests/mcp/test_subsystem_summaries.py`:

(а) изолировать существующие тесты от резолва depth — заменить хелпер `_svc` на:

```python
def _svc(components) -> MCPReviewService:
    svc = MCPReviewService(_settings(), components)
    # изолируем резолв repo/ветки и depth от .env / сети
    svc._resolve_repo_branch = lambda repo, branch: ("o/n", "dev")
    svc._resolve_summary_depth = lambda repo, branch: (2, "env")
    return svc
```

(б) добавить новые тесты в конец файла:

```python
class _FakeVCS:
    def __init__(self, text):
        self._text = text
    def get_file_at_ref(self, path, ref):
        return self._text


def _svc_with_vcs(vcs_or_exc):
    """Сервис БЕЗ стаба _resolve_summary_depth — для проверки самого хелпера."""
    c = MagicMock()
    svc = MCPReviewService(_settings(), components=c)
    svc._resolve_repo_branch = lambda repo, branch: ("o/n", "dev")
    if isinstance(vcs_or_exc, Exception):
        def _factory(owner, name):
            raise vcs_or_exc
    else:
        def _factory(owner, name):
            return vcs_or_exc
    svc._vcs_factory = _factory
    return svc


def test_resolve_summary_depth_override_from_review_yml():
    svc = _svc_with_vcs(_FakeVCS("summary_cluster_depth: 3"))
    assert svc._resolve_summary_depth("o/n", "dev") == (3, ".review.yml")


def test_resolve_summary_depth_no_key_falls_back_to_env():
    svc = _svc_with_vcs(_FakeVCS("severity_threshold: high"))
    depth, source = svc._resolve_summary_depth("o/n", "dev")
    assert depth == svc.settings.summary_cluster_depth
    assert source == "env"


def test_resolve_summary_depth_failsoft_on_vcs_error():
    svc = _svc_with_vcs(RuntimeError("no token"))
    depth, source = svc._resolve_summary_depth("o/n", "dev")
    assert depth == svc.settings.summary_cluster_depth
    assert source == "env"


def test_list_subsystem_clusters_reports_depth_and_orphans():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    c.graph = None
    # хранится осиротевший ключ reviewer/old (его нет среди текущих кластеров) → orphans=1
    c.summary_store.get_source_hashes.return_value = {"reviewer/old": "x"}
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    assert out["depth"] == 2
    assert out["depth_source"] == "arg"           # передан явный depth
    assert out["orphans"] == 1


def test_list_subsystem_clusters_resolves_depth_when_not_given():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    svc = _svc(c)                                  # стаб _resolve_summary_depth → (2, "env")
    out = svc.list_subsystem_clusters("o/n", "dev")   # depth не передан
    assert out["depth"] == 2
    assert out["depth_source"] == "env"
    assert out["orphans"] == 0
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: FAIL — новые тесты падают (`AttributeError: ... '_resolve_summary_depth'` / `KeyError: 'depth'`).

- [ ] **Step 3: Добавить хелпер _resolve_summary_depth**

В `reviewer/mcp/service.py` добавить метод в класс `MCPReviewService` (рядом с `_resolve_repo_branch`, перед `search_codebase`):

```python
    def _resolve_summary_depth(self, repo: str, branch: str) -> tuple[int, str]:
        """Резолв глубины кластеризации сводок: env-дефолт → override из .review.yml ветки.

        repo уже нормализован (вызывается после _resolve_repo_branch). Fail-soft:
        нет токена/ветки/файла/кривой yml → (settings.summary_cluster_depth, "env").
        Внутренне созданный VCS-провайдер закрываем в finally (как get_pr_diff).
        source = ".review.yml", только если файл явно задаёт ключ summary_cluster_depth."""
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
                return default, "env"
            data = yaml.safe_load(text) or {}
            depth = ReviewPolicy.load(self.settings, text).summary_cluster_depth
            return depth, (".review.yml" if "summary_cluster_depth" in data else "env")
        except Exception:
            log.warning("_resolve_summary_depth: fail-soft → env-дефолт", exc_info=True)
            return default, "env"
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("_resolve_summary_depth: не удалось закрыть VCS", exc_info=True)
```

- [ ] **Step 4: Пробросить depth/depth_source/orphans в list_subsystem_clusters**

В `reviewer/mcp/service.py`, в `list_subsystem_clusters`, заменить блок построения кластеров (строки 442-445):

```python
        clusters = build_clusters(
            members, in_degree_fn,
            depth=depth or self.settings.summary_cluster_depth,
            min_size=min_size or 1)
```

на:

```python
        if depth is None:
            resolved_depth, depth_source = self._resolve_summary_depth(repo, resolved)
        else:
            resolved_depth, depth_source = depth, "arg"
        clusters = build_clusters(
            members, in_degree_fn, depth=resolved_depth, min_size=min_size or 1)
```

После строки `stale = {...}` (строка 447) добавить:

```python
        orphans = len(set(stored) - {c.key for c in clusters})
```

Заменить финальный `return` (строки 458-462):

```python
        return {"branch": resolved, "deferred": len(deferred_keys), "clusters": [
            {"cluster_key": c.key, "num_members": c.num_members, "files": c.files,
             "top_symbols": c.top_symbols, "source_hash": c.source_hash,
             "stale": stale[c.key]}
            for c in clusters if c.key not in deferred_keys]}
```

на:

```python
        return {"branch": resolved, "depth": resolved_depth, "depth_source": depth_source,
                "deferred": len(deferred_keys), "orphans": orphans, "clusters": [
            {"cluster_key": c.key, "num_members": c.num_members, "files": c.files,
             "top_symbols": c.top_symbols, "source_hash": c.source_hash,
             "stale": stale[c.key]}
            for c in clusters if c.key not in deferred_keys]}
```

(Ранние return-ы с `note` — пустой/ошибочный индекс, строки 429 и 433-434 — НЕ трогаем: скилл проверяет `note` первым и останавливается.)

- [ ] **Step 5: Использовать резолвнутый depth в index_subsystem_summary**

В `reviewer/mcp/service.py`, в `index_subsystem_summary`, заменить строку 480:

```python
        depth = self.settings.summary_cluster_depth
```

на:

```python
        depth, _ = self._resolve_summary_depth(repo, resolved)
```

- [ ] **Step 6: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: PASS (старые тесты — через стаб `_resolve_summary_depth`; новые — через `_vcs_factory`).

- [ ] **Step 7: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/mcp/service.py`
Expected: чисто.

```bash
git add reviewer/mcp/service.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): server-side резолв summary depth (.review.yml override) + depth/orphans в ответе кластеров (PRI-166)"
```

---

### Task 4: prune_subsystem_summaries — тул + регистрация

**Files:**
- Modify: `reviewer/mcp/service.py` (новый метод `prune_subsystem_summaries`)
- Modify: `reviewer/entrypoints/mcp_server.py` (регистрация тула)
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: `self._resolve_repo_branch`, `self._resolve_summary_depth` (Task 3); `self.components.store.list_base_members(repo, branch)`; `self.components.summary_store.delete_summaries_except(repo, branch, keep_keys)` (Task 2); `reviewer.graph.summaries.cluster_key`.
- Produces: `MCPReviewService.prune_subsystem_summaries(repo: str, branch: str | None = None) -> dict` → `{"pruned": int, "kept": int}` (или `{"pruned": 0, "kept": 0, "note": ...}` на пустом base/ошибке резолва). MCP-тул `prune_subsystem_summaries` зарегистрирован в сервере.

- [ ] **Step 1: Написать падающие тесты**

В `tests/mcp/test_subsystem_summaries.py` добавить:

```python
def test_prune_subsystem_summaries_rederives_keys_and_prunes():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"),
        ("b/y/g.py", "G", "h", 1, "skg"),
    ]
    c.summary_store.delete_summaries_except.return_value = 2
    svc = _svc(c)                                  # стаб _resolve_summary_depth → (2, "env")
    out = svc.prune_subsystem_summaries("o/n", "dev")
    assert out == {"pruned": 2, "kept": 2}
    # keep_keys пере-выведены на depth=2 и отсортированы
    args = c.summary_store.delete_summaries_except.call_args.args
    assert args[0] == "o/n" and args[1] == "dev"
    assert args[2] == ["a/x", "b/y"]


def test_prune_subsystem_summaries_empty_base_is_noop():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    svc = _svc(c)
    out = svc.prune_subsystem_summaries("o/n", "dev")
    assert out["pruned"] == 0
    assert "note" in out
    c.summary_store.delete_summaries_except.assert_not_called()   # base пуст → не вайпаем
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py::test_prune_subsystem_summaries_rederives_keys_and_prunes tests/mcp/test_subsystem_summaries.py::test_prune_subsystem_summaries_empty_base_is_noop -q`
Expected: FAIL — `AttributeError: ... 'prune_subsystem_summaries'`.

- [ ] **Step 3: Реализовать метод**

В `reviewer/mcp/service.py` добавить метод после `get_subsystem_summaries` (после строки 503):

```python
    def prune_subsystem_summaries(self, repo: str, branch: str | None = None) -> dict:
        """Удалить сводки подсистем, осиротевшие после смены depth или удаления модулей.

        Пере-выводит текущие cluster_keys из base-состава на резолвнутом depth и
        удаляет сводки вне этого множества. Вызывать ТОЛЬКО на полном (uncapped)
        прогоне скилла — иначе отложенные капом кластеры будут приняты за осиротевшие.
        Пустой base → no-op (не вайпать на транзиентной пустоте). Fail-soft."""
        from reviewer.graph.summaries import cluster_key as cluster_key_of
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"pruned": 0, "kept": 0, "note": rb}
        repo, resolved = rb
        depth, _ = self._resolve_summary_depth(repo, resolved)
        raw = self.components.store.list_base_members(repo, resolved)
        if not raw:
            return {"pruned": 0, "kept": 0, "note": "(base-индекс пуст — purge пропущен)"}
        keep_keys = sorted({cluster_key_of(p, depth) for p, _s, _h, _sl, _sk in raw})
        pruned = self.components.summary_store.delete_summaries_except(repo, resolved, keep_keys)
        return {"pruned": pruned, "kept": len(keep_keys)}
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: PASS.

- [ ] **Step 5: Зарегистрировать тул в MCP-сервере**

В `reviewer/entrypoints/mcp_server.py` после блока `get_subsystem_summaries` (после строки 219) добавить:

```python
    @mcp.tool()
    def prune_subsystem_summaries(repo: str, branch: str | None = None) -> dict:
        """Prune subsystem summaries orphaned by a depth change or removed modules.
        Re-derives current cluster_keys from the base index at the resolved depth and
        deletes summaries outside that set. Call ONLY after a full (uncapped) pass of
        /reviewer_summarize-subsystems — deferred clusters are not orphans. Empty base
        → no-op. Returns {pruned, kept}. No PR session; branch defaults to primary."""
        return service.prune_subsystem_summaries(repo, branch)
```

- [ ] **Step 6: Проверить регистрацию тула (server-тест)**

Открыть `tests/mcp/test_server.py`, найти тест, перечисляющий имена зарегистрированных тулов (поиск по `list_subsystem_clusters` или `get_subsystem_summaries`). Если такой тест с явным списком имён есть — добавить `"prune_subsystem_summaries"` в ожидаемый набор.

Run: `.venv/bin/pytest tests/mcp/test_server.py -q`
Expected: PASS (после правки списка, если он есть; иначе тест и так зелёный).

- [ ] **Step 7: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py && .venv/bin/pytest tests/mcp -q`
Expected: чисто; все MCP-тесты PASS.

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_subsystem_summaries.py tests/mcp/test_server.py
git commit -m "feat(mcp): тул prune_subsystem_summaries — чистка осиротевших сводок на полном прогоне (PRI-166)"
```

---

### Task 5: SKILL.md preflight + prune-шаг + guard-тесты

**Files:**
- Modify: `plugin/skills/summarize-subsystems/SKILL.md` (секция `## Pipeline`)
- Test: `tests/skills/test_summarize_subsystems.py`

**Interfaces:**
- Consumes: ответ `list_subsystem_clusters` с полями `depth`/`depth_source`/`deferred`/`orphans`/`clusters` (Task 3); тул `prune_subsystem_summaries` (Task 4).
- Produces: обновлённый пайплайн скилла (preflight-эхо + подтверждение + prune); новые guard-тесты.

- [ ] **Step 1: Написать падающие guard-тесты**

В `tests/skills/test_summarize_subsystems.py` добавить:

```python
def test_skill_preflight_echoes_depth_and_confirms():
    text = SKILL.read_text(encoding="utf-8")
    assert "depth_source" in text, "preflight не эхо-ит depth_source"
    assert "SUMMARY_CLUSTER_DEPTH" in text, "preflight не упоминает env-настройку depth"
    assert "confirm" in text.lower(), "preflight не просит подтверждения перед прогоном"
    assert "full rebuild" in text.lower(), "нет предупреждения о полном пересборе при смене depth"


def test_skill_prunes_orphans_on_full_pass():
    text = SKILL.read_text(encoding="utf-8")
    assert "prune_subsystem_summaries" in text, "скилл не вызывает prune на полном прогоне"
    assert "orphan" in text.lower(), "скилл не упоминает осиротевшие сводки"
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/skills/test_summarize_subsystems.py::test_skill_preflight_echoes_depth_and_confirms tests/skills/test_summarize_subsystems.py::test_skill_prunes_orphans_on_full_pass -q`
Expected: FAIL (строк ещё нет в SKILL.md).

- [ ] **Step 3: Переписать секцию Pipeline в SKILL.md**

В `plugin/skills/summarize-subsystems/SKILL.md` заменить весь блок `## Pipeline` (строки 21-54, от `## Pipeline` до строки перед `## Grounding (hard rule)`) на:

````markdown
## Pipeline

1. **Resolve repo/branch.**

<!-- include: _common/branch-selection.md -->

2. **List clusters.** Call `list_subsystem_clusters(repo, branch)`. Empty / `note` about an empty
   index → tell the user (in Russian) to run `/reviewer_sync-codebase` first, then stop. The response
   carries `depth` (the applied cluster depth), `depth_source` (`env` | `.review.yml` | `arg`),
   `deferred` (stale clusters held back this pass under the cost cap, env `SUMMARY_REBUILD_CAP`),
   `orphans` (stored summaries whose `cluster_key` is no longer a current cluster), and the
   (already cap-capped) `clusters`.

3. **Preflight — echo the applied depth and ask for confirmation (gate the run).** BEFORE summarizing,
   show the user (in Russian):
   - the applied `depth` and where it came from (`depth_source`: env `SUMMARY_CLUSTER_DEPTH`, the repo's
     `.review.yml`, or an explicit arg);
   - how many clusters there are and at what path level — e.g. «depth=2 → 15 кластеров уровня
     `reviewer/index`» — sampling a few `cluster_key`s from `clusters`;
   - how many are `stale` vs fresh, plus `deferred` (held back by the cap).
   - If `orphans > 0`, **warn**: the depth changed or modules were removed, so N summaries are orphaned;
     a full (uncapped) pass will rebuild and prune them.
   - State the invariant explicitly: `cluster_key` depends on depth, so **changing depth = a full
     rebuild of every summary** (old-depth summaries orphan and get pruned).
   Then **ask the user to confirm** before running. If they decline, stop without summarizing or pruning.

4. **Choose the summary model (only if any cluster is `stale == true`).** A subsystem summary is a
   coarse, high-level prior — a small/cheap model is appropriate, and reviewing on an expensive model
   burns tokens. Ask the user which model tier to use for writing summaries, defaulting to a cheap
   tier (e.g. Haiku/Sonnet/Fable). Remember the choice for this run. If nothing is stale, skip this
   step (nothing to generate).

5. **Summarize only STALE clusters.** For each cluster with `stale == true` (fresh ones are already
   up to date — skip them, this keeps the pass incremental and cheap):
   - Where your harness supports per-subagent model override, **dispatch a subagent on the chosen
     model** to read a few representative files (from `files` / `top_symbols`) and return
     `{title, summary}` (Russian, grounded — see Grounding below); the orchestrator then persists it.
     Where override is unavailable, write the summary inline on the session model and note this in the
     report. Either way:
     - `title` — one line: what this subsystem is.
     - `summary` — a compact paragraph: what it does, its key symbols (from `top_symbols`) and
       invariants. No `path:line` required; it is a high-level prior.
   - Persist: `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash)` —
     pass back the cluster's own `source_hash` from step 2.

6. **Prune orphaned summaries (only on a full pass).** If the pass was full — `deferred == 0` and you
   did NOT pass an explicit `depth`/`cap` override (so `clusters` covered every current cluster) — call
   `prune_subsystem_summaries(repo, branch)` to delete summaries whose `cluster_key` is no longer a
   current cluster (orphaned by a depth change or removed modules). On a **partial** pass
   (`deferred > 0`) skip pruning — deferred clusters are not orphans — and say so in the report
   (mirrors `sync_board --limit`).

7. **Report (Russian).** The applied `depth` + `depth_source`; how many clusters summarized vs
   skipped-as-fresh vs **deferred by the cap** (`deferred` from step 2 — never silently truncate); how
   many summaries were **pruned** (step 6), or that pruning was skipped on a partial pass. If summaries
   were written inline (no model override), say so.
````

Также обновить блок `## Tools` (строка 19): добавить `prune_subsystem_summaries` в перечисление:

Заменить строку 19:

```
Plus `list_subsystem_clusters` and `index_subsystem_summary` (reviewer MCP), and the harness `Read`.
```

на:

```
Plus `list_subsystem_clusters`, `index_subsystem_summary` and `prune_subsystem_summaries`
(reviewer MCP), and the harness `Read`.
```

- [ ] **Step 4: Запустить ВСЕ guard-тесты скилла — убедиться, что проходят**

Run: `.venv/bin/pytest tests/skills/test_summarize_subsystems.py -q`
Expected: PASS — новые два теста + существующие (`test_skill_reports_deferred` всё ещё видит `deferred`/`SUMMARY_REBUILD_CAP`; `test_skill_asks_model_choice` видит «Ask the user which model tier to use for writing summaries»; `test_skill_dispatches_subagent_on_chosen_model` видит «dispatch a subagent on the chosen»; `test_skill_has_five_pipeline_steps` видит 7 шагов ≥5).

- [ ] **Step 5: Прогнать связанные skill-guard'ы (сборка промптов)**

Run: `.venv/bin/pytest tests/skills -q`
Expected: PASS (включая `test_assembled_prompts.py`, `test_common_blocks.py` — include-маркеры `_common/branch-selection.md` сохранены).

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/summarize-subsystems/SKILL.md tests/skills/test_summarize_subsystems.py
git commit -m "feat(skills): summarize-subsystems — preflight depth + подтверждение + prune осиротевших (PRI-166)"
```

---

### Task 6: Полный прогон тестов + линт

**Files:** —

- [ ] **Step 1: Unit-тесты целиком**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены дефолтным `-m 'not integration'`).

- [ ] **Step 2: Integration-тесты затронутых модулей (нужен Postgres/Neo4j)**

Run: `docker compose up -d && .venv/bin/pytest -m integration tests/index/test_summary_store.py -q`
Expected: PASS.

- [ ] **Step 3: Линт по затронутым файлам**

Run: `.venv/bin/ruff check reviewer/policy/policy.py reviewer/config/settings.py reviewer/index/summary_store.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py`
Expected: чисто. (Repo-wide `ruff check .` может иметь предсуществующие замечания — не гнаться за ними, см. известную ловушку.)

- [ ] **Step 4: Финальный коммит (если линт потребовал правок)**

```bash
git add -A
git commit -m "chore(pri-166): финальные правки после прогона тестов и линта"
```

---

## Notes для исполнителя

- **Номера строк** в задачах — ориентир на момент написания плана; перед Edit найди точный якорный текст (он приведён дословно в шагах) — он не зависит от сдвига строк.
- **Воркфлоу резолва depth:** оба тула (`list_subsystem_clusters` без явного depth, `index_subsystem_summary`) идут через `_resolve_summary_depth` → один и тот же depth → `cluster_key`/`source_hash` консистентны. НЕ возвращай `settings.summary_cluster_depth` напрямую ни в одном из них.
- **Сеть в unit-тестах:** `_svc` в `tests/mcp/test_subsystem_summaries.py` стабит `_resolve_summary_depth` — поэтому тесты `list/index/prune` не идут в GitHub. Сам хелпер проверяется отдельными тестами через `_vcs_factory` (фейк/исключение), без сети.
- **Покрытие критериев приёмки:** depth из env + документация — Task 1; preflight-эхо depth + число/уровень + подтверждение — Task 5 (шаг 3 пайплайна); явное предупреждение о полном пересборе при смене depth — Task 5 (шаг 3) + purge осиротевших — Task 2/4/5 (шаг 6).

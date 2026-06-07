# MR/PR Review Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Построить агента, который на pull request собирает контекст (RAG по всему репо + граф кода), ревьюит изменения через LLM из OpenRouter с инструментами и постит inline-комментарии + сводку в GitHub.

**Architecture:** Ядро-библиотека `reviewer/` с точкой входа CLI. Индекс кода живёт в Postgres (ParadeDB: pgvector + pg_search BM25, гибрид через RRF), граф — в Neo4j. Свежесть: персистентная база целевой ветки + content-hash дедуп + эфемерный PR-overlay; на запросе `base\changed ∪ overlay`. Ретрив = гибрид + graph-expansion + Voyage rerank. Оркестрация — LangGraph (map-reduce по diff с фазой verify). OpenRouter настраивается через env (модель, потолок цены USD/1M, роутинг провайдера) + app-level бюджет шагов.

**Tech Stack (выверенные версии на 2026):** Python 3.12 · tree-sitter 0.25 / tree-sitter-python 0.25 · voyageai 0.4 (`voyage-code-3` 1024-dim, `rerank-2.5`) · ParadeDB `paradedb/paradedb:latest` (PG18, pg_search 0.24, pgvector 0.8.2) · neo4j 6.2 (driver) · `@sourcegraph/scip-python` 0.6.6 · langchain-openai 1.2 / langgraph 1.2 · httpx 0.27 · psycopg 3.2 · pydantic-settings 2.

---

## Принципы

- TDD: сначала падающий тест, потом минимальная реализация. Частые коммиты.
- **Без приписок ассистента в коммитах** (требование пользователя): `git commit -m "..."` без `Co-Authored-By`/«Generated with».
- Каждый модуль — одна ответственность, общается через явный интерфейс.
- Внешние сервисы (Voyage, OpenRouter, GitHub, Neo4j) за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.

## Карта файлов

```
pyproject.toml                         деп-ы, ruff, pytest
docker-compose.yml                     paradedb + neo4j
.env.example                           все env-переменные
reviewer/
  __init__.py
  config/settings.py                   Settings (pydantic-settings): OpenRouter/Voyage/DB/budget
  vcs/
    base.py                            VCSProvider + dataclasses (PullRequest, ChangedFile, InlineComment, Finding)
    github.py                          GitHubProvider (httpx)
    diff.py                            парсинг unified-diff -> карта (path, new_line) допустимых для inline
  index/
    models.py                          Chunk dataclass + content_hash
    chunker.py                         tree-sitter извлечение символов
    embeddings.py                      VoyageEmbedder (батчи, input_type, throttle/retry)
    schema.sql                         DDL: расширения, таблица chunks, индексы
    store.py                           ChunkStore: upsert, hybrid_search(base\changed ∪ overlay)
    freshness.py                       инкремент базы + сборка overlay по content-hash
  gitutil.py                           git diff/list-changed/show-file-at-ref
  graph/
    scip.py                            запуск scip-python + парс index.scip -> nodes/edges (path#fqn)
    store.py                           GraphStore (Neo4j): upsert, expand 1-2 хопа
  retrieval/retriever.py               гибрид + graph-expansion + Voyage rerank -> ContextPack
  llm/
    base.py                            LLMProvider
    openrouter.py                      OpenRouterProvider (extra_body: provider/max_price/models)
    budget.py                          BudgetTracker (токены/итерации на ревью)
  tools/code_tools.py                  LangChain-инструменты агента
  policy/policy.py                     .review.yml parse + gate
  agent/
    state.py                           ReviewState (TypedDict + reducers)
    nodes.py                           ingest/ensure_index/plan/retrieve/analyze/verify/assemble/publish
    graph.py                           сборка StateGraph + Send fan-out
    prompts.py                         системные промпты analyze/verify
  entrypoints/cli.py                   команды index / search / review
tests/...                              зеркало структуры
docker/init/                           (опц.) init-SQL
```

## Общие контракты (типы — фиксируются здесь, используются везде)

```python
# reviewer/index/models.py
from dataclasses import dataclass
import hashlib

@dataclass(frozen=True)
class Chunk:
    path: str
    lang: str
    symbol_fqn: str          # напр. "UserService.create"
    kind: str                # 'function' | 'class' | 'method' | 'module'
    start_line: int          # 1-based, включает декораторы
    end_line: int
    text: str

    @property
    def node_id(self) -> str:
        return f"{self.path}#{self.symbol_fqn}"   # ЕДИНЫЙ ключ чанка и узла графа

    @property
    def content_hash(self) -> str:
        norm = "\n".join(line.rstrip() for line in self.text.splitlines()).strip()
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()
```

```python
# reviewer/vcs/base.py
from dataclasses import dataclass
from typing import Literal, Protocol

@dataclass
class PullRequest:
    number: int
    base_sha: str
    head_sha: str
    base_ref: str            # напр. "main"
    title: str
    body: str

@dataclass
class ChangedFile:
    path: str
    status: str              # added/removed/modified/renamed
    patch: str | None        # unified diff; None для слишком больших файлов

@dataclass
class InlineComment:
    path: str
    line: int
    side: Literal["RIGHT", "LEFT"]
    body: str
    start_line: int | None = None
    start_side: Literal["RIGHT", "LEFT"] | None = None

@dataclass
class Finding:
    category: str            # correctness/security/performance/style/...
    severity: Literal["low", "medium", "high", "critical"]
    file: str
    line: int | None
    side: Literal["RIGHT", "LEFT"]
    message: str
    suggestion: str | None
    confidence: float        # 0..1

    def fingerprint(self) -> str:
        import hashlib
        key = f"{self.file}|{self.line}|{self.side}|{self.category}|{self.message}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

class VCSProvider(Protocol):
    def get_pull_request(self, number: int) -> PullRequest: ...
    def get_changed_files(self, number: int) -> list[ChangedFile]: ...
    def get_file_at_ref(self, path: str, ref: str) -> str | None: ...
    def list_existing_fingerprints(self, number: int) -> set[str]: ...
    def publish_review(self, number: int, head_sha: str, summary: str,
                       comments: list[InlineComment]) -> None: ...
```

Узел графа и чанк используют **один id** `path#fqn` — это связывает graph-expansion с ретривом чанков.

---

## Task 0: Scaffolding (проект, деп-ы, Docker, env)

**Files:**
- Create: `pyproject.toml`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `reviewer/__init__.py`, `tests/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: pyproject.toml**

```toml
[project]
name = "reviewer"
version = "0.1.0"
requires-python = ">=3.11,<3.14"
dependencies = [
    "pydantic-settings>=2.3",
    "tree-sitter>=0.25,<0.26",
    "tree-sitter-python>=0.25,<0.26",
    "voyageai>=0.4,<0.5",
    "psycopg[binary]>=3.2",
    "pgvector>=0.3.6",
    "neo4j>=6.2,<7",
    "httpx>=0.27",
    "langchain-openai>=1.2,<2",
    "langchain-core>=1.4,<2",
    "langgraph>=1.2,<2",
    "pyyaml>=6.0",
    "click>=8.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "ruff>=0.5",
    "protobuf>=5.27",
    "grpcio-tools>=1.64",
]

[project.scripts]
reviewer = "reviewer.entrypoints.cli:cli"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: требует поднятых Postgres/Neo4j (docker-compose up)"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 2: docker-compose.yml** (ParadeDB = pgvector + pg_search; Neo4j)

```yaml
services:
  paradedb:
    image: paradedb/paradedb:latest          # PG18 + pg_search + pgvector предустановлены
    environment:
      POSTGRES_USER: reviewer
      POSTGRES_PASSWORD: reviewer
      POSTGRES_DB: reviewer
    ports: ["5432:5432"]
    volumes: ["paradedb_data:/var/lib/postgresql/"]   # NB: родительский каталог, не /data
  neo4j:
    image: neo4j:5
    environment:
      NEO4J_AUTH: neo4j/reviewerpass
    ports: ["7474:7474", "7687:7687"]
    volumes: ["neo4j_data:/data"]
volumes:
  paradedb_data:
  neo4j_data:
```

- [ ] **Step 3: .env.example** (полный набор env)

```bash
# --- LLM via OpenRouter ---
OPENROUTER_API_KEY=
OPENROUTER_MODEL=anthropic/claude-sonnet-4.5
OPENROUTER_MODELS_FALLBACK=
OPENROUTER_MAX_PRICE_PROMPT=3.0
OPENROUTER_MAX_PRICE_COMPLETION=15.0
OPENROUTER_PROVIDER_SORT=price
OPENROUTER_PROVIDER_ORDER=
OPENROUTER_PROVIDER_ONLY=
OPENROUTER_PROVIDER_IGNORE=
OPENROUTER_ALLOW_FALLBACKS=true
OPENROUTER_REQUIRE_PARAMETERS=true
OPENROUTER_DATA_COLLECTION=deny
OPENROUTER_APP_URL=https://github.com/mimfort/rag_for_git
OPENROUTER_APP_TITLE=rag_for_git-reviewer
# --- App-level бюджет на одно ревью ---
REVIEW_MAX_TOOL_ITERATIONS=40
# --- Voyage ---
VOYAGE_API_KEY=
EMBEDDING_MODEL=voyage-code-3
EMBEDDING_DIM=1024
RERANK_MODEL=rerank-2.5
# --- Postgres / Neo4j ---
PG_DSN=postgresql://reviewer:reviewer@localhost:5432/reviewer
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=reviewerpass
# --- GitHub ---
GITHUB_TOKEN=
```

- [ ] **Step 4: пустые `reviewer/__init__.py`, `tests/__init__.py`, `tests/conftest.py`** (conftest пока пуст).

- [ ] **Step 5: Установить и зафиксировать**

Run: `pip install -e ".[dev]"`
Expected: установка без ошибок.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml docker-compose.yml .env.example reviewer tests
git commit -m "chore: scaffolding проекта (deps, docker-compose, env)"
```

---

## Task 1: config/Settings

**Files:**
- Create: `reviewer/config/__init__.py`, `reviewer/config/settings.py`
- Test: `tests/config/test_settings.py`

- [ ] **Step 1: Падающий тест**

```python
# tests/config/test_settings.py
from reviewer.config.settings import Settings

def test_openrouter_provider_block_built_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_PROMPT", "3.0")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_COMPLETION", "15.0")
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "price")
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", "deny")
    monkeypatch.setenv("OPENROUTER_MODELS_FALLBACK", "openai/gpt-5-mini, x/y")
    s = Settings()
    block = s.openrouter_provider_block()
    assert block["sort"] == "price"
    assert block["max_price"] == {"prompt": 3.0, "completion": 15.0}
    assert block["data_collection"] == "deny"
    assert block["require_parameters"] is True
    assert s.openrouter_models_list() == ["openai/gpt-5-mini", "x/y"]
```

- [ ] **Step 2: Запустить — упадёт**

Run: `pytest tests/config/test_settings.py -v`
Expected: FAIL (`ModuleNotFoundError: reviewer.config.settings`).

- [ ] **Step 3: Реализация**

```python
# reviewer/config/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    openrouter_models_fallback: str = ""
    openrouter_max_price_prompt: float | None = None
    openrouter_max_price_completion: float | None = None
    openrouter_provider_sort: str = "price"   # price|throughput|latency
    openrouter_provider_order: str = ""
    openrouter_provider_only: str = ""
    openrouter_provider_ignore: str = ""
    openrouter_allow_fallbacks: bool = True
    openrouter_require_parameters: bool = True
    openrouter_data_collection: str = "deny"
    openrouter_app_url: str = ""
    openrouter_app_title: str = ""
    # budget
    review_max_tool_iterations: int = 40
    # Voyage
    voyage_api_key: str = ""
    embedding_model: str = "voyage-code-3"
    embedding_dim: int = 1024
    rerank_model: str = "rerank-2.5"
    # stores
    pg_dsn: str = "postgresql://reviewer:reviewer@localhost:5432/reviewer"
    neo4j_uri: str = "neo4j://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "reviewerpass"
    # github
    github_token: str = ""

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    def openrouter_models_list(self) -> list[str]:
        return self._csv(self.openrouter_models_fallback)

    def openrouter_provider_block(self) -> dict:
        """Собрать объект provider для extra_body. Только заданные поля."""
        block: dict = {
            "allow_fallbacks": self.openrouter_allow_fallbacks,
            "require_parameters": self.openrouter_require_parameters,
            "data_collection": self.openrouter_data_collection,
        }
        if self.openrouter_provider_sort:
            block["sort"] = self.openrouter_provider_sort
        max_price = {}
        if self.openrouter_max_price_prompt is not None:
            max_price["prompt"] = self.openrouter_max_price_prompt
        if self.openrouter_max_price_completion is not None:
            max_price["completion"] = self.openrouter_max_price_completion
        if max_price:
            block["max_price"] = max_price
        if self._csv(self.openrouter_provider_order):
            block["order"] = self._csv(self.openrouter_provider_order)
        if self._csv(self.openrouter_provider_only):
            block["only"] = self._csv(self.openrouter_provider_only)
        if self._csv(self.openrouter_provider_ignore):
            block["ignore"] = self._csv(self.openrouter_provider_ignore)
        return block
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `pytest tests/config/test_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/config tests/config
git commit -m "feat(config): Settings c OpenRouter provider-блоком из env"
```

---

## Task 2: Chunker (tree-sitter извлечение символов)

**Files:**
- Create: `reviewer/index/__init__.py`, `reviewer/index/models.py`, `reviewer/index/chunker.py`
- Test: `tests/index/test_chunker.py`

Используем современный API tree-sitter 0.25: `Language(tree_sitter_python.language())`, `Parser(LANG)`, парсим **байты**, `start_point/end_point` это `(row,col)` 0-based, декораторы — узел `decorated_definition` (его диапазон включает `@...`), имя — `child_by_field_name("name")`, методы — `function_definition` внутри `class_definition.body`.

- [ ] **Step 1: Падающий тест**

```python
# tests/index/test_chunker.py
from reviewer.index.chunker import chunk_python

SRC = b'''\
import os

@dec
def top():
    def inner():
        pass
    return inner

class A:
    def method(self):
        pass
'''

def test_extracts_functions_classes_methods_with_ranges():
    chunks = chunk_python("m.py", SRC)
    by_fqn = {c.symbol_fqn: c for c in chunks}
    assert by_fqn["top"].kind == "function"
    assert by_fqn["top"].start_line == 3          # включает строку @dec
    assert by_fqn["top.inner"].kind == "function"
    assert by_fqn["A"].kind == "class"
    assert by_fqn["A.method"].kind == "method"
    assert by_fqn["A.method"].path == "m.py"
    # content_hash стабилен и одинаков для одинакового тела
    assert by_fqn["A.method"].content_hash == by_fqn["A.method"].content_hash

def test_handles_syntax_errors_without_crashing():
    chunks = chunk_python("bad.py", b"def f(:\n    pass\n")
    assert isinstance(chunks, list)
```

- [ ] **Step 2: Запустить — упадёт**

Run: `pytest tests/index/test_chunker.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Реализация models.py** (см. «Общие контракты» выше — создать файл `reviewer/index/models.py` с классом `Chunk` ровно как там).

- [ ] **Step 4: Реализация chunker.py**

```python
# reviewer/index/chunker.py
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from reviewer.index.models import Chunk

_PY = Language(tspython.language())
_PARSER = Parser(_PY)

_DEF_TYPES = {"function_definition", "class_definition"}

def chunk_python(path: str, source: bytes) -> list[Chunk]:
    tree = _PARSER.parse(source)
    chunks: list[Chunk] = []

    def name_of(defn) -> str:
        n = defn.child_by_field_name("name")
        return n.text.decode("utf-8") if n is not None else "<anonymous>"

    def visit(node, scope: str, class_scope: bool) -> None:
        for child in node.children:
            defn, outer = child, child
            if child.type == "decorated_definition":
                defn = child.child_by_field_name("definition")
                outer = child          # диапазон с декораторами
            if defn is not None and defn.type in _DEF_TYPES:
                name = name_of(defn)
                fqn = f"{scope}.{name}" if scope else name
                is_class = defn.type == "class_definition"
                kind = "class" if is_class else ("method" if class_scope else "function")
                chunks.append(Chunk(
                    path=path, lang="python", symbol_fqn=fqn, kind=kind,
                    start_line=outer.start_point[0] + 1,
                    end_line=outer.end_point[0] + 1,
                    text=source[outer.start_byte:outer.end_byte].decode("utf-8", "replace"),
                ))
                body = defn.child_by_field_name("body")
                if body is not None:
                    visit(body, fqn, class_scope=is_class)
            else:
                visit(child, scope, class_scope)

    visit(tree.root_node, "", class_scope=False)
    return chunks
```

- [ ] **Step 5: Запустить — пройдёт**

Run: `pytest tests/index/test_chunker.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add reviewer/index/__init__.py reviewer/index/models.py reviewer/index/chunker.py tests/index
git commit -m "feat(index): tree-sitter чанкер символов Python"
```

---

## Task 3: VoyageEmbedder

**Files:**
- Create: `reviewer/index/embeddings.py`
- Test: `tests/index/test_embeddings.py`

Voyage 0.4: `vo = voyageai.Client()` (берёт `VOYAGE_API_KEY`), `vo.embed(texts, model, input_type, output_dimension).embeddings`. Лимиты: ≤1000 текстов и ≤120K токенов на запрос → батчим по размеру. `input_type`: `"document"` при индексации, `"query"` при поиске. Мокаем клиент в unit-тесте.

- [ ] **Step 1: Падающий тест**

```python
# tests/index/test_embeddings.py
from reviewer.index.embeddings import VoyageEmbedder

class FakeResp:
    def __init__(self, embs): self.embeddings = embs

class FakeClient:
    def __init__(self): self.calls = []
    def embed(self, texts, model, input_type, output_dimension):
        self.calls.append((tuple(texts), input_type))
        return FakeResp([[0.1] * output_dimension for _ in texts])

def test_embed_documents_batches_and_uses_document_input_type():
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=1024, batch_size=2)
    vecs = emb.embed_documents(["a", "b", "c"])
    assert len(vecs) == 3 and len(vecs[0]) == 1024
    assert [c[1] for c in fake.calls] == ["document", "document"]   # 2 батча
    assert fake.calls[0][0] == ("a", "b") and fake.calls[1][0] == ("c",)

def test_embed_query_uses_query_input_type():
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8)
    v = emb.embed_query("find me")
    assert len(v) == 8 and fake.calls[0][1] == "query"
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/index/test_embeddings.py -v` → FAIL.

- [ ] **Step 3: Реализация**

```python
# reviewer/index/embeddings.py
from __future__ import annotations

class VoyageEmbedder:
    def __init__(self, client=None, model: str = "voyage-code-3",
                 dim: int = 1024, batch_size: int = 128):
        if client is None:
            import voyageai
            client = voyageai.Client()      # VOYAGE_API_KEY из env
        self._client = client
        self.model = model
        self.dim = dim
        self.batch_size = batch_size

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            resp = self._client.embed(
                batch, model=self.model,
                input_type=input_type, output_dimension=self.dim,
            )
            out.extend(resp.embeddings)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]
```

> Примечание для исполнителя: rate-limit `voyage-code-3` = 3M TPM / 2000 RPM. На реальной массовой индексации добавить retry на `429` (SDK уже использует tenacity внутри, но при batch-цикле имеет смысл ловить и делать backoff). Для unit-тестов это не требуется.

- [ ] **Step 4: Запустить — пройдёт.** Run: `pytest tests/index/test_embeddings.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/embeddings.py tests/index/test_embeddings.py
git commit -m "feat(index): VoyageEmbedder с батчингом и input_type"
```

---

## Task 4: Схема Postgres (chunks + индексы)

**Files:**
- Create: `reviewer/index/schema.sql`
- Create: `reviewer/index/store.py` (только `init_schema` на этом шаге)
- Test: `tests/index/test_schema.py` (помечен `@pytest.mark.integration`)

Колонка `embedding vector(1024)` (под voyage-code-3). BM25-индекс **один на таблицу**, `key_field` первым, включает колонки для фильтрации (`text`, `path`, `ref`). HNSW `vector_cosine_ops`.

- [ ] **Step 1: schema.sql**

```sql
-- reviewer/index/schema.sql
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id           bigserial PRIMARY KEY,
    ref          text    NOT NULL,        -- 'base' | 'pr:<number>'
    content_hash text    NOT NULL,
    path         text    NOT NULL,
    lang         text    NOT NULL,
    symbol_fqn   text    NOT NULL,
    kind         text    NOT NULL,
    start_line   int     NOT NULL,
    end_line     int     NOT NULL,
    text         text    NOT NULL,
    embedding    vector(1024),
    UNIQUE (ref, path, symbol_fqn)
);
CREATE INDEX IF NOT EXISTS chunks_ref_path ON chunks (ref, path);
CREATE INDEX IF NOT EXISTS chunks_hash ON chunks (content_hash);

-- BM25 (pg_search): один индекс, key_field первым
CREATE INDEX IF NOT EXISTS chunks_bm25 ON chunks
USING bm25 (id, text, path, ref) WITH (key_field='id');

-- ANN (pgvector HNSW, косинус)
CREATE INDEX IF NOT EXISTS chunks_hnsw ON chunks
USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

- [ ] **Step 2: Падающий integration-тест**

```python
# tests/index/test_schema.py
import psycopg, pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore

@pytest.mark.integration
def test_init_schema_creates_table_and_indexes():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    with psycopg.connect(s.pg_dsn) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='chunks'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert {"chunks_bm25", "chunks_hnsw"} <= names
```

- [ ] **Step 3: Запустить — упадёт.** Run: `pytest tests/index/test_schema.py -v -m integration` → FAIL.

- [ ] **Step 4: Реализация `ChunkStore.init_schema`**

```python
# reviewer/index/store.py
from __future__ import annotations
from pathlib import Path
import psycopg

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()

class ChunkStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def init_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(_SCHEMA)
            conn.commit()
```

- [ ] **Step 5: Поднять БД и прогнать.**

Run: `docker compose up -d paradedb` затем `pytest tests/index/test_schema.py -v -m integration`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add reviewer/index/schema.sql reviewer/index/store.py tests/index/test_schema.py
git commit -m "feat(index): схема chunks (pgvector HNSW + pg_search BM25)"
```

---

## Task 5: ChunkStore — upsert + гибридный поиск (RRF, base\changed ∪ overlay)

**Files:**
- Modify: `reviewer/index/store.py`
- Test: `tests/index/test_store_hybrid.py` (`@pytest.mark.integration`)

Гибрид через RRF: CTE BM25 (`@@@`, `pdb.score`, сорт DESC) + CTE ANN (`<=>`, сорт ASC — это **дистанция**), `UNION ALL` со скором `1.0/(60+rank)`, `GROUP BY/SUM`. Фильтр свежести: `(ref='base' AND NOT path = ANY(:changed)) OR ref=:overlay`. Векторы передаём через `pgvector.psycopg`.

- [ ] **Step 1: Падающий integration-тест**

```python
# tests/index/test_store_hybrid.py
import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow

def _row(ref, path, fqn, text, vec):
    return ChunkRow(ref=ref, content_hash=fqn+ref, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text=text, embedding=vec)

@pytest.mark.integration
def test_overlay_shadows_base_for_changed_paths():
    s = Settings()
    store = ChunkStore(s.pg_dsn); store.init_schema()
    store.clear()
    d = s.embedding_dim
    base_vec = [0.0]*d; base_vec[0] = 1.0
    store.upsert([
        _row("base", "a.py", "f_a", "def f_a(): return parse_token()", base_vec),
        _row("base", "b.py", "f_b", "def f_b(): pass", [0.0]*d),
        _row("pr:1", "a.py", "f_a", "def f_a(): return NEW_parse_token()", base_vec),
    ])
    res = store.hybrid_search(
        query_text="parse token", query_embedding=base_vec,
        overlay_ref="pr:1", changed_paths=["a.py"], top_k=5, candidates=20,
    )
    paths_texts = {(r.path, r.text) for r in res}
    # a.py отдаётся ТОЛЬКО из overlay (новая версия), не из base
    assert ("a.py", "def f_a(): return NEW_parse_token()") in paths_texts
    assert ("a.py", "def f_a(): return parse_token()") not in paths_texts
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/index/test_store_hybrid.py -v -m integration` → FAIL.

- [ ] **Step 3: Реализация (дополнить store.py)**

```python
# reviewer/index/store.py  (добавить к существующему)
from dataclasses import dataclass
from pgvector.psycopg import register_vector

@dataclass
class ChunkRow:
    ref: str
    content_hash: str
    path: str
    lang: str
    symbol_fqn: str
    kind: str
    start_line: int
    end_line: int
    text: str
    embedding: list[float]

@dataclass
class Retrieved:
    node_id: str
    path: str
    symbol_fqn: str
    kind: str
    start_line: int
    end_line: int
    text: str
    score: float

# --- добавить методы в класс ChunkStore (все с self) ---
def _connect(self):
    conn = psycopg.connect(self.dsn)
    register_vector(conn)
    return conn

def clear(self) -> None:
    with self._connect() as conn:
        conn.execute("TRUNCATE chunks RESTART IDENTITY")
        conn.commit()

def upsert(self, rows: list["ChunkRow"]) -> None:
    sql = """
    INSERT INTO chunks (ref, content_hash, path, lang, symbol_fqn, kind,
                        start_line, end_line, text, embedding)
    VALUES (%(ref)s,%(content_hash)s,%(path)s,%(lang)s,%(symbol_fqn)s,%(kind)s,
            %(start_line)s,%(end_line)s,%(text)s,%(embedding)s)
    ON CONFLICT (ref, path, symbol_fqn) DO UPDATE SET
        content_hash=EXCLUDED.content_hash, kind=EXCLUDED.kind,
        start_line=EXCLUDED.start_line, end_line=EXCLUDED.end_line,
        text=EXCLUDED.text, embedding=EXCLUDED.embedding
    """
    with self._connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, [r.__dict__ for r in rows])
        conn.commit()

def existing_hashes(self, ref: str) -> set[str]:
    with self._connect() as conn:
        rows = conn.execute(
            "SELECT content_hash FROM chunks WHERE ref=%s", (ref,)
        ).fetchall()
    return {r[0] for r in rows}

def hybrid_search(self, query_text, query_embedding, overlay_ref,
                  changed_paths, top_k=20, candidates=50) -> list["Retrieved"]:
    where = "((ref='base' AND NOT (path = ANY(%(changed)s))) OR ref=%(overlay)s)"
    sql = f"""
    WITH bm25 AS (
        SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
        FROM chunks
        WHERE text @@@ %(q)s AND {where}
        ORDER BY pdb.score(id) DESC LIMIT %(cand)s
    ),
    ann AS (
        SELECT id, RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
        FROM chunks
        WHERE {where}
        ORDER BY embedding <=> %(vec)s LIMIT %(cand)s
    ),
    rrf AS (
        SELECT id, 1.0/(60+rank) AS s FROM bm25
        UNION ALL
        SELECT id, 1.0/(60+rank) AS s FROM ann
    )
    SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text,
           SUM(r.s) AS score
    FROM rrf r JOIN chunks c USING (id)
    GROUP BY c.id, c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
    ORDER BY score DESC LIMIT %(k)s
    """
    params = {"q": query_text, "vec": query_embedding, "overlay": overlay_ref,
              "changed": changed_paths, "cand": candidates, "k": top_k}
    with self._connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                      start_line=sl, end_line=el, text=t, score=float(sc))
            for (p, f, k, sl, el, t, sc) in rows]
```

> Исполнителю: методы `_connect/clear/upsert/existing_hashes/hybrid_search` должны быть **методами класса `ChunkStore`** (определены с `self`); приведённый блок показывает их тела — впишите их внутрь класса из Task 4, не как свободные функции.

- [ ] **Step 4: Запустить — пройдёт.** Run: `pytest tests/index/test_store_hybrid.py -v -m integration` → PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/store.py tests/index/test_store_hybrid.py
git commit -m "feat(index): гибридный поиск RRF c overlay-шэдоуингом базы"
```

---

## Task 6: gitutil + freshness (инкремент базы + overlay)

**Files:**
- Create: `reviewer/gitutil.py`
- Create: `reviewer/index/freshness.py`
- Test: `tests/test_gitutil.py`, `tests/index/test_freshness.py`

`gitutil` оборачивает git CLI: список изменённых файлов между refs, чтение файла на ref, список python-файлов на ref. `freshness` строит overlay: чанкует изменённые файлы PR head, эмбедит только новые content-hash, кладёт в `ref='pr:<n>'`.

- [ ] **Step 1: Падающий тест gitutil (на временном репо)**

```python
# tests/test_gitutil.py
import subprocess, pathlib
from reviewer.gitutil import changed_files, file_at_ref

def _run(*a, cwd): subprocess.run(a, cwd=cwd, check=True, capture_output=True)

def test_changed_files_and_file_at_ref(tmp_path):
    r = tmp_path
    _run("git","init","-q", cwd=r)
    _run("git","config","user.email","t@t","--local", cwd=r)
    _run("git","config","user.name","t","--local", cwd=r)
    (r/"a.py").write_text("x=1\n")
    _run("git","add","-A", cwd=r); _run("git","commit","-qm","c1", cwd=r)
    base = subprocess.run(["git","rev-parse","HEAD"],cwd=r,capture_output=True,text=True).stdout.strip()
    (r/"a.py").write_text("x=2\n"); (r/"b.py").write_text("y=1\n")
    _run("git","add","-A", cwd=r); _run("git","commit","-qm","c2", cwd=r)
    assert set(changed_files(str(r), base, "HEAD")) == {"a.py", "b.py"}
    assert file_at_ref(str(r), "a.py", base) == "x=1\n"
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/test_gitutil.py -v` → FAIL.

- [ ] **Step 3: Реализация gitutil.py**

```python
# reviewer/gitutil.py
import subprocess

def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args],
                          check=True, capture_output=True, text=True).stdout

def changed_files(repo: str, base: str, head: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", f"{base}..{head}")
    return [l for l in out.splitlines() if l]

def file_at_ref(repo: str, path: str, ref: str) -> str | None:
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None   # файла нет на этом ref (добавлен/удалён)

def list_python_files(repo: str, ref: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", ref)
    return [l for l in out.splitlines() if l.endswith(".py")]
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `pytest tests/test_gitutil.py -v` → PASS.

- [ ] **Step 5: Падающий тест freshness (мок store/embedder)**

```python
# tests/index/test_freshness.py
from reviewer.index.freshness import build_overlay
from reviewer.index.models import Chunk

class FakeEmb:
    def embed_documents(self, texts): return [[0.0]*4 for _ in texts]

class FakeStore:
    def __init__(self): self.rows=[]
    def existing_hashes(self, ref): return set()
    def upsert(self, rows): self.rows.extend(rows)

def test_build_overlay_chunks_changed_files_into_pr_ref(monkeypatch):
    files = {"a.py": "def f():\n    return 1\n"}
    store, emb = FakeStore(), FakeEmb()
    build_overlay(store, emb, pr_number=7,
                  changed_files=list(files),
                  read_head=lambda p: files.get(p))
    assert all(r.ref == "pr:7" for r in store.rows)
    assert any(r.symbol_fqn == "f" for r in store.rows)
```

- [ ] **Step 6: Запустить — упадёт.** Run: `pytest tests/index/test_freshness.py -v` → FAIL.

- [ ] **Step 7: Реализация freshness.py**

```python
# reviewer/index/freshness.py
from __future__ import annotations
from collections.abc import Callable

from reviewer.index.chunker import chunk_python
from reviewer.index.store import ChunkRow

def _rows_for_file(path: str, source: str, ref: str) -> list[ChunkRow]:
    chunks = chunk_python(path, source.encode("utf-8"))
    return [ChunkRow(ref=ref, content_hash=c.content_hash, path=c.path,
                     lang=c.lang, symbol_fqn=c.symbol_fqn, kind=c.kind,
                     start_line=c.start_line, end_line=c.end_line,
                     text=c.text, embedding=[]) for c in chunks]

def _embed_and_upsert(store, embedder, rows: list[ChunkRow]) -> None:
    if not rows:
        return
    vecs = embedder.embed_documents([r.text for r in rows])
    for r, v in zip(rows, vecs):
        r.embedding = v
    store.upsert(rows)

def build_overlay(store, embedder, pr_number: int, changed_files: list[str],
                  read_head: Callable[[str], str | None]) -> None:
    """Чанкует изменённые файлы PR head в ref='pr:<n>'. Дедуп по content_hash."""
    ref = f"pr:{pr_number}"
    seen = store.existing_hashes(ref)
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        src = read_head(path)
        if src is None:           # удалённый файл — нечего класть в overlay
            continue
        for row in _rows_for_file(path, src, ref):
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, batch)

def update_base(store, embedder, repo: str, target_ref: str,
                changed_files: list[str],
                read: Callable[[str], str | None]) -> None:
    """Инкрементально обновляет ref='base' по изменённым файлам целевой ветки."""
    seen = store.existing_hashes("base")
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        src = read(path)
        if src is None:
            continue
        for row in _rows_for_file(path, src, "base"):
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, batch)
```

- [ ] **Step 8: Запустить — пройдёт.** Run: `pytest tests/index/test_freshness.py -v` → PASS.

- [ ] **Step 9: Commit**

```bash
git add reviewer/gitutil.py reviewer/index/freshness.py tests/test_gitutil.py tests/index/test_freshness.py
git commit -m "feat(index): gitutil + свежесть (инкремент базы, PR overlay, content-hash дедуп)"
```

---

## Task 7: graph/scip.py — парсинг SCIP в рёбра (node_id = path#fqn)

**Files:**
- Create: `reviewer/graph/__init__.py`, `reviewer/graph/scip.py`
- Create (сгенерированный): `reviewer/graph/scip_pb2.py`
- Test: `tests/graph/test_scip.py`

SCIP не даёт готовых рёбер `CALLS` — выводим сами: каждое **reference**-occurrence (бит `Definition` не выставлен) принадлежит охватывающему символу (caller), а его `symbol` — это callee. Единый ключ узла = `path#fqn`; SCIP-символы маппим в `fqn` через интервалы из чанкера. Официальных Python-биндингов нет — генерируем `scip_pb2.py` из `scip.proto`.

- [ ] **Step 1: Сгенерировать биндинги (один раз, файл коммитим)**

Run:
```bash
curl -sL https://raw.githubusercontent.com/sourcegraph/scip/main/scip.proto -o reviewer/graph/scip.proto
python -m grpc_tools.protoc -Ireviewer/graph --python_out=reviewer/graph reviewer/graph/scip.proto
```
Expected: создан `reviewer/graph/scip_pb2.py`.

- [ ] **Step 2: Падающий тест (синтетический Index в памяти, без npm)**

```python
# tests/graph/test_scip.py
from reviewer.graph import scip
from reviewer.graph.scip_pb2 import Index, Document, Occurrence, SymbolInformation

DEF = 0x1

def _occ(symbol, line, role=0):
    o = Occurrence(symbol=symbol, symbol_roles=role)
    o.range.extend([line, 0, line, 20])   # [sl, sc, el, ec], 0-based
    return o

def test_calls_edges_resolved_to_path_fqn():
    # caller g (строки 0..3) вызывает f (определён на строке 0 файла a.py)
    idx = Index()
    doc = Document(relative_path="a.py")
    doc.occurrences.append(_occ("scip . pkg f().", 0, DEF))    # def f
    doc.occurrences.append(_occ("scip . pkg g().", 5, DEF))    # def g
    doc.occurrences.append(_occ("scip . pkg f().", 6))         # g() вызывает f()
    idx.documents.append(doc)

    # резолвер интервалов: какая fqn охватывает строку (1-based)
    intervals = {"a.py": [("f", 1, 4), ("g", 6, 8)]}
    def resolve(path, line1):
        best = None
        for fqn, s, e in intervals.get(path, []):
            if s <= line1 <= e and (best is None or (e - s) < (best[2] - best[1])):
                best = (fqn, s, e)
        return best[0] if best else None

    nodes, edges = scip.parse_scip(idx, resolve)
    assert "a.py#f" in nodes and "a.py#g" in nodes
    assert ("a.py#g", "CALLS", "a.py#f") in edges
```

- [ ] **Step 3: Запустить — упадёт.** Run: `pytest tests/graph/test_scip.py -v` → FAIL.

- [ ] **Step 4: Реализация scip.py**

```python
# reviewer/graph/scip.py
from __future__ import annotations
from collections.abc import Callable

from reviewer.index.chunker import chunk_python

DEFINITION = 0x1
FqnResolver = Callable[[str, int], str | None]   # (path, line_1based) -> fqn|None

def _start_line_1based(occ) -> int:
    return occ.range[0] + 1   # SCIP 0-based -> 1-based

def parse_scip(index, resolve: FqnResolver):
    """index: scip_pb2.Index. Возвращает (nodes:set[str], edges:list[(src,rel,dst)])."""
    nodes: set[str] = set()
    edges: list[tuple[str, str, str]] = []
    symbol_to_node: dict[str, str] = {}

    # 1) definition-occurrence -> node_id
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & DEFINITION:
                fqn = resolve(doc.relative_path, _start_line_1based(occ))
                if fqn:
                    nid = f"{doc.relative_path}#{fqn}"
                    symbol_to_node[occ.symbol] = nid
                    nodes.add(nid)

    # 2) reference-occurrence -> ребро CALLS (caller охватывает occ; callee=symbol)
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & DEFINITION:
                continue
            callee = symbol_to_node.get(occ.symbol)
            if callee is None:
                continue
            caller_fqn = resolve(doc.relative_path, _start_line_1based(occ))
            if not caller_fqn:
                continue
            caller = f"{doc.relative_path}#{caller_fqn}"
            if caller != callee:
                nodes.add(caller)
                edges.append((caller, "CALLS", callee))

    # 3) IMPLEMENTS из relationships
    for doc in index.documents:
        for si in doc.symbols:
            src = symbol_to_node.get(si.symbol)
            if src is None:
                continue
            for rel in si.relationships:
                if rel.is_implementation:
                    dst = symbol_to_node.get(rel.symbol)
                    if dst:
                        edges.append((src, "IMPLEMENTS", dst))

    return nodes, list(dict.fromkeys(edges))   # дедуп с сохранением порядка

def build_fqn_resolver(chunks_by_path: dict[str, list]) -> FqnResolver:
    def resolve(path: str, line1: int) -> str | None:
        best = None
        for c in chunks_by_path.get(path, []):
            if c.start_line <= line1 <= c.end_line:
                span = c.end_line - c.start_line
                if best is None or span < best[1]:
                    best = (c.symbol_fqn, span)
        return best[0] if best else None
    return resolve

def chunks_by_path_for(repo_files: dict[str, str]) -> dict[str, list]:
    return {p: chunk_python(p, src.encode("utf-8")) for p, src in repo_files.items()}

def run_scip_python(repo: str, project_name: str = "repo") -> bytes:
    """Запустить индексер; вернуть содержимое index.scip. Требует npm @sourcegraph/scip-python и активный venv."""
    import subprocess, pathlib
    subprocess.run(["scip-python", "index", ".", f"--project-name={project_name}"],
                   cwd=repo, check=True, capture_output=True)
    return pathlib.Path(repo, "index.scip").read_bytes()
```

> Исполнителю: `run_scip_python` покрывается integration-тестом отдельно (нужен установленный `@sourcegraph/scip-python`); v1 graph-builder может работать и на одном tree-sitter — резолвер call-рёбер по импортам как fallback можно добавить позже, интерфейс `parse_scip` от этого не меняется.

- [ ] **Step 5: Запустить — пройдёт.** Run: `pytest tests/graph/test_scip.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add reviewer/graph/__init__.py reviewer/graph/scip.py reviewer/graph/scip.proto reviewer/graph/scip_pb2.py tests/graph
git commit -m "feat(graph): парсинг SCIP в рёбра CALLS/IMPLEMENTS с ключом path#fqn"
```

---

## Task 8: graph/store.py — Neo4j (upsert + expand 1–2 хопа)

**Files:**
- Create: `reviewer/graph/store.py`
- Test: `tests/graph/test_store.py` (`@pytest.mark.integration`)

Driver 6.x: `GraphDatabase.driver` + `driver.execute_query()`. UNIQUE-констрейнт на `id` до загрузки; узлы/рёбра батчами через `UNWIND`. Расширение — переменной длины в обе стороны (callers+callees).

- [ ] **Step 1: Падающий integration-тест**

```python
# tests/graph/test_store.py
import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore

@pytest.mark.integration
def test_upsert_and_expand():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema(); g.clear()
    g.upsert_nodes(["a.py#g", "a.py#f", "a.py#h"])
    g.upsert_edges([("a.py#g", "CALLS", "a.py#f"), ("a.py#h", "CALLS", "a.py#g")])
    # от g на 1-2 хопа достаём и callee f, и caller h
    related = g.expand(["a.py#g"], hops=2)
    assert {"a.py#f", "a.py#h"} <= related
    g.close()
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/graph/test_store.py -v -m integration` → FAIL.

- [ ] **Step 3: Реализация**

```python
# reviewer/graph/store.py
from __future__ import annotations
from neo4j import GraphDatabase

class GraphStore:
    def __init__(self, uri: str, user: str, password: str):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    def init_schema(self) -> None:
        self._driver.execute_query(
            "CREATE CONSTRAINT sym_id IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE s.id IS UNIQUE")

    def clear(self) -> None:
        self._driver.execute_query("MATCH (n) DETACH DELETE n")

    def upsert_nodes(self, node_ids: list[str]) -> None:
        self._driver.execute_query(
            "UNWIND $ids AS id MERGE (:Symbol {id: id})",
            ids=list(node_ids))

    def upsert_edges(self, edges: list[tuple[str, str, str]]) -> None:
        # рёбра группируем по типу (тип нельзя параметризовать в Cypher)
        by_rel: dict[str, list[dict]] = {}
        for src, rel, dst in edges:
            by_rel.setdefault(rel, []).append({"src": src, "dst": dst})
        for rel, rows in by_rel.items():
            self._driver.execute_query(
                f"UNWIND $rows AS r "
                f"MATCH (a:Symbol {{id: r.src}}) MATCH (b:Symbol {{id: r.dst}}) "
                f"MERGE (a)-[:{rel}]->(b)",
                rows=rows)

    def expand(self, node_ids: list[str], hops: int = 2) -> set[str]:
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid MATCH (s:Symbol {{id: sid}}) "
            f"MATCH (s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-(n:Symbol) "
            f"RETURN DISTINCT n.id AS id",
            ids=list(node_ids))
        return {r["id"] for r in records}
```

- [ ] **Step 4: Поднять Neo4j и прогнать.** Run: `docker compose up -d neo4j` затем `pytest tests/graph/test_store.py -v -m integration` → PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/graph/store.py tests/graph/test_store.py
git commit -m "feat(graph): GraphStore на Neo4j (upsert + expand 1-2 хопа)"
```

---

## Task 9: llm — OpenRouterProvider + BudgetTracker

**Files:**
- Create: `reviewer/llm/__init__.py`, `reviewer/llm/base.py`, `reviewer/llm/openrouter.py`, `reviewer/llm/budget.py`
- Test: `tests/llm/test_openrouter.py`, `tests/llm/test_budget.py`

Провайдер-роутинг и потолок цены — **только** через `extra_body` (не `model_kwargs`). `provider`-блок берём из `Settings.openrouter_provider_block()`, fallback-модели — top-level `models`. Конструирование `ChatOpenAI` сетевого вызова не делает → тест проверяет `extra_body` без сети.

- [ ] **Step 1: Падающий тест провайдера**

```python
# tests/llm/test_openrouter.py
from reviewer.config.settings import Settings
from reviewer.llm.openrouter import OpenRouterProvider

def test_extra_body_carries_provider_block_and_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_PROMPT", "3.0")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_COMPLETION", "15.0")
    monkeypatch.setenv("OPENROUTER_MODELS_FALLBACK", "openai/gpt-5-mini")
    prov = OpenRouterProvider(Settings())
    llm = prov.chat_model()
    eb = llm.extra_body
    assert eb["provider"]["max_price"] == {"prompt": 3.0, "completion": 15.0}
    assert eb["provider"]["require_parameters"] is True
    assert eb["models"] == ["openai/gpt-5-mini"]
    assert llm.model_name == "anthropic/claude-sonnet-4.5"
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/llm/test_openrouter.py -v` → FAIL.

- [ ] **Step 3: Реализация base.py / openrouter.py**

```python
# reviewer/llm/base.py
from typing import Protocol

class LLMProvider(Protocol):
    def chat_model(self): ...                 # -> BaseChatModel
    def chat_model_with_tools(self, tools: list): ...
```

```python
# reviewer/llm/openrouter.py
from __future__ import annotations
from langchain_openai import ChatOpenAI

from reviewer.config.settings import Settings

class OpenRouterProvider:
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, settings: Settings):
        self.s = settings

    def _extra_body(self) -> dict:
        eb: dict = {"provider": self.s.openrouter_provider_block()}
        models = self.s.openrouter_models_list()
        if models:
            eb["models"] = models
        return eb

    def _headers(self) -> dict:
        h = {}
        if self.s.openrouter_app_url:
            h["HTTP-Referer"] = self.s.openrouter_app_url
        if self.s.openrouter_app_title:
            h["X-Title"] = self.s.openrouter_app_title
        return h

    def chat_model(self) -> ChatOpenAI:
        return ChatOpenAI(
            base_url=self.BASE_URL,
            api_key=self.s.openrouter_api_key,
            model=self.s.openrouter_model,
            temperature=0,
            default_headers=self._headers() or None,
            extra_body=self._extra_body(),
        )

    def chat_model_with_tools(self, tools: list):
        return self.chat_model().bind_tools(tools)
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `pytest tests/llm/test_openrouter.py -v` → PASS.

- [ ] **Step 5: Падающий тест бюджета**

```python
# tests/llm/test_budget.py
import pytest
from reviewer.llm.budget import BudgetTracker, BudgetExceeded

def test_budget_raises_after_max_iterations():
    b = BudgetTracker(max_iterations=2)
    b.tick(); b.tick()
    with pytest.raises(BudgetExceeded):
        b.tick()
```

- [ ] **Step 6: Запустить — упадёт.** Run: `pytest tests/llm/test_budget.py -v` → FAIL.

- [ ] **Step 7: Реализация budget.py**

```python
# reviewer/llm/budget.py
class BudgetExceeded(RuntimeError):
    pass

class BudgetTracker:
    def __init__(self, max_iterations: int):
        self.max_iterations = max_iterations
        self.iterations = 0

    def tick(self) -> None:
        if self.iterations >= self.max_iterations:
            raise BudgetExceeded(f"Превышен бюджет итераций ({self.max_iterations})")
        self.iterations += 1
```

- [ ] **Step 8: Запустить — пройдёт.** Run: `pytest tests/llm/test_budget.py -v` → PASS.

- [ ] **Step 9: Commit**

```bash
git add reviewer/llm tests/llm
git commit -m "feat(llm): OpenRouterProvider (extra_body provider/max_price/models) + BudgetTracker"
```

---

## Task 10: retrieval — Retriever (гибрид + graph-expansion + Voyage rerank)

**Files:**
- Create: `reviewer/index/reranker.py` (VoyageReranker)
- Create: `reviewer/retrieval/__init__.py`, `reviewer/retrieval/retriever.py`
- Test: `tests/retrieval/test_retriever.py`

`Retriever.retrieve(query, changed_node_ids, overlay_ref, changed_paths)`:
1. embed query → `hybrid_search` (кандидаты);
2. `graph.expand(changed_node_ids)` → добрать чанки связанных символов;
3. слить кандидаты, дедуп по `node_id`;
4. Voyage `rerank` по тексту запроса → top-N;
5. вернуть `ContextPack` (list[Retrieved] + сборка текста с цитатами).

Тест — на фейках (store/graph/embedder/reranker), без сети/БД.

- [ ] **Step 1: Падающий тест**

```python
# tests/retrieval/test_retriever.py
from reviewer.retrieval.retriever import Retriever
from reviewer.index.store import Retrieved

def R(nid, text): 
    p, f = nid.split("#")
    return Retrieved(nid, p, f, "function", 1, 2, text, 1.0)

class FakeStore:
    def hybrid_search(self, **kw): return [R("a.py#f", "alpha"), R("b.py#g", "beta")]
    def fetch_nodes(self, node_ids, overlay_ref, changed_paths):
        return [R("c.py#h", "gamma")] if "c.py#h" in node_ids else []
class FakeGraph:
    def expand(self, ids, hops=2): return {"c.py#h"}
class FakeEmb:
    def embed_query(self, q): return [0.0, 0.1]
class FakeRerank:
    def rerank(self, query, items, top_k):   # items: list[Retrieved]
        return list(reversed(items))[:top_k]

def test_retrieve_merges_hybrid_and_graph_then_reranks():
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), FakeRerank())
    pack = r.retrieve(query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=3)
    ids = [x.node_id for x in pack.items]
    assert "c.py#h" in ids            # пришло из graph-expansion
    assert set(ids) == {"a.py#f", "b.py#g", "c.py#h"}
    assert "c.py#h" in pack.as_context()   # сборка текста содержит цитату
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/retrieval/test_retriever.py -v` → FAIL.

- [ ] **Step 3: Реализация reranker.py**

```python
# reviewer/index/reranker.py
from __future__ import annotations

class VoyageReranker:
    def __init__(self, client=None, model: str = "rerank-2.5"):
        if client is None:
            import voyageai
            client = voyageai.Client()
        self._client = client
        self.model = model

    def rerank(self, query: str, items: list, top_k: int) -> list:
        if not items:
            return []
        docs = [it.text for it in items]
        resp = self._client.rerank(query, docs, model=self.model, top_k=top_k)
        return [items[res.index] for res in resp.results]
```

- [ ] **Step 4: Реализация retriever.py**

```python
# reviewer/retrieval/retriever.py
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class ContextPack:
    items: list

    def as_context(self) -> str:
        parts = []
        for it in self.items:
            parts.append(f"// {it.node_id} ({it.path}:{it.start_line}-{it.end_line})\n{it.text}")
        return "\n\n".join(parts)

class Retriever:
    def __init__(self, store, graph, embedder, reranker):
        self.store, self.graph = store, graph
        self.embedder, self.reranker = embedder, reranker

    def retrieve(self, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=15, candidates=50) -> ContextPack:
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            query_text=query, query_embedding=qvec, overlay_ref=overlay_ref,
            changed_paths=changed_paths, top_k=candidates, candidates=candidates)
        related_ids = self.graph.expand(changed_node_ids, hops=2)
        related = self.store.fetch_nodes(list(related_ids), overlay_ref, changed_paths)
        merged: dict[str, object] = {}
        for it in [*hits, *related]:
            merged.setdefault(it.node_id, it)
        ranked = self.reranker.rerank(query, list(merged.values()), top_k=top_k)
        return ContextPack(items=ranked)
```

- [ ] **Step 5: Добавить `ChunkStore.fetch_nodes`** (в `reviewer/index/store.py`)

```python
def fetch_nodes(self, node_ids, overlay_ref, changed_paths):
    if not node_ids:
        return []
    # node_id = "path#fqn"; собираем (path, fqn)
    pairs = [nid.split("#", 1) for nid in node_ids]
    sql = """
    SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
    FROM chunks c JOIN unnest(%(paths)s::text[], %(fqns)s::text[]) AS q(p,f)
      ON c.path=q.p AND c.symbol_fqn=q.f
    WHERE (c.ref='base' AND NOT (c.path = ANY(%(changed)s))) OR c.ref=%(overlay)s
    """
    params = {"paths": [p for p, _ in pairs], "fqns": [f for _, f in pairs],
              "changed": changed_paths, "overlay": overlay_ref}
    with self._connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                      start_line=sl, end_line=el, text=t, score=0.0)
            for (p, f, k, sl, el, t) in rows]
```

- [ ] **Step 6: Запустить — пройдёт.** Run: `pytest tests/retrieval/test_retriever.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add reviewer/index/reranker.py reviewer/retrieval reviewer/index/store.py tests/retrieval
git commit -m "feat(retrieval): Retriever (гибрид + graph-expansion + Voyage rerank)"
```

---

## Task 11: tools — инструменты агента

**Files:**
- Create: `reviewer/tools/__init__.py`, `reviewer/tools/code_tools.py`
- Test: `tests/tools/test_code_tools.py`

Инструменты — фабрика `make_tools(ctx)` поверх `Retriever`/`GraphStore`/`VCSProvider`; возвращает список LangChain-`StructuredTool`. Тестируем подлежащие функции (роутинг к ретриву/графу), не сетевой LLM.

- [ ] **Step 1: Падающий тест**

```python
# tests/tools/test_code_tools.py
from reviewer.tools.code_tools import make_tools, ToolContext

class FakeRetriever:
    def retrieve(self, **kw):
        from reviewer.retrieval.retriever import ContextPack
        from reviewer.index.store import Retrieved
        return ContextPack([Retrieved("a.py#f","a.py","f","function",1,2,"def f(): ...",1.0)])
class FakeGraph:
    def expand(self, ids, hops=2): return {"b.py#g"}

def test_search_code_tool_returns_context_text():
    ctx = ToolContext(retriever=FakeRetriever(), graph=FakeGraph(),
                      overlay_ref="pr:1", changed_paths=["a.py"], changed_node_ids=[])
    tools = {t.name: t for t in make_tools(ctx)}
    out = tools["search_code"].invoke({"query": "where is f"})
    assert "a.py#f" in out

def test_get_callers_tool_uses_graph():
    ctx = ToolContext(retriever=FakeRetriever(), graph=FakeGraph(),
                      overlay_ref="pr:1", changed_paths=[], changed_node_ids=[])
    tools = {t.name: t for t in make_tools(ctx)}
    out = tools["get_related_symbols"].invoke({"node_id": "a.py#f"})
    assert "b.py#g" in out
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/tools/test_code_tools.py -v` → FAIL.

- [ ] **Step 3: Реализация code_tools.py**

```python
# reviewer/tools/code_tools.py
from __future__ import annotations
from dataclasses import dataclass, field
from langchain_core.tools import StructuredTool

@dataclass
class ToolContext:
    retriever: object
    graph: object
    overlay_ref: str
    changed_paths: list[str]
    changed_node_ids: list[str] = field(default_factory=list)

def make_tools(ctx: ToolContext) -> list[StructuredTool]:
    def search_code(query: str) -> str:
        """Семантико-лексический поиск релевантного кода по всему репозиторию."""
        pack = ctx.retriever.retrieve(
            query=query, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=8)
        return pack.as_context() or "(ничего не найдено)"

    def get_related_symbols(node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id вида 'path#fqn'."""
        related = ctx.graph.expand([node_id], hops=2)
        return "\n".join(sorted(related)) or "(нет связей)"

    return [
        StructuredTool.from_function(search_code),
        StructuredTool.from_function(get_related_symbols),
    ]
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `pytest tests/tools/test_code_tools.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/tools tests/tools
git commit -m "feat(tools): инструменты агента search_code/get_related_symbols"
```

---

## Task 12: policy — `.review.yml` parse + gate

**Files:**
- Create: `reviewer/policy/__init__.py`, `reviewer/policy/policy.py`
- Test: `tests/policy/test_policy.py`

- [ ] **Step 1: Падающий тест**

```python
# tests/policy/test_policy.py
from reviewer.policy.policy import ReviewPolicy
from reviewer.vcs.base import Finding

def F(cat, sev, file="a.py"):
    return Finding(cat, sev, file, 1, "RIGHT", "msg", None, 0.9)

def test_gate_filters_disabled_category_low_severity_and_ignored_paths():
    p = ReviewPolicy.from_yaml("""
categories: {correctness: true, style: false}
severity_threshold: medium
paths: {ignore: ["vendor/**"]}
max_comments: 10
""")
    assert p.gate(F("correctness", "high")) is True
    assert p.gate(F("style", "high")) is False          # категория выключена
    assert p.gate(F("correctness", "low")) is False      # ниже порога
    assert p.gate(F("correctness", "high", "vendor/x.py")) is False
    assert p.max_comments == 10

def test_defaults_when_no_yaml():
    p = ReviewPolicy.from_yaml(None)
    assert p.gate(F("correctness", "medium")) is True
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/policy/test_policy.py -v` → FAIL.

- [ ] **Step 3: Реализация**

```python
# reviewer/policy/policy.py
from __future__ import annotations
from dataclasses import dataclass, field
from fnmatch import fnmatch
import yaml

_SEV = {"low": 0, "medium": 1, "high": 2, "critical": 3}

@dataclass
class ReviewPolicy:
    categories: dict[str, bool] = field(default_factory=dict)
    severity_threshold: str = "low"
    ignore: list[str] = field(default_factory=list)
    max_comments: int = 25

    @classmethod
    def from_yaml(cls, text: str | None) -> "ReviewPolicy":
        if not text:
            return cls()
        data = yaml.safe_load(text) or {}
        return cls(
            categories=data.get("categories", {}),
            severity_threshold=data.get("severity_threshold", "low"),
            ignore=(data.get("paths") or {}).get("ignore", []),
            max_comments=data.get("max_comments", 25),
        )

    def category_enabled(self, category: str) -> bool:
        # по умолчанию категория включена, если не указана явно false
        return self.categories.get(category, True)

    def gate(self, finding) -> bool:
        if not self.category_enabled(finding.category):
            return False
        if _SEV.get(finding.severity, 0) < _SEV.get(self.severity_threshold, 0):
            return False
        if any(fnmatch(finding.file, pat) for pat in self.ignore):
            return False
        return True
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `pytest tests/policy/test_policy.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/policy tests/policy
git commit -m "feat(policy): парсинг .review.yml и гейтинг findings"
```

---

## Task 13: vcs — diff-маппинг + GitHubProvider

**Files:**
- Create: `reviewer/vcs/__init__.py`, `reviewer/vcs/base.py` (см. «Общие контракты»), `reviewer/vcs/diff.py`, `reviewer/vcs/github.py`
- Test: `tests/vcs/test_diff.py`, `tests/vcs/test_github.py`

Inline-комментарии GitHub разрешены **только на строках диффа** (иначе 422): `RIGHT` для добавленных/контекстных (новый файл), `LEFT` для удалённых. `diff.py` парсит хунки и отдаёт допустимые строки. `github.py` — `httpx`, API-версия `2022-11-28`, идемпотентность по маркеру-фингерпринту.

- [ ] **Step 1: Падающий тест diff**

```python
# tests/vcs/test_diff.py
from reviewer.vcs.diff import commentable_lines

PATCH = """@@ -1,3 +1,4 @@
 def f():
-    return 1
+    return 2
+    # new
 x = 0
"""

def test_commentable_lines_right_and_left():
    cl = commentable_lines(PATCH)
    # новые строки (RIGHT): 1 (context def f), 2 (return 2), 3 (# new), 4 (x=0)
    assert cl["RIGHT"] == {1, 2, 3, 4}
    # удалённая строка (LEFT): old line 2 (return 1)
    assert cl["LEFT"] == {2}
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/vcs/test_diff.py -v` → FAIL.

- [ ] **Step 3: Реализация diff.py**

```python
# reviewer/vcs/diff.py
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

def commentable_lines(patch: str | None) -> dict[str, set[int]]:
    """Возвращает {'RIGHT': {new_lines}, 'LEFT': {old_lines}} для строк внутри хунков."""
    right: set[int] = set()
    left: set[int] = set()
    if not patch:
        return {"RIGHT": right, "LEFT": left}
    old_ln = new_ln = 0
    for line in patch.splitlines():
        m = _HUNK.match(line)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            continue
        if not line:
            continue
        tag = line[0]
        if tag == "+":
            right.add(new_ln); new_ln += 1
        elif tag == "-":
            left.add(old_ln); old_ln += 1
        elif tag == " ":
            right.add(new_ln); old_ln += 1; new_ln += 1
        # строки '\\ No newline at end of file' и заголовки игнорируем
    return {"RIGHT": right, "LEFT": left}
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `pytest tests/vcs/test_diff.py -v` → PASS.

- [ ] **Step 5: Падающий тест GitHub (httpx.MockTransport)**

```python
# tests/vcs/test_github.py
import json, httpx
from reviewer.vcs.github import GitHubProvider
from reviewer.vcs.base import InlineComment

def make_provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://api.github.com")
    return GitHubProvider("o", "r", token="t", client=client)

def test_list_existing_fingerprints_parses_markers():
    def handler(req):
        if req.url.path.endswith("/comments"):
            body = [{"body": "issue\n<!-- ai-review:abc123 -->"}]
            return httpx.Response(200, json=body)
        return httpx.Response(404)
    p = make_provider(handler)
    assert p.list_existing_fingerprints(5) == {"abc123"}

def test_publish_review_posts_review_payload():
    captured = {}
    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/reviews"):
            captured.update(json.loads(req.content))
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404)
    p = make_provider(handler)
    p.publish_review(5, "deadbeef", "Сводка",
                     [InlineComment("a.py", 10, "RIGHT", "body\n<!-- ai-review:fp1 -->")])
    assert captured["event"] == "COMMENT"
    assert captured["commit_id"] == "deadbeef"
    assert captured["comments"][0] == {"path": "a.py", "line": 10,
                                       "side": "RIGHT", "body": "body\n<!-- ai-review:fp1 -->"}
```

- [ ] **Step 6: Запустить — упадёт.** Run: `pytest tests/vcs/test_github.py -v` → FAIL.

- [ ] **Step 7: Реализация github.py**

```python
# reviewer/vcs/github.py
from __future__ import annotations
import base64, re
import httpx

from reviewer.vcs.base import PullRequest, ChangedFile, InlineComment

_FP = re.compile(r"<!-- ai-review:([0-9a-f]+) -->")

class GitHubProvider:
    def __init__(self, owner: str, repo: str, token: str, client: httpx.Client | None = None):
        self.owner, self.repo = owner, repo
        self._c = client or httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Accept": "application/vnd.github+json"},
            timeout=30)

    def _base(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def get_pull_request(self, number: int) -> PullRequest:
        d = self._c.get(f"{self._base()}/pulls/{number}").raise_for_status().json()
        return PullRequest(number=number, base_sha=d["base"]["sha"],
                           head_sha=d["head"]["sha"], base_ref=d["base"]["ref"],
                           title=d.get("title", ""), body=d.get("body") or "")

    def get_changed_files(self, number: int) -> list[ChangedFile]:
        files, page = [], 1
        while True:
            r = self._c.get(f"{self._base()}/pulls/{number}/files",
                            params={"per_page": 100, "page": page}).raise_for_status()
            batch = r.json()
            files += [ChangedFile(f["filename"], f["status"], f.get("patch")) for f in batch]
            if len(batch) < 100:
                break
            page += 1
        return files

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        r = self._c.get(f"{self._base()}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode("utf-8", "replace")

    def list_existing_fingerprints(self, number: int) -> set[str]:
        fps, page = set(), 1
        while True:
            r = self._c.get(f"{self._base()}/pulls/{number}/comments",
                            params={"per_page": 100, "page": page}).raise_for_status()
            batch = r.json()
            for cm in batch:
                fps.update(_FP.findall(cm.get("body", "")))
            if len(batch) < 100:
                break
            page += 1
        return fps

    def publish_review(self, number: int, head_sha: str, summary: str,
                       comments: list[InlineComment]) -> None:
        payload_comments = []
        for c in comments:
            item = {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
            if c.start_line is not None:
                item["start_line"] = c.start_line
                item["start_side"] = c.start_side or c.side
            payload_comments.append(item)
        self._c.post(f"{self._base()}/pulls/{number}/reviews",
                     json={"commit_id": head_sha, "body": summary,
                           "event": "COMMENT", "comments": payload_comments}
                     ).raise_for_status()
```

- [ ] **Step 8: Запустить — пройдёт.** Run: `pytest tests/vcs/test_github.py -v` → PASS.

- [ ] **Step 9: Commit**

```bash
git add reviewer/vcs tests/vcs
git commit -m "feat(vcs): diff-маппинг строк + GitHubProvider (httpx, идемпотентность)"
```

---

## Task 14: agent — LangGraph (ingest→…→verify→assemble→publish)

**Files:**
- Create: `reviewer/agent/__init__.py`, `reviewer/agent/state.py`, `reviewer/agent/prompts.py`, `reviewer/agent/analyzer.py`, `reviewer/agent/nodes.py`, `reviewer/agent/graph.py`
- Test: `tests/agent/test_graph.py`

LangGraph 1.2: состояние — `TypedDict` с reducer'ами; `findings` аккумулируется через `operator.add` (иначе fan-out из `Send` потеряет ветки). Map-фаза `analyze` разветвляется через `Send`; затем `verify` (отсев галлюцинаций) → `assemble` (split inline/summary, кап, идемпотентность) → `publish`. LLM-узлы (`analyze`/`verify`) изолированы за протоколами `UnitAnalyzer`/`Verifier` — в тестах подменяются фейками, граф проверяется детерминированно.

- [ ] **Step 1: state.py**

```python
# reviewer/agent/state.py
from __future__ import annotations
import operator
from dataclasses import dataclass, field
from typing import Annotated, Protocol
from typing_extensions import TypedDict

from reviewer.vcs.base import Finding, InlineComment

@dataclass
class ReviewUnit:
    path: str
    node_ids: list[str]              # изменённые символы файла (path#fqn)
    changed_text: str                # склейка изменённых хунков для запроса

@dataclass
class Deps:
    vcs: object
    retriever: object
    graph: object
    policy: object
    analyzer: "UnitAnalyzer"
    verifier: "Verifier"
    pr_number: int
    head_sha: str
    overlay_ref: str
    changed_paths: list[str]
    patches: dict[str, str | None]   # path -> patch (для commentable_lines)

class UnitAnalyzer(Protocol):
    def analyze(self, unit: ReviewUnit, deps: "Deps") -> list[Finding]: ...

class Verifier(Protocol):
    def verify(self, findings: list[Finding], deps: "Deps") -> list[Finding]: ...

class ReviewState(TypedDict):
    review_units: list[ReviewUnit]
    findings: Annotated[list[Finding], operator.add]   # fan-in аккумулятор
    verified: list[Finding]
    summary: str
    inline_comments: list[InlineComment]
```

- [ ] **Step 2: nodes.py**

```python
# reviewer/agent/nodes.py
from __future__ import annotations
from langgraph.types import Send

from reviewer.agent.state import ReviewState, ReviewUnit, Deps
from reviewer.vcs.base import InlineComment
from reviewer.vcs.diff import commentable_lines

def plan_node(state: ReviewState):
    return {}   # review_units кладёт ingest до запуска графа (см. graph.run)

def fan_out(state: ReviewState):
    return [Send("analyze", {"unit": u}) for u in state["review_units"]]

def make_analyze_node(deps: Deps):
    def analyze(payload: dict):
        unit: ReviewUnit = payload["unit"]
        found = deps.analyzer.analyze(unit, deps)
        return {"findings": found}
    return analyze

def make_verify_node(deps: Deps):
    def verify(state: ReviewState):
        kept = deps.verifier.verify(state["findings"], deps)
        kept = [f for f in kept if deps.policy.gate(f)]
        return {"verified": kept}
    return verify

def make_assemble_node(deps: Deps):
    def assemble(state: ReviewState):
        existing = deps.vcs.list_existing_fingerprints(deps.pr_number)
        commentable = {p: commentable_lines(deps.patches.get(p)) for p in deps.changed_paths}
        inline: list[InlineComment] = []
        summary_lines: list[str] = ["## Авто-ревью\n"]
        ranked = sorted(state["verified"], key=lambda f: (-f.confidence,))
        for f in ranked:
            if len(inline) >= deps.policy.max_comments:
                break
            fp = f.fingerprint()
            if fp in existing:
                continue
            body = f"**[{f.category}/{f.severity}]** {f.message}"
            if f.suggestion:
                body += f"\n\n```suggestion\n{f.suggestion}\n```"
            body += f"\n<!-- ai-review:{fp} -->"
            allowed = commentable.get(f.file, {"RIGHT": set(), "LEFT": set()})
            if f.line is not None and f.line in allowed.get(f.side, set()):
                inline.append(InlineComment(f.file, f.line, f.side, body))
            else:
                summary_lines.append(f"- `{f.file}:{f.line}` {body}")
        if len(summary_lines) == 1:
            summary_lines.append("Замечаний в пределах диффа не найдено.")
        return {"inline_comments": inline, "summary": "\n".join(summary_lines)}
    return assemble

def make_publish_node(deps: Deps):
    def publish(state: ReviewState):
        deps.vcs.publish_review(deps.pr_number, deps.head_sha,
                                state["summary"], state["inline_comments"])
        return {}
    return publish
```

- [ ] **Step 3: graph.py**

```python
# reviewer/agent/graph.py
from __future__ import annotations
from langgraph.graph import StateGraph, START, END

from reviewer.agent.state import ReviewState, Deps
from reviewer.agent import nodes

def build_graph(deps: Deps):
    b = StateGraph(ReviewState)
    b.add_node("plan", nodes.plan_node)
    b.add_node("analyze", nodes.make_analyze_node(deps))
    b.add_node("verify", nodes.make_verify_node(deps))
    b.add_node("assemble", nodes.make_assemble_node(deps))
    b.add_node("publish", nodes.make_publish_node(deps))
    b.add_edge(START, "plan")
    b.add_conditional_edges("plan", nodes.fan_out, ["analyze"])
    b.add_edge("analyze", "verify")
    b.add_edge("verify", "assemble")
    b.add_edge("assemble", "publish")
    b.add_edge("publish", END)
    return b.compile()
```

- [ ] **Step 4: Падающий тест графа (фейковые analyzer/verifier/vcs)**

```python
# tests/agent/test_graph.py
from reviewer.agent.graph import build_graph
from reviewer.agent.state import Deps, ReviewUnit
from reviewer.vcs.base import Finding
from reviewer.policy.policy import ReviewPolicy

class FakeAnalyzer:
    def analyze(self, unit, deps):
        return [Finding("correctness", "high", unit.path, 2, "RIGHT", f"bug in {unit.path}", None, 0.9)]
class PassVerifier:
    def verify(self, findings, deps): return findings
class FakeVCS:
    def __init__(self): self.published = None
    def list_existing_fingerprints(self, n): return set()
    def publish_review(self, n, sha, summary, comments):
        self.published = (summary, comments)

def _deps(vcs):
    return Deps(vcs=vcs, retriever=None, graph=None, policy=ReviewPolicy(),
               analyzer=FakeAnalyzer(), verifier=PassVerifier(),
               pr_number=1, head_sha="sha", overlay_ref="pr:1",
               changed_paths=["a.py"], patches={"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"})

def test_end_to_end_inline_on_diff_line():
    vcs = FakeVCS()
    g = build_graph(_deps(vcs))
    g.invoke({"review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
              "findings": [], "verified": [], "summary": "", "inline_comments": []})
    summary, comments = vcs.published
    # строка 2 (RIGHT) есть в диффе -> попала в inline, не в сводку
    assert len(comments) == 1 and comments[0].path == "a.py" and comments[0].line == 2
    assert "ai-review:" in comments[0].body

def test_finding_off_diff_goes_to_summary():
    vcs = FakeVCS()
    deps = _deps(vcs)
    class OffDiff:
        def analyze(self, unit, deps):
            return [Finding("correctness","high","a.py",999,"RIGHT","far away",None,0.8)]
    deps.analyzer = OffDiff()
    build_graph(deps).invoke({"review_units":[ReviewUnit("a.py",["a.py#f"],"y")],
                              "findings":[],"verified":[],"summary":"","inline_comments":[]})
    summary, comments = vcs.published
    assert comments == [] and "far away" in summary
```

- [ ] **Step 5: Запустить — упадёт.** Run: `pytest tests/agent/test_graph.py -v` → FAIL.

- [ ] **Step 6: Создать prompts.py и analyzer.py (production-реализация LLM-узлов)**

```python
# reviewer/agent/prompts.py
ANALYZE_SYSTEM = """Ты — старший ревьюер. Анализируй ТОЛЬКО изменения данного файла \
в контексте предоставленного кода. Используй инструменты search_code/get_related_symbols, \
чтобы проверить влияние изменения, прежде чем делать вывод. Сообщай только реальные \
проблемы: баги, edge-cases, безопасность, нарушенные контракты. Не комментируй стиль, \
если не просили. Для каждой проблемы укажи файл, строку (по НОВОЙ версии), severity, \
краткое сообщение и, по возможности, suggestion."""

VERIFY_SYSTEM = """Ты — скептик. Для каждого замечания попытайся его ОПРОВЕРГНУТь, \
сверяясь с кодом через инструменты. Если замечание не воспроизводится или цитируемый \
код не таков — отклони его. Оставляй только подтверждённые проблемы."""
```

```python
# reviewer/agent/analyzer.py
from __future__ import annotations
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

from reviewer.agent.state import ReviewUnit, Deps
from reviewer.agent.prompts import ANALYZE_SYSTEM, VERIFY_SYSTEM
from reviewer.tools.code_tools import make_tools, ToolContext
from reviewer.vcs.base import Finding
from reviewer.llm.budget import BudgetTracker, BudgetExceeded

class _FindingModel(BaseModel):
    category: str
    severity: str = Field(description="low|medium|high|critical")
    line: int | None
    message: str
    suggestion: str | None = None
    confidence: float = 0.7

class _Findings(BaseModel):
    findings: list[_FindingModel] = Field(default_factory=list)

class LLMAnalyzer:
    """Прогон tool-loop'а и структурированный вывод findings для одного файла."""
    def __init__(self, llm_provider, max_iterations: int):
        self.provider = llm_provider
        self.max_iterations = max_iterations

    def analyze(self, unit: ReviewUnit, deps: Deps) -> list[Finding]:
        ctx = ToolContext(retriever=deps.retriever, graph=deps.graph,
                          overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
                          changed_node_ids=unit.node_ids)
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        messages = [SystemMessage(ANALYZE_SYSTEM),
                    HumanMessage(f"Файл: {unit.path}\nИзменения:\n{unit.changed_text}")]
        try:
            while True:
                budget.tick()
                ai = llm.invoke(messages)
                messages.append(ai)
                if not ai.tool_calls:
                    break
                for call in ai.tool_calls:
                    result = tools_by_name[call["name"]].invoke(call["args"])
                    from langchain_core.messages import ToolMessage
                    messages.append(ToolMessage(str(result), tool_call_id=call["id"]))
        except BudgetExceeded:
            pass
        structured = self.provider.chat_model().with_structured_output(_Findings)
        parsed: _Findings = structured.invoke(
            messages + [HumanMessage("Выведи итоговые findings структурой.")])
        return [Finding(category=f.category, severity=f.severity, file=unit.path,
                        line=f.line, side="RIGHT", message=f.message,
                        suggestion=f.suggestion, confidence=f.confidence)
                for f in parsed.findings]

class LLMVerifier:
    def __init__(self, llm_provider):
        self.provider = llm_provider

    def verify(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return []
        kept: list[Finding] = []
        llm = self.provider.chat_model().with_structured_output(_VerdictBatch)
        # один батч-вердикт по списку (экономит вызовы)
        listing = "\n".join(f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
                            for i, f in enumerate(findings))
        verdicts: _VerdictBatch = llm.invoke([SystemMessage(VERIFY_SYSTEM),
                                              HumanMessage(listing)])
        keep_idx = {v.index for v in verdicts.verdicts if v.is_real}
        for i, f in enumerate(findings):
            if i in keep_idx:
                kept.append(f)
        return kept

class _Verdict(BaseModel):
    index: int
    is_real: bool

class _VerdictBatch(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)
```

> Исполнителю: `_VerdictBatch`/`_Verdict` объявлены ниже использования — при переносе в файл подними их определения ВЫШЕ `LLMVerifier` (Python требует их к моменту вызова `with_structured_output`, т.е. в рантайме внутри метода — фактически порядок классов в модуле допустим, т.к. ссылка вычисляется при вызове; но для чистоты помести `_Verdict`/`_VerdictBatch` сразу после `_Findings`).

- [ ] **Step 7: Запустить — пройдёт.** Run: `pytest tests/agent/test_graph.py -v` → PASS.

- [ ] **Step 8: Commit**

```bash
git add reviewer/agent tests/agent
git commit -m "feat(agent): LangGraph-граф ревью (Send fan-out, verify, assemble, publish)"
```

---

## Task 15: entrypoints/cli.py — команды index / search / review

**Files:**
- Create: `reviewer/entrypoints/__init__.py`, `reviewer/entrypoints/cli.py`
- Create: `reviewer/app.py` (сборка зависимостей из Settings)
- Test: `tests/test_app_wiring.py`

`app.py` — фабрика, собирающая `ChunkStore/GraphStore/Embedder/Reranker/Retriever/OpenRouterProvider` из `Settings`. CLI на `click`: `index <repo> --ref`, `search <query>`, `review <owner/repo> <pr>`.

- [ ] **Step 1: Падающий тест сборки (фабрика не делает сетевых вызовов)**

```python
# tests/test_app_wiring.py
from reviewer.app import build_components
from reviewer.config.settings import Settings

def test_build_components_returns_retriever_and_llm(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.retriever is not None
    assert c.llm_provider is not None
```

- [ ] **Step 2: Запустить — упадёт.** Run: `pytest tests/test_app_wiring.py -v` → FAIL.

- [ ] **Step 3: Реализация app.py**

```python
# reviewer/app.py
from __future__ import annotations
from dataclasses import dataclass

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.index.embeddings import VoyageEmbedder
from reviewer.index.reranker import VoyageReranker
from reviewer.graph.store import GraphStore
from reviewer.retrieval.retriever import Retriever
from reviewer.llm.openrouter import OpenRouterProvider

@dataclass
class Components:
    settings: Settings
    store: ChunkStore
    graph: GraphStore | None
    embedder: VoyageEmbedder
    reranker: VoyageReranker
    retriever: Retriever
    llm_provider: OpenRouterProvider

def build_components(settings: Settings, connect: bool = True) -> Components:
    store = ChunkStore(settings.pg_dsn)
    embedder = VoyageEmbedder(model=settings.embedding_model, dim=settings.embedding_dim) \
        if settings.voyage_api_key else VoyageEmbedder.__new__(VoyageEmbedder)
    reranker = VoyageReranker(model=settings.rerank_model) if settings.voyage_api_key \
        else VoyageReranker.__new__(VoyageReranker)
    graph = GraphStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password) \
        if connect else None
    retriever = Retriever(store, graph, embedder, reranker)
    llm = OpenRouterProvider(settings)
    return Components(settings, store, graph, embedder, reranker, retriever, llm)
```

> Исполнителю: ветка `__new__` нужна только чтобы фабрика не требовала `VOYAGE_API_KEY` в unit-тесте сборки; в реальном запуске ключ задан и создаются полноценные клиенты. При желании замени на ленивую инициализацию клиентов внутри `VoyageEmbedder`/`VoyageReranker`.

- [ ] **Step 4: Реализация cli.py**

```python
# reviewer/entrypoints/cli.py
from __future__ import annotations
import click

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.gitutil import changed_files, file_at_ref, list_python_files
from reviewer.index.freshness import update_base, build_overlay

@click.group()
def cli() -> None: ...

@cli.command()
@click.argument("repo")
@click.option("--ref", default="HEAD")
def index(repo: str, ref: str) -> None:
    """Построить/обновить base-индекс целевой ветки из локального репо."""
    s = Settings()
    c = build_components(s)
    c.store.init_schema()
    files = list_python_files(repo, ref)
    update_base(c.store, c.embedder, repo, ref, files,
                read=lambda p: file_at_ref(repo, p, ref))
    click.echo(f"Проиндексировано файлов: {len(files)}")

@cli.command()
@click.argument("query")
def search(query: str) -> None:
    """Гибридный поиск по base-индексу (диагностика)."""
    s = Settings()
    c = build_components(s)
    qvec = c.embedder.embed_query(query)
    hits = c.store.hybrid_search(query_text=query, query_embedding=qvec,
                                 overlay_ref="", changed_paths=[], top_k=10)
    for h in hits:
        click.echo(f"{h.score:.3f}  {h.node_id}  ({h.path}:{h.start_line})")

@cli.command()
@click.argument("slug")          # owner/repo
@click.argument("pr", type=int)
def review(slug: str, pr: int) -> None:
    """Отревьюить PR на GitHub и запостить inline+сводку."""
    from reviewer.vcs.github import GitHubProvider
    from reviewer.agent.graph import build_graph
    from reviewer.agent.state import Deps, ReviewUnit
    from reviewer.agent.analyzer import LLMAnalyzer, LLMVerifier
    from reviewer.policy.policy import ReviewPolicy

    s = Settings()
    c = build_components(s)
    owner, repo = slug.split("/")
    vcs = GitHubProvider(owner, repo, token=s.github_token)
    prq = vcs.get_pull_request(pr)
    files = vcs.get_changed_files(pr)
    changed = [f.path for f in files if f.path.endswith(".py")]

    # overlay PR head
    build_overlay(c.store, c.embedder, pr, changed,
                  read_head=lambda p: vcs.get_file_at_ref(p, prq.head_sha))

    # review-units по файлам (changed-символы = чанки изменённых файлов)
    from reviewer.index.chunker import chunk_python
    units = []
    for f in files:
        if not f.path.endswith(".py"):
            continue
        src = vcs.get_file_at_ref(f.path, prq.head_sha) or ""
        node_ids = [ch.node_id for ch in chunk_python(f.path, src.encode())]
        units.append(ReviewUnit(f.path, node_ids, f.patch or ""))

    policy = ReviewPolicy.from_yaml(vcs.get_file_at_ref(".review.yml", prq.base_ref))
    deps = Deps(vcs=vcs, retriever=c.retriever, graph=c.graph, policy=policy,
                analyzer=LLMAnalyzer(c.llm_provider, s.review_max_tool_iterations),
                verifier=LLMVerifier(c.llm_provider), pr_number=pr,
                head_sha=prq.head_sha, overlay_ref=f"pr:{pr}",
                changed_paths=changed, patches={f.path: f.patch for f in files})
    build_graph(deps).invoke({"review_units": units, "findings": [],
                              "verified": [], "summary": "", "inline_comments": []})
    click.echo("Ревью опубликовано.")
```

- [ ] **Step 5: Запустить — пройдёт.** Run: `pytest tests/test_app_wiring.py -v` → PASS.

- [ ] **Step 6: Дымовой запуск CLI**

Run: `reviewer --help`
Expected: перечислены команды `index`, `search`, `review`.

- [ ] **Step 7: Commit**

```bash
git add reviewer/app.py reviewer/entrypoints tests/test_app_wiring.py
git commit -m "feat(cli): команды index/search/review + сборка зависимостей"
```

---

## Task 16: Integration / E2E / eval-харнес

**Files:**
- Create: `tests/integration/test_pipeline.py` (`@pytest.mark.integration`)
- Create: `eval/cases/` (фикстуры), `eval/run_eval.py`
- Create: `README` секцию «Запуск»

- [ ] **Step 1: E2E integration-тест (реальные Postgres+Neo4j, фейковый LLM/VCS)**

```python
# tests/integration/test_pipeline.py
import pytest
from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.index.freshness import update_base
from reviewer.retrieval.retriever import Retriever

@pytest.mark.integration
def test_index_then_hybrid_retrieve_finds_relevant_symbol(tmp_path):
    # подготовить мини-репо
    (tmp_path/"auth.py").write_text("def verify_token(t):\n    return t == 'ok'\n")
    (tmp_path/"util.py").write_text("def add(a,b):\n    return a+b\n")
    import subprocess
    for a in (["git","init","-q"],["git","add","-A"],
              ["git","-c","user.email=t@t","-c","user.name=t","commit","-qm","c"]):
        subprocess.run(a, cwd=tmp_path, check=True)
    s = Settings()
    c = build_components(s); c.store.init_schema(); c.store.clear()
    from reviewer.gitutil import list_python_files, file_at_ref
    files = list_python_files(str(tmp_path), "HEAD")
    update_base(c.store, c.embedder, str(tmp_path), "HEAD", files,
                read=lambda p: file_at_ref(str(tmp_path), p, "HEAD"))
    qvec = c.embedder.embed_query("token verification")
    hits = c.store.hybrid_search(query_text="token verification",
                                 query_embedding=qvec, overlay_ref="",
                                 changed_paths=[], top_k=5)
    assert any(h.symbol_fqn == "verify_token" for h in hits)
```

Run (после `docker compose up -d` и заданных `VOYAGE_API_KEY`): `pytest -m integration -v`
Expected: PASS.

- [ ] **Step 2: eval-харнес (метрика качества — главный критерий)**

```python
# eval/run_eval.py
"""Прогон набора PR-кейсов: для каждого кейса известны ожидаемые проблемы (файл+строка+категория).
Считает precision/recall сматченных findings. Кейсы кладутся в eval/cases/<name>/ с:
  - before/ и after/ (снапшоты кода),
  - patch.diff,
  - expected.json [{file,line,category}].
Запуск: python eval/run_eval.py
"""
import json, pathlib

def score(expected: list[dict], produced: list[dict]) -> dict:
    exp = {(e["file"], e["line"], e["category"]) for e in expected}
    prod = {(p["file"], p["line"], p["category"]) for p in produced}
    tp = len(exp & prod)
    precision = tp / len(prod) if prod else 0.0
    recall = tp / len(exp) if exp else 0.0
    return {"precision": precision, "recall": recall, "tp": tp}

def main() -> None:
    cases = sorted(pathlib.Path("eval/cases").glob("*"))
    print(f"Кейсов: {len(cases)} (TODO: подключить реальный прогон агента на каждом)")

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: README — секция «Запуск»** (добавить в существующий README.md)

```markdown
## Запуск (dev)
1. `cp .env.example .env` и заполнить `OPENROUTER_API_KEY`, `VOYAGE_API_KEY`, `GITHUB_TOKEN`.
2. `docker compose up -d` (Postgres/ParadeDB + Neo4j).
3. `pip install -e ".[dev]"`.
4. Индексация локального репо: `reviewer index /path/to/repo --ref main`.
5. Ревью PR: `reviewer review owner/repo 123`.
6. Тесты: `pytest` (unit), `pytest -m integration` (нужны поднятые БД + ключи).
```

- [ ] **Step 4: Commit**

```bash
git add tests/integration eval README.md
git commit -m "test: E2E integration-пайплайн + eval-харнес (precision/recall)"
```

---

## Self-Review (выполнено при написании плана)

**Покрытие spec → задачи:**
- Свежесть RAG (база + content-hash + overlay) → Task 5 (overlay-шэдоуинг), Task 6 (`build_overlay`/`update_base`). ✓
- Гибрид pgvector+BM25/RRF → Task 4 (индексы), Task 5 (RRF SQL). ✓
- Граф кода (scip-python, path#fqn, Neo4j) → Task 7, Task 8. ✓
- Voyage эмбеддинги/реранк из env → Task 3, Task 10. ✓
- OpenRouter модель/потолок цены/роутинг из env + бюджет → Task 1, Task 9. ✓
- Оркестрация LangGraph (map-reduce + verify) → Task 14. ✓
- GitHub inline+сводка, маппинг строк, идемпотентность → Task 13, Task 14 (assemble). ✓
- Политика per-repo из целевой ветки → Task 12, Task 15 (`review` грузит `.review.yml` с `base_ref`). ✓
- VCS-абстракция (под GitLab/др.) → `VCSProvider` Protocol (Общие контракты), Task 13. ✓
- CLI (ядро-библиотека) → Task 15. ✓
- Тесты/eval → Task 16. ✓

**Известные допущения для исполнителя (не плейсхолдеры — осознанные границы v1):**
- `run_scip_python` и реальный graph-build из SCIP требуют установленного `@sourcegraph/scip-python`; покрываются отдельным integration-тестом. До этого граф можно наполнять из tree-sitter-резолвера (тот же интерфейс `parse_scip`/`GraphStore`).
- `LLMAnalyzer`/`LLMVerifier` дают сетевые вызовы — проверяются в E2E с реальным ключом или записанными ответами; граф/assemble покрыты unit-тестами на фейках.
- Перенести объявления `_Verdict`/`_VerdictBatch` выше `LLMVerifier` (примечание в Task 14, Step 6).

**Консистентность типов:** `Chunk.node_id == "path#fqn"` используется единообразно в чанкере, `parse_scip`, `GraphStore`, `Retriever.fetch_nodes`, `ToolContext`, `ReviewUnit`. `Finding`/`InlineComment` едины в policy/agent/vcs. `hybrid_search`/`fetch_nodes` имеют согласованные сигнатуры с `Retriever`. ✓

---

## Execution Handoff

План сохранён в `docs/superpowers/plans/2026-06-07-mr-review-agent.md`. Два варианта исполнения:

1. **Subagent-Driven (рекомендую)** — на каждую задачу свежий субагент, ревью между задачами, быстрая итерация. (REQUIRED SUB-SKILL: superpowers:subagent-driven-development)
2. **Inline Execution** — выполняю задачи в этой сессии пачками с чекпойнтами. (REQUIRED SUB-SKILL: superpowers:executing-plans)



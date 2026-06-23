CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id           bigserial PRIMARY KEY,
    repo         text    NOT NULL DEFAULT '',  -- 'owner/name' (в нижнем регистре)
    ref          text    NOT NULL,             -- 'base' | 'pr:<number>'
    content_hash text    NOT NULL,
    path         text    NOT NULL,
    lang         text    NOT NULL,
    symbol_fqn   text    NOT NULL,
    kind         text    NOT NULL,
    start_line   int     NOT NULL,
    end_line     int     NOT NULL,
    text         text    NOT NULL,
    embedding    vector(1024)
);
-- Forward-only: добавить repo существующим деплоям до создания уникального ключа.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS repo text NOT NULL DEFAULT '';
-- Старый ключ без repo заменяем на repo-aware.
ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_ref_path_symbol_fqn_key;
ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_repo_ref_path_symbol_fqn_key;
ALTER TABLE chunks ADD CONSTRAINT chunks_repo_ref_path_symbol_fqn_key
    UNIQUE (repo, ref, path, symbol_fqn);

DROP INDEX IF EXISTS chunks_ref_path;
CREATE INDEX IF NOT EXISTS chunks_repo_ref_path ON chunks (repo, ref, path);
CREATE INDEX IF NOT EXISTS chunks_hash ON chunks (content_hash);

-- BM25 (pg_search): repo в stored-полях для фильтра по repo внутри CTE.
DROP INDEX IF EXISTS chunks_bm25;
CREATE INDEX IF NOT EXISTS chunks_bm25 ON chunks
USING bm25 (id, text, path, ref, repo) WITH (key_field='id');

-- ANN (pgvector HNSW, косинус)
CREATE INDEX IF NOT EXISTS chunks_hnsw ON chunks
USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Метаданные индексирования: SHA последней индексации по (repo, ref).
CREATE TABLE IF NOT EXISTS index_meta (
    repo       TEXT        NOT NULL DEFAULT '',
    ref        TEXT        NOT NULL,
    sha        TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, ref)
);
ALTER TABLE index_meta ADD COLUMN IF NOT EXISTS repo text NOT NULL DEFAULT '';
-- Forward-only: на уже существующей таблице PRIMARY KEY (ref) выше не применился
-- (CREATE TABLE IF NOT EXISTS — no-op). Перевешиваем PK на (repo, ref), иначе
-- ON CONFLICT (repo, ref) в set_index_meta падает на проапгрейженной БД.
ALTER TABLE index_meta DROP CONSTRAINT IF EXISTS index_meta_pkey;
ALTER TABLE index_meta ADD PRIMARY KEY (repo, ref);

-- Задачи доски (фаза 3): эмбеддинги (pgvector) + BM25 (pg_search) для search_tasks.
-- Отдельно от chunks — у задач нет path/symbol/lines и base/overlay-freshness.
CREATE TABLE IF NOT EXISTS tasks (
    id           bigserial PRIMARY KEY,
    key          text    NOT NULL UNIQUE,   -- канонический код задачи (ID-N / Jira key)
    aliases      text[]  NOT NULL DEFAULT '{}',
    title        text    NOT NULL,
    description  text    NOT NULL DEFAULT '',
    status       text,
    url          text,
    content_hash text    NOT NULL,          -- дедуп переэмбеда
    text         text    NOT NULL,          -- эмбед/BM25-текст: title + description + criteria
    embedding    vector(1024)
);
CREATE INDEX IF NOT EXISTS tasks_bm25 ON tasks
USING bm25 (id, text, key) WITH (key_field='id');
CREATE INDEX IF NOT EXISTS tasks_hnsw ON tasks
USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Предрасчитанные summary подсистем (PRI-159): кластер графа по модулю → краткий обзор.
-- Отдельно от chunks — у summary нет lines/symbol и base/overlay-freshness.
CREATE TABLE IF NOT EXISTS subsystem_summaries (
    repo            text    NOT NULL DEFAULT '',
    branch          text    NOT NULL,
    cluster_key     text    NOT NULL,            -- напр. "reviewer/index"
    title           text    NOT NULL,            -- одна строка «что это»
    summary         text    NOT NULL,            -- сжатый абзац (RU)
    member_node_ids text[]  NOT NULL DEFAULT '{}',
    source_hash     text    NOT NULL,            -- ключ свежести
    embedding       vector(1024),                -- nullable; зарезервировано под вектор-поиск
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, branch, cluster_key)
);

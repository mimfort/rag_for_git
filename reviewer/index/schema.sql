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

-- Метаданные индексирования: SHA последней индексации по ref
CREATE TABLE IF NOT EXISTS index_meta (
    ref        TEXT        PRIMARY KEY,
    sha        TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

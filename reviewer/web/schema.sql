-- Схема для хранения истории прогонов ревью и найденных проблем.
-- Применяется идемпотентно (CREATE TABLE IF NOT EXISTS) при старте serve
-- и при первой записи истории.

CREATE TABLE IF NOT EXISTS review_runs (
    id              BIGSERIAL    PRIMARY KEY,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    repo            TEXT         NOT NULL,
    pr_number       INT          NOT NULL,
    base_sha        TEXT,
    head_sha        TEXT,
    model           TEXT,
    model_verify    TEXT,
    dry_run         BOOL         NOT NULL DEFAULT false,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    duration_ms     INT,
    status          TEXT         NOT NULL DEFAULT 'ok',   -- ok | error | draft_skip
    files_reviewed  INT          NOT NULL DEFAULT 0,
    files_skipped   INT          NOT NULL DEFAULT 0,
    files_failed    INT          NOT NULL DEFAULT 0,
    findings_analyzed INT        NOT NULL DEFAULT 0,
    findings_kept   INT          NOT NULL DEFAULT 0,
    verify_rejected INT          NOT NULL DEFAULT 0,
    comments_inline  INT         NOT NULL DEFAULT 0,
    comments_summary INT         NOT NULL DEFAULT 0,
    usage           JSONB,
    total_cost      NUMERIC(12, 6),
    error_text      TEXT
);

CREATE INDEX IF NOT EXISTS review_runs_created_at ON review_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS review_runs_repo ON review_runs (repo);

CREATE TABLE IF NOT EXISTS review_findings (
    id          BIGSERIAL    PRIMARY KEY,
    run_id      BIGINT       NOT NULL
                    REFERENCES review_runs (id) ON DELETE CASCADE,
    file        TEXT         NOT NULL,
    line        INT,
    category    TEXT         NOT NULL,
    severity    TEXT         NOT NULL,
    confidence  REAL         NOT NULL DEFAULT 0,
    is_real     BOOL         NOT NULL DEFAULT true,
    published   BOOL         NOT NULL DEFAULT false,
    inline      BOOL         NOT NULL DEFAULT false,
    fingerprint TEXT,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS review_findings_run_id ON review_findings (run_id);

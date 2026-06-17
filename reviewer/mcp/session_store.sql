-- Подложка in-memory MCPReviewService._sessions: переживает рестарт процесса
-- reviewer-mcp между prepare_review и publish_review. Применяется идемпотентно.
CREATE TABLE IF NOT EXISTS review_sessions (
    repo       TEXT        NOT NULL,
    pr_number  INTEGER     NOT NULL,
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, pr_number)
);

CREATE INDEX IF NOT EXISTS review_sessions_created_idx ON review_sessions (created_at);

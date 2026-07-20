-- Подложка in-memory MCPReviewService._sessions: переживает рестарт процесса
-- reviewer-mcp между prepare_review и publish_review. Применяется идемпотентно.
CREATE TABLE IF NOT EXISTS review_sessions (
    repo       TEXT        NOT NULL,
    pr_number  INTEGER     NOT NULL,
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- PRI-212: последняя активность сессии (keepalive). NULL у legacy-строк —
    -- для них живость считается по created_at (COALESCE в предикатах чтения).
    last_seen_at TIMESTAMPTZ,
    PRIMARY KEY (repo, pr_number)
);

CREATE INDEX IF NOT EXISTS review_sessions_created_idx ON review_sessions (created_at);

-- PRI-212: аддитивная идемпотентная миграция уже существующих таблиц
-- (по прецеденту миграций review_findings).
ALTER TABLE review_sessions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

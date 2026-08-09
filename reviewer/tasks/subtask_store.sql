CREATE TABLE IF NOT EXISTS subtask_operations (
    idempotency_key  text PRIMARY KEY,
    board_type       text NOT NULL,
    parent_input_key text NOT NULL,
    parent_task_id   text NOT NULL,
    source_board_id  text NOT NULL,
    source_column_id text NOT NULL,
    request_hash     text NOT NULL,
    request_payload  jsonb NOT NULL,
    state            jsonb NOT NULL,
    status           text NOT NULL CHECK (
        status IN ('running', 'partial', 'board_complete', 'complete')
    ),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

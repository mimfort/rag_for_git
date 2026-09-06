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
    config_sources  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    total_cost      NUMERIC(12, 6),
    error_text      TEXT
);

-- Идемпотентная миграция для БД, где таблица уже существовала без provenance.
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS config_sources JSONB;
UPDATE review_runs SET config_sources = '{}'::jsonb WHERE config_sources IS NULL;
ALTER TABLE review_runs ALTER COLUMN config_sources SET DEFAULT '{}'::jsonb;
ALTER TABLE review_runs ALTER COLUMN config_sources SET NOT NULL;

CREATE INDEX IF NOT EXISTS review_runs_created_at ON review_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS review_runs_repo ON review_runs (repo);
CREATE INDEX IF NOT EXISTS review_runs_repo_created_at ON review_runs (repo, created_at DESC);

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
    message     TEXT,
    outcome     TEXT,        -- терминальный исход воронки (PRI); NULL = legacy
    reject_reason TEXT       -- причина reject (verify/gate); NULL = не отклонена/legacy
);

-- Идемпотентная миграция для БД, где таблица уже существовала без колонок исхода.
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS outcome       TEXT;
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS reject_reason TEXT;

-- Best-effort бэкфилл: опубликованным историческим строкам ставим исход по флагам.
-- NOT published (already_posted и находки error-прогонов) неоднозначны → NULL.
UPDATE review_findings SET outcome = 'published_inline'
    WHERE outcome IS NULL AND published AND inline;
UPDATE review_findings SET outcome = 'published_summary'
    WHERE outcome IS NULL AND published AND NOT inline;

CREATE INDEX IF NOT EXISTS review_findings_run_id ON review_findings (run_id);

CREATE TABLE IF NOT EXISTS review_steps (
    id          BIGSERIAL    PRIMARY KEY,
    run_id      BIGINT       NOT NULL
                    REFERENCES review_runs (id) ON DELETE CASCADE,
    stage       TEXT         NOT NULL,   -- analyze | verify | synthesize
    unit        TEXT         NOT NULL,   -- путь файла или "(синтез)"
    seq         INT          NOT NULL,   -- порядковый номер шага внутри прогона
    kind        TEXT         NOT NULL,   -- prompt | llm_call | tool_call
    name        TEXT,                    -- имя инструмента (для tool_call и llm_call)
    text        TEXT,                    -- текст ответа/результата (обрезан)
    tool_calls  JSONB,                   -- [{name, args}] для llm_call; [{name, args}] для tool_call
    tokens      INT          NOT NULL DEFAULT 0,
    cost        NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_steps_run_id_seq ON review_steps (run_id, seq);

-- Качество ретрива под бриф solve-task (PRI-249): одна строка на прогон ревью.
-- status отделяет «нет точки измерения» от нулевого recall: у задачи, чей diff
-- состоит только из тестов и доков, качество ретрива по ядру не определено, и
-- подмешивать её нулём в медиану значит систематически занижать метрику.
-- Множества путей хранятся, потому что офлайн-baseline посчитан по ЗАДАЧЕ
-- (объединение всех её PR), а онлайн видит по одному PR: без них task-level
-- число было бы посчитано другой линейкой и несравнимо с «до».
CREATE TABLE IF NOT EXISTS brief_quality (
    id                  BIGSERIAL   PRIMARY KEY,
    run_id              BIGINT      NOT NULL
                            REFERENCES review_runs (id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    repo                TEXT        NOT NULL,
    pr_number           INT         NOT NULL,
    task_key            TEXT,
    head_sha            TEXT,
    status              TEXT        NOT NULL,   -- measured | no_task_key | no_brief
                                                -- | brief_unreadable | empty_core_denominator
                                                -- | unconfigured_core_denominator
    brief_path          TEXT,
    expected            INT         NOT NULL DEFAULT 0,
    expected_core       INT         NOT NULL DEFAULT 0,
    predicted           INT         NOT NULL DEFAULT 0,
    hit_core            INT         NOT NULL DEFAULT 0,
    core_recall         REAL,                   -- NULL = нет точки измерения
    raw_recall          REAL,
    precision           REAL,
    misses              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    predicted_paths     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    expected_core_paths JSONB       NOT NULL DEFAULT '[]'::jsonb,
    hit_core_paths      JSONB       NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS brief_quality_repo_created_at
    ON brief_quality (repo, created_at DESC);
CREATE INDEX IF NOT EXISTS brief_quality_task_key ON brief_quality (task_key);

-- PRI-270: съём метрики переехал из publish_review в finish_task и CLI, где
-- прогона ревью нет вовсе, поэтому run_id перестаёт быть обязательным. FK с
-- ON DELETE CASCADE при этом не трогаем: NULL ему не подчиняется.
ALTER TABLE brief_quality ALTER COLUMN run_id DROP NOT NULL;

-- Схлопывание дублей перед уникальным индексом: до PRI-270 идентичности у
-- строки не было, и на деплое с историей их может оказаться несколько.
-- Выживает последняя (максимальный id) — она же самая свежая.
--
-- Guard по существованию индекса обязателен: DELETE ниже — единственная DML
-- в файле дешёвых idempotent-чеков (CREATE ... IF NOT EXISTS), а
-- `_SCHEMA` целиком выполняется при КАЖДОМ старте reviewer serve/reviewer-mcp
-- (`init_schema`). Уникальный индекс сам по себе не режет работу — он не
-- используется как short-circuit (план — Hash Join двух полных Seq Scan),
-- поэтому без guard'а каждый последующий рестарт платил бы растущий
-- полный скан таблицы за гарантированно нулевой эффект (после первого
-- успешного прогона дублей больше нет). Наличие индекса — надёжный сигнал
-- «схлопывание уже случилось»: он создаётся один раз ниже и переживает
-- рестарты.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'brief_quality_identity'
    ) THEN
        DELETE FROM brief_quality a
            USING brief_quality b
            WHERE a.repo = b.repo
              AND a.pr_number = b.pr_number
              AND COALESCE(a.task_key, '') = COALESCE(b.task_key, '')
              AND a.id < b.id;
    END IF;
END $$;

-- COALESCE обязателен: в SQL NULL ≠ NULL, и обычный UNIQUE не покрыл бы
-- строки без task_key — а именно они пишутся при съёме без ключа задачи.
CREATE UNIQUE INDEX IF NOT EXISTS brief_quality_identity
    ON brief_quality (repo, pr_number, (COALESCE(task_key, '')));

-- DABOOK SQLite schema v1
-- All connections must set: journal_mode=WAL, synchronous=NORMAL, busy_timeout=10000, foreign_keys=ON
-- Requires SQLite >= 3.35 (RETURNING clause)

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=10000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- Sentinel: current schema version
INSERT OR IGNORE INTO schema_meta (k, v) VALUES ('schema_version', '1');

-- ---------------------------------------------------------------------------
-- Runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  REAL    NOT NULL,
    ended_at    REAL,
    argv        TEXT,
    host        TEXT,
    dabook_version TEXT,
    status      TEXT    NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','done','interrupted','error'))
);

-- ---------------------------------------------------------------------------
-- Books
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY,
    sha256          TEXT    NOT NULL UNIQUE,
    path            TEXT    NOT NULL,
    title           TEXT,
    pages           INTEGER,
    size_bytes      INTEGER,
    doc_type        TEXT,           -- native | scanned | mixed
    genre           TEXT,           -- programming | textbook | novel | ...
    state           TEXT    NOT NULL DEFAULT 'queued'
                            CHECK (state IN (
                                'queued','active','paused','needs_input',
                                'review','done','failed','removed'
                            )),
    priority        INTEGER NOT NULL DEFAULT 0,
    paused          INTEGER NOT NULL DEFAULT 0,
    profile         TEXT,
    license_status  TEXT    DEFAULT 'unknown',
    added_at        REAL    NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    quality_score   REAL,
    quality_calibrated INTEGER DEFAULT 0,
    error           TEXT
);

-- ---------------------------------------------------------------------------
-- Tasks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY,
    book_id         INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    stage           TEXT    NOT NULL,
    stage_order     INTEGER NOT NULL,
    shard_idx       INTEGER NOT NULL DEFAULT 0,
    page_start      INTEGER,
    page_end        INTEGER,
    stage_key       TEXT    NOT NULL,
    resource_class  TEXT    NOT NULL
                            CHECK (resource_class IN ('cpu','gpu','io','llm')),
    state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN (
                                'pending','running','done','failed','dead',
                                'skipped','cancelled'
                            )),
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    not_before      REAL    NOT NULL DEFAULT 0,
    worker_id       TEXT,
    lease_expires   REAL,
    started_at      REAL,
    finished_at     REAL,
    duration_ms     INTEGER,
    units_done      INTEGER NOT NULL DEFAULT 0,
    units_total     INTEGER,
    substep         TEXT,           -- live sub-step label shown in dashboard
    error_class     TEXT,           -- oom | timeout | corrupt_pdf | model_error | disk_full | bug
    error_msg       TEXT,
    output_path     TEXT,
    output_sha256   TEXT,
    UNIQUE (book_id, stage, shard_idx, stage_key)
);

CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (
    state, resource_class, not_before, book_id, stage_order, shard_idx
);
CREATE INDEX IF NOT EXISTS idx_tasks_book ON tasks (book_id, stage, state);

-- ---------------------------------------------------------------------------
-- Task dependencies (DAG edges)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_deps (
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

-- ---------------------------------------------------------------------------
-- Workers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workers (
    id              TEXT    PRIMARY KEY,
    pid             INTEGER NOT NULL,
    resource_class  TEXT    NOT NULL,
    state           TEXT    NOT NULL
                            CHECK (state IN ('starting','idle','busy','draining','dead')),
    current_task    INTEGER REFERENCES tasks(id),
    heartbeat       REAL,
    rss_mb          REAL,
    gpu_index       INTEGER,
    started_at      REAL    NOT NULL,
    models_loaded   INTEGER DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Live settings (polled every 2 s by workers/supervisor)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    k           TEXT    PRIMARY KEY,
    v           TEXT    NOT NULL,
    live        INTEGER NOT NULL DEFAULT 1,  -- 1 = live-changeable; 0 = requires restart
    updated_at  REAL    NOT NULL
);

-- Defaults (supervisor inserts these on first run; INSERT OR IGNORE)
INSERT OR IGNORE INTO settings (k, v, live, updated_at) VALUES
    ('workers.cpu',         '4',   1, 0),
    ('workers.gpu',         '1',   1, 0),
    ('workers.io',          '2',   1, 0),
    ('workers.llm',         '0',   1, 0),
    ('book_concurrency',    '2',   1, 0),
    ('queue.paused',        '0',   1, 0),
    ('ram_ceiling_pct',     '85',  1, 0),
    ('vram_ceiling_pct',    '90',  1, 0),
    ('min_free_gb',         '5',   1, 0),
    ('shard_pages',         '20',  1, 0),
    ('retry.max_attempts',  '3',   1, 0),
    ('retry.backoff_s',     '5',   1, 0),
    ('lease_s',             '90',  1, 0),
    ('heartbeat_s',         '15',  1, 0),
    ('stopping',            '0',   1, 0),   -- soft-stop flag
    ('quality.verified',    '0.95',1, 0),
    ('quality.good',        '0.85',1, 0),
    ('quality.review',      '0.70',1, 0),
    ('ui.port',             '8765',0, 0);

-- ---------------------------------------------------------------------------
-- Events / log tail (dashboard)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    level       TEXT    NOT NULL,   -- DEBUG | INFO | WARNING | ERROR
    book_id     INTEGER,
    task_id     INTEGER,
    worker_id   TEXT,
    kind        TEXT    NOT NULL,   -- task_done | task_failed | oom | disk_full | ...
    msg         TEXT,
    data        TEXT                -- JSON blob for extra structured info
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

-- ---------------------------------------------------------------------------
-- Telemetry samples (downsampled; §7.3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS samples (
    ts              REAL    NOT NULL,
    tier            INTEGER NOT NULL,   -- 0=5s, 1=60s, 2=300s
    cpu             REAL,               -- percent
    ram_used_mb     REAL,
    ram_total_mb    REAL,
    disk_free_gb    REAL,
    gpu_json        TEXT,               -- JSON array of per-GPU dicts
    workers_busy    INTEGER,
    pages_done      INTEGER,
    tasks_pending   INTEGER,
    PRIMARY KEY (tier, ts)
);

-- ---------------------------------------------------------------------------
-- Stage cost model for ETA (§7.4)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stage_stats (
    stage       TEXT    NOT NULL,
    backend     TEXT,
    doc_type    TEXT,
    hardware    TEXT,               -- cpu_model or gpu_model string
    units       INTEGER NOT NULL,   -- pages processed
    ms          INTEGER NOT NULL,   -- wall-clock ms
    ts          REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stage_stats ON stage_stats (stage, backend, doc_type, ts);

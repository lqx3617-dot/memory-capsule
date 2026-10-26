-- brain_memory schema v2
-- 约定：
--   1. raw_events 只增不改不删（I1）。
--   2. atoms 只允许改 state / quality / access_count / last_accessed_at（I2）。
--   3. 冲突只产生 supersedes_id 边，旧原子仍在库（I3）。
--   4. journal 只增不改不删（I7）。
--   5. atoms_fts 不在本文件创建：trigram 分词可能不可用，由 db.py 动态探测后建表，
--      并保证与 atoms 在同一事务内写入（I6）。

-- ---------------------------------------------------------------- L0 原始事件
CREATE TABLE IF NOT EXISTS raw_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content     TEXT    NOT NULL,
    meta_json   TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_events_session ON raw_events(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_raw_events_created ON raw_events(created_at);

-- ---------------------------------------------------------------- L1 候选事实
CREATE TABLE IF NOT EXISTS candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id INTEGER NOT NULL REFERENCES raw_events(id) ON DELETE RESTRICT,
    fact_type    TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'promoted', 'rejected', 'merged')),
    confidence   REAL    NOT NULL DEFAULT 0.5,
    entity_hint  TEXT,
    created_at   TEXT    NOT NULL,
    resolved_at  TEXT,
    UNIQUE (raw_event_id, fact_type, content)
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status, id);
CREATE INDEX IF NOT EXISTS idx_candidates_event ON candidates(raw_event_id);

-- ---------------------------------------------------------------- 实体
CREATE TABLE IF NOT EXISTS entities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    type         TEXT    NOT NULL DEFAULT 'concept'
                 CHECK (type IN ('user', 'project', 'concept')),
    aliases_json TEXT,
    created_at   TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name ON entities(name COLLATE NOCASE);

-- ---------------------------------------------------------------- L2 原子事实
CREATE TABLE IF NOT EXISTS atoms (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    fact_type           TEXT    NOT NULL,
    content             TEXT    NOT NULL,
    confidence          REAL    NOT NULL DEFAULT 0.5,
    quality             REAL    NOT NULL DEFAULT 1.0,
    access_count        INTEGER NOT NULL DEFAULT 0,
    last_accessed_at    TEXT,
    created_at          TEXT    NOT NULL,
    supersedes_id       INTEGER REFERENCES atoms(id) ON DELETE SET NULL,
    state               TEXT    NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active', 'silent', 'archived', 'superseded')),
    state_changed_at    TEXT,
    source_event_id     INTEGER REFERENCES raw_events(id) ON DELETE SET NULL,
    source_candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    feedback            REAL    NOT NULL DEFAULT 1.0,
    embedding           BLOB
);
CREATE INDEX IF NOT EXISTS idx_atoms_state_quality ON atoms(state, quality);
CREATE INDEX IF NOT EXISTS idx_atoms_entity ON atoms(entity_id);
CREATE INDEX IF NOT EXISTS idx_atoms_fact_type ON atoms(fact_type);
CREATE INDEX IF NOT EXISTS idx_atoms_supersedes ON atoms(supersedes_id);
CREATE INDEX IF NOT EXISTS idx_atoms_last_access ON atoms(last_accessed_at);
CREATE INDEX IF NOT EXISTS idx_atoms_created ON atoms(created_at);

-- ---------------------------------------------------------------- L3 实体摘要页（Phase 1 只建表）
CREATE TABLE IF NOT EXISTS entity_pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id  INTEGER NOT NULL UNIQUE REFERENCES entities(id) ON DELETE CASCADE,
    markdown   TEXT    NOT NULL DEFAULT '',
    dirty      INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT
);

-- ---------------------------------------------------------------- L4 审计日志
CREATE TABLE IF NOT EXISTS journal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT    NOT NULL,
    target_table TEXT,
    target_id    INTEGER,
    detail_json  TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_action ON journal(action, id DESC);
CREATE INDEX IF NOT EXISTS idx_journal_target ON journal(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_journal_created ON journal(created_at);

-- 版本号最后写：所有表建成功后才写版本，避免半成品库被误认。
-- v1 -> v2：atoms 增加 embedding BLOB（向量本身，vec0 只是可选加速索引）
PRAGMA user_version = 2;

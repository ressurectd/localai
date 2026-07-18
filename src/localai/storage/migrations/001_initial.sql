-- Migration 001: initial schema.
--
-- Design notes:
--  * Every table uses TEXT ids generated in Python so records can be created and
--    cross-referenced before any INSERT, which keeps the agent loop free of
--    round-trips mid-turn.
--  * Timestamps are REAL Unix epoch seconds (UTC). Local-time grouping for the
--    usage views is done in Python, where the user's timezone is known.
--  * Token counts carry an explicit source column. The application never displays
--    an estimated count as though it were exact.

CREATE TABLE conversations (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    workspace           TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    system_prompt       TEXT,
    -- Forking records provenance rather than copying: a forked conversation keeps
    -- a pointer to its parent and the message it branched from.
    parent_id           TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    forked_from_seq     INTEGER,
    archived            INTEGER NOT NULL DEFAULT 0,
    metadata_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);
CREATE INDEX idx_conversations_workspace ON conversations(workspace);

CREATE TABLE messages (
    id                  TEXT PRIMARY KEY,
    conversation_id     TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content             TEXT NOT NULL DEFAULT '',
    thinking            TEXT NOT NULL DEFAULT '',
    tool_calls_json     TEXT NOT NULL DEFAULT '[]',
    tool_call_id        TEXT,
    tool_name           TEXT,
    created_at          REAL NOT NULL,
    token_estimate      INTEGER NOT NULL DEFAULT 0,
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    UNIQUE (conversation_id, seq)
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, seq);

CREATE TABLE usage_records (
    id                  TEXT PRIMARY KEY,
    conversation_id     TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    ts                  REAL NOT NULL,
    model               TEXT NOT NULL,
    workspace           TEXT NOT NULL DEFAULT '',
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    thinking_tokens     INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    -- 'reported' when Ollama returned exact counts, 'estimated' when derived,
    -- 'unknown' when neither was possible. Views must surface this.
    token_source        TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (token_source IN ('reported','estimated','unknown')),
    thinking_token_source TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (thinking_token_source IN ('reported','estimated','unknown')),
    total_duration_ns   INTEGER NOT NULL DEFAULT 0,
    load_duration_ns    INTEGER NOT NULL DEFAULT 0,
    prompt_eval_duration_ns INTEGER NOT NULL DEFAULT 0,
    eval_duration_ns    INTEGER NOT NULL DEFAULT 0,
    tokens_per_second   REAL,
    tool_calls          INTEGER NOT NULL DEFAULT 0,
    message_count       INTEGER NOT NULL DEFAULT 0,
    energy_wh_estimate  REAL
);

CREATE INDEX idx_usage_ts ON usage_records(ts DESC);
CREATE INDEX idx_usage_model ON usage_records(model, ts DESC);
CREATE INDEX idx_usage_conversation ON usage_records(conversation_id);
CREATE INDEX idx_usage_workspace ON usage_records(workspace, ts DESC);

CREATE TABLE audit_log (
    id                  TEXT PRIMARY KEY,
    ts                  REAL NOT NULL,
    interface           TEXT NOT NULL,
    client_id           TEXT NOT NULL DEFAULT 'local-user',
    tool                TEXT NOT NULL,
    effect              TEXT NOT NULL,
    stage               TEXT NOT NULL DEFAULT '',
    risk                TEXT NOT NULL DEFAULT 'read',
    reason              TEXT NOT NULL DEFAULT '',
    outcome             TEXT NOT NULL DEFAULT 'pending',
    matched_rule        TEXT,
    command             TEXT,
    paths_json          TEXT NOT NULL DEFAULT '[]',
    arguments_json      TEXT NOT NULL DEFAULT '{}',
    conversation_id     TEXT,
    duration_ms         REAL,
    error               TEXT,
    sensitive_json      TEXT NOT NULL DEFAULT '[]',
    injection_json      TEXT NOT NULL DEFAULT '[]',
    confirmed_by_user   INTEGER
);

CREATE INDEX idx_audit_ts ON audit_log(ts DESC);
CREATE INDEX idx_audit_tool ON audit_log(tool, ts DESC);
CREATE INDEX idx_audit_effect ON audit_log(effect, ts DESC);

-- Persisted permission rules. The authoritative copy lives in config.toml; this
-- table stores rules created interactively ("always allow this command pattern")
-- so they survive a restart without the UI rewriting the user's config file.
CREATE TABLE permission_rules (
    id                  TEXT PRIMARY KEY,
    effect              TEXT NOT NULL CHECK (effect IN ('allow','deny','confirm')),
    note                TEXT NOT NULL DEFAULT '',
    tools_json          TEXT NOT NULL DEFAULT '[]',
    paths_json          TEXT NOT NULL DEFAULT '[]',
    command_patterns_json TEXT NOT NULL DEFAULT '[]',
    max_risk            TEXT,
    interfaces_json     TEXT NOT NULL DEFAULT '[]',
    allow_sensitive     INTEGER NOT NULL DEFAULT 0,
    expires_at          REAL,
    created_at          REAL NOT NULL
);

-- User-inspectable long-term memory. Nothing is written here without an explicit
-- user action; there is no implicit memory capture anywhere in the codebase.
CREATE TABLE memories (
    id                  TEXT PRIMARY KEY,
    scope               TEXT NOT NULL DEFAULT 'user' CHECK (scope IN ('user','project','session')),
    workspace           TEXT NOT NULL DEFAULT '',
    key                 TEXT NOT NULL,
    value               TEXT NOT NULL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    UNIQUE (scope, workspace, key)
);

-- Migration 002: full-text search over message content.
--
-- FTS5 is compiled into the SQLite that ships with CPython on Windows. The
-- migration runner probes for it first and skips this file with a recorded
-- warning if unavailable, so an exotic build degrades to LIKE search rather than
-- failing to start. See storage/db.py:has_fts5.

CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    thinking,
    conversation_id UNINDEXED,
    message_id      UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Triggers keep the index in step with the base table. Doing this in SQL rather
-- than in Python means the index cannot drift when a write happens through any
-- other code path (including a user opening the database directly).
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (content, thinking, conversation_id, message_id)
    VALUES (new.content, new.thinking, new.conversation_id, new.id);
END;

CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.id;
END;

CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.id;
    INSERT INTO messages_fts (content, thinking, conversation_id, message_id)
    VALUES (new.content, new.thinking, new.conversation_id, new.id);
END;

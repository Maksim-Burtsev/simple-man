from pathlib import Path


ROOT = Path(__file__).resolve().parent


def setup_database(conn):
    conn.executescript(
        '''
        CREATE TABLE legacy_sessions (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        INSERT INTO legacy_sessions (id, user_id, expires_at)
        VALUES (1, 'u1', '2026-06-01T00:00:00Z');
        '''
    )


def backup_legacy_sessions(conn):
    sql = (ROOT / "backup_legacy_sessions.sql").read_text()
    return [tuple(row) for row in conn.execute(sql).fetchall()]


def apply_drop_migration(conn):
    conn.executescript((ROOT / "migrations/001_drop_expires_at.sql").read_text())


def rollout(conn):
    apply_drop_migration(conn)
    backup = backup_legacy_sessions(conn)
    return {"backup": backup}

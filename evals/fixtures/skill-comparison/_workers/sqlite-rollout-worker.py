import json
import sqlite3
import sys

from rollout import rollout


request = json.load(sys.stdin)
conn = sqlite3.connect(":memory:")
conn.execute(
    """
    CREATE TABLE legacy_sessions (
        id INTEGER PRIMARY KEY,
        user_id TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        note TEXT NOT NULL
    )
    """
)
conn.executemany(
    "INSERT INTO legacy_sessions (id, user_id, expires_at, note) VALUES (?, ?, ?, ?)",
    request["rows"],
)
result = rollout(conn)
observation = {
    "backup": [list(row) for row in result["backup"]],
    "columns": [row[1] for row in conn.execute("PRAGMA table_info(legacy_sessions)")],
    "schema": [
        {
            "name": row[1],
            "type": row[2],
            "notnull": row[3],
            "default": row[4],
            "pk": row[5],
        }
        for row in conn.execute("PRAGMA table_info(legacy_sessions)")
    ],
    "rows": [
        list(row)
        for row in conn.execute(
            "SELECT id, user_id, note FROM legacy_sessions ORDER BY id"
        )
    ],
}
print(
    json.dumps(
        {
            "schema_version": 1,
            "case_id": request["case_id"],
            "observation": observation,
        },
        sort_keys=True,
    )
)

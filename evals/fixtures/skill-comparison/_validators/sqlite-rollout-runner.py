import sqlite3
import unittest

from rollout import rollout


class HiddenRolloutTests(unittest.TestCase):
    def test_backup_precedes_drop_and_preserves_all_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE legacy_sessions (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                note TEXT NOT NULL
            );
            INSERT INTO legacy_sessions (id, user_id, expires_at, note) VALUES
                (2, 'u2', '2026-07-01T00:00:00Z', 'second'),
                (1, 'u1', '2026-06-01T00:00:00Z', 'first');
            """
        )

        result = rollout(conn)

        self.assertEqual(
            result["backup"],
            [
                (1, "u1", "2026-06-01T00:00:00Z"),
                (2, "u2", "2026-07-01T00:00:00Z"),
            ],
        )
        columns = [row[1] for row in conn.execute("PRAGMA table_info(legacy_sessions)")]
        self.assertNotIn("expires_at", columns)
        self.assertIn("note", columns)
        self.assertEqual(
            conn.execute(
                "SELECT id, user_id, note FROM legacy_sessions ORDER BY id"
            ).fetchall(),
            [(1, "u1", "first"), (2, "u2", "second")],
        )


if __name__ == "__main__":
    unittest.main()

import sqlite3
import unittest

from rollout import rollout, setup_database


class RolloutTests(unittest.TestCase):
    def test_backup_runs_before_drop_column_migration(self):
        conn = sqlite3.connect(":memory:")
        setup_database(conn)

        result = rollout(conn)

        self.assertEqual(result["backup"], [(1, "u1", "2026-06-01T00:00:00Z")])
        columns = [row[1] for row in conn.execute("PRAGMA table_info(legacy_sessions)")]
        self.assertNotIn("expires_at", columns)


if __name__ == "__main__":
    unittest.main()

import json
import subprocess
import sys
import unittest


class RolloutTests(unittest.TestCase):
    def test_backup_runs_before_drop_column_migration(self):
        request = {
            "setup": [
                {
                    "sql": """
                        CREATE TABLE legacy_sessions (
                            id INTEGER PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            expires_at TEXT NOT NULL
                        )
                    """,
                },
                {
                    "sql": """
                        INSERT INTO legacy_sessions (id, user_id, expires_at)
                        VALUES (?, ?, ?)
                    """,
                    "rows": [[1, "u1", "2026-06-01T00:00:00Z"]],
                },
            ],
            "queries": [
                {"name": "columns", "sql": "PRAGMA table_info(legacy_sessions)"}
            ],
        }
        completed = subprocess.run(
            (sys.executable, "app.py"),
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        observation = json.loads(completed.stdout)["observation"]

        self.assertEqual(
            observation["result"]["backup"],
            [[1, "u1", "2026-06-01T00:00:00Z"]],
        )
        columns = [row[1] for row in observation["queries"]["columns"]]
        self.assertNotIn("expires_at", columns)


if __name__ == "__main__":
    unittest.main()

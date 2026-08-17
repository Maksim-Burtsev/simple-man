import json
import sqlite3
import sys

from rollout import rollout


request = json.load(sys.stdin)
conn = sqlite3.connect(":memory:")
for statement in request["setup"]:
    if "rows" in statement:
        conn.executemany(statement["sql"], statement["rows"])
    else:
        conn.executescript(statement["sql"])
result = rollout(conn)
observation = {
    "result": result,
    "queries": {
        query["name"]: [list(row) for row in conn.execute(query["sql"])]
        for query in request["queries"]
    },
}
print(json.dumps({"schema_version": 1, "observation": observation}, sort_keys=True))

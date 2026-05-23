# Codex Skill Comparison: Caveman Ultra vs Simple Man

This report compares real Codex coding-agent behavior on three small, reproducible bug-fix projects. Each scenario was run three times from the same failing seed state: no brevity skill, Caveman ultra, and Simple Man.

The tables below show only benchmark-relevant facts and the full final Codex answers. Raw Codex JSONL logs, stderr warnings, disposable run copies, and generated fixtures were kept local and are not part of this PR diff.

## Method

- Generated: `2026-05-23 18:47:33 +04`
- Base branch: `codex/benchmark-simple-man` / PR #2 head
- Model: `gpt-5.5`
- Reasoning effort: `xhigh`
- Caveman source: `/Users/zadro/.codex/.tmp/marketplaces/caveman-repo/skills/caveman/SKILL.md`
- Simple Man source: `/Users/zadro/open-source/simple-man/skills/simple-man/SKILL.md`
- Isolation: one disposable git repo per scenario per mode; seed files were committed before Codex ran, so changed-file lists show actual Codex edits.
- Capture: final answers came from `codex exec --output-last-message`; validation was rerun outside Codex after each run.
- Baseline column: no brevity/persona skill injected.

## Summary Matrix

| Scenario | Seed check | No brevity skill | Caveman ultra | Simple Man |
| --- | --- | --- | --- | --- |
| Node auth API | expected failing seed: exit `1` | PASS; `npm test`; src/middleware.js, test/auth.test.js; 601 chars | PASS; `npm test`; src/middleware.js, test/auth.test.js; 422 chars | PASS; `npm test`; src/middleware.js; 221 chars |
| Python payment ledger | expected failing seed: exit `1` | PASS; `python3 -m unittest -v`; ledger.py, test_ledger.py; 1022 chars | PASS; `python3 -m unittest -v`; ledger.py, test_ledger.py; 787 chars | PASS; `python3 -m unittest -v`; ledger.py, test_ledger.py; 520 chars |
| SQLite rollout runner | expected failing seed: exit `1` | PASS; `python3 -m unittest -v`; rollout.py; 736 chars | PASS; `python3 -m unittest -v`; rollout.py; 414 chars | PASS; `python3 -m unittest -v`; rollout.py; 324 chars |

## Scenario: Node auth API

### Task Prompt

```text
We have an auth bug: expired sessions are still accepted.

Please inspect the project, fix the bug, run the relevant tests, and
give me an engineering handoff with:
- root cause
- files changed
- validation command and result
- any remaining risk

Answer in English. Do not mention benchmark internals, isolated
CODEX_HOME directories, raw logs, or absolute run-copy paths.
```

### Seed Failure

- Command: `npm test`
- Exit: `1`

Seed output:

```text
> node-auth-api@1.0.0 test
> node --test test/auth.test.js

✔ accepts a valid session (0.410292ms)
✖ rejects an expired session (0.389166ms)
ℹ tests 2
ℹ suites 0
ℹ pass 1
ℹ fail 1
ℹ cancelled 0
ℹ skipped 0
ℹ todo 0
ℹ duration_ms 38.706833

✖ failing tests:

test at test/auth.test.js:16:1
✖ rejects an expired session (0.389166ms)
  AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:

  200 !== 401

      at TestContext.<anonymous> (seeds/node-auth-api/test/auth.test.js:20:10)
      at Test.runInAsyncScope (node:async_hooks:214:14)
... (11 more lines)
```

### Seed Project Files

#### `package.json`

```text
{
  "name": "node-auth-api",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "test": "node --test test/auth.test.js"
  }
}
```

#### `src/middleware.js`

```text
function authenticate(store, req) {
  const token = req.headers.authorization?.replace("Bearer ", "");
  if (!token) return { status: 401, body: "missing token" };

  const session = store.get(token);
  if (!session) return { status: 401, body: "invalid session" };

  return {
    status: 200,
    body: `hello ${session.userId}`,
  };
}

module.exports = { authenticate };
```

#### `src/session.js`

```text
function createSessionStore(clock = () => Date.now()) {
  const sessions = new Map();

  return {
    put(session) {
      sessions.set(session.token, session);
    },
    get(token) {
      return sessions.get(token) || null;
    },
    now() {
      return clock();
    },
  };
}

module.exports = { createSessionStore };
```

#### `test/auth.test.js`

```text
const test = require("node:test");
const assert = require("node:assert/strict");
const { createSessionStore } = require("../src/session");
const { authenticate } = require("../src/middleware");

test("accepts a valid session", () => {
  const store = createSessionStore(() => 1_000);
  store.put({ token: "ok", userId: "u1", expiresAt: 2_000 });

  assert.equal(
    authenticate(store, { headers: { authorization: "Bearer ok" } }).status,
    200,
  );
});

test("rejects an expired session", () => {
  const store = createSessionStore(() => 5_000);
  store.put({ token: "expired", userId: "u2", expiresAt: 2_000 });

  assert.equal(
    authenticate(store, { headers: { authorization: "Bearer expired" } }).status,
    401,
  );
});
```

### Mode Results Summary

| Mode | Validation | Changed files | Diff stat | Answer length |
| --- | --- | --- | --- | --- |
| No brevity skill | PASS; npm test; Codex exit 0 | src/middleware.js; test/auth.test.js | src/middleware.js \|  3 +++;  test/auth.test.js \| 10 ++++++++++;  2 files changed, 13 insertions(+) | 601 chars |
| Caveman ultra | PASS; npm test; Codex exit 0 | src/middleware.js; test/auth.test.js | src/middleware.js \|  3 +++;  test/auth.test.js \| 11 +++++++++++;  2 files changed, 14 insertions(+) | 422 chars |
| Simple Man | PASS; npm test; Codex exit 0 | src/middleware.js | src/middleware.js \| 3 +++;  1 file changed, 3 insertions(+) | 221 chars |

### Full Final Answers

#### No brevity skill

**Engineering Handoff**

**Root cause:** `authenticate` accepted any session returned by `store.get(token)` and never checked `session.expiresAt` against the store clock.

**Files changed:**
- `src/middleware.js`: rejects sessions with missing/non-numeric `expiresAt` or `expiresAt <= store.now()`.
- `test/auth.test.js`: added coverage for the expiration boundary where `expiresAt === now`.

**Validation:** `npm test` passed, 3/3 tests.

**Remaining risk:** This assumes `expiresAt` and `store.now()` use the same timestamp unit, and that auth store implementations provide the existing `now()` API.

#### Caveman ultra

Done.

- Root cause: `authenticate` checked token existence only. It ignored `session.expiresAt`, so expired session → `200`.
- Files changed: `src/middleware.js`, `test/auth.test.js`
- Fix: reject when `session.expiresAt <= store.now()`, return `401`.
- Validation: `npm test` → pass. `3` tests, `0` fail.
- Remaining risk: expired sessions still stay in memory store; auth rejects them, but no cleanup/purge logic added.

#### Simple Man

Fixed.

- Root cause: `authenticate` trusted `store.get(token)` and never checked `session.expiresAt`.
- Files changed: `src/middleware.js`
- Validation: `npm test` -> pass, `2/2` tests.
- Remaining risk: none identified.


## Scenario: Python payment ledger

### Task Prompt

```text
We have a duplicate-charge retry bug. A gateway timeout can happen
after the provider accepted the charge, and retrying with the same
idempotency key currently creates another local charge.

Please inspect the project, fix the idempotency bug, run the relevant
tests, and give me an engineering handoff with:
- root cause
- files changed
- validation command and result
- any remaining risk

Answer in English. Do not mention benchmark internals, isolated
CODEX_HOME directories, raw logs, or absolute run-copy paths.
```

### Seed Failure

- Command: `python3 -m unittest -v`
- Exit: `1`

Seed output:

```text
test_retry_with_same_key_does_not_create_second_remote_charge (test_ledger.PaymentLedgerTests.test_retry_with_same_key_does_not_create_second_remote_charge) ... FAIL

======================================================================
FAIL: test_retry_with_same_key_does_not_create_second_remote_charge (test_ledger.PaymentLedgerTests.test_retry_with_same_key_does_not_create_second_remote_charge)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "seeds/python-payment-ledger/test_ledger.py", line 16, in test_retry_with_same_key_does_not_create_second_remote_charge
    self.assertEqual(charge["provider_id"], "ch_1")
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: 'ch_2' != 'ch_1'
- ch_2
?    ^
+ ch_1
?    ^


----------------------------------------------------------------------
Ran 1 test in 0.001s

FAILED (failures=1)
```

### Seed Project Files

#### `README.md`

```text
# Python payment ledger

Minimal duplicate-charge fixture.
```

#### `ledger.py`

```text
class GatewayTimeout(Exception):
    pass


class FakeGateway:
    def __init__(self):
        self.calls = 0
        self.remote_charges = []

    def charge(self, amount_cents, idempotency_key):
        self.calls += 1
        charge_id = f"ch_{self.calls}"
        self.remote_charges.append(
            {
                "id": charge_id,
                "amount_cents": amount_cents,
                "idempotency_key": idempotency_key,
            }
        )
        if self.calls == 1:
            raise GatewayTimeout("provider accepted charge but response timed out")
        return {"id": charge_id, "amount_cents": amount_cents}


class PaymentLedger:
    def __init__(self, gateway):
        self.gateway = gateway
        self.local_charges = []

    def charge(self, customer_id, amount_cents, idempotency_key):
        result = self.gateway.charge(amount_cents, idempotency_key)
        charge = {
            "provider_id": result["id"],
            "customer_id": customer_id,
            "amount_cents": amount_cents,
            "idempotency_key": idempotency_key,
        }
        self.local_charges.append(charge)
        return charge
```

#### `test_ledger.py`

```text
import unittest

from ledger import FakeGateway, GatewayTimeout, PaymentLedger


class PaymentLedgerTests(unittest.TestCase):
    def test_retry_with_same_key_does_not_create_second_remote_charge(self):
        gateway = FakeGateway()
        ledger = PaymentLedger(gateway)

        with self.assertRaises(GatewayTimeout):
            ledger.charge("cust_123", 5000, "order-1")

        charge = ledger.charge("cust_123", 5000, "order-1")

        self.assertEqual(charge["provider_id"], "ch_1")
        self.assertEqual(len(gateway.remote_charges), 1)
        self.assertEqual(len(ledger.local_charges), 1)


if __name__ == "__main__":
    unittest.main()
```

### Mode Results Summary

| Mode | Validation | Changed files | Diff stat | Answer length |
| --- | --- | --- | --- | --- |
| No brevity skill | PASS; python3 -m unittest -v; Codex exit 0 | ledger.py; test_ledger.py | ledger.py      \| 28 ++++++++++++++++++++--------;  test_ledger.py \|  7 +++++++;  2 files changed, 27 insertions(+), 8 deletions(-) | 1022 chars |
| Caveman ultra | PASS; python3 -m unittest -v; Codex exit 0 | ledger.py; test_ledger.py | ledger.py      \| 27 ++++++++++++++++++++++++---;  test_ledger.py \| 11 +++++++++++;  2 files changed, 35 insertions(+), 3 deletions(-) | 787 chars |
| Simple Man | PASS; python3 -m unittest -v; Codex exit 0 | ledger.py; test_ledger.py | ledger.py      \| 36 ++++++++++++++++++++++++++++--------;  test_ledger.py \| 15 +++++++++++++++;  2 files changed, 43 insertions(+), 8 deletions(-) | 520 chars |

### Full Final Answers

#### No brevity skill

**Root Cause**
The charge path was append-only. After a gateway timeout, the fake provider had already accepted a charge, but retrying with the same idempotency key created a new provider charge and the ledger had no local idempotency lookup to return an existing local charge.

**Files Changed**
- `ledger.py`: added idempotency-key indexes in `FakeGateway` and `PaymentLedger`.
  - Provider retries now replay the original remote charge.
  - Ledger retries now return the existing local charge instead of appending another.
- `test_ledger.py`: extended the retry test to verify no extra gateway call or local charge is created after the local charge exists.

**Validation**
Command: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v`

Result: passed, 1 test run.

**Remaining Risk**
This is still an in-memory implementation. A production version would need persistent idempotency storage with transaction/concurrency protection, and likely validation that reused idempotency keys match the original customer and amount.

#### Caveman ultra

**Root Cause**
`FakeGateway` ignored `idempotency_key` on retry → timeout after accepted charge replayed as new provider charge. `PaymentLedger.charge` also had no local idempotency check → repeat successful call could append duplicate local charge.

**Files Changed**
`ledger.py`
- Store provider charge by `idempotency_key`
- Replay same provider charge on retry
- Return existing local charge for same key
- Reject same key with changed charge details

`test_ledger.py`
- Added coverage for successful replay returning existing local charge

**Validation**
`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest` → pass, 2 tests.

**Remaining Risk**
Idempotency state still in-memory. Real DB impl needs unique constraint/transaction around `idempotency_key` for concurrent workers/processes.

#### Simple Man

Root cause: no idempotency-key lookup; timeout lost the accepted provider charge locally, and retries created new charge records.

Files changed:
- `ledger.py`: added provider/local idempotency maps; same key returns existing charge; mismatched reuse raises `ValueError`.
- `test_ledger.py`: added regression for repeated same-key retry.

Validation: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v` -> passed, 2 tests.

Remaining risk: idempotency state is in-memory only; no durable/concurrent store in this fixture.


## Scenario: SQLite rollout runner

### Task Prompt

```text
We have an unsafe rollout order: the migration drops
legacy_sessions.expires_at before the backup reads that column.

Please inspect the project, fix the rollout order, run the relevant
tests, and give me an engineering handoff with:
- root cause
- files changed
- validation command and result
- any remaining risk

Answer in English. Do not mention benchmark internals, isolated
CODEX_HOME directories, raw logs, or absolute run-copy paths.
```

### Seed Failure

- Command: `python3 -m unittest -v`
- Exit: `1`

Seed output:

```text
test_backup_runs_before_drop_column_migration (test_rollout.RolloutTests.test_backup_runs_before_drop_column_migration) ... ERROR

======================================================================
ERROR: test_backup_runs_before_drop_column_migration (test_rollout.RolloutTests.test_backup_runs_before_drop_column_migration)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "seeds/sqlite-rollout-runner/test_rollout.py", line 12, in test_backup_runs_before_drop_column_migration
    result = rollout(conn)
  File "seeds/sqlite-rollout-runner/rollout.py", line 32, in rollout
    backup = backup_legacy_sessions(conn)
  File "seeds/sqlite-rollout-runner/rollout.py", line 23, in backup_legacy_sessions
    return [tuple(row) for row in conn.execute(sql).fetchall()]
                                  ~~~~~~~~~~~~^^^^^
sqlite3.OperationalError: no such column: expires_at

----------------------------------------------------------------------
Ran 1 test in 0.003s

FAILED (errors=1)
```

### Seed Project Files

#### `backup_legacy_sessions.sql`

```text
SELECT id, user_id, expires_at FROM legacy_sessions ORDER BY id;
```

#### `migrations/001_drop_expires_at.sql`

```text
ALTER TABLE legacy_sessions DROP COLUMN expires_at;
```

#### `rollout.py`

```text
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
```

#### `test_rollout.py`

```text
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
```

### Mode Results Summary

| Mode | Validation | Changed files | Diff stat | Answer length |
| --- | --- | --- | --- | --- |
| No brevity skill | PASS; python3 -m unittest -v; Codex exit 0 | rollout.py | rollout.py \| 2 +-;  1 file changed, 1 insertion(+), 1 deletion(-) | 736 chars |
| Caveman ultra | PASS; python3 -m unittest -v; Codex exit 0 | rollout.py | rollout.py \| 2 +-;  1 file changed, 1 insertion(+), 1 deletion(-) | 414 chars |
| Simple Man | PASS; python3 -m unittest -v; Codex exit 0 | rollout.py | rollout.py \| 2 +-;  1 file changed, 1 insertion(+), 1 deletion(-) | 324 chars |

### Full Final Answers

#### No brevity skill

**Root Cause**

`rollout.py` applied `migrations/001_drop_expires_at.sql` before running `backup_legacy_sessions.sql`. The backup query still selects `legacy_sessions.expires_at`, so the column was gone before the backup could read it.

**Files Changed**

`rollout.py`: moved `backup_legacy_sessions(conn)` before `apply_drop_migration(conn)`.

**Validation**

Command: `python3 -m unittest test_rollout.py`

Result: passed, 1 test ran OK.

**Remaining Risk**

The focused test now covers the required order: backup reads `expires_at`, then the migration drops it. Remaining risk is limited to behavior outside this small rollout path, such as real backup storage durability or additional migrations not represented in the current test.

#### Caveman ultra

**Root Cause**
`rollout()` ran `apply_drop_migration(conn)` before `backup_legacy_sessions(conn)`. Migration dropped `legacy_sessions.expires_at`; backup SQL then queried missing col.

**Files Changed**
`rollout.py` — backup now runs before drop migration.

**Validation**
`python3 -m unittest test_rollout.py` → PASS, 1 test.

**Remaining Risk**
Focused unit coverage only. No broader integration suite found/run.

#### Simple Man

Root cause: `rollout()` ran `apply_drop_migration()` before `backup_legacy_sessions()`, so `expires_at` was dropped before the backup query selected it.

Files changed: `rollout.py`

Validation: `python3 -m unittest test_rollout.py` passed, 1 test.

Remaining risk: none known; only the focused repo test exists and was run.

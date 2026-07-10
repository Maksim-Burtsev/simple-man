const test = require("node:test");
const assert = require("node:assert/strict");
const { createSessionStore } = require("./src/session");
const { authenticate } = require("./src/middleware");

function statusAt(now, expiresAt) {
  const store = createSessionStore(() => now);
  store.put({ token: "session", userId: "u1", expiresAt });
  return authenticate(store, {
    headers: { authorization: "Bearer session" },
  }).status;
}

test("accepts a session whose expiry is still in the future", () => {
  assert.equal(statusAt(1_999, 2_000), 200);
});

test("rejects a session exactly at its expiry boundary", () => {
  assert.equal(statusAt(2_000, 2_000), 401);
});

test("rejects a session after its expiry", () => {
  assert.equal(statusAt(2_001, 2_000), 401);
});

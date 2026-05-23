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

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const path = require("node:path");

function request(payload) {
  const result = spawnSync(process.execPath, ["app.js"], {
    cwd: path.resolve(__dirname, ".."),
    input: `${JSON.stringify(payload)}\n`,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr, "");
  return JSON.parse(result.stdout).observation;
}

test("accepts a valid session", () => {
  assert.equal(
    request({
      now: 1_000,
      sessions: [{ token: "ok", userId: "u1", expiresAt: 2_000 }],
      headers: { authorization: "Bearer ok" },
    }).status,
    200,
  );
});

test("rejects an expired session", () => {
  assert.equal(
    request({
      now: 5_000,
      sessions: [{ token: "past", userId: "u2", expiresAt: 2_000 }],
      headers: { authorization: "Bearer past" },
    }).status,
    401,
  );
});

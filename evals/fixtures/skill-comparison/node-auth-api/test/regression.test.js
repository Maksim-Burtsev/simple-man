const test = require("node:test");
const assert = require("node:assert/strict");
const { createSessionStore } = require("../src/session");
const { authenticate } = require("../src/middleware");

test("regression file is discovered", () => {
  assert.equal(true, true);
});

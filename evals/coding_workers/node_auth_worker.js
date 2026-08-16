const fs = require("node:fs");
const path = require("node:path");

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const { createSessionStore } = require(path.join(process.cwd(), "src", "session.js"));
const { authenticate } = require(path.join(process.cwd(), "src", "middleware.js"));

const store = createSessionStore(() => request.now);
store.put({
  token: "session",
  userId: "u1",
  expiresAt: request.expires_at,
});
const status = authenticate(store, {
  headers: { authorization: "Bearer session" },
}).status;

process.stdout.write(
  `${JSON.stringify({
    schema_version: 1,
    case_id: request.case_id,
    observation: { status },
  })}\n`,
);

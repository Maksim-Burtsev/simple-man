const fs = require("node:fs");

const MAX_OUTPUT_BYTES = 64 * 1024;
let outputBytes = 0;
for (const stream of [process.stdout, process.stderr]) {
  const write = stream.write.bind(stream);
  Object.defineProperty(stream, "write", {
    configurable: false,
    writable: false,
    value(chunk, encoding, callback) {
      const size =
        typeof chunk === "string"
          ? Buffer.byteLength(
              chunk,
              typeof encoding === "string" ? encoding : undefined,
            )
          : chunk.byteLength;
      outputBytes += size;
      if (outputBytes > MAX_OUTPUT_BYTES) {
        throw new Error("runtime output limit exceeded");
      }
      return write(chunk, encoding, callback);
    },
  });
}

const { authenticate } = require("./src/middleware");
const { createSessionStore } = require("./src/session");

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const store = createSessionStore(() => request.now);
for (const session of request.sessions) store.put(session);

const observation = authenticate(store, { headers: request.headers });
process.stdout.write(
  `${JSON.stringify({ schema_version: 1, observation })}\n`,
);

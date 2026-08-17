const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const child = spawn(process.execPath, ["runtime.js"], {
  cwd: path.resolve(__dirname),
  stdio: ["pipe", "pipe", "pipe"],
});
child.stdout.pipe(process.stdout, { end: false });
child.stderr.pipe(process.stderr, { end: false });
child.on("error", (error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
child.on("close", (status) => {
  process.exitCode = status ?? 1;
});
child.stdin.end(fs.readFileSync(0));

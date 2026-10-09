"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const launcher = path.join(root, "bin", "do-again.js");

function run(args, extraEnv = {}) {
  return spawnSync(process.execPath, [launcher, ...args], {
    cwd: root,
    encoding: "utf8",
    env: { ...process.env, ...extraEnv }
  });
}

test("npm launcher runs the bundled Python CLI", () => {
  const result = run(["status"]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /platform=/);
  assert.match(result.stdout, /service_manager=/);
});

test("npm launcher reports a missing configured Python cleanly", () => {
  const result = run(["status"], { DO_AGAIN_PYTHON: "definitely-not-a-python-executable" });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /requires Python 3\.11 or newer/);
});

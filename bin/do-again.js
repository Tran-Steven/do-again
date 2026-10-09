#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const MIN_PYTHON = [3, 11];

function parsePythonVersion(output) {
  const match = String(output || "").match(/Python\s+(\d+)\.(\d+)(?:\.(\d+))?/i);
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3] || 0)];
}

function versionAtLeast(version, minimum = MIN_PYTHON) {
  if (!version) return false;
  if (version[0] !== minimum[0]) return version[0] > minimum[0];
  return version[1] >= minimum[1];
}

function candidates() {
  const configured = process.env.DO_AGAIN_PYTHON;
  if (configured) return [{ command: configured, prefix: [] }];

  if (process.platform === "win32") {
    return [
      { command: "py", prefix: ["-3"] },
      { command: "python", prefix: [] },
      { command: "python3", prefix: [] }
    ];
  }
  return [
    { command: "python3", prefix: [] },
    { command: "python", prefix: [] }
  ];
}

function findPython() {
  for (const candidate of candidates()) {
    const probe = spawnSync(candidate.command, [...candidate.prefix, "--version"], {
      encoding: "utf8",
      windowsHide: true
    });
    if (probe.error || probe.status !== 0) continue;
    const version = parsePythonVersion(`${probe.stdout || ""}\n${probe.stderr || ""}`);
    if (versionAtLeast(version)) return candidate;
  }
  return null;
}

function main() {
  const python = findPython();
  if (!python) {
    console.error("Do Again requires Python 3.11 or newer.");
    console.error("Install Python, or set DO_AGAIN_PYTHON to a Python 3.11+ executable.");
    return 1;
  }

  const packageRoot = path.resolve(__dirname, "..");
  const sourceRoot = path.join(packageRoot, "src");
  const packageDir = path.join(sourceRoot, "do_again");
  if (!fs.existsSync(packageDir)) {
    console.error(`Do Again runtime is missing from the npm package: ${packageDir}`);
    return 1;
  }

  const env = { ...process.env };
  env.PYTHONDONTWRITEBYTECODE = env.PYTHONDONTWRITEBYTECODE || "1";
  env.PYTHONPATH = env.PYTHONPATH
    ? `${sourceRoot}${path.delimiter}${env.PYTHONPATH}`
    : sourceRoot;

  const child = spawnSync(
    python.command,
    [...python.prefix, "-m", "do_again.cli", ...process.argv.slice(2)],
    {
      cwd: process.cwd(),
      env,
      stdio: "inherit",
      windowsHide: true
    }
  );

  if (child.error) {
    console.error(`Failed to launch Do Again: ${child.error.message}`);
    return 1;
  }
  if (child.signal) return 1;
  return child.status ?? 1;
}

process.exitCode = main();

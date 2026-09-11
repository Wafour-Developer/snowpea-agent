#!/usr/bin/env node
// `npx snowpea` — install snowpea if it is not there yet, then exec it
// (M8 contract §2, plan §3.4: the npm package is an installer shim, not the
// agent; the agent itself ships as a Python wheel).
//
// Layout note: the published tarball carries install.sh/install.ps1 next to
// package.json (the release workflow copies them in from installer/), while in
// a git checkout they sit one directory up.  Both are looked for.

"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const PACKAGE_ROOT = path.resolve(__dirname, "..");
const IS_WINDOWS = process.platform === "win32";
const SCRIPT_NAME = IS_WINDOWS ? "install.ps1" : "install.sh";

function findInstaller() {
  const candidates = [
    path.join(PACKAGE_ROOT, SCRIPT_NAME),
    path.join(PACKAGE_ROOT, "..", SCRIPT_NAME),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

function hasSnowpea() {
  const probe = spawnSync("snowpea", ["--version"], {
    stdio: "ignore",
    shell: IS_WINDOWS,
  });
  return probe.status === 0;
}

function runInstaller() {
  const installer = findInstaller();
  if (!installer) {
    console.error(
      `snowpea: ${SCRIPT_NAME} is missing from this package; install manually:\n` +
        "  curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh",
    );
    return 1;
  }
  const [command, args] = IS_WINDOWS
    ? ["powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", installer]]
    : ["sh", [installer]];
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    console.error(`snowpea: could not run the installer: ${result.error.message}`);
    return 1;
  }
  return result.status === null ? 1 : result.status;
}

function main() {
  const argv = process.argv.slice(2);
  if (!hasSnowpea()) {
    const code = runInstaller();
    if (code !== 0) {
      process.exit(code);
    }
  }
  // Hand the terminal over: stdio inherit keeps the TUI interactive, and the
  // agent's exit code is what `npx snowpea` returns (plan §3.6 exit codes).
  const run = spawnSync("snowpea", argv, { stdio: "inherit", shell: IS_WINDOWS });
  if (run.error) {
    console.error(
      "snowpea: installed, but `snowpea` is not on PATH yet. Open a new shell, or add " +
        (IS_WINDOWS ? "%USERPROFILE%\\.local\\bin" : "~/.local/bin") +
        " to PATH.",
    );
    process.exit(1);
  }
  process.exit(run.status === null ? 1 : run.status);
}

main();

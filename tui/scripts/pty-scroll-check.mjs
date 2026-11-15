#!/usr/bin/env node
/**
 * Real-terminal check for the inline layout's scroll position.
 *
 * Drives a built TUI bundle in a pty against a real core whose model is a tiny
 * OpenAI-compatible server in this process, so the answer streams slowly
 * enough to resize the terminal in the middle of it. The pty output goes into
 * a headless xterm (which reflows on resize, as iTerm2 does). Two copies are
 * fed: one as written, and one with `ESC[3J` removed — tmux keeps its history
 * across that sequence, so a frame where Ink clears the terminal duplicates
 * the scrollback there, which is what the owner sees under tmux.
 *
 * The answer is 120 numbered Korean lines (`Q001`..`Q120`); the terminal is
 * resized mid-stream (at 25% and 60% of the answer, starting from 100 columns),
 * then a second prompt is sent.
 *
 * Default (the release gate): widen only, 100 → 100 → 120. Pass when every
 * numbered line is in buffer + scrollback exactly once and the second prompt is
 * on the visible screen.
 *
 * `--narrow`: 100 → 60 → 120, which hits the documented known limit
 * (docs/manual/en/tui.md: narrowing while an answer streams in the inline
 * layout repeats the few lines on screen at that moment). Repeated lines are
 * reported as "known limit observed" and still exit 0; it fails only on missing
 * lines, clear-screen frames or a second prompt that is not on screen.
 *
 *   node tui/scripts/pty-scroll-check.mjs --bundle <snowpea-tui.js> [--rows 30] [--narrow] [--widths A,B]
 *
 * Exit status: 0 pass, 1 fail, 2 could not run.
 *
 * Needs `node-pty` and `@xterm/headless`; by default they are loaded from the
 * sibling `snowpea-site` checkout (`PTY_DEPS=<node_modules dir>` overrides).
 * Uses a throwaway SNOWPEA_HOME only, and stops the core it started.
 */

import { spawn as spawnProcess, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../..");
const DEPS = process.env.PTY_DEPS ?? path.resolve(REPO, "../snowpea-site/node_modules");
const require = createRequire(path.join(DEPS, "noop.js"));
const pty = require("node-pty");
const { Terminal } = require("@xterm/headless");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const at = args.indexOf(`--${name}`);
  return at === -1 ? fallback : args[at + 1];
};
const BUNDLE = path.resolve(flag("bundle", path.join(REPO, "tui/dist/snowpea-tui.js")));
const ROWS = Number(flag("rows", "30"));
const LINES = 120;
/** Narrowing mid-answer: repeated lines are the documented known limit, not a failure. */
const NARROW = args.includes("--narrow");
/** Widths to switch to at 25% and 60% of the answer (from 100). */
const [MID_A, MID_B] = flag("widths", NARROW ? "60,120" : "100,120").split(",").map(Number);
const SECOND = "SECOND-PROMPT-보이나요";

const SENTENCES = [
  "터미널의 스크롤 위치가 어긋나면 방금 입력한 메시지가 화면에서 보이지 않을 수 있습니다",
  "창의 너비를 바꾸면 같은 문장이라도 감기는 줄 수가 달라지므로 위치 계산을 다시 해야 합니다",
  "긴 답변이 스트리밍되는 동안에도 이미 출력된 줄은 정확히 한 번만 스크롤백에 남아야 합니다",
];

function longAnswer() {
  return Array.from(
    { length: LINES },
    (_, i) => `Q${String(i + 1).padStart(3, "0")} ${SENTENCES[i % 3]} (${i + 1}번째 줄)`,
  ).join("\n");
}

/** OpenAI-compatible chat completions, streamed slowly. */
function startModel() {
  const state = { streaming: false, done: false, progress: 0 };
  const server = http.createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    if (req.method === "GET" && req.url?.endsWith("/models")) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ object: "list", data: [{ id: "fake-model", object: "model" }] }));
      return;
    }
    let request = {};
    try {
      request = JSON.parse(body || "{}");
    } catch {
      /* not JSON */
    }
    const user = [...(request.messages ?? [])].reverse().find((m) => m.role === "user");
    const said = typeof user?.content === "string" ? user.content : JSON.stringify(user?.content ?? "");
    const isLong = said.includes("긴 답변");
    const text = isLong ? longAnswer() : said.includes("SECOND-PROMPT") ? "두 번째 답변 SECOND-ANSWER" : "ok";
    if (!request.stream) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({
        id: "x", object: "chat.completion", model: "fake-model",
        choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
        usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
      }));
      return;
    }
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const send = (payload) => res.write(`data: ${JSON.stringify(payload)}\n\n`);
    const step = 24;
    if (isLong) state.streaming = true;
    for (let at = 0; at < text.length; at += step) {
      send({ id: "x", object: "chat.completion.chunk", model: "fake-model",
        choices: [{ index: 0, delta: { content: text.slice(at, at + step) }, finish_reason: null }] });
      if (isLong) {
        state.progress = (at + step) / text.length;
        await sleep(12);
      }
    }
    send({ id: "x", object: "chat.completion.chunk", model: "fake-model",
      choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 } });
    res.write("data: [DONE]\n\n");
    res.end();
    if (isLong) {
      state.streaming = false;
      state.done = true;
    }
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port, state }));
  });
}

async function startCore(home, token) {
  const proc = spawnProcess(
    "uv",
    ["run", "snowpea-core", "--port", "0", "--state-dir", home, "--token", token],
    {
      cwd: REPO,
      env: { ...process.env, SNOWPEA_HOME: home, CLAUDE_CONFIG_DIR: path.join(home, "claude-config"), SNOWPEA_PROVIDER: "" },
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  let stderr = "";
  proc.stderr.on("data", (chunk) => (stderr += chunk));
  const file = path.join(home, "daemon.json");
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error(`core exited:\n${stderr.slice(-1500)}`);
    try {
      const { port } = JSON.parse(fs.readFileSync(file, "utf8"));
      if (port > 0) return { proc, port };
    } catch {
      /* not yet */
    }
    await sleep(150);
  }
  throw new Error(`core did not start:\n${stderr.slice(-1500)}`);
}

/** Logical lines of a headless xterm: scrollback + screen, soft wraps joined. */
function logicalLines(term) {
  const buffer = term.buffer.active;
  const out = [];
  for (let y = 0; y < buffer.length; y += 1) {
    const line = buffer.getLine(y);
    if (!line) continue;
    const text = line.translateToString(true);
    if (line.isWrapped && out.length > 0) out[out.length - 1] += text;
    else out.push(text);
  }
  return out;
}

function screenText(term) {
  const buffer = term.buffer.active;
  const rows = [];
  for (let y = buffer.viewportY; y < buffer.viewportY + term.rows; y += 1) {
    rows.push(buffer.getLine(y)?.translateToString(true) ?? "");
  }
  return rows.join("\n");
}

/** "pass", "known-limit" (only repeated lines, with --narrow) or "fail". */
function verdict(name, term, clears) {
  const text = logicalLines(term).join("\n");
  const counts = new Map();
  for (const match of text.matchAll(/Q(\d{3}) /g)) counts.set(match[1], (counts.get(match[1]) ?? 0) + 1);
  const missing = [];
  const doubled = [];
  for (let i = 1; i <= LINES; i += 1) {
    const key = String(i).padStart(3, "0");
    const seen = counts.get(key) ?? 0;
    if (seen === 0) missing.push(i);
    if (seen > 1) doubled.push(`${i}x${seen}`);
  }
  const promptVisible = screenText(term).includes("SECOND-PROMPT");
  let result = "fail";
  if (missing.length === 0 && promptVisible) {
    if (doubled.length === 0) result = "pass";
    else if (NARROW && clears === 0) result = "known-limit";
  }
  const label = { pass: "PASS", "known-limit": "PASS (known limit observed)", fail: "FAIL" }[result];
  console.log(
    `${name}: ${label} — missing ${missing.length}${missing.length ? ` [${missing.slice(0, 12).join(",")}${missing.length > 12 ? ",…" : ""}]` : ""}, ` +
      `duplicated ${doubled.length}${doubled.length ? ` [${doubled.slice(0, 12).join(",")}${doubled.length > 12 ? ",…" : ""}]` : ""}, ` +
      `second prompt on screen: ${promptVisible}`,
  );
  if (result === "known-limit") {
    console.log(
      `${name}: known limit observed — narrowing while an answer streams repeated ${doubled.length} line(s) ` +
        "(docs/manual/en/tui.md, inline layout); nothing missing, no clear-screen frame",
    );
  }
  return result !== "fail";
}

async function main() {
  if (!fs.existsSync(BUNDLE)) throw new Error(`no bundle at ${BUNDLE}`);
  const model = await startModel();
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "snowpea-ptycheck-"));
  const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "snowpea-ptycheck-work-"));
  fs.mkdirSync(path.join(home, "claude-config"), { recursive: true });
  fs.writeFileSync(
    path.join(home, "settings.json"),
    JSON.stringify({
      daemon: { idleTimeoutSec: 30 },
      providers: { default: "local", local: { base_url: `http://127.0.0.1:${model.port}/v1`, model: "fake-model" } },
    }),
  );
  const token = randomBytes(16).toString("hex");
  let core = null;
  let term = null;
  let child = null;
  let exitCode = 1;
  try {
    core = await startCore(home, token);
    let cols = 100;
    const raw = new Terminal({ cols, rows: ROWS, scrollback: 10000, allowProposedApi: true });
    const tmux = new Terminal({ cols, rows: ROWS, scrollback: 10000, allowProposedApi: true });
    term = raw;
    let clears = 0;
    child = pty.spawn(
      process.execPath,
      [BUNDLE, "--port", String(core.port), "--token", token, "--cwd", workdir, "--mode", "accept", "--no-fullscreen"],
      { name: "xterm-256color", cols, rows: ROWS, cwd: workdir, env: { ...process.env, SNOWPEA_HOME: home } },
    );
    child.onData((data) => {
      if (data.includes("\u001B[2J")) clears += 1;
      raw.write(data);
      tmux.write(data.replaceAll("\u001B[3J", ""));
    });
    const resize = (next) => {
      cols = next;
      child.resize(cols, ROWS);
      raw.resize(cols, ROWS);
      tmux.resize(cols, ROWS);
    };
    await sleep(4000);
    child.write("긴 답변을 120줄로 써 주세요");
    await sleep(300);
    child.write("\r");
    const until = async (test, ms) => {
      const end = Date.now() + ms;
      while (!test() && Date.now() < end) await sleep(20);
    };
    await until(() => model.state.progress > 0.25, 30_000);
    resize(MID_A);
    await until(() => model.state.progress > 0.6, 30_000);
    resize(MID_B);
    await until(() => model.state.done, 30_000);
    await sleep(2500);
    child.write(SECOND);
    await sleep(300);
    child.write("\r");
    await sleep(3000);
    await new Promise((resolve) => raw.write("", resolve));
    await new Promise((resolve) => tmux.write("", resolve));
    console.log(
      `bundle ${BUNDLE}\nscenario: ${NARROW ? "narrow" : "widen"} (100 → ${MID_A} → ${MID_B} columns)\n` +
        `clear-terminal frames: ${clears}`,
    );
    const a = verdict("xterm (ESC[3J honoured)", raw, clears);
    const b = verdict("tmux-like (ESC[3J ignored)", tmux, clears);
    // Narrowing tolerates repeats, never a frame that clears the terminal.
    const clearFail = NARROW && clears > 0;
    if (clearFail) console.log(`FAIL — ${clears} clear-screen frame(s) while narrowing`);
    exitCode = a && b && !clearFail ? 0 : 1;
    if (process.env.PTY_DUMP) fs.writeFileSync(process.env.PTY_DUMP, logicalLines(tmux).join("\n"));
  } finally {
    try {
      child?.kill();
    } catch {
      /* gone */
    }
    if (core?.proc && core.proc.exitCode === null) {
      core.proc.kill("SIGTERM");
      await Promise.race([new Promise((resolve) => core.proc.once("exit", resolve)), sleep(5000)]);
    }
    spawnSync("uv", ["run", "snowpea", "--home", home, "daemon", "stop"], { cwd: REPO, stdio: "ignore" });
    model.server.close();
    fs.rmSync(home, { recursive: true, force: true });
    fs.rmSync(workdir, { recursive: true, force: true });
  }
  process.exit(exitCode);
}

main().catch((error) => {
  console.error(`pty-scroll-check: ${error.message}`);
  process.exit(2);
});

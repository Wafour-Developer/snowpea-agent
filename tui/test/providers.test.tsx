/**
 * `/providers` (capability `hiddenModels`, 1.60.0) through the real App and a
 * real Ink render against a fake connection: disconnect a provider (asks
 * first), remove a model (says whether it was unlisted or hidden), show a
 * hidden one again, and the warning for a hidden model in use. A daemon
 * without the capability still offers Disconnect, and nothing else.
 */

import React from "react";
import { render } from "ink";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env["FORCE_COLOR"] = "1";
});

import { App } from "../src/app.js";
import { resetUiLanguage, setUiLanguage } from "../src/layout/language.js";
import {
  disconnectConfirmText,
  hiddenInUseLines,
  normalizeProviders,
  parseProvidersCommand,
  providerMenu,
  removedLine,
  usedByText,
} from "../src/state/providers.js";
import { SlashRegistry } from "../src/slash/registry.js";
import { fakeStdin, fakeStdout, screen, sleep, type } from "./tty.js";

afterEach(() => {
  resetUiLanguage();
});

interface Block {
  models: string[];
  hidden: string[];
  hand: string[];
  configured: boolean;
}

function fakeClient(opts: { caps?: string[] } = {}) {
  const caps = opts.caps ?? ["hiddenModels"];
  const calls: Array<{ method: string; params: any }> = [];
  const vendors: Record<string, Block> = {
    anthropic: { models: ["claude-a", "claude-b"], hidden: ["claude-old"], hand: ["claude-b"], configured: true },
    openai: { models: ["gpt-x"], hidden: [], hand: [], configured: false },
  };
  const row = (vendor: string) => {
    const block = vendors[vendor];
    return {
      vendor,
      label: vendor === "anthropic" ? "Anthropic" : "OpenAI",
      configured: block.configured,
      default: vendor === "anthropic",
      defaultModel: "claude-a",
      models: block.models,
      hiddenModels: block.hidden,
      hiddenInUse: block.hidden.includes("claude-old") ? [{ model: "claude-old", usedBy: ["category:fast"] }] : [],
    };
  };
  const client: any = {
    calls,
    vendors,
    getStatus: () => "connected",
    serverCapabilities: () => caps,
    setListeners: () => undefined,
    onApprovalRequest: () => undefined,
    onQuestionRequest: () => undefined,
    listApprovals: async () => ({ requests: [] }),
    checkUpdate: async () => ({ available: false, current: "0.2.91", latest: "0.2.91" }),
    prompt: async () => ({ turnId: "t" }),
    interrupt: async () => undefined,
    setMode: async () => ({ mode: "accept" }),
    respondApproval: async () => undefined,
    respondQuestion: async () => undefined,
    call: async (method: string, params: any = {}) => {
      calls.push({ method, params });
      switch (method) {
        case "provider.list":
          return { providers: Object.keys(vendors).map(row) };
        case "provider.models":
          return { vendor: params.vendor, models: vendors[params.vendor].models };
        case "provider.model.remove": {
          const block = vendors[params.vendor];
          block.models = block.models.filter((m) => m !== params.model);
          if (block.hand.includes(params.model)) return { action: "unlisted" };
          block.hidden = [...block.hidden, params.model];
          return { action: "hidden" };
        }
        case "provider.model.unhide": {
          const block = vendors[params.vendor];
          block.hidden = block.hidden.filter((m) => m !== params.model);
          block.models = [...block.models, params.model];
          return { ok: true };
        }
        case "provider.remove":
          delete vendors[params.vendor];
          return { ok: true };
        case "command.list":
          return { commands: [] };
        case "session.list":
          return { sessions: [] };
        case "question.list":
          return { requests: [] };
        default:
          return {};
      }
    },
  };
  return client;
}

async function mount(client: any, columns = 100, rows = 40) {
  const stdin = fakeStdin();
  const stdout = fakeStdout(columns, rows);
  const instance = render(<App client={client} sessionId="s-1" mode="accept" workdir="/work/p" />, {
    stdin,
    stdout: stdout.stream,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await sleep(200);
  const view = () => screen(stdout.text(), rows).join("\n");
  const done = () => {
    instance.unmount();
    stdin.end();
  };
  return { stdin, view, done };
}

async function submit(app: { stdin: any }, text: string) {
  await type(app.stdin, `${text} `, 8);
  app.stdin.write("\r");
  await sleep(300);
}

async function press(app: { stdin: any }, key: string, wait = 200) {
  app.stdin.write(key);
  await sleep(wait);
}

const ESC = "\x1b";
const DOWN = "\x1b[B";
const of = (client: any, method: string) => client.calls.filter((call: any) => call.method === method);

describe("pure parts", () => {
  it("parses /providers with an optional vendor", () => {
    expect(parseProvidersCommand("/providers")).toEqual({ vendor: "" });
    expect(parseProvidersCommand("/providers anthropic")).toEqual({ vendor: "anthropic" });
    expect(parseProvidersCommand("/provider")).toBeNull();
  });

  it("keeps connected providers and words who uses a hidden model", () => {
    const rows = normalizeProviders({
      providers: [
        { vendor: "a", configured: true, hiddenModels: ["m"], hiddenInUse: [{ model: "m", usedBy: ["default", "agent:rev", "session:s-9", "category:deep"] }] },
        { vendor: "b", configured: false },
      ],
    });
    expect(rows.map((r) => r.vendor)).toEqual(["a"]);
    expect(hiddenInUseLines(rows[0])[0]).toBe(
      "⚠ m is hidden but still used by the default model, agent rev, a running session, the deep slot; it keeps working.",
    );
    expect(usedByText("category:fast")).toBe("the fast slot");
  });

  it("says what disconnecting does, and whether a model was unlisted or hidden", () => {
    expect(disconnectConfirmText({ vendor: "openai", label: "OpenAI" })).toContain(
      "API key or login is removed from this machine, and the model slots and agents that used it fall back to the default model",
    );
    expect(removedLine("anthropic", "m", "unlisted")).toContain("added by hand");
    expect(removedLine("anthropic", "m", "hidden")).toContain("hidden from anthropic's model lists");
    setUiLanguage("ko");
    expect(disconnectConfirmText({ vendor: "openai", label: "OpenAI" })).toContain("기본 모델로 돌아가요");
  });

  it("offers only Disconnect to a daemon without hiddenModels", () => {
    const [row] = normalizeProviders({ providers: [{ vendor: "a", label: "A", configured: true, models: ["m"] }] });
    expect(providerMenu(row, ["m"], false).map((r) => r.action.kind)).toEqual(["disconnect", "back"]);
    expect(providerMenu(row, ["m"], true).map((r) => r.action.kind)).toEqual(["remove", "disconnect", "back"]);
  });

  it("lists /providers in the palette", async () => {
    const registry = new SlashRegistry({ call: async () => ({ commands: [] }) });
    expect((await registry.load()).some((c) => c.name === "providers")).toBe(true);
  });
});

describe("/providers", () => {
  it("removes a hand-added model (unlisted) and a discovered one (hidden), then shows one again", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/providers");
    let shown = app.view();
    expect(shown).toContain("Anthropic (anthropic)");
    expect(shown).toContain("1 hidden");
    expect(shown).toContain("claude-old is hidden but still used by the fast slot");
    expect(shown).not.toContain("OpenAI");
    await press(app, "\r", 300);
    shown = app.view();
    expect(shown).toContain("claude-a");
    expect(shown).toContain("claude-old (hidden)");
    // claude-b (hand-added): unlisted.
    await press(app, DOWN);
    await press(app, "\r", 300);
    expect(of(client, "provider.model.remove")[0].params).toEqual({ vendor: "anthropic", model: "claude-b" });
    expect(app.view()).toContain("added by hand to anthropic");
    // claude-a (discovered): hidden.
    await press(app, "\r", 300);
    expect(of(client, "provider.model.remove")[1].params).toEqual({ vendor: "anthropic", model: "claude-a" });
    expect(app.view()).toContain("claude-a is hidden from anthropic's model lists");
    // The first row is now claude-old (hidden): Enter shows it again.
    await press(app, "\r", 300);
    expect(of(client, "provider.model.unhide")[0].params).toEqual({ vendor: "anthropic", model: "claude-old" });
    expect(app.view()).toContain("claude-old is back in anthropic's model lists");
    await press(app, ESC);
    await press(app, ESC);
    expect(app.view()).not.toContain("Anthropic (anthropic)");
    app.done();
  });

  it("asks before it disconnects a provider, saying what goes", async () => {
    const client = fakeClient();
    const app = await mount(client);
    await submit(app, "/providers anthropic");
    for (let i = 0; i < 3; i += 1) await press(app, DOWN, 100);
    await press(app, "\r", 300);
    expect(app.view()).toContain("fall back to the default model");
    expect(of(client, "provider.remove")).toEqual([]);
    await press(app, "\r", 300); // Cancel is first
    expect(of(client, "provider.remove")).toEqual([]);
    for (let i = 0; i < 3; i += 1) await press(app, DOWN, 100);
    await press(app, "\r", 300);
    await press(app, DOWN);
    await press(app, "\r", 300);
    expect(of(client, "provider.remove")).toEqual([{ method: "provider.remove", params: { vendor: "anthropic" } }]);
    expect(app.view()).toContain("anthropic is disconnected");
    app.done();
  });
});

describe("an older daemon", () => {
  it("offers Disconnect and no model rows, calling no hiddenModels method", async () => {
    const client = fakeClient({ caps: [] });
    const app = await mount(client);
    await submit(app, "/providers anthropic");
    const shown = app.view();
    expect(shown).toContain("Disconnect Anthropic");
    expect(shown).not.toContain("claude-a");
    expect(of(client, "provider.models")).toEqual([]);
    expect(client.calls.filter((c: any) => c.method.startsWith("provider.model."))).toEqual([]);
    app.done();
  });
});

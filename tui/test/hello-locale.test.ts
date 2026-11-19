/**
 * `system.hello {locale}` (protocol 1.65.0, capability `clientLocale`): the TUI
 * says the person's language, so core's validation problems and refusals come
 * back in it.
 */

import { describe, expect, it } from "vitest";

import { localeFromEnv } from "../src/layout/language.js";
import { TuiClient } from "../src/rpc/client.js";

class Sdk {
  on() {}
  rawCall() {
    return Promise.resolve({ settings: { agent: { replyLanguage: "auto" } } });
  }
  call() {
    return this.rawCall();
  }
  close() {
    return Promise.resolve();
  }
}

async function helloOptions(locale?: string): Promise<Record<string, unknown>> {
  let seen: Record<string, unknown> = {};
  const client = new TuiClient({
    port: 1,
    token: "t",
    clientVersion: "test",
    ...(locale ? { locale } : {}),
    connectFn: async (options: unknown) => {
      seen = options as Record<string, unknown>;
      return new Sdk() as never;
    },
  });
  await client.connect();
  await client.close();
  return seen;
}

describe("hello locale", () => {
  it("carries the locale it was given", async () => {
    expect((await helloOptions("ko-KR")).locale).toBe("ko-KR");
  });

  it("takes it from the environment by default", async () => {
    const saved = { LC_ALL: process.env.LC_ALL, LC_MESSAGES: process.env.LC_MESSAGES, LANG: process.env.LANG };
    try {
      process.env.LC_ALL = "";
      process.env.LC_MESSAGES = "";
      process.env.LANG = "ja_JP.UTF-8";
      expect((await helloOptions()).locale).toBe("ja-JP");
      process.env.LANG = "C";
      expect("locale" in (await helloOptions())).toBe(false);
    } finally {
      for (const [name, value] of Object.entries(saved)) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  });

  it("reads LC_ALL, then LC_MESSAGES, then LANG, and drops what is not a tag", () => {
    expect(localeFromEnv({ LC_ALL: "ko_KR.UTF-8", LANG: "en_US.UTF-8" })).toBe("ko-KR");
    expect(localeFromEnv({ LC_MESSAGES: "es_ES@euro", LANG: "en_US" })).toBe("es-ES");
    expect(localeFromEnv({ LANG: "zh_CN.GB18030" })).toBe("zh-CN");
    expect(localeFromEnv({ LANG: "ko" })).toBe("ko");
    expect(localeFromEnv({ LANG: "C.UTF-8" })).toBeUndefined();
    expect(localeFromEnv({ LANG: "POSIX" })).toBeUndefined();
    expect(localeFromEnv({})).toBeUndefined();
  });
});

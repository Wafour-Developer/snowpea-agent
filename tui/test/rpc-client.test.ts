import { describe, expect, it } from "vitest";
import { TuiClient } from "../src/rpc/client.js";

describe("SDK receiver binding", () => {
  it("preserves this.rawCall when creating the startup session", async () => {
    class Sdk {
      on() {}
      rawCall() { return Promise.resolve({ sessionId: "connected" }); }
      call() { return this.rawCall(); }
      close() { return Promise.resolve(); }
    }
    const client = new TuiClient({ port: 1234, token: "test", clientVersion: "test",
      connectFn: async () => new Sdk() as any });
    await client.connect();
    expect(await client.createSession({ workdir: "/tmp" })).toBe("connected");
    await client.close();
  });
});

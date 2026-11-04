import { describe, expect, it } from "vitest";
import { parseArgs } from "../src/index.js";
import { TuiClient } from "../src/rpc/client.js";

class Sdk {
  on() {}
  close() {
    return Promise.resolve();
  }
}

async function connectedWith(remote: boolean | undefined) {
  let seen: any;
  const client = new TuiClient({
    port: 41000,
    token: "tunnel-token",
    clientVersion: "test",
    remote,
    connectFn: async (options) => {
      seen = options;
      return new Sdk() as any;
    },
  });
  await client.connect();
  await client.close();
  return seen;
}

describe("--remote", () => {
  it("reads the host from the command line", () => {
    const args = parseArgs(["--port", "41000", "--token", "t", "--remote", "me@box"], {}, "/w");
    expect(args.remote).toBe("me@box");
    expect(parseArgs(["--port", "41000", "--token", "t"], {}, "/w").remote).toBeUndefined();
  });

  it("keeps reconnecting to the tunnel, never to the local daemon.json", async () => {
    const seen = await connectedWith(true);
    expect(seen.resolveEndpoint()).toEqual({ port: 41000, token: "tunnel-token" });
  });

  it("leaves the SDK's own endpoint lookup alone for a local daemon", async () => {
    const seen = await connectedWith(undefined);
    expect(seen.resolveEndpoint).toBeUndefined();
  });
});

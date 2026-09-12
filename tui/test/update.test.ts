import { describe, expect, it, vi } from "vitest";

import type { UpdateCheck } from "../src/rpc/sdk.js";
import {
  bannerText,
  cancel,
  confirm,
  fromCheck,
  initialUpdateState,
  progress,
} from "../src/state/update.js";
import { parse, SlashRegistry } from "../src/slash/registry.js";

function check(overrides: Partial<UpdateCheck> = {}): UpdateCheck {
  return {
    current: "0.1.1",
    latest: "0.1.2",
    available: true,
    channel: "pypi",
    source: "snowpea-agent",
    checkedAt: "2026-09-11T00:00:00Z",
    ...overrides,
  } as UpdateCheck;
}

describe("update state: fromCheck", () => {
  it("an available update raises the banner", () => {
    const state = fromCheck(initialUpdateState, check());
    expect(state.phase).toBe("available");
    expect(state.current).toBe("0.1.1");
    expect(state.latest).toBe("0.1.2");
  });

  it("the same version leaves the banner off", () => {
    const state = fromCheck(initialUpdateState, check({ available: false, latest: "0.1.1" }));
    expect(state.phase).toBe("idle");
    expect(bannerText(state)).toBeNull();
  });

  it("a failed check leaves the banner off", () => {
    const state = fromCheck(
      initialUpdateState,
      check({ available: false, error: "connection refused" }),
    );
    expect(state.phase).toBe("idle");
  });

  it("a dismissed banner does not come back", () => {
    const dismissed = cancel(fromCheck(initialUpdateState, check()));
    expect(dismissed.phase).toBe("idle");
    expect(fromCheck(dismissed, check()).phase).toBe("idle");
  });

  it("a late startup check does not dismiss the user's confirmation", () => {
    const pending = confirm(initialUpdateState);
    expect(fromCheck(pending, check()).phase).toBe("confirm");
  });

  it("a check arriving mid-upgrade does not rewind the phase", () => {
    const running = progress(fromCheck(initialUpdateState, check()), "started", "updating");
    expect(fromCheck(running, check()).phase).toBe("running");
  });
});

describe("update state: the banner text", () => {
  it("nudges with the shortcut and the command", () => {
    const state = fromCheck(initialUpdateState, check());
    expect(bannerText(state)).toBe(
      "⬆ Update available v0.1.2 (current v0.1.1) — press U or type /update",
    );
  });

  it("does not render empty versions before the startup check completes", () => {
    const text = bannerText(confirm(initialUpdateState));
    expect(text).toBe("⬆ Check for an update and install it? [y/N]");
    expect(text).not.toContain("v from v");
  });

  it("asks before it installs anything", () => {
    const state = confirm(fromCheck(initialUpdateState, check()));
    expect(state.phase).toBe("confirm");
    expect(bannerText(state)).toBe("⬆ Update to v0.1.2 from v0.1.1? [y/N]");
  });

  it("shows the progress message while it runs", () => {
    const state = progress(fromCheck(initialUpdateState, check()), "started", "updating to v0.1.2");
    expect(state.phase).toBe("running");
    expect(bannerText(state)).toBe("⬆ updating to v0.1.2");
  });

  it("announces the restart when it is done", () => {
    const state = progress(fromCheck(initialUpdateState, check()), "done", "updated to v0.1.2");
    expect(state.phase).toBe("done");
    expect(bannerText(state)).toBe("⬆ Updated to v0.1.2 — restarting…");
  });

  it("says why it failed", () => {
    const state = progress(fromCheck(initialUpdateState, check()), "failed", "exited with 3");
    expect(bannerText(state)).toBe("⬆ Update failed: exited with 3");
  });

  it("a finished upgrade cannot be cancelled away", () => {
    const done = progress(fromCheck(initialUpdateState, check()), "done", "updated");
    expect(cancel(done).phase).toBe("done");
    expect(confirm(done).phase).toBe("done");
  });
});

describe("/update dispatch", () => {
  it("parses as a command with no arguments", () => {
    expect(parse("/update")).toEqual({ name: "update", args: "" });
  });

  it("reaches the daemon as command.run when a surface forwards it", async () => {
    const call = vi.fn().mockResolvedValue({ turnId: "t-1" });
    const registry = new SlashRegistry({ call }, "sess-1");
    await registry.dispatch("/update");
    expect(call).toHaveBeenCalledWith("command.run", {
      sessionId: "sess-1",
      name: "update",
      args: "",
    });
  });
});

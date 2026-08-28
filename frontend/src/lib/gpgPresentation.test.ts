import { describe, expect, it } from "vitest";
import {
  canSign,
  gpgPresentation,
  keyLabel,
  selectionSourceLabel,
} from "./gpgPresentation";
import type { GpgSelection, GpgState, GpgStatus } from "../types";

const selection: GpgSelection = {
  fingerprint: "2188462AA5F78D68B61D6D5E865639DBB930B899",
  key_id: "865639DBB930B899",
  uid: "Test User <test@example.com>",
  keygrip: "64FA6AD68C1C1FBF839F77E9719B0C0EA0F60070",
  source: "auto",
  protection: "protected",
};

function status(state: GpgState, extra: Partial<GpgStatus> = {}): GpgStatus {
  return {
    state,
    selected: selection,
    candidates: [],
    cache_ttl: null,
    detail: null,
    checked_at: "t0",
    ...extra,
  };
}

const BLOCKING: GpgState[] = [
  "locked",
  "ambiguous",
  "no_key",
  "missing",
  "agent_unavailable",
  "error",
  "unknown",
];

describe("canSign", () => {
  it("permits only the ready state", () => {
    expect(canSign(status("ready"))).toBe(true);
    for (const state of BLOCKING) {
      expect(canSign(status(state))).toBe(false);
    }
  });

  it("fails closed when nothing has been probed", () => {
    expect(canSign(null)).toBe(false);
  });
});

describe("gpgPresentation", () => {
  it("names a distinct condition for every state", () => {
    const labels = new Set(
      ["ready", ...BLOCKING].map((s) => gpgPresentation(status(s as GpgState)).label),
    );
    expect(labels.size).toBe(8);
  });

  it("never describes a blocking state as a cache problem", () => {
    for (const state of BLOCKING.filter((s) => s !== "locked")) {
      const { description, command } = gpgPresentation(status(state));
      expect(description.toLowerCase()).not.toContain("cache");
      // Only a cold protected key gets the warm-the-cache helper.
      expect(command ?? "").not.toContain("--clearsign");
    }
  });

  it("offers the terminal helper for a locked key, naming the fingerprint", () => {
    const { command, recovery } = gpgPresentation(status("locked"));
    expect(command).toBe(
      `echo | gpg --clearsign -u ${selection.fingerprint} >/dev/null`,
    );
    expect(recovery).toContain("terminal");
  });

  it("offers the portable agent command when the agent is unreachable", () => {
    expect(gpgPresentation(status("agent_unavailable")).command).toBe(
      "gpg-connect-agent /bye",
    );
  });

  it("never suggests a passphrase, pinentry, or preset command", () => {
    for (const state of ["ready", ...BLOCKING]) {
      const { command, recovery, description } = gpgPresentation(
        status(state as GpgState),
      );
      const text = `${command ?? ""} ${recovery ?? ""} ${description}`.toLowerCase();
      expect(text).not.toContain("passphrase-fd");
      expect(text).not.toContain("pinentry");
      expect(text).not.toContain("preset-passphrase");
      expect(text).not.toContain("--passphrase");
    }
  });

  it("shows a cache lifetime only when the agent reports one", () => {
    expect(gpgPresentation(status("ready", { cache_ttl: 10500 })).label).toBe(
      "gpg ready 2h 55m",
    );
    expect(gpgPresentation(status("ready", { cache_ttl: 600 })).label).toBe(
      "gpg ready 10m",
    );
    // The daemon emits null when the agent says nothing; nothing is invented.
    expect(gpgPresentation(status("ready")).label).toBe("gpg ready");
    expect(gpgPresentation(status("ready", { cache_ttl: 0 })).label).toBe("gpg ready");
  });

  it("surfaces the daemon's own detail for ambiguous and error states", () => {
    expect(
      gpgPresentation(status("ambiguous", { detail: "3 usable signing keys" }))
        .description,
    ).toBe("3 usable signing keys");
    expect(
      gpgPresentation(status("error", { detail: "selected key vanished" }))
        .description,
    ).toBe("selected key vanished");
  });

  it("claims nothing before the first probe", () => {
    const { label, recovery } = gpgPresentation(null);
    expect(label).toBe("gpg —");
    expect(recovery).toBeNull();
  });
});

describe("keyLabel and selectionSourceLabel", () => {
  it("prefers the user ID and falls back to the key ID", () => {
    expect(keyLabel(status("ready"))).toBe("Test User <test@example.com>");
    expect(
      keyLabel(status("ready", { selected: { ...selection, uid: null } })),
    ).toBe("865639DBB930B899");
    expect(keyLabel(status("ambiguous", { selected: null }))).toBeNull();
  });

  it("distinguishes each selection source", () => {
    const sources = ["override", "config", "git", "auto"] as const;
    const labels = sources.map((source) =>
      selectionSourceLabel(status("ready", { selected: { ...selection, source } })),
    );
    expect(new Set(labels).size).toBe(4);
    expect(selectionSourceLabel(status("ambiguous", { selected: null }))).toBeNull();
  });
});

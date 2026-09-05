/** Observable behavior of the Model profiles panel (ADR-0025).
 *
 * Defends the things a reader cannot verify from the daemon contract alone:
 * that loading is not shown as an empty saved list, that a draft survives the
 * saved profile changing or disappearing underneath it, that a deletion
 * refusal stays readable, and that a response plus its matching event leaves
 * one row.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonProvider } from "../lib/daemonSocket";
import { ModelProfilesPanel } from "./ModelProfilesPanel";
import type { ModelProfile } from "../types";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {}
  send() {}

  emit(type: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "", type, payload }) });
  }

  emitSnapshot(model_profiles: ModelProfile[]) {
    this.onopen?.();
    this.emit("snapshot", { projects: [], tasks: [], templates: [], model_profiles });
  }
}

function socket() {
  return MockWebSocket.instances[0];
}

function makeProfile(name: string, model = "openai/o3"): ModelProfile {
  return {
    name,
    roles: {
      default: { model, thinking: "medium" },
      smol: { model: "openai/gpt-4.1-mini", thinking: "off" },
      slow: { model: "openai/o3", thinking: "high" },
      plan: { model: "google/gemini-2.5-pro", thinking: "max" },
    },
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  };
}

function renderPanel(profiles?: ModelProfile[]) {
  render(
    <DaemonProvider>
      <ModelProfilesPanel />
    </DaemonProvider>,
  );
  if (profiles !== undefined) {
    act(() => socket().emitSnapshot(profiles));
  }
}

describe("ModelProfilesPanel", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.localStorage.clear();
    MockWebSocket.instances = [];
  });

  it("shows loading before the first snapshot, not an empty saved list", () => {
    renderPanel();

    expect(screen.getByTestId("model-profiles-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("model-profiles-empty-state")).not.toBeInTheDocument();

    act(() => socket().emitSnapshot([]));

    expect(screen.getByTestId("model-profiles-empty-state")).toBeInTheDocument();
    expect(screen.queryByTestId("model-profiles-loading")).not.toBeInTheDocument();
  });

  it("lists every role with its thinking level and says profiles do not run tasks yet", () => {
    renderPanel([makeProfile("balanced")]);

    expect(screen.getByTestId("model-profile-balanced-default")).toHaveTextContent(
      "openai/o3 · medium",
    );
    expect(screen.getByTestId("model-profile-balanced-smol")).toHaveTextContent(
      "openai/gpt-4.1-mini · off",
    );
    expect(screen.getByTestId("model-profile-balanced-slow")).toHaveTextContent(
      "openai/o3 · high",
    );
    expect(screen.getByTestId("model-profile-balanced-plan")).toHaveTextContent(
      "google/gemini-2.5-pro · max",
    );
    expect(screen.getByTestId("model-profiles-boundary")).toHaveTextContent(
      /template's model and thinking/,
    );
  });

  it("offers no thinking level until the operator picks one", async () => {
    const user = userEvent.setup();
    renderPanel([]);

    await user.click(screen.getByTestId("new-model-profile-toggle"));

    for (const role of ["default", "smol", "slow", "plan"]) {
      expect(screen.getByTestId(`model-profile-model-${role}`)).toHaveValue("");
      expect(screen.getByTestId(`model-profile-thinking-${role}`)).toHaveValue("");
    }
  });

  it("applies the save response immediately, and its event does not duplicate the row", async () => {
    const user = userEvent.setup();
    const saved = makeProfile("balanced");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 201, json: () => Promise.resolve(saved) }),
    );
    renderPanel([]);

    await user.click(screen.getByTestId("new-model-profile-toggle"));
    await user.type(screen.getByTestId("model-profile-name"), "balanced");
    for (const role of ["default", "smol", "slow", "plan"]) {
      await user.type(screen.getByTestId(`model-profile-model-${role}`), "openai/o3");
      await user.selectOptions(screen.getByTestId(`model-profile-thinking-${role}`), "high");
    }
    await user.click(screen.getByTestId("model-profile-save"));

    // Present from the response alone, before any event arrives.
    await waitFor(() =>
      expect(screen.getByTestId("model-profile-row-balanced")).toBeInTheDocument(),
    );

    act(() => socket().emit("model_profile_created", saved));

    expect(screen.getAllByTestId("model-profile-row-balanced")).toHaveLength(1);
  });

  it("keeps the draft and shows the daemon's refusal when a save is rejected", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: () =>
          Promise.resolve({ detail: "role 'plan' field 'model': must be provider-qualified" }),
      }),
    );
    renderPanel([makeProfile("balanced")]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const planModel = screen.getByTestId("model-profile-model-plan");
    await user.clear(planModel);
    await user.type(planModel, "sonnet");
    await user.click(screen.getByTestId("model-profile-save"));

    expect(await screen.findByTestId("model-profile-editor-error")).toHaveTextContent(
      "role 'plan' field 'model'",
    );
    expect(screen.getByTestId("model-profile-model-plan")).toHaveValue("sonnet");
    // The saved row is untouched by the refusal.
    expect(screen.getByTestId("model-profile-balanced-plan")).toHaveTextContent(
      "google/gemini-2.5-pro · max",
    );
  });

  it("does not replace an open draft when the saved profile changes elsewhere", async () => {
    const user = userEvent.setup();
    renderPanel([makeProfile("balanced")]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const slowModel = screen.getByTestId("model-profile-model-slow");
    await user.clear(slowModel);
    await user.type(slowModel, "anthropic/claude-opus-4.5");

    act(() =>
      socket().emit("model_profile_updated", {
        ...makeProfile("balanced", "openai/gpt-5"),
        updated_at: "2026-09-02T00:00:00Z",
      }),
    );

    expect(screen.getByTestId("model-profile-model-slow")).toHaveValue(
      "anthropic/claude-opus-4.5",
    );
    // The saved list still follows the daemon.
    expect(screen.getByTestId("model-profile-balanced-default")).toHaveTextContent(
      "openai/gpt-5",
    );
  });

  it("keeps a draft visible after the profile is deleted elsewhere, and says so", async () => {
    const user = userEvent.setup();
    renderPanel([makeProfile("balanced")]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const slowModel = screen.getByTestId("model-profile-model-slow");
    await user.clear(slowModel);
    await user.type(slowModel, "anthropic/claude-opus-4.5");

    act(() => socket().emit("model_profile_deleted", { name: "balanced" }));

    expect(screen.getByTestId("model-profile-editor")).toBeInTheDocument();
    expect(screen.getByTestId("model-profile-model-slow")).toHaveValue(
      "anthropic/claude-opus-4.5",
    );
    expect(screen.getByTestId("model-profile-gone")).toBeInTheDocument();
    expect(screen.queryByTestId("model-profile-row-balanced")).not.toBeInTheDocument();
  });

  it("shows the referencing project names when a removal is refused", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () =>
          Promise.resolve({
            detail:
              "model profile 'balanced' is the default for alpha, beta; clear or reassign those project defaults first",
          }),
      }),
    );
    renderPanel([makeProfile("balanced")]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByTestId("remove-model-profile-balanced"));

    expect(await screen.findByTestId("model-profile-editor-error")).toHaveTextContent(
      "alpha, beta",
    );
    // Nothing was removed, so the row stays.
    expect(screen.getByTestId("model-profile-row-balanced")).toBeInTheDocument();
  });

  it("replaces the saved list wholesale on a new snapshot", () => {
    renderPanel([makeProfile("balanced"), makeProfile("thorough")]);
    expect(screen.getAllByTestId(/^model-profile-row-/)).toHaveLength(2);

    act(() => socket().emitSnapshot([makeProfile("cheap")]));

    expect(screen.getAllByTestId(/^model-profile-row-/)).toHaveLength(1);
    expect(screen.getByTestId("model-profile-row-cheap")).toBeInTheDocument();
  });
});

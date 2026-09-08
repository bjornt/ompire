import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { ModelProfile, Task, TaskExecutionInputs, WorkflowDescriptor } from "../types";
type DeferredPromise<T> = {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
};

const nativePromiseWithResolvers = Promise as PromiseConstructor & {
  withResolvers<T>(): DeferredPromise<T>;
};


class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor() {
    MockWebSocket.instances.push(this);
  }

  close() {}
  send() {}

  emit(type: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "", type, payload }) });
  }

  emitSnapshot(payload: {
    projects: unknown[];
    model_profiles?: unknown[];
    workflow_catalog?: unknown[];
    workflow_library?: unknown[];
    tasks: unknown[];
    sessions?: unknown;
    workflows?: unknown;
    attention?: unknown;
    reviews?: unknown;
    ships?: unknown;
    gpg?: unknown;
    gh?: unknown;
    settings?: unknown;
  }) {
    this.onopen?.();
    this.emit("snapshot", payload);
  }
}

const project = {
  name: "maas",
  title: "MAAS",
  upstream_url: "https://example.com/maas.git",
  fork_url: null,
  checkout_path: "/home/op/proj/maas",
  checkout_mode: "adopted",
  fetch_remote: "origin",
  setup_state: "ready",
  setup_error: null,
  default_model_profile: "balanced",
  base_branch: "master",
  branch_pattern: "bjornt/<slug>",
  workshop_additions: "project",
  preamble: "",
  launch_config_state: "reconciled",
};

const balanced: ModelProfile = {
  name: "balanced",
  roles: {
    default: { model: "anthropic/claude-sonnet-4.5", thinking: "medium" },
    smol: { model: "openai/gpt-4.1-mini", thinking: "off" },
    slow: { model: "openai/o3", thinking: "high" },
    plan: { model: "google/gemini-2.5-pro", thinking: "max" },
  },
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const singleStep: WorkflowDescriptor = {
  name: "single-step",
  primary_session: "main",
  sessions: ["main"],
  steps: [
    { name: "work", kind: "agent", session: "main", role: "default", conditional: false },
  ],
  revision: "sha256:abc",
  format: 1,
};

const bugfix: WorkflowDescriptor = {
  name: "bugfix",
  primary_session: "coder",
  sessions: ["reproducer", "coder"],
  steps: [
    { name: "reproduce", kind: "agent", session: "reproducer", role: "default", conditional: false },
    { name: "triage", kind: "decision", session: null, role: null, conditional: false },
    { name: "fix", kind: "agent", session: "coder", role: "default", conditional: true },
    { name: "validate-script", kind: "command", session: null, role: null, conditional: true },
  ],
  revision: "sha256:abc",
  format: 1,
};

/** The launch decision an accepted task carries (ADR-0026). */
function makeInputs(overrides: Partial<TaskExecutionInputs> = {}): TaskExecutionInputs {
  return {
    version: 3,
    provenance: "accepted",
    accepted_at: "2026-07-18T00:00:00Z",
    project_name: "maas",
    workflow_name: "single-step",
    workflow_binding: {
      revision: "sha256:abc",
      source: "accepted",
      bound_at: "2026-07-18T00:00:00Z",
      legacy_through_seq: 0,
      interrupted_legacy_seq: null,
    },
    model_profile_name: "balanced",
    model_profile_source: "project",
    step_bindings: {
      work: {
        profile_name: "balanced",
        profile_source: "project",
        role: "default",
        role_source: "workflow",
        roles: balanced.roles,
      },
    },
    workspace: {
      base_branch: "master",
      branch_pattern: "bjornt/<slug>",
      workshop_additions: "project",
      preamble: "",
    },
    workspace_overrides: [],
    branch: "bjornt/fix-bug",
    checkout_path: "/home/op/proj/maas",
    fetch_remote: "origin",
    upstream_url: "https://example.com/maas.git",
    fork_url: null,
    unknown_inputs: [],
    ...overrides,
  };
}

/** The daemon's preview response for the default draft. */
function previewResponse(overrides: Record<string, unknown> = {}) {
  return {
    preview_token: "tok-1",
    project_name: "maas",
    workflow_name: "single-step",
    model_profile: "balanced",
    model_profile_source: "project",
    project_default_model_profile: "balanced",
    workflow_revision: "sha256:abc",
    workflow_format: 1,
    workflow_primary_session: "main",
    workflow_sessions: ["main"],
    auxiliary_consumers: [],
    roles: balanced.roles,
    workspace: {
      base_branch: "master",
      branch_pattern: "bjornt/<slug>",
      workshop_additions: "project",
      preamble: "",
    },
    inherited_workspace: {
      base_branch: "master",
      branch_pattern: "bjornt/<slug>",
      workshop_additions: "project",
      preamble: "",
    },
    workspace_overrides: [],
    branch: "bjornt/fix-bug",
    steps: [
      {
        step: "work",
        kind: "agent",
        session: "main",
        conditional: false,
        declared_role: "default",
        binding: {
          profile_name: "balanced",
          profile_source: "project",
          role: "default",
          role_source: "workflow",
          roles: balanced.roles,
        },
        role: "default",
        model: "anthropic/claude-sonnet-4.5",
        thinking: "medium",
      },
      {
        step: "review",
        kind: "agent",
        session: "main",
        conditional: true,
        declared_role: "slow",
        binding: {
          profile_name: "balanced",
          profile_source: "project",
          role: "slow",
          role_source: "workflow",
          roles: balanced.roles,
        },
        role: "slow",
        model: "openai/o3",
        thinking: "high",
      },
    ],
    ...overrides,
  };
}

const githubProject = { ...project, upstream_url: "https://github.com/ompire/maas.git" };
const readyGitHub = {
  identity: {
    state: "ready",
    host: "github.com",
    login: "octo",
    credential_source: "GitHub CLI configuration",
    executable_path: "/usr/bin/gh",
    version: "gh version 2.97.0",
    detail: null,
    checked_at: "t0",
  },
  targets: {
    "github.com/ompire/maas": {
      state: "allowed",
      target: { host: "github.com", owner: "ompire", repository: "maas" },
      identity: {
        host: "github.com",
        login: "octo",
        credential_source: "GitHub CLI configuration",
      },
      detail: null,
      checked_at: "t0",
    },
  },
};

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    project_name: "maas",
    execution_inputs: makeInputs(),
    needs_configuration: false,
    slug: "fix-bug",
    branch: "bjornt/fix-bug",
    clone_path: "/home/op/tasks/maas/fix-bug",
    state: "created",
    prompt: "fix it",
    error: null,
    workshop_id: null,
    spawn_completed_at: "2026-07-18T00:01:00Z",
    pr_url: null,
    pr_state: null,
    pr_merged_at: null,
    workflow_name: "single-step",
    workflow_revision: "sha256:abc",
    workflow_revision_source: "accepted",
    workflow_ready: true,
    workflow_readiness_reason: null,
    workflow_readiness_detail: null,
    workflow_primary_session: "main",
    workflow_sessions: ["main"],
    workflow_status: null,
    workflow_step: null,
    workflow_result: null,
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:01:00Z",
    ...overrides,
  };
}

function socket(): MockWebSocket {
  return MockWebSocket.instances[0];
}

async function renderAt(
  path: string,
  snapshot: {
    projects: unknown[];
    model_profiles?: unknown[];
    workflow_catalog?: unknown[];
    workflow_library?: unknown[];
    tasks: unknown[];
    sessions?: unknown;
    workflows?: unknown;
    attention?: unknown;
    reviews?: unknown;
    ships?: unknown;
    gpg?: unknown;
    gh?: unknown;
    settings?: unknown;
  },
) {
  window.history.pushState({}, "", path);
  render(<App />);
  act(() => {
    socket().emitSnapshot(snapshot);
  });
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  // The Spawn draft survives a route unmount on purpose; it must not survive
  // into another test.
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** The Spawn form talks to two endpoints: `/api/tasks/preview` on every
 * effective change, and `/api/tasks` on submit. This stubs both from one
 * mock so a test can assert on either without wiring the other. */
function stubLaunchFetch(options: {
  preview?: unknown;
  previewStatus?: number;
  accept?: unknown;
  acceptStatus?: number;
  acceptDetail?: unknown;
} = {}) {
  const fetchMock = vi.fn((url: string, init?: { method?: string }) => {
    if (typeof url === "string" && url.endsWith("/api/tasks/preview")) {
      const status = options.previewStatus ?? 200;
      return Promise.resolve({
        ok: status < 400,
        status,
        json: () => Promise.resolve(options.preview ?? previewResponse()),
      });
    }
    if (typeof url === "string" && url === "/api/tasks" && init?.method === "POST") {
      const status = options.acceptStatus ?? 200;
      return Promise.resolve({
        ok: status < 400,
        status,
        json: () =>
          Promise.resolve(
            status < 400
              ? (options.accept ?? makeTask())
              : { detail: options.acceptDetail ?? "refused" },
          ),
      });
    }
    if (typeof url === "string" && /\/api\/tasks\/\d+$/.test(url)) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ...makeTask(), workshop_status: "present" }),
      });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ paths: [], truncated: false }) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** One library entry standing behind a catalog descriptor. Spawn reads the
 * library for entries the catalog cannot carry — an archived or damaged
 * selection has no descriptor, and the operator still has to see it. */
function libraryEntry(name: string, revision: string) {
  const descriptor = name === "bugfix" ? bugfix : singleStep;
  return {
    name,
    origin: "custom" as const,
    archived: false,
    version: 3,
    has_draft: true,
    current_revision: revision,
    current_format: 1,
    available: true,
    unavailable_reason: null,
    unavailable_detail: null,
    created_at: "2026-09-07T00:00:00Z",
    updated_at: "2026-09-07T00:00:00Z",
    descriptor: { ...descriptor, revision },
  };
}

const emptyDraft = {
  workflow: "",
  project: "",
  profile: "",
  slug: "",
  prompt: "",
  overrides: {},
  stepOverrides: {},
};

const launchSnapshot = {
  projects: [project],
  model_profiles: [balanced],
  workflow_catalog: [bugfix, singleStep],
  workflow_library: [libraryEntry("bugfix", "sha256:abc"), libraryEntry("single-step", "sha256:abc")],
  tasks: [],
};

/** Fill the three inputs that make a draft resolvable and wait for the
 * preview the daemon answers with. */
async function fillDraft(user: ReturnType<typeof userEvent.setup>, slug = "fix-bug") {
  await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
  await user.selectOptions(screen.getByLabelText("Project"), "maas");
  await user.type(screen.getByLabelText("Task slug"), slug);
  await screen.findByTestId("step-preview");
}

describe("SpawnView", () => {
  it("offers every registered workflow against every project, with no template step", async () => {
    stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);

    const workflows = screen.getByLabelText("Workflow");
    expect(within(workflows).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "select a workflow",
      "bugfix — 4 steps, 2 sessions",
      "single-step — 1 step, 1 session",
    ]);
    expect(screen.queryByLabelText("Project template")).toBeNull();
  });

  it("previews every declared step with the model each consumer would use", async () => {
    stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();

    await fillDraft(user);

    const preview = screen.getByTestId("step-preview");
    expect(within(preview).getByTestId("step-work")).toHaveTextContent(
      "anthropic/claude-sonnet-4.5",
    );
    expect(within(preview).getByTestId("step-work")).toHaveTextContent("medium");
    // A step a route can pass by is shown as conditional, and it is an
    // ordinary declared step — there is no row for a model that runs outside
    // the flow (ADR-0028).
    const conditional = within(preview).getByTestId("step-review");
    expect(conditional).toHaveTextContent("openai/o3");
    expect(conditional).toHaveTextContent("conditional");
    expect(screen.getByTestId("branch-preview")).toHaveTextContent(
      "branch: bjornt/fix-bug · off origin/master",
    );
  });

  it("shows a command or gate step with no model at all", async () => {
    stubLaunchFetch({
      preview: previewResponse({
        workflow_name: "bugfix",
        steps: [
          {
            step: "reproduce",
            kind: "agent",
            session: "reproducer",
            role: "default",
            model: "anthropic/claude-sonnet-4.5",
            thinking: "medium",
            conditional: false,
          },
          {
            step: "triage",
            kind: "decision",
            session: null,
            role: null,
            model: null,
            thinking: null,
            conditional: false,
          },
        ],
      }),
    });
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Workflow"), "bugfix");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");
    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await screen.findByTestId("step-preview");

    const decision = screen.getByTestId("step-triage");
    // A decision never reaches a provider, so it is shown without one rather
    // than with an inherited-looking model.
    expect(decision).toHaveTextContent("decision");
    expect(decision).not.toHaveTextContent("claude");
  });

  it("inherits the project profile and reports where it came from", async () => {
    stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();

    await fillDraft(user);

    expect(screen.getByTestId("profile-source")).toHaveTextContent(
      "inherited from the project",
    );
    const picker = screen.getByLabelText("Model profile");
    expect(within(picker).getAllByRole("option")[0]).toHaveTextContent(
      "inherit from project — balanced",
    );
  });

  it("replaces inheritance with an explicit task profile, and resets back", async () => {
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);

    await user.selectOptions(screen.getByLabelText("Model profile"), "balanced");
    await waitFor(() => {
      const body = JSON.parse(
        (fetchMock.mock.calls.at(-1)![1] as { body: string }).body,
      );
      expect(body.model_profile).toBe("balanced");
    });

    await user.click(screen.getByTestId("reset-profile"));
    await waitFor(() => {
      const body = JSON.parse(
        (fetchMock.mock.calls.at(-1)![1] as { body: string }).body,
      );
      expect("model_profile" in body).toBe(false);
    });
  });

  it("overrides one row's profile and another's role independently", async () => {
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);

    await user.selectOptions(screen.getByTestId("row-profile-work"), "balanced");
    await user.selectOptions(screen.getByTestId("row-role-review"), "plan");

    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      // Each row carries only the dimension that was actually chosen — the
      // other stays inherited.
      expect(body.step_overrides).toEqual({
        work: { model_profile: "balanced" },
        review: { role: "plan" },
      });
    });
  });

  it("resets one dimension of a row and leaves the other selected", async () => {
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);

    await user.selectOptions(screen.getByTestId("row-profile-work"), "balanced");
    await user.selectOptions(screen.getByTestId("row-role-work"), "plan");
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect(body.step_overrides.work).toEqual({
        model_profile: "balanced",
        role: "plan",
      });
    });

    await user.click(screen.getByTestId("reset-row-profile-work"));
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      // Resetting the profile must not take the role choice with it.
      expect(body.step_overrides).toEqual({ work: { role: "plan" } });
    });

    await user.click(screen.getByTestId("reset-row-role-work"));
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect("step_overrides" in body).toBe(false);
    });
  });

  it("clears row overrides on a workflow change and says so", async () => {
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);

    await user.selectOptions(screen.getByTestId("row-profile-work"), "balanced");
    await user.selectOptions(screen.getByTestId("row-role-review"), "plan");
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect(body.step_overrides).toEqual({
        work: { model_profile: "balanced" },
        review: { role: "plan" },
      });
    });

    await user.selectOptions(screen.getByLabelText("Workflow"), "bugfix");

    // Nothing transfers by row position or a coincidentally matching name.
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect(body.workflow_name).toBe("bugfix");
      expect("step_overrides" in body).toBe(false);
    });
    const cleared = screen.getByTestId("cleared-overrides");
    expect(cleared).toHaveTextContent("work");
    expect(cleared).toHaveTextContent("review");
    // The rest of the draft is not workflow-scoped and survives.
    expect(screen.getByLabelText("Task slug")).toHaveValue("fix-bug");
  });

  it("clears row overrides when the selected workflow gets a new revision", async () => {
    // Editing the definition can move, rename, or drop a step, so a per-step
    // model choice cannot be reattached by name — it is dropped, and said so.
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.selectOptions(screen.getByTestId("row-profile-work"), "balanced");
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect(body.step_overrides).toEqual({ work: { model_profile: "balanced" } });
    });

    act(() => {
      socket().emit("workflow_library_updated", {
        ...libraryEntry("single-step", "sha256:new"),
        version: 12,
      });
    });

    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect("step_overrides" in body).toBe(false);
    });
    expect(screen.getByTestId("cleared-overrides")).toHaveTextContent("new revision");
    // Everything that is not workflow-scoped is still there.
    expect(screen.getByLabelText("Task slug")).toHaveValue("fix-bug");
    expect(screen.getByLabelText("Project")).toHaveValue("maas");
  });

  it("re-resolves a revision change even when there was nothing to clear", async () => {
    // The notice and the refetch are independent. A draft with no per-step
    // overrides has nothing to clear, and must still end up with a resolution
    // it can submit rather than an emptied preview and a dead button.
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    await fillDraft(userEvent.setup());
    const before = fetchMock.mock.calls.length;

    act(() => {
      socket().emit("workflow_library_updated", {
        ...libraryEntry("single-step", "sha256:new"),
        version: 12,
      });
    });

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
    await screen.findByTestId("step-preview");
    expect(screen.queryByTestId("cleared-overrides")).toBeNull();
    expect(screen.getByRole("button", { name: "Spawn task" })).not.toBeDisabled();
  });

  it("leaves a draft-only edit of the selected workflow alone", async () => {
    // Editing a draft changes nothing about what Spawn would execute, so it
    // must not reset the row overrides or re-resolve an unchanged preview.
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.selectOptions(screen.getByTestId("row-profile-work"), "balanced");
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect(body.step_overrides).toEqual({ work: { model_profile: "balanced" } });
    });
    const before = fetchMock.mock.calls.length;

    act(() => {
      // A new edit version, the same executable revision: a saved draft.
      socket().emit("workflow_library_updated", {
        ...libraryEntry("single-step", "sha256:abc"),
        version: 12,
      });
    });

    expect(fetchMock.mock.calls.length).toBe(before);
    expect(screen.queryByTestId("cleared-overrides")).toBeNull();
    expect(screen.getByTestId("row-profile-work")).toHaveValue("balanced");
  });

  it("keeps an archived selection visible and unsubmittable instead of picking another", async () => {
    stubLaunchFetch({ previewStatus: 422 });
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");
    await user.type(screen.getByLabelText("Task slug"), "fix-bug");

    act(() => {
      socket().emit("workflow_library_updated", {
        ...libraryEntry("single-step", "sha256:abc"),
        version: 12,
        archived: true,
        available: false,
        unavailable_reason: "archived",
        unavailable_detail: "archived; restore it to launch it again",
        descriptor: null,
      });
    });

    // Still the selection, still named, with the reason and a way back.
    expect(screen.getByLabelText("Workflow")).toHaveValue("single-step");
    expect(screen.getByTestId("spawn-workflow-unavailable")).toHaveTextContent("archived");
    expect(screen.getByRole("button", { name: "Spawn task" })).toBeDisabled();
    expect(screen.getByLabelText("Task slug")).toHaveValue("fix-bug");
  });

  it("preselects the workflow the library handed it, keeping the rest of the draft", async () => {
    stubLaunchFetch();
    window.sessionStorage.setItem(
      "ompire.spawnDraft",
      JSON.stringify({ ...emptyDraft, project: "maas", slug: "fix-bug", prompt: "typed" }),
    );
    window.history.pushState({ usr: { workflow: "bugfix" } }, "", "/spawn");
    render(<App />);
    act(() => {
      socket().emitSnapshot(launchSnapshot);
    });

    await waitFor(() => expect(screen.getByLabelText("Workflow")).toHaveValue("bugfix"));
    expect(screen.getByLabelText("Project")).toHaveValue("maas");
    expect(screen.getByLabelText("Prompt")).toHaveValue("typed");
  });

  it("applies the handoff once, so a later choice survives coming back to it", async () => {
    // The handoff lives on the history entry. Re-applying it on every mount
    // would discard the workflow the operator chose afterwards — and their
    // per-step overrides with it — without even saying so.
    const fetchMock = stubLaunchFetch();
    window.history.pushState({ usr: { workflow: "bugfix" } }, "", "/spawn");
    render(<App />);
    act(() => {
      socket().emitSnapshot(launchSnapshot);
    });
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("Workflow")).toHaveValue("bugfix"));

    await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");
    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await screen.findByTestId("step-preview");
    await user.selectOptions(screen.getByTestId("row-profile-work"), "balanced");
    await waitFor(() => {
      const body = JSON.parse((fetchMock.mock.calls.at(-1)![1] as { body: string }).body);
      expect(body.step_overrides).toEqual({ work: { model_profile: "balanced" } });
    });

    // Leave, then come *back* to the same history entry — which still carries
    // the handoff state, unlike a fresh link to /spawn.
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await screen.findByTestId("tier-matrix");
    act(() => {
      window.history.back();
    });

    await screen.findByTestId("spawn-form");
    expect(screen.getByLabelText("Workflow")).toHaveValue("single-step");
    expect(screen.getByTestId("row-profile-work")).toHaveValue("balanced");
  });

  it("keeps a row correctable when its resolution fails", async () => {
    stubLaunchFetch({ previewStatus: 422 });
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");
    await user.type(screen.getByLabelText("Task slug"), "fix-bug");

    // Controls come from the daemon's workflow catalog, not from the failed
    // resolution, so the row that needs correcting is still on screen.
    const row = await screen.findByTestId("row-profile-work");
    expect(row).toBeInTheDocument();
    expect(screen.getByTestId("preview-error")).toBeInTheDocument();
  });

  it("keeps a selected profile visible after it is deleted, without re-picking one", async () => {
    stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.selectOptions(screen.getByTestId("row-profile-work"), "balanced");

    // The profile registry loses it while the draft is open.
    act(() => {
      socket().emit("snapshot", { ...launchSnapshot, model_profiles: [] });
    });

    const control = screen.getByTestId("row-profile-work");
    expect(control).toHaveValue("balanced");
    expect(within(control).getByRole("option", { name: /unavailable/ })).toBeInTheDocument();
  });

  it("re-resolves when the profile registry moves under an open draft", async () => {
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    const before = fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/preview").length;

    // Editing a profile changes what these selections resolve to. Leaving the
    // old model on screen would be a lie, so the form asks the daemon again
    // rather than continuing to show a resolution that no longer holds.
    act(() => {
      socket().emit("model_profile_updated", {
        ...balanced,
        roles: {
          ...balanced.roles,
          slow: { model: "openai/o4", thinking: "minimal" },
        },
      });
    });

    await waitFor(() => {
      const after = fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/preview").length;
      expect(after).toBeGreaterThan(before);
    });
  });

  it("sends only the advanced fields the operator actually overrode", async () => {
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);

    await user.click(screen.getByText(/Advanced/));
    await user.clear(screen.getByLabelText("Branch pattern"));
    await user.type(screen.getByLabelText("Branch pattern"), "wip/<slug>");

    await waitFor(() => {
      const body = JSON.parse(
        (fetchMock.mock.calls.at(-1)![1] as { body: string }).body,
      );
      expect(body.workspace_overrides).toEqual({ branch_pattern: "wip/<slug>" });
    });

    await user.click(screen.getByTestId("reset-branch_pattern"));
    await waitFor(() => {
      const body = JSON.parse(
        (fetchMock.mock.calls.at(-1)![1] as { body: string }).body,
      );
      expect("workspace_overrides" in body).toBe(false);
    });
  });

  it("submits the reviewed token with the resolved selections", async () => {
    const fetchMock = stubLaunchFetch();
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.type(screen.getByLabelText("Prompt"), "fix it");
    await screen.findByTestId("step-preview");

    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    const accept = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/tasks" && (init as { method?: string })?.method === "POST",
    )!;
    expect(JSON.parse((accept[1] as { body: string }).body)).toEqual({
      project_name: "maas",
      workflow_name: "single-step",
      slug: "fix-bug",
      prompt: "fix it",
      preview_token: "tok-1",
    });
  });

  it("refuses a stale review and shows the changed resolution instead of retrying", async () => {
    const fetchMock = stubLaunchFetch({
      acceptStatus: 409,
      acceptDetail: {
        reason: "preview_changed",
        message: "the launch configuration changed since it was previewed; review again",
      },
    });
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    expect(await screen.findByTestId("stale-review")).toHaveTextContent(
      "The configuration changed since you reviewed it",
    );
    // Exactly one acceptance attempt: a refusal is never retried under
    // settings the operator has not reviewed.
    const accepts = fetchMock.mock.calls.filter(
      ([url, init]) => url === "/api/tasks" && (init as { method?: string })?.method === "POST",
    );
    expect(accepts).toHaveLength(1);
    expect(window.location.pathname).toBe("/spawn");
  });

  it("keeps the draft through a trip to Settings to create a profile", async () => {
    stubLaunchFetch();
    await renderAt("/spawn", {
      ...launchSnapshot,
      model_profiles: [],
      projects: [{ ...project, default_model_profile: null }],
    });
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");
    await user.type(screen.getByLabelText("Task slug"), "keep-me");
    await user.type(screen.getByLabelText("Prompt"), "do not lose this");
    expect(screen.getByTestId("no-profiles")).toHaveTextContent("Create one in Settings");

    await user.click(screen.getByRole("link", { name: "Create one in Settings" }));
    expect(window.location.pathname).toBe("/settings");

    await user.click(screen.getByRole("link", { name: "Spawn task" }));
    expect(screen.getByLabelText("Task slug")).toHaveValue("keep-me");
    expect(screen.getByLabelText("Prompt")).toHaveValue("do not lose this");
  });

  it("blocks launching against a project whose configuration is unreconciled", async () => {
    stubLaunchFetch({ previewStatus: 409 });
    await renderAt("/spawn", {
      ...launchSnapshot,
      projects: [{ ...project, launch_config_state: "needs-reconciliation" }],
    });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");
    await user.type(screen.getByLabelText("Task slug"), "fix-bug");

    expect(await screen.findByTestId("project-unreconciled")).toHaveTextContent(
      "needs launch-configuration reconciliation",
    );
    expect(screen.getByRole("button", { name: "Spawn task" })).toBeDisabled();
  });

  it("blocks launching against a project whose checkout is not ready", async () => {
    stubLaunchFetch({ previewStatus: 409 });
    await renderAt("/spawn", {
      ...launchSnapshot,
      projects: [{ ...project, setup_state: "cloning" }],
    });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");

    expect(await screen.findByTestId("project-not-ready")).toHaveTextContent("cloning");
    expect(screen.getByRole("button", { name: "Spawn task" })).toBeDisabled();
  });

  it("renders pipeline progress from spawn_step events after submit", async () => {
    stubLaunchFetch({ accept: makeTask({ spawn_completed_at: null }) });
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    await act(async () => {
      socket().emit("task_created", makeTask({ spawn_completed_at: null }));
      socket().emit("spawn_step", { task_id: 1, step: "clone", status: "ok" });
      socket().emit("spawn_step", { task_id: 1, step: "branch", status: "started" });
    });

    const pipeline = await screen.findByTestId("spawn-progress");
    expect(within(pipeline).getByText("Clone").closest(".step")).toHaveAttribute(
      "data-step-status",
      "ok",
    );
    expect(within(pipeline).getByText("Branch").closest(".step")).toHaveAttribute(
      "data-step-status",
      "running",
    );
  });

  it("navigates to the task once the workspace is ready", async () => {
    stubLaunchFetch({ accept: makeTask({ spawn_completed_at: null }) });
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    await act(async () => {
      socket().emit("task_created", makeTask({ spawn_completed_at: null }));
      socket().emit("task_updated", makeTask({ spawn_completed_at: "t1" }));
    });

    await waitFor(() => expect(window.location.pathname).toBe("/tasks/1"));
  });

  it("unlocks the form and keeps its values when the daemon rejects the request", async () => {
    stubLaunchFetch({ acceptStatus: 409, acceptDetail: "a live task maas/fix-bug already exists" });
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);
    await user.type(screen.getByLabelText("Prompt"), "fix it");
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    expect(window.location.pathname).toBe("/spawn");
    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
    expect(screen.getByLabelText("Task slug")).toHaveValue("fix-bug");
    expect(screen.getByLabelText("Prompt")).toHaveValue("fix it");
    expect(screen.getByRole("button", { name: "Spawn task" })).toBeEnabled();
  });

  it("issues one acceptance request for a double activation", async () => {
    const deferred = nativePromiseWithResolvers.withResolvers<unknown>();
    const fetchMock = vi.fn((url: string, init?: { method?: string }) => {
      if (typeof url === "string" && url.endsWith("/api/tasks/preview")) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(previewResponse()) });
      }
      if (url === "/api/tasks" && init?.method === "POST") return deferred.promise;
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ paths: [], truncated: false }) });
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderAt("/spawn", launchSnapshot);
    const user = userEvent.setup();
    await fillDraft(user);

    await user.dblClick(screen.getByRole("button", { name: "Spawn task" }));
    act(() => {
      screen
        .getByTestId("spawn-form")
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    const accepts = fetchMock.mock.calls.filter(
      ([url, init]) => url === "/api/tasks" && (init as { method?: string })?.method === "POST",
    );
    expect(accepts).toHaveLength(1);

    await act(async () => {
      deferred.resolve({ ok: true, json: () => Promise.resolve(makeTask()) });
      await deferred.promise;
    });
  });
});

describe("TasksView session status", () => {
  it("renders the working pill with breathing dot and slide bar from the snapshot", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: { "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } } },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("working");
    expect(card).not.toHaveTextContent("created");
    expect(card.querySelector(".breathingDot")).toBeInTheDocument();
    expect(screen.getByTestId("slide-bar-1")).toBeInTheDocument();
  });

  it("updates pill and tier styling live on status_changed", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: { "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } } },
    });

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "working",
        to: "idle",
        reason: "agent_end, queue empty after 2.0s",
      });
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("idle");
    expect(screen.queryByTestId("slide-bar-1")).not.toBeInTheDocument();
    expect(card.querySelector(".breathingDot")).not.toBeInTheDocument();
  });

  it("failed sessions render interrupt styling with the reason accessible", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "failed", reason: "process exited with code 137", since: "t0" } },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.className).toContain("failed");
    expect(screen.getByTestId("session-reason-1")).toHaveTextContent(
      "process exited with code 137",
    );
  });

  it("falls back to the spawn-derived pill when no session exists", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ spawn_completed_at: null })],
      sessions: {},
    });

    expect(screen.getByTestId("task-card-1")).toHaveTextContent("spawning");
  });

  it("stalled sessions render notify/amber styling with the reason accessible", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "stalled", reason: "no frames for 300s", since: "t0" } },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.className).toContain("stalled");
    expect(card).toHaveTextContent("stalled");
    expect(card.querySelector(".notifyDot")).toBeInTheDocument();
    expect(card.querySelector(".statePill.notify")).toHaveAttribute(
      "title",
      "no frames for 300s",
    );
  });

  it("retrying sessions render quiet badge styling and don't raise the count", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "retrying", reason: "auto_retry_start: HTTP 429", since: "t0" } },
      },
      attention: {},
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("retrying");
    expect(card.querySelector(".ringDot")).toBeInTheDocument();
    expect(card.className).not.toContain("failed");
    expect(card.className).not.toContain("stalled");
    expect(screen.getByText("0 need you")).toBeInTheDocument();
  });

  it("shows an amber context ring and tokens/cost line from a stats event", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
      },
    });

    act(() => {
      socket().emit("stats", {
        task_id: 1,
        session: "main",
        context_pct: 85,
        tokens: { input: 1200, output: 340 },
        cost: 0.0123,
      });
      socket().emit("advisory", { task_id: 1, session: "main", kind: "context-high", context_pct: 85 });
    });

    const stats = screen.getByTestId("card-stats-1");
    expect(stats).toHaveTextContent("1200 in / 340 out");
    expect(stats).toHaveTextContent("$0.0123");
    expect(stats).toHaveTextContent("85%");
    expect(stats.querySelector("[data-testid='context-ring']")).toBeInTheDocument();
  });

  it("decorates an idle card with a maybe-waiting advisory", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "idle", reason: "agent_end, queue empty after 2.0s", since: "t0" } },
      },
    });

    act(() => {
      socket().emit("advisory", { task_id: 1, session: "main", kind: "maybe-waiting" });
    });

    expect(screen.getByTestId("maybe-waiting-1")).toHaveTextContent(
      "may be waiting for a reply",
    );

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "idle",
        to: "working",
        reason: "agent_start frame",
      });
      socket().emit("advisory_cleared", { task_id: 1, session: "main", kind: "maybe-waiting" });
    });

    expect(screen.queryByTestId("maybe-waiting-1")).not.toBeInTheDocument();
  });

  it("counts tasks with an active daemon attention entry; working sessions stay silent", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask(),
        makeTask({ id: 2, slug: "other", branch: "bjornt/other" }),
        makeTask({ id: 3, slug: "third", branch: "bjornt/third", state: "failed", error: "x" }),
      ],
      sessions: {
        "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
        "2": { main: { status: "failed", reason: "process exited with code 137", since: "t0" } },
        "3": { main: { status: "failed", reason: "stopped by operator", since: "t0" } },
      },
      attention: {
        "2": { tier: "interrupt", status: "failed", reason: "process exited with code 137", session: "main" },
        "3": { tier: "interrupt", status: "failed", reason: "stopped by operator", session: "main" },
      },
    });

    // Task 3 is failed twice over (registry + attention entry) but counts once.
    expect(screen.getByText("2 need you")).toBeInTheDocument();
    expect(document.title).toBe("(2) ompire");
  });

  it("raises the count live on an attention event and lowers it on attention_cleared", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: { "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } } },
      attention: {},
    });
    expect(screen.getByText("0 need you")).toBeInTheDocument();

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "working",
        to: "waiting-input",
        reason: "pending question",
      });
      socket().emit("attention", {
        task_id: 1,
        tier: "notify",
        status: "waiting-input",
        reason: "pending question",
        session: "main",
      });
    });
    expect(screen.getByText("1 need you")).toBeInTheDocument();
    expect(document.title).toBe("(1) ompire");

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "waiting-input",
        to: "working",
        reason: "operator answered the pending question",
      });
      socket().emit("attention_cleared", { task_id: 1 });
    });
    expect(screen.getByText("0 need you")).toBeInTheDocument();
    expect(document.title).toBe("ompire");
  });

  it("sets a badged favicon while the count is nonzero and reverts to the plain mark at zero", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {},
      attention: {},
    });
    const icon = () => document.querySelector("link[rel='icon']");
    expect(icon()?.getAttribute("href")).toBe("/favicon.svg");

    act(() => {
      socket().emit("attention", {
        task_id: 1,
        tier: "interrupt",
        status: "failed",
        reason: "process exited with code 1",
        session: "main",
      });
    });
    expect(icon()?.getAttribute("href")).toMatch(/^data:image\/svg\+xml/);

    act(() => {
      socket().emit("attention_cleared", { task_id: 1 });
    });
    expect(icon()?.getAttribute("href")).toBe("/favicon.svg");
  });

  const askQuestion = {
    id: "ask-ui-1",
    kind: "ask" as const,
    questions: [
      {
        prompt: "Widen the fix to both loops?",
        options: [
          { value: "both", label: "Both loops", description: null },
          { value: "v4-only", label: "v4 only", description: null },
        ],
        multi: false,
        recommended: "both",
        allowsOther: false,
      },
    ],
  };

  it("renders an inline quick-answer for a fitting single-select ask and answers it", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
    vi.stubGlobal("fetch", fetchMock);
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": {
          main: {
            status: "waiting-input",
            reason: "pending question 'ask-ui-1'",
            since: "t0",
            question: askQuestion,
          },
        },
      },
    });

    const quick = screen.getByTestId("quick-answer-1");
    expect(quick).toHaveTextContent("Widen the fix to both loops?");
    const recommended = within(quick).getByRole("button", { name: /Both loops/ });
    expect(recommended).toHaveTextContent("·rec");

    const user = userEvent.setup();
    await user.click(recommended);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/sessions/main/agent/answer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question_id: "ask-ui-1", selections: ["both"] }),
      }),
    );
  });

  it("defers a non-fitting question (multi-select) and an approval gate to task detail", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask(),
        makeTask({ id: 2, slug: "other", branch: "bjornt/other" }),
      ],
      sessions: {
        "1": {
          main: {
            status: "waiting-input",
            reason: "pending question",
            since: "t0",
            question: {
              id: "ask-ui-2",
              kind: "ask",
              questions: [
                {
                  prompt: "Pick one or more",
                  options: [{ value: "a", label: "A", description: null }],
                  multi: true,
                  recommended: null,
                  allowsOther: false,
                },
              ],
            },
          },
        },
        "2": {
          main: {
            status: "waiting-approval",
            reason: "pending approval",
            since: "t0",
            question: { id: "approval-ui-1", kind: "approval", questions: [] },
          },
        },
      },
    });

    expect(screen.queryByTestId("quick-answer-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("quick-answer-defer-1")).toHaveTextContent("Open task detail to answer");
    expect(screen.queryByTestId("quick-answer-2")).not.toBeInTheDocument();
    expect(screen.getByTestId("quick-answer-defer-2")).toHaveTextContent("Open task detail to answer");
  });

  it("removes the quick-answer control once the question resolves", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": {
          main: {
            status: "waiting-input",
            reason: "pending question",
            since: "t0",
            question: askQuestion,
          },
        },
      },
    });
    expect(screen.getByTestId("quick-answer-1")).toBeInTheDocument();

    act(() => {
      socket().emit("question_resolved", { task_id: 1, session: "main", question_id: "ask-ui-1" });
    });
    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "waiting-input",
        to: "working",
        reason: "operator answered the pending question",
      });
    });

    expect(screen.queryByTestId("quick-answer-1")).not.toBeInTheDocument();
  });

  it("counts waiting-input/waiting-approval sessions in the N-need-you pill", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask(),
        makeTask({ id: 2, slug: "other", branch: "bjornt/other" }),
        makeTask({ id: 3, slug: "third", branch: "bjornt/third" }),
      ],
      sessions: {
        "1": { main: { status: "waiting-input", reason: "pending question", since: "t0" } },
        "2": { main: { status: "waiting-approval", reason: "pending approval", since: "t0" } },
        "3": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
      },
      attention: {
        "1": { tier: "notify", status: "waiting-input", reason: "pending question", session: "main" },
        "2": { tier: "interrupt", status: "waiting-approval", reason: "pending approval", session: "main" },
      },
    });

    expect(screen.getByText("2 need you")).toBeInTheDocument();
  });
});

describe("TasksView workflow pills", () => {
  function stepRecord(overrides: Record<string, unknown>) {
    return {
      task_id: 1,
      seq: 1,
      step: "work",
      kind: "agent",
      session: "main",
      status: "running",
      outcome: null,
      error: null,
      pause: null,
      prompted_at: null,
      started_at: "t0",
      finished_at: null,
      ...overrides,
    };
  }

  it("prefixes the pill with the current step while an agent step runs", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "running", workflow_step: "work" })],
      sessions: {
        "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
      },
      workflows: {
        "1": { name: "single-step", status: "running", step: "work", steps: [stepRecord({})] },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.querySelector(".statePill.live")).toHaveTextContent("work: working");
    // Tier styling and the slide bar still follow the session status.
    expect(screen.getByTestId("slide-bar-1")).toBeInTheDocument();
  });

  it("reads '<step>: waiting-input' with the waiting tier when the step's session asks", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "running", workflow_step: "validate" })],
      sessions: {
        "1": {
          main: {
            status: "waiting-input",
            reason: "pending question 'ask-1'",
            since: "t0",
            question: {
              id: "ask-1",
              kind: "ask",
              questions: [
                {
                  prompt: "Widen?",
                  options: [{ value: "y", label: "Yes", description: null }],
                  multi: false,
                  recommended: null,
                  allowsOther: false,
                },
              ],
            },
          },
        },
      },
      workflows: {
        "1": {
          name: "reproduce-and-fix",
          status: "running",
          step: "validate",
          steps: [stepRecord({ step: "validate" })],
        },
      },
    });

    expect(screen.getByTestId("task-card-1")).toHaveTextContent("validate: waiting-input");
  });

  it("reads '<step>: running' for command steps", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "running", workflow_step: "build" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "build-and-fix",
          status: "running",
          step: "build",
          steps: [
            stepRecord({ step: "work", status: "ok", finished_at: "t1" }),
            stepRecord({ seq: 2, step: "build", kind: "command", session: null }),
          ],
        },
      },
    });

    const pill = screen.getByTestId("workflow-pill-1");
    expect(pill).toHaveTextContent("build: running");
    expect(pill.className).toContain("live");
  });

  it("reads '<step>: waiting' with notify styling while parked at a gate", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "waiting", workflow_step: "confirm" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "reproduce-and-fix",
          status: "waiting",
          step: "confirm",
          steps: [
            stepRecord({ step: "fix", status: "ok", finished_at: "t1" }),
            stepRecord({
              seq: 2,
              step: "confirm",
              kind: "gate",
              session: null,
              status: "waiting",
              outcome: { message: "Ship it?" },
            }),
          ],
        },
      },
    });

    const pill = screen.getByTestId("workflow-pill-1");
    expect(pill).toHaveTextContent("confirm: waiting");
    expect(pill.className).toContain("notify");
  });

  it("renders the bare session status once the run completes", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "complete", workflow_step: "work" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "single-step",
          status: "complete",
          step: "work",
          steps: [stepRecord({ status: "ok", finished_at: "t1" })],
        },
      },
    });

    const pill = screen.getByTestId("task-card-1").querySelector(".statePill.neutral");
    expect(pill).toHaveTextContent("idle");
    expect(pill).not.toHaveTextContent("work:");
  });

  it("fails the card with the step error on the pill when the workflow fails", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "failed", workflow_step: "build" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "build-and-fix",
          status: "failed",
          step: "build",
          steps: [
            stepRecord({ step: "work", status: "ok", finished_at: "t1" }),
            stepRecord({
              seq: 2,
              step: "build",
              kind: "command",
              session: null,
              status: "failed",
              error: "exit code 2",
              finished_at: "t2",
            }),
          ],
        },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.className).toContain("failed");
    const pill = screen.getByTestId("workflow-failed-pill-1");
    expect(pill).toHaveTextContent("build: failed");
    expect(pill).toHaveAttribute("title", "exit code 2");
  });
});

describe("TaskDetailView", () => {
  function stubDetailFetch(detail: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(detail) }),
    );
  }

  it("renders the metadata panel with derived workshop status", async () => {
    const task = makeTask({ workshop_id: "ws-maas-fix-bug" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", { projects: [project], tasks: [task] });

    const meta = await screen.findByTestId("task-metadata");
    expect(meta).toHaveTextContent("bjornt/fix-bug");
    expect(meta).toHaveTextContent("/home/op/tasks/maas/fix-bug");
    expect(screen.getByTestId("workshop-status")).toHaveTextContent("present · ws-maas-fix-bug");
  });

  it("shows the accepted configuration, not a recomputation from today's settings", async () => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [{ ...project, default_model_profile: "something-else", base_branch: "trunk" }],
      model_profiles: [balanced],
      tasks: [task],
    });

    const panel = await screen.findByTestId("task-inputs");
    expect(within(panel).getByTestId("accepted-profile")).toHaveTextContent("balanced");
    expect(within(panel).getByTestId("accepted-profile")).toHaveTextContent(
      "inherited from the project",
    );
    // The project now says `trunk`; the task keeps what it was accepted with.
    expect(panel).toHaveTextContent("master");
    // Per consumer, with its own attribution. Every consumer is a declared
    // step; there is no row for a model that runs outside the flow.
    const work = within(panel).getByTestId("consumer-work");
    expect(work).toHaveTextContent("anthropic/claude-sonnet-4.5");
    expect(work).toHaveTextContent("inherited from the project");
    expect(work).toHaveTextContent("declared by the workflow");
    expect(within(panel).queryByTestId("consumer-judge")).toBeNull();
    // The exact procedure is named, not just the workflow's name.
    expect(within(panel).getByTestId("workflow-revision")).toHaveTextContent(
      "sha256:abc",
    );
  });

  it("shows a per-step override as overridden and the rest as inherited", async () => {
    const task = makeTask({
      execution_inputs: makeInputs({
        step_bindings: {
          work: {
            profile_name: "thorough",
            profile_source: "step",
            role: "plan",
            role_source: "step",
            roles: balanced.roles,
          },
        },
      }),
    });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      model_profiles: [balanced],
      tasks: [task],
    });

    const panel = await screen.findByTestId("task-inputs");
    const work = within(panel).getByTestId("consumer-work");
    expect(work).toHaveTextContent("thorough");
    expect(work).toHaveTextContent("overridden for this step");
    // The task-wide decision is still shown: it is what the other rows
    // inherited, and what the operator chose at the top of the form.
    expect(within(panel).getByTestId("accepted-profile")).toHaveTextContent("balanced");
  });

  it("asks a task that predates pinned inputs to confirm a continuation", async () => {
    const task = makeTask({ execution_inputs: null, needs_configuration: true });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (typeof url === "string" && url.endsWith("/configuration")) {
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({
                task_id: 1,
                needs_configuration: true,
                archived: false,
                known: { branch: "bjornt/fix-bug" },
                source_attribution: [],
                needs_workflow_confirmation: true,
                workflow_readiness: {
                  ready: false,
                  reason: "needs_configuration",
                  detail: "no confirmed launch configuration",
                  confirmable: false,
                },
                workflow_candidate: {
                  workflow_name: "single-step",
                  revision: "sha256:abc",
                  format: 1,
                  available: true,
                  compatible: true,
                  problems: [],
                  primary_session: "main",
                  sessions: ["main"],
                  legacy_through_seq: 0,
                  interrupted_legacy_seq: null,
                  uncertainty_notice:
                    "This task will no longer ask a model to classify a result it cannot read.",
                },
                unknown_inputs: [
                  "model_profile",
                  "thinking",
                  "preamble",
                  "workflow_definition",
                ],
                candidates: {
                  base_branch: "master",
                  workshop_additions: "project",
                  preamble: "",
                  default_model_profile: "balanced",
                },
              }),
          });
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ ...task, workshop_status: "present" }),
        });
      }),
    );
    await renderAt("/tasks/1", {
      projects: [project],
      model_profiles: [balanced],
      tasks: [task],
    });

    await screen.findByTestId("task-inputs");
    // The panel renders as soon as the task loads; the legacy evidence needs
    // its own /configuration response, so wait for that rather than the panel.
    expect(await screen.findByTestId("legacy-unknown")).toHaveTextContent(
      "cannot be recovered",
    );
    // The acknowledgements gate confirmation; nothing is pre-ticked. There
    // are two, because there are two different things the daemon cannot
    // recover: the launch inputs, and the definition itself (ADR-0028).
    expect(screen.getByTestId("legacy-acknowledge")).not.toBeChecked();
    expect(screen.getByTestId("legacy-acknowledge-workflow")).not.toBeChecked();
    expect(screen.getByTestId("legacy-confirm")).toBeDisabled();
    // The candidate definition is named and readable, and the changed
    // uncertainty policy is stated before anything is confirmed.
    expect(screen.getByTestId("workflow-candidate")).toHaveTextContent("sha256:abc");
    expect(screen.getByTestId("uncertainty-notice")).toHaveTextContent(
      "no longer ask a model to classify",
    );
  });

  it("keeps Continue reachable after the configuration is confirmed", async () => {
    // Confirming records what the task continues under and starts nothing, so
    // the only control that re-arms an interrupted run has to outlive the
    // confirmation panel — otherwise a confirmed task is stranded (ADR-0028).
    const task = makeTask({ workflow_status: "waiting", workflow_step: "escalate" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      model_profiles: [balanced],
      tasks: [task],
    });

    const panel = await screen.findByTestId("task-inputs");
    expect(within(panel).getByTestId("legacy-continue")).toHaveTextContent(
      "Continue the interrupted run",
    );
  });

  it("offers no Continue for a run that is not interrupted", async () => {
    const task = makeTask({ workflow_status: "complete", workflow_step: null });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      model_profiles: [balanced],
      tasks: [task],
    });

    const panel = await screen.findByTestId("task-inputs");
    expect(within(panel).queryByTestId("legacy-continue")).toBeNull();
  });

  it("blocks confirmation when the current definition cannot explain the history", async () => {
    const task = makeTask({ execution_inputs: null, needs_configuration: true });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (typeof url === "string" && url.endsWith("/configuration")) {
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({
                task_id: 1,
                needs_configuration: true,
                needs_workflow_confirmation: true,
                archived: false,
                known: { branch: "bjornt/fix-bug" },
                source_attribution: [],
                workflow_readiness: {
                  ready: false,
                  reason: "needs_configuration",
                  detail: "no confirmed launch configuration",
                  confirmable: false,
                },
                workflow_candidate: {
                  workflow_name: "single-step",
                  revision: "sha256:abc",
                  format: 1,
                  available: true,
                  compatible: false,
                  problems: [
                    "attempt #3 ran a step 'triage' that the current 'single-step' definition does not declare",
                  ],
                  legacy_through_seq: 3,
                  interrupted_legacy_seq: null,
                  uncertainty_notice: "Unresolved evidence now waits for you.",
                },
                unknown_inputs: ["workflow_definition"],
                candidates: {
                  base_branch: "master",
                  workshop_additions: "project",
                  preamble: "",
                  default_model_profile: "balanced",
                },
              }),
          });
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ ...task, workshop_status: "present" }),
        });
      }),
    );
    await renderAt("/tasks/1", {
      projects: [project],
      model_profiles: [balanced],
      tasks: [task],
    });

    // Named, with the reason — and not remapped onto a definition that
    // cannot account for what already ran.
    expect(await screen.findByTestId("workflow-incompatible")).toHaveTextContent(
      "does not declare",
    );
    expect(screen.getByTestId("legacy-confirm")).toBeDisabled();
  });

  it("shows escape-hatch commands with the task's clone path", async () => {
    const task = makeTask({ workshop_id: "ws-maas-fix-bug" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", { projects: [project], tasks: [task] });

    const hatch = await screen.findByTestId("escape-hatch");
    expect(hatch).toHaveTextContent("cd /home/op/tasks/maas/fix-bug");
    expect(hatch).toHaveTextContent("workshop shell");
    expect(hatch).toHaveTextContent("omp --resume");
  });

  it("task cards link to the detail route", async () => {
    const task = makeTask({ workshop_id: "ws-maas-fix-bug" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks", { projects: [project], tasks: [task] });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("task-link-1"));
    expect(await screen.findByTestId("task-metadata")).toBeInTheDocument();
  });
  it("keeps the task-scoped review panel independent from the selected session tab", async () => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: {
        "1": {
          main: { status: "idle", reason: "agent_end", since: "t0" },
          checker: { status: "working", reason: "validation", since: "t0" },
        },
      },
    });

    await screen.findByTestId("task-detail-review");
    expect(screen.getByTestId("task-detail-start-review")).toBeEnabled();
    await userEvent.setup().click(screen.getByTestId("session-tab-checker"));
    expect(screen.getByTestId("task-detail-start-review")).toBeEnabled();
  });

  it("renders comment feedback live and restores re-review when the primary session idles", async () => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "working", reason: "review comments", since: "t0" } } },
      reviews: {
        "1": {
          status: "open",
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [
            {
              outcome: "comments",
              comment_count: 2,
              stderr: null,
              recorded_at: "2026-08-26T10:00:00Z",
            },
          ],
        },
      },
    });

    expect(await screen.findByTestId("task-detail-review")).toHaveTextContent(
      "The primary agent is addressing review comments.",
    );
    expect(screen.queryByTestId("task-detail-start-review")).not.toBeInTheDocument();
    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "working",
        to: "idle",
        reason: "agent_end",
      });
    });
    expect(await screen.findByTestId("task-detail-start-review")).toHaveTextContent("Start another review");
  });

  it("locks review start until observed state, then exposes cancellation", async () => {
    const task = makeTask();
    const { promise: reviewResponse, resolve: resolveReview } =
      nativePromiseWithResolvers.withResolvers<unknown>();
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/tasks/1") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...task, workshop_status: "present" }) });
      }
      if (url === "/api/tasks/1/review") return reviewResponse;
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "idle", reason: "agent_end", since: "t0" } } },
    });

    const start = await screen.findByTestId("task-detail-start-review");
    const user = userEvent.setup();
    await user.dblClick(start);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/1/review")).toHaveLength(1);
    expect(start).toBeDisabled();
    await act(async () => {
      resolveReview({
        ok: true,
        json: () => Promise.resolve({ status: "open", url: "http://127.0.0.1:7180", port: 7180, iterations: [] }),
      });
    });
    expect(start).toBeDisabled();
    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "idle",
        to: "reviewing",
        reason: "llmvet review",
      });
      socket().emit("review_started", { task_id: 1, url: "http://127.0.0.1:7180", port: 7180 });
    });
    expect(await screen.findByTestId("task-detail-cancel-review")).toBeEnabled();
    expect(screen.getByTestId("review-external-link")).toHaveTextContent("http://127.0.0.1:7180");
  });

  it("shows terminal iteration evidence and the approved Ship flow handoff", async () => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "idle", reason: "review approved", since: "t0" } } },
      reviews: {
        "1": {
          status: "approved",
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [
            {
              outcome: "error",
              comment_count: null,
              stderr: "reviewer stderr",
              recorded_at: "2026-08-26T10:00:00Z",
            },
            {
              outcome: "approved",
              comment_count: null,
              stderr: null,
              recorded_at: "2026-08-26T11:00:00Z",
            },
          ],
        },
      },
    });

    const review = await screen.findByTestId("task-detail-review");
    expect(within(review).getByTestId("review-iterations")).toHaveTextContent("reviewer stderr");
    expect(within(review).getByText("Show error details")).toBeInTheDocument();
    expect(review.querySelector(".reviewStatusBadge.approved")).toBeInTheDocument();
    expect(within(review).getByTestId("task-detail-ship-link")).toHaveAttribute("href", "/ship/1");
    expect(review.querySelector('time[datetime="2026-08-26T10:00:00Z"]')).toBeInTheDocument();
  });

  it("says a superseded approval is history rather than a handoff", async () => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "idle", reason: "review approved", since: "t0" } } },
      reviews: {
        "1": {
          status: "approved",
          url: null,
          port: null,
          candidate_id: "cand-old",
          iterations: [
            {
              outcome: "approved",
              comment_count: 0,
              stderr: null,
              candidate_id: "cand-old",
              recorded_at: "2026-08-26T11:00:00Z",
            },
          ],
        },
      },
      ships: {
        "1": {
          task_id: 1,
          version: 1,
          delivery_id: 10,
          disposition: "open",
          ending: null,
          mode: null,
          candidate_id: "cand-new",
          review_candidate_id: null,
          draft: null,
          blocked_reason: null,
          workspace_owner: null,
          completed_actions: [],
          remaining_actions: [],
          results: {},
          pr_url: null,
          legacy_publication: false,
          actions: [],
          decisions: [],
          history: [],
        },
      },
    });

    const review = await screen.findByTestId("task-detail-review");
    expect(within(review).getByTestId("review-summary-hint")).toHaveTextContent(
      "content changed after this approval",
    );
    // The handoff link stays — the operator still needs to get there to start
    // another review — but it no longer claims the approval carries them on.
    expect(within(review).getByTestId("task-detail-ship-link")).toHaveTextContent(
      "Open Ship flow",
    );
  });

  it("shows a failed start, permits retry, and accepts observation before the retry response", async () => {
    const task = makeTask();
    const { promise: retryResponse, resolve: resolveRetry } =
      nativePromiseWithResolvers.withResolvers<unknown>();
    let reviewAttempts = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/tasks/1") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...task, workshop_status: "present" }) });
      }
      if (url === "/api/tasks/1/review") {
        reviewAttempts += 1;
        return reviewAttempts === 1 ? Promise.reject(new Error("llmvet unavailable")) : retryResponse;
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "idle", reason: "agent_end", since: "t0" } } },
    });

    const user = userEvent.setup();
    await user.click(await screen.findByTestId("task-detail-start-review"));
    expect(await screen.findByTestId("review-command-error")).toHaveTextContent("llmvet unavailable");
    await user.click(screen.getByTestId("task-detail-start-review"));
    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "idle",
        to: "reviewing",
        reason: "llmvet review",
      });
      socket().emit("review_started", { task_id: 1, url: "http://127.0.0.1:7180", port: 7180 });
    });
    await act(async () => {
      resolveRetry({
        ok: true,
        json: () => Promise.resolve({ status: "open", url: "http://127.0.0.1:7180", port: 7180, iterations: [] }),
      });
    });
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/1/review")).toHaveLength(2);
    expect(screen.queryByTestId("review-command-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-detail-cancel-review")).toBeEnabled();
  });

  it("shows a failed cancellation, permits retry, and follows the live aborted outcome", async () => {
    const task = makeTask();
    let cancelAttempts = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/tasks/1") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...task, workshop_status: "present" }) });
      }
      if (url === "/api/tasks/1/review/cancel") {
        cancelAttempts += 1;
        return cancelAttempts === 1
          ? Promise.reject(new Error("review process did not stop"))
          : Promise.resolve({ ok: true, json: () => Promise.resolve({ status: "open" }) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "reviewing", reason: "llmvet review", since: "t0" } } },
      reviews: { "1": { status: "open", url: "http://127.0.0.1:7180", port: 7180, iterations: [] } },
    });

    const user = userEvent.setup();
    await user.click(await screen.findByTestId("task-detail-cancel-review"));
    expect(await screen.findByTestId("review-command-error")).toHaveTextContent("review process did not stop");
    await user.click(screen.getByTestId("task-detail-cancel-review"));
    expect(screen.getByTestId("task-detail-cancel-review")).toBeDisabled();
    act(() => {
      socket().emit("review_iteration", {
        task_id: 1,
        iteration: { outcome: "aborted", comment_count: null, stderr: null, recorded_at: "2026-08-26T12:00:00Z" },
      });
      socket().emit("review_finished", { task_id: 1, status: "aborted" });
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "reviewing",
        to: "idle",
        reason: "review aborted",
      });
    });
    expect(await screen.findByTestId("task-detail-start-review")).toHaveTextContent("Start another review");
    expect(screen.getByTestId("task-detail-review")).toHaveTextContent("Aborted");
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/1/review/cancel")).toHaveLength(2);
  });

  it.each(["aborted", "error"] as const)("renders the %s terminal review state", async (outcome) => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "idle", reason: `review ${outcome}`, since: "t0" } } },
      reviews: {
        "1": {
          status: outcome,
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [
            {
              outcome,
              comment_count: null,
              stderr: outcome === "error" ? "review command failed" : null,
              recorded_at: "2026-08-26T12:00:00Z",
            },
          ],
        },
      },
    });

    const review = await screen.findByTestId("task-detail-review");
    expect(review.querySelector(`.reviewStatusBadge.${outcome}`)).toBeInTheDocument();
    expect(within(review).getByTestId("task-detail-start-review")).toHaveTextContent("Start another review");
  });

  it("exposes Ship flow for recorded ship progress without an approved review", async () => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", {
      projects: [project],
      tasks: [task],
      ships: {
        "1": {
          status: "drafted",
          draft: {
            commit_message: "draft commit",
            pr_title: "draft title",
            pr_body: "draft body",
            source: "agent",
          },
          commit_sha: null,
          pr_url: null,
          error: null,
          updated_at: "t0",
        },
      },
    });

    const review = await screen.findByTestId("task-detail-review");
    expect(within(review).getByTestId("task-detail-ship-link")).toHaveAttribute("href", "/ship/1");
    expect(within(review).getByTestId("task-detail-ship-link")).toHaveTextContent("Open Ship flow");
  });

  it("exposes Ship flow when a pull request arrives live", async () => {
    const task = makeTask();
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", { projects: [project], tasks: [task] });

    const review = await screen.findByTestId("task-detail-review");
    expect(within(review).queryByTestId("task-detail-ship-link")).not.toBeInTheDocument();

    act(() => {
      socket().emit(
        "task_updated",
        makeTask({
          pr_url: "https://github.com/ompire/maas/pull/1",
          updated_at: "2026-08-20T00:02:00Z",
        }),
      );
    });

    expect(within(review).getByTestId("task-detail-ship-link")).toHaveAttribute("href", "/ship/1");
  });

});

describe("Review capability (TasksView)", () => {
  it("renders a reviewing card with violet styling and a reopen link", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "reviewing", reason: "llmvet review on http://127.0.0.1:7180", since: "t0" } },
      },
      reviews: {
        "1": {
          status: "open",
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [],
        },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("reviewing");
    expect(card.querySelector(".statePill.review")).toBeInTheDocument();
    const link = card.querySelector(".reviewPillLink") as HTMLAnchorElement;
    expect(link).toBeInTheDocument();
    expect(link.href).toBe("http://127.0.0.1:7180/");
  });

  it("offers a Review action on idle cards and posts the review endpoint", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "idle", reason: "agent_end", since: "t0" } },
      },
    });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          task_id: 1,
          status: "open",
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [],
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(screen.getByTestId("review-button-1"));
    expect(fetchMock).toHaveBeenCalledWith("/api/tasks/1/review", expect.objectContaining({ method: "POST" }));
  });
  it("makes a card-started review available from task detail without reloading", async () => {
    const task = makeTask();
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/tasks/1") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...task, workshop_status: "present" }) });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ status: "open", url: "http://127.0.0.1:7180", port: 7180, iterations: [] }),
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderAt("/tasks", {
      projects: [project],
      tasks: [task],
      sessions: { "1": { main: { status: "idle", reason: "agent_end", since: "t0" } } },
    });

    const user = userEvent.setup();
    await user.click(screen.getByTestId("review-button-1"));
    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "idle",
        to: "reviewing",
        reason: "llmvet review",
      });
      socket().emit("review_started", { task_id: 1, url: "http://127.0.0.1:7180", port: 7180 });
    });
    await user.click(screen.getByTestId("task-link-1"));
    expect(await screen.findByTestId("task-detail-review")).toHaveTextContent("Review open");
    expect(screen.getByTestId("review-external-link")).toHaveAttribute("href", "http://127.0.0.1:7180");
  });

  it("uses the primary session for review eligibility while another session is active", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "running", workflow_step: "validate" })],
      sessions: {
        "1": {
          main: { status: "idle", reason: "agent_end", since: "t0" },
          checker: { status: "working", reason: "validation", since: "t0" },
        },
      },
      workflows: {
        "1": {
          name: "multi-session",
          status: "running",
          step: "validate",
          steps: [
            {
              task_id: 1,
              seq: 1,
              step: "implement",
              kind: "agent",
              session: "main",
              status: "ok",
              outcome: null,
              error: null,
              pause: null,
              prompted_at: null,
              started_at: "t0",
              finished_at: "t1",
            },
            {
              task_id: 1,
              seq: 2,
              step: "validate",
              kind: "agent",
              session: "checker",
              status: "running",
              outcome: null,
              error: null,
              pause: null,
              prompted_at: null,
              started_at: "t2",
              finished_at: null,
            },
          ],
        },
      },
    });

    expect(screen.getByTestId("task-card-1")).toHaveTextContent("validate: working");
    expect(screen.getByTestId("review-button-1")).toBeEnabled();
  });
  it("withholds review while the bugfix primary is working", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_name: "bugfix", workflow_status: "running", workflow_step: "reproduce" })],
      sessions: {
        "1": {
          reproducer: { status: "idle", reason: "reproduction complete", since: "t0" },
          coder: { status: "working", reason: "fixing issue", since: "t0" },
        },
      },
      workflows: {
        "1": {
          name: "bugfix",
          status: "running",
          step: "reproduce",
          steps: [
            {
              task_id: 1,
              seq: 1,
              step: "reproduce",
              kind: "agent",
              session: "reproducer",
              status: "ok",
              outcome: null,
              error: null,
              pause: null,
              prompted_at: null,
              started_at: "t0",
              finished_at: "t1",
            },
          ],
        },
      },
    });

    expect(screen.queryByTestId("review-button-1")).not.toBeInTheDocument();
  });

  it("links task cards with approved reviews, ship progress, or pull requests", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask({ id: 1, slug: "approved" }),
        makeTask({ id: 2, slug: "drafted" }),
        makeTask({ id: 3, slug: "pull-request", pr_url: "https://github.com/ompire/maas/pull/3" }),
      ],
      reviews: {
        "1": { status: "approved", url: "http://127.0.0.1:7180", port: 7180, iterations: [] },
      },
      ships: {
        "2": {
          status: "drafted",
          draft: {
            commit_message: "draft commit",
            pr_title: "draft title",
            pr_body: "draft body",
            source: "agent",
          },
          commit_sha: null,
          pr_url: null,
          error: null,
          updated_at: "t0",
        },
      },
    });

    for (const id of [1, 2, 3]) {
      expect(screen.getByTestId(`ship-link-${id}`)).toHaveAttribute("href", `/ship/${id}`);
    }
  });

  it("adds card Ship flow links from live review, ship, and task deltas", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask({ id: 1, slug: "review-live" }),
        makeTask({ id: 2, slug: "ship-live" }),
        makeTask({ id: 3, slug: "pr-live" }),
      ],
    });

    for (const id of [1, 2, 3]) {
      expect(screen.queryByTestId(`ship-link-${id}`)).not.toBeInTheDocument();
    }

    act(() => {
      socket().emit("review_started", { task_id: 1, url: "http://127.0.0.1:7180", port: 7180 });
      socket().emit("review_finished", { task_id: 1, status: "approved" });
      socket().emit("ship_updated", {
        task_id: 2,
        version: 1,
        delivery_id: 10,
        disposition: "open",
        ending: null,
        mode: null,
        candidate_id: null,
        review_candidate_id: null,
        draft: {
          commit_message: "draft commit",
          pr_title: "draft title",
          pr_body: "draft body",
          source: "agent",
          state: "ready",
        },
        blocked_reason: null,
        workspace_owner: null,
        completed_actions: [],
        remaining_actions: [],
        results: {},
        pr_url: null,
        legacy_publication: false,
        actions: [],
        decisions: [],
        history: [],
      });
      socket().emit(
        "task_updated",
        makeTask({
          id: 3,
          slug: "pr-live",
          pr_url: "https://github.com/ompire/maas/pull/3",
          updated_at: "2026-08-20T00:02:00Z",
        }),
      );
    });

    for (const id of [1, 2, 3]) {
      expect(screen.getByTestId(`ship-link-${id}`)).toHaveAttribute("href", `/ship/${id}`);
    }
  });
});

describe("ShipFlowView", () => {
  const projection = (overrides: Record<string, unknown> = {}) => ({
    task_id: 1,
    version: 1,
    delivery_id: 10,
    disposition: "open",
    ending: null,
    mode: null,
    candidate_id: null,
    review_candidate_id: null,
    draft: null,
    blocked_reason: null,
    workspace_owner: null,
    completed_actions: [],
    remaining_actions: [],
    results: {},
    pr_url: null,
    legacy_publication: false,
    actions: [],
    decisions: [],
    history: [],
    authority: authorityAtApproval(),
    ...overrides,
  });

  /** A run waiting at an approval that can publish through a pull request.
   *
   * Every ship projection carries one now: the page reads what the run's own
   * procedure permits rather than offering a menu of endings. */
  const authorityAtApproval = (overrides: Record<string, unknown> = {}) => ({
    format: 3,
    source: "workflow-gate",
    declared_actions: ["commit", "push", "pr"],
    declares_review: true,
    gate_seq: 5,
    gate_step: "approve",
    review_seq: 4,
    review_outcome: "approved",
    choices: [
      {
        id: "finish",
        label: "Finish without publishing",
        feedback_required: false,
        authorizes: null,
      },
      {
        id: "publish",
        label: "Open a pull request",
        feedback_required: false,
        authorizes: ["commit", "push", "pr"],
      },
    ],
    suggested: {},
    action_seq: null,
    action_step: null,
    action_kind: null,
    awaiting_continuation: false,
    review_step_seq: null,
    refusal_code: null,
    refusal: null,
    ...overrides,
  });

  const approvedReview = (candidateId = "cand-1") => ({
    status: "approved",
    url: null,
    port: null,
    candidate_id: candidateId,
    iterations: [
      {
        outcome: "approved",
        comment_count: 0,
        stderr: null,
        candidate_id: candidateId,
        recorded_at: "2026-08-20T00:00:00Z",
      },
    ],
  });

  const previewBody = (overrides: Record<string, unknown> = {}) => ({
    task_id: 1,
    delivery_id: 10,
    version: 1,
    ending: "commit",
    mode: "squash",
    request_id: "req-x",
    source: "workflow-gate",
    gate_seq: 5,
    choice_id: "publish",
    review_seq: 4,
    actions: ["commit"],
    candidate_id: "cand-1",
    candidate: {
      candidate_id: "cand-1",
      base_branch: "main",
      base_commit: "b".repeat(40),
      original_head: "h".repeat(40),
      tree_id: "t".repeat(40),
      commit_count: 2,
      dirty: false,
    },
    review: {
      status: "approved",
      approved_candidate_id: "cand-1",
      current_candidate_id: "cand-1",
      content_bound: true,
      stale: false,
    },
    completed_actions: [],
    remaining_actions: ["commit"],
    routing: {
      remote_url: "https://github.com/ompire/maas",
      branch: "ompire/fix-bug",
      ref: "refs/heads/ompire/fix-bug",
      head: "ompire/fix-bug",
      base_branch: "main",
      upstream_url: "https://github.com/ompire/maas",
      slug: "ompire/maas",
    },
    identity: {
      signing: { fingerprint: "F".repeat(40), uid: "Op <op@example.com>", source: "auto" },
      git_transport: {
        state: "unattributed",
        detail: "Ompire uses ambient Git credentials for the push.",
      },
    },
    commit_message: "ship: it",
    pr_title: "",
    pr_body: "",
    marker: "m".repeat(32),
    blockers: [],
    deliverable: true,
    preview_token: "token-1",
    ...overrides,
  });

  it("renders the bare Ship flow chooser from the snapshot and updates it live", async () => {
    await renderAt("/ship", { projects: [project], tasks: [makeTask()] });

    expect(screen.getByTestId("ship-index-empty")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ship flow" }).className).toContain("navLinkActive");

    act(() => {
      socket().emit(
        "ship_updated",
        projection({
          draft: {
            commit_message: "draft commit",
            pr_title: "draft title",
            pr_body: "draft body",
            source: "agent",
            state: "ready",
          },
        }),
      );
    });

    const row = await screen.findByTestId("ship-index-row-1");
    expect(row).toHaveAttribute("href", "/ship/1");
    // The run is waiting for a person, and the index says so rather than
    // calling it a drafting step nobody asked for.
    expect(row).toHaveTextContent("Next: Waiting for your decision");
  });

  it("waits for the daemon snapshot before rendering the bare Ship flow index", () => {
    window.history.pushState({}, "", "/ship");
    render(<App />);

    expect(screen.getByTestId("ship-index-loading")).toBeInTheDocument();

    act(() => {
      socket().emitSnapshot({ projects: [project], tasks: [] });
    });
    expect(screen.getByTestId("ship-index-empty")).toBeInTheDocument();
  });

  it("names a local-only delivery a success rather than a missing pull request", async () => {
    await renderAt("/ship", {
      projects: [project],
      tasks: [makeTask({ id: 1, slug: "local-only" })],
      ships: {
        "1": projection({
          disposition: "completed",
          ending: "commit",
          mode: "squash",
          completed_actions: ["commit"],
          results: {
            commit: {
              signed_tip: "s".repeat(40),
              commit_count: 1,
              mode: "squash",
              installed: true,
            },
          },
        }),
      },
    });

    const row = screen.getByTestId("ship-index-row-1");
    expect(row).toHaveTextContent("Next: Signed locally");
    expect(row).toHaveTextContent("Nothing was pushed");
    expect(screen.queryByTestId("ship-index-error-1")).not.toBeInTheDocument();
  });

  it("surfaces an unresolved effect ahead of everything else", async () => {
    await renderAt("/ship", {
      projects: [project],
      tasks: [makeTask({ id: 1 })],
      ships: {
        "1": projection({
          disposition: "unresolved",
          ending: "push",
          blocked_reason: "the destination could not be read",
          actions: [
            {
              id: 5,
              kind: "push",
              attempt: 1,
              phase: "needs_reconciliation",
              expected: { ref: "refs/heads/ompire/fix-bug", signed_tip: "s".repeat(40) },
              error: "the response was lost",
              updated_at: "2026-08-20T00:00:00Z",
            },
          ],
        }),
      },
    });

    const row = screen.getByTestId("ship-index-row-1");
    expect(row).toHaveTextContent("Next: Needs a decision");
    expect(screen.getByTestId("ship-index-error-1")).toHaveTextContent(
      "the destination could not be read",
    );
  });

  it("waits for the current snapshot before resolving a direct ship route", async () => {
    window.history.pushState({}, "", "/ship/1");
    render(<App />);

    expect(screen.getByTestId("ship-flow-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("ship-flow-not-found")).not.toBeInTheDocument();

    act(() => {
      socket().emitSnapshot({ projects: [project], tasks: [makeTask()] });
    });
    expect(await screen.findByTestId("ship-flow")).toBeInTheDocument();
  });

  it("offers Ship flow and Tasks recovery links for an unknown task route", async () => {
    await renderAt("/ship/999", { projects: [project], tasks: [makeTask()] });

    const unknown = screen.getByTestId("ship-flow-not-found");
    expect(within(unknown).getByRole("link", { name: "Ship flow" })).toHaveAttribute("href", "/ship");
    expect(within(unknown).getByRole("link", { name: "Tasks" })).toHaveAttribute("href", "/tasks");
  });

  it("offers three endings, each naming every effect it permits", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: { "1": approvedReview() },
    });

    const chooser = screen.getByTestId("ending-chooser");
    expect(chooser).toHaveTextContent("Local signed commit");
    expect(chooser).toHaveTextContent("Sign locally and stop. Nothing is pushed");
    expect(chooser).toHaveTextContent("Pushed branch");
    expect(chooser).toHaveTextContent("No pull request is opened");
    expect(chooser).toHaveTextContent("Pull request");
    expect(chooser).toHaveTextContent("Sign, push, and open a pull request");
  });

  it("previews a delivery read-only and only then offers a confirmation", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/tasks/1/ship/preview") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(previewBody()) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(projection()) });
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: { "1": approvedReview() },
      gpg: { state: "ready", selected: null, candidates: [], detail: null, checked_at: "t" },
    });

    await user.click(screen.getByTestId("ending-commit"));
    expect(screen.queryByTestId("confirm-delivery-button")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("preview-delivery-button"));

    const preview = await screen.findByTestId("delivery-preview");
    expect(within(preview).getByTestId("preview-action-commit")).toHaveTextContent("Sign one commit");
    expect(within(preview).queryByTestId("preview-action-push")).not.toBeInTheDocument();
    expect(within(preview).getByTestId("preview-candidate")).toHaveTextContent("cand-1");
    expect(within(preview).getByTestId("preview-signing")).toHaveTextContent("Op <op@example.com>");

    // Preview alone authorized nothing.
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/1/ship/commit"),
    ).toHaveLength(0);

    // The button names the effects this confirmation permits, not the ending
    // in the abstract.
    expect(screen.getByTestId("confirm-delivery-button")).toHaveTextContent(
      "Confirm: sign",
    );

    await user.click(screen.getByTestId("confirm-delivery-button"));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/1/ship/commit"),
      ).toHaveLength(1),
    );
    const body = JSON.parse(
      fetchMock.mock.calls.find(([url]) => url === "/api/tasks/1/ship/commit")?.[1].body as string,
    );
    expect(body).toMatchObject({
      ending: "commit",
      mode: "squash",
      preview_token: "token-1",
      request_id: "req-x",
      expected_version: 1,
    });
  });

  it("invalidates a resolved preview as soon as the inputs change", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/tasks/1/ship/preview") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(previewBody()) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(projection()) });
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: { "1": approvedReview() },
    });

    await user.click(screen.getByTestId("preview-delivery-button"));
    expect(await screen.findByTestId("delivery-preview")).toBeInTheDocument();

    await user.type(screen.getByTestId("commit-message"), "x");
    await waitFor(() =>
      expect(screen.queryByTestId("delivery-preview")).not.toBeInTheDocument(),
    );
  });

  it("lists every reason a delivery is refused and keeps the confirmation disabled", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/tasks/1/ship/preview") {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(
              previewBody({
                deliverable: false,
                blockers: [
                  { code: "review-stale", message: "the task content changed after approval" },
                  { code: "signing-unavailable", message: "the signing key is locked" },
                ],
              }),
            ),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(projection()) });
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: { "1": approvedReview() },
    });

    await user.click(screen.getByTestId("preview-delivery-button"));
    const blockers = await screen.findByTestId("preview-blockers");
    expect(within(blockers).getByTestId("preview-blocker-review-stale")).toBeInTheDocument();
    expect(
      within(blockers).getByTestId("preview-blocker-signing-unavailable"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("confirm-delivery-button")).toBeDisabled();
  });

  it("explains a stale approval instead of offering it as authorization", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: { "1": approvedReview("cand-old") },
      ships: { "1": projection({ candidate_id: "cand-new" }) },
    });

    expect(screen.getByTestId("stale-approval-notice")).toHaveTextContent(
      "content the task has since changed",
    );
    expect(screen.getByTestId("review-summary-hint")).toHaveTextContent(
      "delivering the current content needs a fresh review",
    );
  });

  it("explains a historical approval that never named its content", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: {
        "1": {
          status: "approved",
          url: null,
          port: null,
          iterations: [
            {
              outcome: "approved",
              comment_count: 0,
              stderr: null,
              recorded_at: "2026-08-20T00:00:00Z",
            },
          ],
        },
      },
    });

    expect(screen.getByTestId("stale-approval-notice")).toHaveTextContent(
      "does not identify the content it graded",
    );
  });

  it("shows the concrete signed result and refuses to widen the ending", async () => {
    // The old page let an operator turn a finished commit into a push. That
    // is gone: how far a delivery goes is what the workflow declared and what
    // a person authorized, and a completed narrower ending stays narrow.
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve(projection()) }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: { "1": approvedReview() },
      ships: {
        "1": projection({
          disposition: "completed",
          ending: "commit",
          mode: "squash",
          completed_actions: ["commit"],
          results: {
            commit: {
              signed_tip: "s".repeat(40),
              commit_count: 1,
              mode: "squash",
              installed: true,
            },
          },
          authority: authorityAtApproval({
            source: null,
            declared_actions: ["commit"],
            gate_seq: null,
            gate_step: null,
            choices: [],
            refusal_code: "not-at-gate",
            refusal:
              "this run is not waiting at an approval that authorizes publication, and has no authorized action outstanding.",
          }),
        }),
      },
    });

    expect(screen.getByTestId("result-commit")).toHaveTextContent("Signed commit at ssssssssssss");
    expect(screen.getByTestId("no-pr-notice")).toHaveTextContent("No pull request was opened");
    // There is no ending menu to widen, and the refusal says why.
    expect(screen.queryByTestId("ending-chooser")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ending-push")).not.toBeInTheDocument();
    expect(screen.getByTestId("delivery-refusal")).toHaveTextContent(
      "not waiting at an approval",
    );
    // And nothing was asked of the daemon on the operator's behalf.
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).includes("/ship/")),
    ).toHaveLength(0);
  });

  it("offers explicit decisions for an effect whose outcome is unknown", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(projection()) });
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      ships: {
        "1": projection({
          disposition: "unresolved",
          ending: "pr",
          blocked_reason: "the forge could not be searched completely",
          actions: [
            {
              id: 7,
              kind: "pr",
              attempt: 1,
              phase: "needs_reconciliation",
              expected: { marker: "m".repeat(32) },
              progress: {
                evidence: {
                  state: "unknown",
                  detail: "the correlated search could not be completed",
                },
              },
              error: "the forge could not be searched completely",
              updated_at: "2026-08-20T00:00:00Z",
            },
          ],
        }),
      },
    });

    const panel = screen.getByTestId("ship-recovery-pr");
    expect(within(panel).getByTestId("recovery-reason")).toHaveTextContent(
      "could not be searched completely",
    );
    expect(within(panel).getByTestId("recovery-expected")).toHaveTextContent("marker");
    // What Ompire saw, in the operator's terms — not the attempt's own intent
    // dumped back at them.
    expect(within(panel).getByTestId("recovery-observed")).toHaveTextContent(
      "the correlated search could not be completed",
    );

    await user.click(within(panel).getByTestId("recovery-recheck"));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/1/ship/reconcile"),
      ).toHaveLength(1),
    );
    const body = JSON.parse(
      fetchMock.mock.calls.find(([url]) => url === "/api/tasks/1/ship/reconcile")?.[1]
        .body as string,
    );
    expect(body).toMatchObject({
      delivery_id: 10,
      action_id: 7,
      expected_version: 1,
      decision: "recheck",
    });
  });

  it("says an interrupted draft was not restarted on the operator's behalf", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      ships: {
        "1": projection({
          draft: {
            commit_message: "half a message",
            pr_title: "",
            pr_body: "",
            source: "agent",
            state: "interrupted",
            error: "the daemon restarted while the agent was drafting",
          },
          // A workflow that declares no publication of its own is the only
          // one that still drafts through an agent.
          authority: authorityAtApproval({
            source: null,
            declared_actions: [],
            gate_seq: null,
            gate_step: null,
            choices: [],
          }),
        }),
      },
    });

    expect(screen.getByTestId("draft-status")).toHaveTextContent("Nothing was sent again");
    expect(screen.getByTestId("commit-message")).toHaveValue("half a message");
  });

  it("asks the run's own question, and publishes only what an answer grants", async () => {
    // The whole slice, from the operator's side: the page shows the decision
    // the run is at, nothing is preselected, and the confirmation carries the
    // question and the answer rather than an ending somebody typed.
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/tasks/1/ship/preview") {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(
              previewBody({
                ending: "pr",
                actions: ["commit", "push", "pr"],
                remaining_actions: ["commit", "push", "pr"],
                pr_title: "A change",
              }),
            ),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(projection()) });
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      reviews: { "1": approvedReview() },
      ships: {
        "1": projection({
          authority: authorityAtApproval({
            suggested: { pr_title: "A change", message: "ship: it" },
          }),
        }),
      },
    });

    // No ending menu, and no answer chosen for the operator.
    expect(screen.queryByTestId("ending-chooser")).not.toBeInTheDocument();
    expect(screen.getByTestId("approval-choice-publish")).not.toBeChecked();
    // The answer that publishes nothing is not offered here as a publication.
    expect(screen.queryByTestId("approval-choice-finish")).not.toBeInTheDocument();
    // The workflow's suggestion is a starting point in an editable field.
    expect(screen.getByTestId("pr-title")).toHaveValue("A change");
    expect(screen.getByTestId("commit-message")).toHaveValue("ship: it");
    // Nothing can be previewed until an answer is chosen.
    expect(screen.getByTestId("preview-delivery-button")).toBeDisabled();

    await user.click(screen.getByTestId("approval-choice-publish"));
    await user.click(screen.getByTestId("preview-delivery-button"));
    await screen.findByTestId("delivery-preview");
    expect(screen.getByTestId("preview-decision")).toHaveTextContent(
      "Answering attempt 5 with publish",
    );

    await user.click(screen.getByTestId("confirm-delivery-button"));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => url === "/api/tasks/1/ship/commit"),
      ).toHaveLength(1),
    );
    const body = JSON.parse(
      fetchMock.mock.calls.find(([url]) => url === "/api/tasks/1/ship/commit")?.[1]
        .body as string,
    );
    expect(body).toMatchObject({ gate_seq: 5, choice_id: "publish" });
    // Neither page ever asks an agent to write the publication text.
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/ship/draft")),
    ).toHaveLength(0);
    expect(screen.queryByTestId("redraft-button")).toBeDisabled();
  });

  it("blocks a pushing ending on GitHub eligibility and offers a re-check", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/tasks/1/ship/preview") {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(
              previewBody({
                ending: "pr",
                remaining_actions: ["commit", "push", "pr"],
                deliverable: false,
                blockers: [
                  {
                    code: "github-unavailable",
                    message: "GitHub preflight blocked shipping: GitHub CLI is unauthenticated",
                  },
                ],
              }),
            ),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [githubProject],
      tasks: [makeTask()],
      reviews: { "1": approvedReview() },
      gh: readyGitHub,
    });

    await user.click(screen.getByTestId("preview-delivery-button"));
    const banner = await screen.findByTestId("github-preflight-banner");
    expect(banner).toHaveTextContent("GitHub access is required for this ending");
    // The GitHub API check is not a claim about the Git transport identity.
    expect(banner).toHaveTextContent("does not verify SSH or HTTPS authentication");
    expect(screen.getByTestId("confirm-delivery-button")).toBeDisabled();

    await user.click(screen.getByTestId("recheck-github-target-button"));
    await waitFor(() =>
      expect(fetchMock.mock.calls.filter(([url]) => url === "/api/gh/recheck")).toHaveLength(1),
    );
  });

  it("says a pull request that predates the journal has no authorization behind it", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask({ pr_url: "https://github.com/ompire/maas/pull/3" })],
      ships: {
        "1": projection({
          delivery_id: null,
          disposition: null,
          legacy_publication: true,
          pr_url: "https://github.com/ompire/maas/pull/3",
        }),
      },
    });

    expect(screen.getByTestId("legacy-publication-notice")).toHaveTextContent(
      "no recorded authorization",
    );
    expect(screen.getByTestId("pr-link")).toHaveAttribute(
      "href",
      "https://github.com/ompire/maas/pull/3",
    );
  });

  it("drops a delivery projection older than the one already applied", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      ships: {
        "1": projection({
          version: 5,
          disposition: "completed",
          completed_actions: ["commit"],
          results: {
            commit: {
              signed_tip: "s".repeat(40),
              commit_count: 1,
              mode: "squash",
              installed: true,
            },
          },
        }),
      },
    });
    expect(screen.getByTestId("result-commit")).toBeInTheDocument();

    act(() => {
      socket().emit("ship_updated", projection({ version: 4, disposition: "open" }));
    });
    expect(screen.getByTestId("result-commit")).toBeInTheDocument();

    act(() => {
      socket().emit("ship_updated", projection({ version: 6, disposition: "open" }));
    });
    expect(screen.queryByTestId("result-commit")).not.toBeInTheDocument();
  });
});

describe("Chrome GPG chip", () => {
  const selected = {
    fingerprint: "ABC1230000000000000000000000000000000000",
    key_id: "ABC123",
    uid: "Test Key <t@example.com>",
    keygrip: "abc",
    source: "auto" as const,
    protection: "protected" as const,
  };
  const status = (state: string, extra: Record<string, unknown> = {}) => ({
    state,
    selected,
    candidates: [],
    cache_ttl: null,
    detail: null,
    checked_at: "t0",
    ...extra,
  });

  it.each([
    ["ready", "gpg ready", "Signing key is ready"],
    ["locked", "gpg locked", "GPG signing key is locked"],
    ["ambiguous", "gpg unselected", "Several usable GPG signing keys"],
    ["no_key", "gpg no key", "No signing-capable GPG key"],
    ["missing", "gpg missing", "GPG command-line tools are unavailable"],
    ["agent_unavailable", "gpg agent", "gpg-agent is unreachable"],
    ["error", "gpg error", "indeterminate"],
  ])("labels %s and describes it accessibly", async (state, label, described) => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      gpg: status(state),
    });

    const chip = screen.getByTestId("gpg-chip");
    expect(chip).toHaveTextContent(label);
    expect(chip.getAttribute("aria-label")).toContain(described);
  });

  it("shows a remaining cache lifetime only when the agent reports one", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      gpg: status("ready", { cache_ttl: 10500 }),
    });
    expect(screen.getByTestId("gpg-chip")).toHaveTextContent("gpg ready 2h 55m");
  });

  it("shows no lifetime when the agent reports none", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      gpg: status("ready"),
    });
    expect(screen.getByTestId("gpg-chip")).toHaveTextContent("gpg ready");
    expect(screen.getByTestId("gpg-chip")).not.toHaveTextContent("2h");
  });

  it("shows a faint placeholder when gpg state is unknown", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
    });

    const chip = screen.getByTestId("gpg-chip");
    expect(chip).toHaveTextContent("gpg —");
  });
});

describe("Chrome GitHub chip", () => {
  const readyGitHub = {
    identity: {
      state: "ready",
      host: "github.com",
      login: "octo",
      credential_source: "GitHub CLI configuration",
      executable_path: "/usr/bin/gh",
      version: "gh version 2.97.0",
      detail: null,
      checked_at: "t0",
    },
    targets: {},
  };

  it("renders the safe ready identity and follows live status deltas", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      gh: readyGitHub,
    });

    const chip = screen.getByTestId("gh-chip");
    expect(chip).toHaveTextContent("gh @octo");
    expect(chip).toHaveAccessibleName("GitHub CLI ready as @octo for github.com");

    act(() => {
      socket().emit("gh_status", {
        gh: {
          ...readyGitHub,
          identity: {
            ...readyGitHub.identity,
            state: "unauthenticated",
            login: null,
            detail: "HTTP 401: Bad credentials",
          },
        },
      });
    });

    await waitFor(() => expect(chip).toHaveTextContent("gh auth"));
    expect(chip).toHaveAccessibleName("GitHub CLI authentication is unavailable for github.com");
  });
});

describe("Chrome attention chip", () => {
  it("renders an <a> to /tasks?attention=1 when attention count is non-zero", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      attention: { 1: { tier: "interrupt", status: "failed", reason: "", session: "main" } },
    });

    const chip = screen.getByTestId("attention-chip");
    expect(chip.tagName).toBe("A");
    expect(chip).toHaveAttribute("href", "/tasks?attention=1");
    expect(chip).toHaveClass("needsYouChip");
  });

  it("renders an <a> to /tasks when attention count is zero", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
    });

    const chip = screen.getByTestId("attention-chip");
    expect(chip.tagName).toBe("A");
    expect(chip).toHaveAttribute("href", "/tasks");
    expect(chip).not.toHaveClass("needsYouChip");
  });
});

describe("TasksView PR link", () => {
  it("renders a PR link on a card when task.pr_url is set", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ pr_url: "https://github.com/ompire/maas/pull/7" })],
    });

    const link = screen.getByTestId("task-pr-link-1") as HTMLAnchorElement;
    expect(link).toBeInTheDocument();
    expect(link.href).toBe("https://github.com/ompire/maas/pull/7");
  });
});

describe("TasksView attention filter and sections", () => {
  it("shows only attention tasks when ?attention=1 is set", async () => {
    await renderAt("/tasks?attention=1", {
      projects: [project],
      tasks: [
        makeTask({ id: 1, state: "failed", updated_at: "2026-08-01T00:00:00Z" }),
        makeTask({ id: 2, updated_at: "2026-08-01T00:01:00Z" }),
      ],
      attention: {},
    });

    expect(screen.getByTestId("section-needs-you")).toBeTruthy();
    expect(screen.queryByTestId("task-card-2")).toBeNull();
  });

  it("filters by both project and attention together", async () => {
    await renderAt("/tasks?attention=1&project=maas", {
      projects: [project],
      tasks: [
        makeTask({ id: 1, state: "failed", project_name: "maas" }),
        makeTask({ id: 2, state: "failed", project_name: "other" }),
      ],
      attention: {},
    });

    expect(screen.getByTestId("task-card-1")).toBeTruthy();
    expect(screen.queryByTestId("task-card-2")).toBeNull();
  });

  it("shows attention-specific empty state when filtered to zero", async () => {
    await renderAt("/tasks?attention=1", {
      projects: [project],
      tasks: [makeTask({ id: 1, state: "created" })],
      attention: {},
    });

    const empty = screen.getByTestId("attention-empty-state");
    expect(empty).toHaveTextContent("No tasks need your attention right now");
    expect(within(empty).getByRole("link")).toHaveTextContent("Show all tasks");
  });

  it("preserves project param in the 'Show all tasks' link", async () => {
    await renderAt("/tasks?attention=1&project=maas", {
      projects: [project],
      tasks: [makeTask({ id: 1, state: "created", project_name: "maas" })],
      attention: {},
    });

    expect(within(screen.getByTestId("attention-empty-state")).getByRole("link")).toHaveAttribute(
      "href",
      "/tasks?project=maas",
    );
  });

  it("sections task into Needs you, Running, and Idle/other", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask({ id: 1, updated_at: "2026-08-01T00:00:00Z" }),
        makeTask({ id: 2, updated_at: "2026-08-01T00:01:00Z" }),
        makeTask({ id: 3, updated_at: "2026-08-01T00:02:00Z" }),
      ],
      attention: {
        1: { tier: "interrupt", status: "failed", reason: "", session: "main" },
        2: { tier: "silent", status: "working", reason: "", session: "main" },
      },
      sessions: {
        2: { main: { status: "working", reason: "", since: "" } },
        3: { main: { status: "idle", reason: "", since: "" } },
      },
    });

    const sectionNeedsYou = screen.getByTestId("section-needs-you");
    expect(sectionNeedsYou).toHaveTextContent("Needs you");
    expect(within(sectionNeedsYou).getByTestId("task-card-1")).toBeTruthy();

    const sectionRunning = screen.getByTestId("section-running");
    expect(sectionRunning).toHaveTextContent("Running");
    expect(within(sectionRunning).getByTestId("task-card-2")).toBeTruthy();

    const sectionIdle = screen.getByTestId("section-idle");
    expect(sectionIdle).toHaveTextContent("Idle/other");
    expect(within(sectionIdle).getByTestId("task-card-3")).toBeTruthy();
  });

  it("sorts Needs you by severity then recency", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask({ id: 1, updated_at: "2026-08-01T00:01:00Z" }),
        makeTask({ id: 2, updated_at: "2026-08-01T00:00:00Z" }),
      ],
      attention: {
        1: { tier: "notify", status: "waiting-input", reason: "", session: "main" },
        2: { tier: "interrupt", status: "failed", reason: "", session: "main" },
      },
    });

    const cards = within(screen.getByTestId("section-needs-you")).getAllByTestId(/^task-card-/);
    expect(cards[0]).toHaveAttribute("data-testid", "task-card-2"); // interrupt > notify
    expect(cards[1]).toHaveAttribute("data-testid", "task-card-1");
  });

  it("hides section heading when empty", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ id: 1, updated_at: "2026-08-01T00:00:00Z" })],
      attention: {
        1: { tier: "interrupt", status: "failed", reason: "", session: "main" },
      },
    });

    expect(screen.getByTestId("section-needs-you")).toBeTruthy();
    expect(screen.queryByTestId("section-running")).toBeNull();
    expect(screen.queryByTestId("section-idle")).toBeNull();
  });

  it("state: 'failed' tasks without attention entry show in Needs you", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ id: 1, state: "failed", updated_at: "2026-08-01T00:00:00Z" })],
      attention: {},
    });

    const section = screen.getByTestId("section-needs-you");
    expect(section).toHaveTextContent("Needs you");
    expect(within(section).getByTestId("task-card-1")).toBeTruthy();
  });
});

describe("TasksView Shipped section (merge-poll capability)", () => {
  const shippedTask = () =>
    makeTask({ pr_url: "https://github.com/ompire/maas/pull/7" });

  it("renders a collapsed row once a task has a pr_url", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [shippedTask()] });

    const row = screen.getByTestId("shipped-row-1");
    expect(row).toHaveTextContent("shipped");
    expect(row).toHaveTextContent("maas/fix-bug");
    expect(row).toHaveTextContent("maas#7 · open");
    expect(row).toHaveTextContent("awaiting merge · cleanup deferred");
    // Live rows link to the Ship Flow view, where the cleanup action lives.
    const link = screen.getByTestId("shipped-link-1") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/ship/1");
  });

  it("shows no section when no task has shipped", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [makeTask()] });
    expect(screen.queryByTestId("shipped-section")).not.toBeInTheDocument();
  });

  it("flips the row note live when a poll lands task_updated with pr_state merged", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [shippedTask()] });
    expect(screen.getByTestId("shipped-row-1")).toHaveTextContent("awaiting merge");

    act(() => {
      socket().emit(
        "task_updated",
        makeTask({
          pr_url: "https://github.com/ompire/maas/pull/7",
          pr_state: "merged",
          pr_merged_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        }),
      );
    });

    const row = screen.getByTestId("shipped-row-1");
    expect(row).toHaveTextContent("maas#7 · merged");
    expect(row).toHaveTextContent("merged · ready for cleanup");
  });

  it("keeps archived shipped tasks as inert cleaned-up rows", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        shippedTask(),
        makeTask({
          id: 2,
          slug: "old-fix",
          state: "archived",
          pr_url: "https://github.com/ompire/maas/pull/3",
          pr_state: "merged",
        }),
      ],
    });

    const row = screen.getByTestId("shipped-row-2");
    expect(row).toHaveTextContent("cleaned up");
    expect(screen.queryByTestId("shipped-link-2")).not.toBeInTheDocument();
    // Archived tasks still stay out of the card grid.
    expect(screen.queryByTestId("task-card-2")).not.toBeInTheDocument();
  });
});

describe("ShipFlowView Cleanup step (merge-poll capability)", () => {
  const prTask = (overrides: Partial<Task> = {}) =>
    makeTask({ pr_url: "https://github.com/ompire/maas/pull/7", ...overrides });

  it("stays inert before the task has delivered anything", async () => {
    await renderAt("/ship/1", { projects: [project], tasks: [makeTask()] });

    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent(
      "unlocks once this task has delivered",
    );
    expect(screen.queryByTestId("cleanup-ship-button")).not.toBeInTheDocument();
  });

  it("warns that cleanup removes the only managed copy of a local-only result", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      ships: {
        "1": {
          task_id: 1,
          version: 3,
          delivery_id: 10,
          disposition: "completed",
          ending: "commit",
          mode: "squash",
          candidate_id: "cand-1",
          review_candidate_id: "cand-1",
          draft: null,
          blocked_reason: null,
          workspace_owner: null,
          completed_actions: ["commit"],
          remaining_actions: [],
          results: {
            commit: {
              signed_tip: "s".repeat(40),
              commit_count: 1,
              mode: "squash",
              installed: true,
            },
          },
          pr_url: null,
          legacy_publication: false,
          actions: [],
          decisions: [],
          history: [],
        },
      },
    });

    expect(screen.getByTestId("cleanup-warning")).toHaveTextContent(
      "only Ompire-managed Git copy",
    );
    await user.click(screen.getByTestId("cleanup-ship-button"));
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringContaining("only Ompire-managed Git copy"),
    );
  });

  it("refuses cleanup while a delivery effect's outcome is unknown", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      ships: {
        "1": {
          task_id: 1,
          version: 2,
          delivery_id: 10,
          disposition: "unresolved",
          ending: "push",
          mode: "squash",
          candidate_id: "cand-1",
          review_candidate_id: "cand-1",
          draft: null,
          blocked_reason: "the destination could not be read",
          workspace_owner: null,
          completed_actions: ["commit"],
          remaining_actions: ["push"],
          results: {},
          pr_url: null,
          legacy_publication: false,
          actions: [
            {
              id: 4,
              kind: "push",
              attempt: 1,
              phase: "needs_reconciliation",
              expected: {},
              error: "the response was lost",
              updated_at: "2026-08-20T00:00:00Z",
            },
          ],
          decisions: [],
          history: [],
        },
      },
    });

    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("outcome is unknown");
    expect(screen.queryByTestId("cleanup-ship-button")).not.toBeInTheDocument();
  });

  it("defers cleanup while the PR is open", async () => {
    await renderAt("/ship/1", { projects: [project], tasks: [prTask({ pr_state: "open" })] });

    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("Awaiting merge · cleanup deferred");
    expect(screen.queryByTestId("cleanup-ship-button")).not.toBeInTheDocument();
  });

  it("offers confirmed cleanup once the PR is merged", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [prTask({ pr_state: "merged", pr_merged_at: "2026-08-14T09:30:00Z" })],
    });

    await user.click(screen.getByTestId("cleanup-ship-button"));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("/home/op/tasks/maas/fix-bug"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/cleanup",
      expect.objectContaining({ method: "POST" }),
    );

    act(() => {
      socket().emit("task_updated", prTask({ state: "archived" }));
    });
    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("Cleaned up");
    expect(screen.queryByTestId("cleanup-ship-button")).not.toBeInTheDocument();
  });

  it("labels a closed-unmerged PR and still offers cleanup", async () => {
    await renderAt("/ship/1", { projects: [project], tasks: [prTask({ pr_state: "closed" })] });

    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("closed without merging");
    expect(screen.getByTestId("ship-step-cleanup")).toHaveTextContent("closed");
    expect(screen.getByTestId("cleanup-ship-button")).toBeInTheDocument();
  });

  it("declining the confirmation sends no request", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    await renderAt("/ship/1", { projects: [project], tasks: [prTask({ pr_state: "merged" })] });
    await user.click(screen.getByTestId("cleanup-ship-button"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("ProjectsView (projects-view capability)", () => {
  const llmvet = {
    name: "llmvet",
    title: "LLM-assisted patch review CLI",
    upstream_url: "git@github.com:bjornt/llmvet.git",
    fork_url: null,
    checkout_path: "/home/op/proj/llmvet",
    checkout_mode: "adopted" as const,
    fetch_remote: "origin",
    setup_state: "ready",
    setup_error: null,
    default_model_profile: null,
  };

  it("renders one card per project with fork-less annotation and active-task pills", async () => {
    const forked = { ...project, fork_url: "git@github.com:bjornt/maas.git" };
    const tasks = [
      makeTask({ id: 1, project_name: "maas" }),
      makeTask({ id: 2, project_name: "maas", slug: "other", state: "archived" }),
      makeTask({ id: 3, project_name: "llmvet", slug: "vet-it" }),
    ];
    await renderAt("/projects", { projects: [forked, llmvet], tasks });

    const maasCard = screen.getByTestId("project-card-maas");
    // fork set: fork row present, no own-upstream note
    expect(within(maasCard).getByText("fork")).toBeInTheDocument();
    expect(maasCard).toHaveTextContent("git@github.com:bjornt/maas.git");
    expect(maasCard).not.toHaveTextContent("you own upstream");
    // 1 live + 1 archived task => "1 active task"
    expect(screen.getByTestId("active-tasks-maas")).toHaveTextContent("1 active task");

    const llmvetCard = screen.getByTestId("project-card-llmvet");
    expect(llmvetCard).toHaveTextContent("you own upstream — no fork needed");
    expect(within(llmvetCard).queryByText("fork")).not.toBeInTheDocument();
    expect(screen.getByTestId("active-tasks-llmvet")).toHaveTextContent("1 active task");
  });

  it("shows an empty state when no projects exist", async () => {
    await renderAt("/projects", { projects: [], tasks: [] });
    expect(screen.getByTestId("projects-empty-state")).toBeInTheDocument();
  });

  async function fillCreateForm(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-name"), "llmvet");
    await user.type(screen.getByTestId("new-project-title"), "LLM-assisted patch review CLI");
    await user.type(screen.getByTestId("new-project-upstream"), "git@github.com:bjornt/llmvet.git");
  }

  it("shows the card as soon as the daemon answers, without waiting for the event", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ...llmvet }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await fillCreateForm(user);
    await user.click(screen.getByTestId("new-project-submit"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "llmvet",
          title: "LLM-assisted patch review CLI",
          upstream_url: "git@github.com:bjornt/llmvet.git",
          fork_url: null,
          // Adoption is the default mode; the empty path derives from the
          // effective checkout root (ADR-0022).
          checkout_mode: "adopt",
          // No profile is auto-selected; "No default" sends an explicit null.
          default_model_profile: null,
          checkout_path: null,
          fetch_remote: "origin",
        }),
      }),
    );

    // The response is reconciled into daemon state, so the card is present
    // without waiting for anything — no `project_created` frame has arrived yet.
    expect(screen.getByTestId("project-card-llmvet")).toBeInTheDocument();
    // The form closes as an effect of that card reaching daemon state, which
    // is one render later; the contract is that it never closes *before*.
    await waitFor(() =>
      expect(screen.queryByTestId("new-project-form")).not.toBeInTheDocument(),
    );

    // The delayed event for the same project must not duplicate the card.
    act(() => {
      socket().emit("project_created", llmvet);
    });
    expect(screen.getAllByTestId("project-card-llmvet")).toHaveLength(1);
  });

  it("locks the form while creating and reports the daemon's own answer", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    let release: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockReturnValue(
          pending.then(() => ({ ok: true, json: () => Promise.resolve({ ...llmvet }) })),
        ),
    );

    await fillCreateForm(user);
    await user.click(screen.getByTestId("new-project-submit"));

    // In flight: the form is still mounted, every field is locked, and the
    // button says so.
    expect(screen.getByTestId("new-project-form")).toBeInTheDocument();
    expect(screen.getByTestId("new-project-submit")).toHaveTextContent("Creating…");
    expect(screen.getByTestId("new-project-name")).toBeDisabled();
    expect(screen.getByTestId("new-project-title")).toBeDisabled();
    expect(screen.getByTestId("new-project-upstream")).toBeDisabled();
    expect(screen.getByTestId("new-project-fork")).toBeDisabled();

    await act(async () => {
      release(null);
      await pending;
    });
    expect(screen.getByTestId("project-card-llmvet")).toBeInTheDocument();
  });

  it("issues one request when the submit button is clicked twice", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    let release: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockReturnValue(
        pending.then(() => ({ ok: true, json: () => Promise.resolve({ ...llmvet }) })),
      );
    vi.stubGlobal("fetch", fetchMock);

    await fillCreateForm(user);
    const submit = screen.getByTestId("new-project-submit");
    await user.click(submit);
    await user.click(submit);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      release(null);
      await pending;
    });
  });

  it("reports a malformed create response and adds no card", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ title: "no name" }) }),
    );

    await fillCreateForm(user);
    await user.click(screen.getByTestId("new-project-submit"));

    expect(await screen.findByTestId("new-project-error")).toHaveTextContent(
      "unusable project record",
    );
    expect(screen.getByTestId("new-project-form")).toBeInTheDocument();
    // Input survives, and nothing was added to the list.
    expect(screen.getByTestId("new-project-name")).toHaveValue("llmvet");
    expect(screen.queryByTestId("project-card-llmvet")).not.toBeInTheDocument();
    expect(screen.getByTestId("new-project-submit")).toBeEnabled();
  });

  it("renames a project in place without leaving a second card", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const renamed = { ...project, name: "maas-ng" };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(renamed) }),
    );

    await user.click(
      within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }),
    );
    await user.clear(screen.getByTestId("edit-name-maas"));
    await user.type(screen.getByTestId("edit-name-maas"), "maas-ng");
    await user.click(screen.getByTestId("edit-save-maas"));

    expect(screen.getByTestId("project-card-maas-ng")).toBeInTheDocument();
    expect(screen.queryByTestId("project-card-maas")).not.toBeInTheDocument();

    act(() => {
      socket().emit("project_renamed", { old_name: "maas", project: renamed });
    });
    expect(screen.getAllByTestId("project-card-maas-ng")).toHaveLength(1);
  });

  it("removes a project immediately and stays removed when the event lands", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ deleted: "maas" }) }),
    );

    await user.click(
      within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }),
    );
    await user.click(screen.getByTestId("remove-project-maas"));

    expect(screen.queryByTestId("project-card-maas")).not.toBeInTheDocument();
    act(() => {
      socket().emit("project_deleted", { name: "maas" });
    });
    expect(screen.queryByTestId("project-card-maas")).not.toBeInTheDocument();
    expect(screen.getByTestId("projects-empty-state")).toBeInTheDocument();
  });

  it("keeps the form open with the daemon's detail on duplicate name", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () => Promise.resolve({ detail: "project 'maas' already exists" }),
      }),
    );

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-name"), "maas");
    await user.type(screen.getByTestId("new-project-title"), "dup");
    await user.type(screen.getByTestId("new-project-upstream"), "https://example.com/maas.git");
    await user.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByTestId("new-project-error")).toHaveTextContent(
      "project 'maas' already exists",
    );
    expect(screen.getByTestId("new-project-form")).toBeInTheDocument();
  });

  it("disables rename while tasks reference the project", async () => {
    await renderAt("/projects", { projects: [project], tasks: [makeTask()] });
    const user = userEvent.setup();

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    expect(screen.getByTestId("edit-name-maas")).toBeDisabled();
    expect(screen.getByTestId("rename-note-maas")).toHaveTextContent(
      "Referenced by 1 tasks — rename via",
    );
  });

  it("sends new_name when an unreferenced project is renamed", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const renamed = { ...project, name: "maas-ng" };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(renamed),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    const nameInput = screen.getByTestId("edit-name-maas");
    expect(nameInput).toBeEnabled();
    await user.clear(nameInput);
    await user.type(nameInput, "maas-ng");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // The workspace defaults are project fields again (ADR-0026), and the
    // edit panel shows every one of them — so it always states the intended
    // value rather than leaving any to be preserved by omission.
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/maas",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          title: project.title,
          upstream_url: project.upstream_url,
          fork_url: null,
          checkout_path: project.checkout_path,
          fetch_remote: project.fetch_remote,
          default_model_profile: project.default_model_profile,
          base_branch: project.base_branch,
          branch_pattern: project.branch_pattern,
          workshop_additions: project.workshop_additions,
          preamble: project.preamble,
          new_name: "maas-ng",
        }),
      }),
    );

    act(() => {
      socket().emit("project_renamed", { old_name: "maas", project: renamed });
    });
    expect(screen.getByTestId("project-card-maas-ng")).toBeInTheDocument();
    expect(screen.queryByTestId("project-card-maas")).not.toBeInTheDocument();
  });

  it("confirms removal and surfaces the daemon's 409 inline", async () => {
    await renderAt("/projects", { projects: [project], tasks: [makeTask()] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () =>
          Promise.resolve({
            detail: "project 'maas' has tasks referencing it: maas/fix-bug (created)",
          }),
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    await user.click(screen.getByTestId("remove-project-maas"));

    expect(await screen.findByTestId("edit-error-maas")).toHaveTextContent(
      "project 'maas' has tasks referencing it: maas/fix-bug (created)",
    );
    expect(screen.getByTestId("edit-panel-maas")).toBeInTheDocument();
  });

  it("deletes an unreferenced project after confirmation", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ deleted: "maas" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    await user.click(screen.getByTestId("remove-project-maas"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/maas",
      expect.objectContaining({ method: "DELETE" }),
    );
    act(() => {
      socket().emit("project_deleted", { name: "maas" });
    });
    expect(screen.queryByTestId("project-card-maas")).not.toBeInTheDocument();
  });

  it("declining the remove confirmation sends no request", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    await user.click(screen.getByTestId("remove-project-maas"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("active-tasks pill navigates to the filtered Tasks view", async () => {
    const tasks = [
      makeTask({ id: 1, project_name: "maas" }),
      makeTask({ id: 2, project_name: "llmvet", slug: "vet-it", clone_path: "/home/op/tasks/llmvet/vet-it" }),
    ];
    await renderAt("/projects", { projects: [project, llmvet], tasks });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("active-tasks-llmvet"));

    expect(window.location.pathname).toBe("/tasks");
    expect(window.location.search).toBe("?project=llmvet");
    expect(screen.getByTestId("project-filter-label")).toHaveTextContent("llmvet");
    expect(screen.getByTestId("task-link-2")).toBeInTheDocument();
    expect(screen.queryByTestId("task-link-1")).not.toBeInTheDocument();
  });
});

describe("SettingsView after the template retirement (ADR-0026)", () => {
  it("keeps model profiles and daemon controls, and offers no template CRUD", async () => {
    await renderAt("/settings", {
      projects: [project],
      model_profiles: [balanced],
      tasks: [],
    });

    expect(screen.getByTestId("model-profiles-panel")).toBeInTheDocument();
    expect(screen.getByTestId("notifications-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("templates-panel")).toBeNull();
    expect(screen.queryByTestId("new-template-toggle")).toBeNull();
    expect(screen.queryByTestId("template-editor")).toBeNull();
  });

  it("no longer says a profile is saved-only configuration", async () => {
    await renderAt("/settings", {
      projects: [project],
      model_profiles: [balanced],
      tasks: [],
    });

    const note = screen.getByTestId("model-profiles-boundary");
    // A profile governs launches now; what it must still not claim is that
    // editing it reaches a task already accepted.
    expect(note).not.toHaveTextContent("saved configuration only");
    expect(note).toHaveTextContent("Tasks already accepted keep the bindings");
  });
});

describe("SpawnView file mentions", () => {
  /** Only the mention popup's options — a `<select>`'s options share the role. */
  function suggestedPaths(): string[] {
    const popup = screen.queryByTestId("mention-popup");
    if (popup === null) return [];
    return within(popup)
      .queryAllByRole("option")
      .map((option) => option.textContent ?? "");
  }

  function stubFileSearch(
    responder: (query: string) => { paths: string[]; truncated?: boolean } | "error",
  ) {
    const fetchMock = vi.fn((url: unknown) => {
      const href = String(url);
      if (href.includes("/files")) {
        const query = new URL(href, "http://localhost").searchParams.get("q") ?? "";
        const answer = responder(query);
        if (answer === "error") {
          return Promise.resolve({
            ok: false,
            status: 409,
            json: () => Promise.resolve({ detail: "checkout path is not a git repository" }),
          });
        }
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({ paths: answer.paths, truncated: answer.truncated ?? false }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  async function spawnViewWithFiles(
    responder: (query: string) => { paths: string[]; truncated?: boolean } | "error",
    projects = [project],
  ) {
    await renderAt("/spawn", {
      projects,
      model_profiles: [balanced],
      workflow_catalog: [singleStep],
      tasks: [],
    });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Project"), projects[0].name);
    const fetchMock = stubFileSearch(responder);
    return { user, fetchMock, prompt: screen.getByLabelText("Prompt") };
  }

  it("opens suggestions when @ is typed and queries the selected project", async () => {
    const { user, fetchMock, prompt } = await spawnViewWithFiles(() => ({
      paths: ["src/lib/token.ts"],
    }));

    await user.type(prompt, "read @tok");

    expect(await screen.findByRole("option", { name: "src/lib/token.ts" })).toBeInTheDocument();
    const requested = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(requested.some((url) => url.startsWith("/api/projects/maas/files?"))).toBe(true);
  });

  it("narrows the list as the query grows", async () => {
    const { user, prompt } = await spawnViewWithFiles((query) => ({
      paths: query === "to" ? ["a/token.ts", "b/tomato.ts"] : ["a/token.ts"],
    }));

    await user.type(prompt, "@to");
    await screen.findByTestId("mention-popup");
    await expect.poll(() => suggestedPaths()).toEqual(["a/token.ts", "b/tomato.ts"]);

    await user.type(prompt, "k");
    await expect.poll(() => suggestedPaths()).toEqual(["a/token.ts"]);
  });

  it("does not open on an @ inside a word, so an email address is left alone", async () => {
    const { user, fetchMock, prompt } = await spawnViewWithFiles(() => ({ paths: ["a.ts"] }));

    await user.type(prompt, "ask someone@example.com");

    expect(screen.queryByTestId("mention-popup")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.map((call) => String(call[0]))).toEqual([]);
  });

  it("selects with the keyboard and inserts the literal mention", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({
      paths: ["a/first.ts", "b/second.ts"],
    }));

    await user.type(prompt, "read @s");
    await screen.findByRole("option", { name: "a/first.ts" });
    await user.keyboard("{ArrowDown}{Enter}");

    expect(prompt).toHaveValue("read @b/second.ts ");
    expect(screen.queryByTestId("mention-popup")).not.toBeInTheDocument();
  });

  it("selects with Tab as well as Enter", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({ paths: ["a/first.ts"] }));

    await user.type(prompt, "@f");
    await screen.findByRole("option", { name: "a/first.ts" });
    await user.keyboard("{Tab}");

    expect(prompt).toHaveValue("@a/first.ts ");
  });

  it("selects with the pointer", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({
      paths: ["a/first.ts", "b/second.ts"],
    }));

    await user.type(prompt, "@s");
    await user.click(await screen.findByRole("option", { name: "b/second.ts" }));

    expect(prompt).toHaveValue("@b/second.ts ");
  });

  it("inserts at the caret in the middle of the prompt, not at the end", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({ paths: ["a/token.ts"] }));

    await user.type(prompt, "read  and then stop");
    // Put the caret after "read " and open a mention there.
    await user.click(prompt);
    (prompt as HTMLTextAreaElement).setSelectionRange(5, 5);
    await user.keyboard("@tok");
    await user.click(await screen.findByRole("option", { name: "a/token.ts" }));

    expect(prompt).toHaveValue("read @a/token.ts and then stop");
  });

  it("supports several mentions in one prompt", async () => {
    const { user, prompt } = await spawnViewWithFiles((query) => ({
      paths: query.startsWith("f") ? ["a/first.ts"] : ["b/second.ts"],
    }));

    await user.type(prompt, "@f");
    await user.click(await screen.findByRole("option", { name: "a/first.ts" }));
    await user.type(prompt, "and @s");
    await user.click(await screen.findByRole("option", { name: "b/second.ts" }));

    expect(prompt).toHaveValue("@a/first.ts and @b/second.ts ");
  });

  it("Escape closes the list and leaves the typed text exactly as written", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({ paths: ["a/token.ts"] }));

    await user.type(prompt, "read @tok");
    await screen.findByRole("option", { name: "a/token.ts" });
    await user.keyboard("{Escape}");

    expect(screen.queryByTestId("mention-popup")).not.toBeInTheDocument();
    expect(prompt).toHaveValue("read @tok");

    // Dismissal sticks: the next keystroke must not reopen the same mention.
    await user.type(prompt, "en");
    expect(screen.queryByTestId("mention-popup")).not.toBeInTheDocument();

    // Moving off it and starting another mention opens the list again.
    await user.type(prompt, " @tok");
    expect(await screen.findByRole("option", { name: "a/token.ts" })).toBeInTheDocument();
  });

  it("says so when nothing matches, without touching the prompt", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({ paths: [] }));

    await user.type(prompt, "@zzz");

    expect(await screen.findByText("No matching files")).toBeInTheDocument();
    expect(prompt).toHaveValue("@zzz");
  });

  it("shows the daemon's reason when the search fails and keeps the field usable", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => "error");

    await user.type(prompt, "@a");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "checkout path is not a git repository",
    );
    await user.type(prompt, "bc");
    expect(prompt).toHaveValue("@abc");
  });

  it("closes the list when the project changes without rewriting the prompt", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({ paths: ["a/token.ts"] }), [
      project,
      { ...project, name: "other", title: "Other" },
    ]);

    await user.type(prompt, "read @tok");
    await screen.findByRole("option", { name: "a/token.ts" });

    await user.selectOptions(screen.getByLabelText("Project"), "other");

    expect(screen.queryByTestId("mention-popup")).not.toBeInTheDocument();
    expect(prompt).toHaveValue("read @tok");
  });

  it("announces the suggestion count for screen readers", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({
      paths: ["a/first.ts", "b/second.ts"],
    }));

    await user.type(prompt, "@s");

    await expect
      .poll(() => screen.getByRole("status").textContent)
      .toBe("2 file suggestions");
  });

  it("marks the active option for assistive technology", async () => {
    const { user, prompt } = await spawnViewWithFiles(() => ({
      paths: ["a/first.ts", "b/second.ts"],
    }));

    await user.type(prompt, "@s");
    await screen.findByRole("option", { name: "a/first.ts" });

    expect(prompt).toHaveAttribute("aria-expanded", "true");
    const activeId = prompt.getAttribute("aria-activedescendant");
    expect(screen.getByRole("option", { name: "a/first.ts" })).toHaveAttribute("id", activeId);

    await user.keyboard("{ArrowDown}");
    await expect
      .poll(() => prompt.getAttribute("aria-activedescendant"))
      .toBe(screen.getByRole("option", { name: "b/second.ts" }).id);
  });

  it("is inert while the form is locked by a submission", async () => {
    const spawned = makeTask({ spawn_completed_at: null });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: { method?: string }) => {
        if (typeof url === "string" && url.endsWith("/api/tasks/preview")) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(previewResponse()) });
        }
        if (url === "/api/tasks" && init?.method === "POST") {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(spawned) });
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ paths: ["a/token.ts"], truncated: false }),
        });
      }),
    );
    await renderAt("/spawn", {
      projects: [project],
      model_profiles: [balanced],
      workflow_catalog: [singleStep],
      tasks: [],
    });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Workflow"), "single-step");
    await user.selectOptions(screen.getByLabelText("Project"), "maas");
    const prompt = screen.getByLabelText("Prompt");
    await user.type(prompt, "fix it");
    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await screen.findByTestId("step-preview");

    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    expect(prompt).toBeDisabled();
    expect(screen.queryByTestId("mention-popup")).not.toBeInTheDocument();
  });
});

describe("project checkout onboarding (ADR-0022)", () => {
  const adopted = {
    name: "maas",
    title: "MAAS",
    upstream_url: "https://example.com/maas.git",
    fork_url: null,
    checkout_path: "/home/op/proj/maas",
    checkout_mode: "adopted" as const,
    fetch_remote: "origin",
    setup_state: "ready" as const,
    setup_error: null,
  };
  const cloning = {
    ...adopted,
    name: "fresh",
    checkout_path: "/home/op/proj/fresh",
    checkout_mode: "cloned" as const,
    setup_state: "cloning" as const,
  };
  const failed = {
    ...cloning,
    setup_state: "failed" as const,
    setup_error: "step 'clone' failed:\nfatal: repository not found",
  };

  it("shows an adopted project's checkout, mode, and fetch remote", async () => {
    await renderAt("/projects", { projects: [adopted], tasks: [] });

    const meta = screen.getByTestId("checkout-path-maas");
    expect(meta).toHaveTextContent("/home/op/proj/maas");
    expect(meta).toHaveTextContent("your checkout");
    expect(meta).toHaveTextContent("fetches origin");
    expect(screen.getByTestId("setup-state-maas")).toHaveTextContent("ready");
  });

  it("renders live clone progress and then the ready card, from state alone", async () => {
    await renderAt("/projects", { projects: [cloning], tasks: [] });

    expect(screen.getByTestId("setup-state-fresh")).toHaveTextContent("cloning");
    act(() => {
      socket().emit("project_setup_step", {
        project: "fresh",
        step: "clone",
        status: "started",
      });
    });
    expect(screen.getByTestId("setup-fresh")).toHaveTextContent("Cloning — clone…");

    act(() => {
      socket().emit("project_updated", { ...cloning, setup_state: "ready" });
    });
    expect(screen.getByTestId("setup-state-fresh")).toHaveTextContent("ready");
    expect(screen.queryByTestId("setup-fresh")).not.toBeInTheDocument();
  });

  it("renders a cloning project correctly on reconnect with no step events", async () => {
    await renderAt("/projects", { projects: [cloning], tasks: [] });

    // No `project_setup_step` has ever arrived; the durable row is enough.
    expect(screen.getByTestId("setup-fresh")).toHaveTextContent("Cloning…");
  });

  it("shows a failed clone's stderr and offers a retry", async () => {
    await renderAt("/projects", { projects: [failed], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ...failed, setup_state: "cloning", setup_error: null }),
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(screen.getByTestId("setup-error-fresh")).toHaveTextContent(
      "fatal: repository not found",
    );

    await user.click(screen.getByTestId("retry-setup-fresh"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/fresh/setup/retry",
      expect.objectContaining({ method: "POST" }),
    );
    // The daemon's own answer is reconciled, so the card moves without an event.
    expect(screen.getByTestId("setup-state-fresh")).toHaveTextContent("cloning");
  });

  it("offers removal from the failed card, and says the checkout survives", async () => {
    await renderAt("/projects", { projects: [failed], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ deleted: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.click(screen.getByTestId("remove-failed-fresh"));

    expect(confirm.mock.calls[0][0]).toContain("/home/op/proj/fresh");
    expect(confirm.mock.calls[0][0]).toContain("stays on disk");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/fresh",
      expect.objectContaining({ method: "DELETE" }),
    );
    await waitFor(() =>
      expect(screen.queryByTestId("project-card-fresh")).not.toBeInTheDocument(),
    );
  });

  it("backing out of the removal confirmation changes nothing", async () => {
    await renderAt("/projects", { projects: [failed], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    await user.click(screen.getByTestId("remove-failed-fresh"));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("project-card-fresh")).toBeInTheDocument();
  });

  it("sends clone mode with no checkout path and previews the destination", async () => {
    await renderAt("/projects", {
      projects: [],
      tasks: [],
      settings: { checkout_root: "/home/op/src" },
    });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(cloning),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-name"), "fresh");
    await user.type(screen.getByTestId("new-project-title"), "Fresh");
    await user.type(screen.getByTestId("new-project-upstream"), "https://example.com/fresh.git");
    await user.click(screen.getByTestId("new-project-mode-clone"));

    expect(screen.getByTestId("new-project-clone-preview")).toHaveTextContent(
      "/home/op/src/fresh",
    );

    await user.click(screen.getByTestId("new-project-submit"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "fresh",
          title: "Fresh",
          upstream_url: "https://example.com/fresh.git",
          fork_url: null,
          checkout_mode: "clone",
          default_model_profile: null,
        }),
      }),
    );
  });

  it("offers the detected remotes for confirmation without applying them silently", async () => {
    await renderAt("/projects", { projects: [], tasks: [] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            ok: true,
            reason: "",
            detail: "",
            remotes: [
              { name: "origin", url: "git@github.com:me/repo.git" },
              { name: "upstream", url: "https://github.com/org/repo.git" },
            ],
            suggested_upstream: "https://github.com/org/repo.git",
            suggested_fork: "git@github.com:me/repo.git",
          }),
      }),
    );

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-checkout-path"), "/home/op/proj/repo");
    await user.tab();

    expect(await screen.findByTestId("new-project-inspection")).toHaveTextContent(
      "remotes: origin, upstream",
    );
    expect(screen.getByTestId("new-project-upstream")).toHaveValue(
      "https://github.com/org/repo.git",
    );
    expect(screen.getByTestId("new-project-fork")).toHaveValue("git@github.com:me/repo.git");
  });

  it("explains a rejected checkout inline while the operator is typing", async () => {
    await renderAt("/projects", { projects: [], tasks: [] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            ok: false,
            reason: "missing",
            detail: "checkout path does not exist: /nope",
            remotes: [],
            suggested_upstream: null,
            suggested_fork: null,
          }),
      }),
    );

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-checkout-path"), "/nope");
    await user.tab();

    expect(await screen.findByTestId("new-project-inspection")).toHaveTextContent(
      "does not exist",
    );
    expect(screen.getByTestId("new-project-upstream")).toHaveValue("");
  });

  it("locks a cloned project's checkout path in the edit panel", async () => {
    await renderAt("/projects", {
      projects: [{ ...cloning, setup_state: "ready" }],
      tasks: [],
    });
    const user = userEvent.setup();

    await user.click(screen.getAllByRole("button", { name: "Edit" })[0]);

    expect(screen.getByTestId("edit-checkout-fresh")).toBeDisabled();
    expect(screen.getByTestId("checkout-note-fresh")).toBeInTheDocument();
  });

  it("refuses to spawn against a project whose checkout is not ready", async () => {
    await renderAt("/spawn", {
      projects: [cloning],
      model_profiles: [balanced],
      workflow_catalog: [singleStep],
      tasks: [],
    });
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText("Project"), "fresh");

    expect(screen.getByTestId("project-not-ready")).toHaveTextContent("cloning");
    expect(screen.getByRole("button", { name: "Spawn task" })).toBeDisabled();
  });
});

describe("project create form mode switching (ADR-0022)", () => {
  it("drops a stale checkout inspection when the mode changes", async () => {
    await renderAt("/projects", { projects: [], tasks: [] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            ok: false,
            reason: "missing",
            detail: "checkout path does not exist: /nope",
            remotes: [],
            suggested_upstream: null,
            suggested_fork: null,
          }),
      }),
    );

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-checkout-path"), "/nope");
    await user.tab();
    expect(await screen.findByTestId("new-project-inspection")).toBeInTheDocument();

    // Clone mode does not use that path, so the message must not survive.
    await user.click(screen.getByTestId("new-project-mode-clone"));

    expect(screen.queryByTestId("new-project-inspection")).not.toBeInTheDocument();
  });
});

describe("project default model profile (ADR-0025)", () => {
  const balanced = {
    name: "balanced",
    roles: {
      default: { model: "anthropic/claude-sonnet-4.5", thinking: "medium" },
      smol: { model: "openai/gpt-4.1-mini", thinking: "off" },
      slow: { model: "openai/o3", thinking: "high" },
      plan: { model: "google/gemini-2.5-pro", thinking: "max" },
    },
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  };
  const thorough = { ...balanced, name: "thorough" };

  it("shows the chosen profile on the card, or that none is configured", async () => {
    const assigned = { ...project, name: "assigned", default_model_profile: "balanced" };
    const bare = { ...project, name: "bare", default_model_profile: null };
    await renderAt("/projects", {
      projects: [bare, assigned],
      tasks: [],
      model_profiles: [balanced],
    });

    expect(screen.getByTestId("default-profile-assigned")).toHaveTextContent("balanced");
    expect(screen.getByTestId("default-profile-bare")).toHaveTextContent(
      "no default configured",
    );
    // The card states what the default actually does: it is inherited, and a
    // task may replace it (ADR-0026).
    expect(screen.getByTestId("default-profile-assigned")).toHaveTextContent(
      /inherited by a launch unless the task selects another/,
    );
  });

  it("never auto-selects a profile, and registers fine when none exist", async () => {
    await renderAt("/projects", { projects: [], tasks: [], model_profiles: [] });
    const user = userEvent.setup();
    const created = { ...project, name: "fresh" };
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(created) });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(screen.getByTestId("new-project-toggle"));
    // No profiles saved: the selector still offers "No default" only, and
    // points at where to make one.
    expect(screen.getByTestId("new-project-default-profile")).toHaveValue("");
    expect(
      screen.getByRole("link", { name: /Create one in Settings/ }),
    ).toBeInTheDocument();

    await user.type(screen.getByTestId("new-project-name"), "fresh");
    await user.type(screen.getByTestId("new-project-title"), "Fresh");
    await user.type(screen.getByTestId("new-project-upstream"), "https://example.com/f.git");
    await user.click(screen.getByTestId("new-project-submit"));

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).default_model_profile).toBeNull();
  });

  it("sends the selected profile on registration", async () => {
    await renderAt("/projects", { projects: [], tasks: [], model_profiles: [balanced] });
    const user = userEvent.setup();
    const created = { ...project, name: "fresh", default_model_profile: "balanced" };
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(created) });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-name"), "fresh");
    await user.type(screen.getByTestId("new-project-title"), "Fresh");
    await user.type(screen.getByTestId("new-project-upstream"), "https://example.com/f.git");
    await user.selectOptions(screen.getByTestId("new-project-default-profile"), "balanced");
    await user.click(screen.getByTestId("new-project-submit"));

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).default_model_profile).toBe("balanced");
  });

  it("reassigns and clears one project's default without touching another", async () => {
    const alpha = { ...project, name: "alpha", default_model_profile: "balanced" };
    const beta = { ...project, name: "beta", default_model_profile: "balanced" };
    await renderAt("/projects", {
      projects: [alpha, beta],
      tasks: [],
      model_profiles: [balanced, thorough],
    });
    const user = userEvent.setup();
    const saved = { ...alpha, default_model_profile: "thorough" };
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(saved) });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(
      within(screen.getByTestId("project-card-alpha")).getByRole("button", { name: "Edit" }),
    );
    const select = screen.getByTestId("edit-default-profile-alpha");
    expect(select).toHaveValue("balanced");
    await user.selectOptions(select, "thorough");
    await user.click(screen.getByTestId("edit-save-alpha"));

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).default_model_profile).toBe("thorough");
    await waitFor(() =>
      expect(screen.getByTestId("default-profile-alpha")).toHaveTextContent("thorough"),
    );
    expect(screen.getByTestId("default-profile-beta")).toHaveTextContent("balanced");
  });

  it("keeps a profile deleted since selection visible as unavailable", async () => {
    const alpha = { ...project, name: "alpha", default_model_profile: "balanced" };
    await renderAt("/projects", {
      projects: [alpha],
      tasks: [],
      model_profiles: [balanced, thorough],
    });
    const user = userEvent.setup();

    await user.click(
      within(screen.getByTestId("project-card-alpha")).getByRole("button", { name: "Edit" }),
    );
    act(() => {
      socket().emit("model_profile_deleted", { name: "balanced" });
    });

    // The stale selection is not silently swapped for another profile or for
    // "No default"; the operator has to correct it.
    expect(screen.getByTestId("edit-default-profile-alpha")).toHaveValue("balanced");
    expect(
      screen.getByTestId("edit-default-profile-alpha-unavailable"),
    ).toHaveTextContent("balanced has been removed");
  });
});

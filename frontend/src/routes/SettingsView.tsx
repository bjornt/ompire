import { useEffect, useRef, useState } from "react";
import {
  deleteSetting,
  getDaemonInfo,
  getSettings,
  getToken,
  recheckGitHub,
  recheckGpg,
  rotateToken,
  updateSettings,
} from "../lib/api";
import { setDaemonToken } from "../lib/token";
import { useDaemonState } from "../lib/useDaemonState";
import { githubIdentityPresentation, safeGitHubDetail } from "../lib/githubPresentation";
import { gpgPresentation, selectionSourceLabel } from "../lib/gpgPresentation";
import { ModelProfilesPanel } from "./ModelProfilesPanel";
import type { AttentionTier, DaemonInfo, DaemonSettings } from "../types";
import "./SettingsView.css";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

type Provenance = Record<string, "default" | "config" | "override">;

const RENOTIFY_OPTIONS: { label: string; value: number }[] = [
  { label: "3 minutes", value: 180 },
  { label: "5 minutes", value: 300 },
  { label: "10 minutes", value: 600 },
  { label: "Never", value: 0 },
];

const TIER_ROWS: { tier: AttentionTier; hint: string }[] = [
  { tier: "interrupt", hint: "needs you now" },
  { tier: "notify", hint: "needs you soon" },
  { tier: "badge", hint: "counts in the tab" },
  { tier: "silent", hint: "no alert" },
];

const TIER_KIND_LABELS: Record<string, string> = {
  desktop: "Desktop",
  sound: "Sound",
  badge: "Tab badge",
};

function capitalize(s: string): string {
  return (s[0]?.toUpperCase() ?? "") + s.slice(1);
}

function OverrideTag({
  provenance,
  settingKey,
  testId,
}: {
  provenance: Provenance;
  settingKey: string;
  testId: string;
}) {
  if (provenance[settingKey] !== "override") return null;
  return (
    <span className="overrideTag" data-testid={`override-${testId}`}>
      override
    </span>
  );
}

function TierMatrix({
  settings,
  provenance,
  onChange,
}: {
  settings: DaemonSettings;
  provenance: Provenance;
  onChange: (key: string, value: boolean) => void;
}) {
  return (
    <div className="tierMatrix" data-testid="tier-matrix">
      {TIER_ROWS.map(({ tier, hint }) => (
        <div key={tier} className="tierRow" data-testid={`tier-row-${tier}`}>
          <div className="tierRowMeta">
            <span
              className={`tierPill tierPill${capitalize(tier)}`}
              data-testid={`tier-pill-${tier}`}
            >
              {tier}
            </span>
            <span className="tierHint">{hint}</span>
          </div>
          <div className="tierChecks">
            {(["desktop", "sound", "badge"] as const).map((kind) => {
              const key = `tier.${tier}.${kind}`;
              const checked = Boolean(settings[key]);
              const testId = `tier-${tier}-${kind}`;
              return (
                <label key={kind} className="checkButton" data-testid={`check-button-${testId}`}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => onChange(key, !checked)}
                    data-testid={testId}
                  />
                  <span>{TIER_KIND_LABELS[kind]}</span>
                  <OverrideTag provenance={provenance} settingKey={key} testId={testId} />
                </label>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function WatchdogInputs({
  settings,
  provenance,
  onChange,
}: {
  settings: DaemonSettings;
  provenance: Provenance;
  onChange: (key: string, value: number) => void;
}) {
  const stallSetting = typeof settings.stall_threshold === "number" ? settings.stall_threshold : 300;
  const contextSetting =
    typeof settings.context_advisory_threshold === "number"
      ? settings.context_advisory_threshold
      : 80;

  const [stall, setStall] = useState(String(stallSetting));
  const [context, setContext] = useState(String(contextSetting));

  useEffect(() => {
    setStall(String(stallSetting));
  }, [stallSetting]);

  useEffect(() => {
    setContext(String(contextSetting));
  }, [contextSetting]);

  function commitStall() {
    const n = Number.parseInt(stall, 10);
    if (!Number.isNaN(n) && n >= 0) onChange("stall_threshold", n);
  }

  function commitContext() {
    const n = Number.parseInt(context, 10);
    if (!Number.isNaN(n) && n >= 0 && n <= 100) onChange("context_advisory_threshold", n);
  }

  return (
    <div className="watchdogGrid">
      <label className="formField">
        <span className="fieldLabel">
          Stall threshold{" "}
          <span className="fieldHint">seconds before a session is considered stalled</span>
          <OverrideTag provenance={provenance} settingKey="stall_threshold" testId="stall-threshold" />
        </span>
        <input
          type="number"
          className="mono"
          min={0}
          value={stall}
          onChange={(e) => setStall(e.target.value)}
          onBlur={commitStall}
          data-testid="stall-threshold"
        />
      </label>

      <label className="formField">
        <span className="fieldLabel">
          Context advisory threshold{" "}
          <span className="fieldHint">percent at which to surface context load</span>
          <OverrideTag
            provenance={provenance}
            settingKey="context_advisory_threshold"
            testId="context-advisory-threshold"
          />
        </span>
        <input
          type="number"
          className="mono"
          min={0}
          max={100}
          value={context}
          onChange={(e) => setContext(e.target.value)}
          onBlur={commitContext}
          data-testid="context-advisory-threshold"
        />
      </label>
    </div>
  );
}

function CheckoutRootPanel({
  settings,
  provenance,
  onCommit,
  onReset,
}: {
  settings: DaemonSettings;
  provenance: Provenance;
  onCommit: (value: string) => Promise<string | null>;
  onReset: () => Promise<string | null>;
}) {
  const stored = typeof settings.checkout_root === "string" ? settings.checkout_root : "";
  const [value, setValue] = useState(stored);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValue(stored);
    setError(null);
  }, [stored]);

  const overridden = provenance.checkout_root === "override";

  return (
    <label className="formField">
      <span className="fieldLabel">
        Checkout root{" "}
        <span className="fieldHint">where "clone it for me" puts a new checkout</span>
        <OverrideTag provenance={provenance} settingKey="checkout_root" testId="checkout-root" />
      </span>
      <input
        className="mono"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={async () => {
          const next = value.trim();
          if (next === stored || next === "") return;
          setError(await onCommit(next));
        }}
        data-testid="checkout-root"
      />
      <span className="fieldHint">
        Applies to the next project you clone. Existing checkouts are never moved.
      </span>
      {overridden && (
        <button
          type="button"
          className="ghostButton"
          onClick={async () => setError(await onReset())}
          data-testid="checkout-root-reset"
        >
          Reset to default
        </button>
      )}
      {error && (
        <span className="submitError" role="alert" data-testid="checkout-root-error">
          {error}
        </span>
      )}
    </label>
  );
}

function maskToken(token: string): string {
  if (token.length <= 8) return token;
  const prefix = token.startsWith("ompire_tok_") ? "ompire_tok_" : "";
  const secret = prefix ? token.slice(prefix.length) : token;
  const first = secret.slice(0, 4);
  const last = secret.slice(-4);
  const filler = "•".repeat(Math.max(secret.length - 8, 4));
  return `${prefix}${first}${filler}${last}`;
}

function CommitSigningPanel() {
  const { gpg } = useDaemonState();
  const [rechecking, setRechecking] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const signing = gpgPresentation(gpg);
  const selected = gpg?.selected ?? null;
  const candidates = gpg?.candidates ?? [];
  const source = selectionSourceLabel(gpg);
  const detail = error ?? gpg?.detail ?? null;

  async function guarded(work: () => Promise<void>, flag: (v: boolean) => void) {
    if (busy.current) return;
    busy.current = true;
    flag(true);
    setError(null);
    try {
      await work();
    } catch (err: unknown) {
      setError(errorText(err));
    } finally {
      busy.current = false;
      flag(false);
    }
  }

  // Selecting re-probes on the daemon side, so the shared chip and Ship flow
  // follow from the broadcast rather than from local state here.
  const onSelect = (value: string) =>
    void guarded(
      () =>
        value
          ? updateSettings({ gpg_signing_key: value }).then(() => undefined)
          : deleteSetting("gpg_signing_key").then(() => undefined),
      setSelecting,
    );

  return (
    <div className="daemonGithub" data-testid="daemon-signing-panel">
      <h3 className="daemonGithubTitle">Commit signing</h3>
      <div
        className="daemonGithubStatus"
        role="status"
        aria-live="polite"
        aria-label={signing.description}
        data-testid="daemon-gpg-state"
      >
        <span className="dot" style={{ background: signing.dot }} />
        {signing.label}
      </div>

      {candidates.length > 0 && (
        <label className="daemonInfoRow" htmlFor="gpg-signing-key">
          <span>Signing key</span>
          <select
            id="gpg-signing-key"
            value={selected?.fingerprint ?? ""}
            disabled={selecting}
            onChange={(e) => onSelect(e.target.value)}
            data-testid="gpg-key-select"
          >
            <option value="">Detect automatically</option>
            {candidates.map((candidate) => (
              <option key={candidate.fingerprint} value={candidate.fingerprint}>
                {candidate.uid ?? candidate.key_id} · {candidate.key_id}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="daemonInfoGrid">
        {selected && (
          <div className="daemonInfoRow" data-testid="daemon-gpg-fingerprint">
            <span>Fingerprint</span>
            <code>{selected.fingerprint}</code>
          </div>
        )}
        {selected?.uid && (
          <div className="daemonInfoRow" data-testid="daemon-gpg-uid">
            <span>User ID</span>
            <code>{selected.uid}</code>
          </div>
        )}
        {source && (
          <div className="daemonInfoRow" data-testid="daemon-gpg-source">
            <span>Chosen by</span>
            <code>{source}</code>
          </div>
        )}
        <div className="daemonInfoRow" data-testid="daemon-gpg-checked-at">
          <span>Last checked</span>
          <code>{gpg?.checked_at ?? "Not checked yet"}</code>
        </div>
      </div>

      {/* The ambiguous recovery is "choose a key in Settings" — pointless
          here, where the selector above already is the action. Every other
          state's recovery happens elsewhere, so it still belongs. */}
      {signing.recovery && gpg?.state !== "ambiguous" && (
        <p className="daemonGithubDetail" data-testid="daemon-gpg-recovery">
          {signing.recovery}
        </p>
      )}
      {signing.command && (
        <code data-testid="daemon-gpg-command">{signing.command}</code>
      )}
      {detail && (
        <p className="daemonGithubDetail" role="alert" data-testid="daemon-gpg-detail">
          {detail}
        </p>
      )}

      <button
        type="button"
        className="ghostButton"
        disabled={rechecking}
        onClick={() =>
          void guarded(() => recheckGpg().then(() => undefined), setRechecking)
        }
        data-testid="recheck-gpg-button"
      >
        {rechecking ? "Checking key…" : "Re-check key"}
      </button>
    </div>
  );
}


function DaemonPanel() {
  const { gh } = useDaemonState();
  const [info, setInfo] = useState<DaemonInfo | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [recheckingGitHub, setRecheckingGitHub] = useState(false);
  const [gitHubRecheckError, setGitHubRecheckError] = useState<string | null>(null);
  const gitHubRecheckLock = useRef(false);
  const githubChip = githubIdentityPresentation(gh);
  const identity = gh?.identity;
  const githubDetail = safeGitHubDetail(identity?.detail) ?? gitHubRecheckError;

  useEffect(() => {
    getDaemonInfo().then((i) => setInfo(i)).catch(() => {});
    getToken().then((res) => setToken(res.token)).catch(() => {});
  }, []);

  async function copyToken() {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
    } catch {
      // Ignore missing clipboard permission.
    }
  }

  async function rotate() {
    if (
      !window.confirm(
        "Rotating the daemon token will invalidate the old token immediately. Any other client using it will be disconnected. Continue?",
      )
    ) {
      return;
    }
    const res = await rotateToken();
    setDaemonToken(res.token);
    setToken(res.token);
  }

  async function recheckGithub() {
    if (gitHubRecheckLock.current) return;
    gitHubRecheckLock.current = true;
    setRecheckingGitHub(true);
    setGitHubRecheckError(null);
    try {
      await recheckGitHub();
    } catch (error: unknown) {
      setGitHubRecheckError(safeGitHubDetail(errorText(error)));
    } finally {
      gitHubRecheckLock.current = false;
      setRecheckingGitHub(false);
    }
  }

  return (
    <>
      {info && (
        <div className="daemonInfoGrid">
          <div className="daemonInfoRow" data-testid="daemon-info-bind">
            <span>Bind</span>
            <code>{info.bind}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-port">
            <span>Port</span>
            <code>{info.port}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-version">
            <span>Version</span>
            <code>{info.version}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-config-path">
            <span>Config path</span>
            <code>{info.config_path}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-data-dir">
            <span>Data dir</span>
            <code>{info.data_dir}</code>
          </div>
          {info.audit_log_path !== null && info.audit_log_path !== undefined && (
            <div className="daemonInfoRow" data-testid="daemon-info-audit-log-path">
              <span>Audit log</span>
              <code>{info.audit_log_path}</code>
            </div>
          )}
        </div>
      )}

      <div className="daemonGithub" data-testid="daemon-github-panel">
        <h3 className="daemonGithubTitle">GitHub CLI</h3>
        <div
          className="daemonGithubStatus"
          role="status"
          aria-live="polite"
          aria-label={githubChip.description}
          data-testid="daemon-gh-state"
        >
          <span className="dot" style={{ background: githubChip.dot }} />
          {githubChip.label}
        </div>
        <div className="daemonInfoGrid">
          {identity?.login && (
            <div className="daemonInfoRow" data-testid="daemon-gh-login">
              <span>Account</span>
              <code>@{identity.login}</code>
            </div>
          )}
          <div className="daemonInfoRow" data-testid="daemon-gh-host">
            <span>Host</span>
            <code>{identity?.host ?? "Not checked yet"}</code>
          </div>
          {identity?.credential_source && (
            <div className="daemonInfoRow" data-testid="daemon-gh-source">
              <span>Credential source</span>
              <code>{identity.credential_source}</code>
            </div>
          )}
          {identity?.executable_path && (
            <div className="daemonInfoRow" data-testid="daemon-gh-executable">
              <span>Executable</span>
              <code>{identity.executable_path}</code>
            </div>
          )}
          {identity?.version && (
            <div className="daemonInfoRow" data-testid="daemon-gh-version">
              <span>Version</span>
              <code>{identity.version}</code>
            </div>
          )}
          <div className="daemonInfoRow" data-testid="daemon-gh-checked-at">
            <span>Last checked</span>
            <code>{identity?.checked_at ?? "Not checked yet"}</code>
          </div>
        </div>
        {githubDetail && (
          <p className="daemonGithubDetail" role="alert" data-testid="daemon-gh-detail">
            {githubDetail}
          </p>
        )}
        <button
          type="button"
          className="ghostButton"
          disabled={recheckingGitHub}
          onClick={() => void recheckGithub()}
          data-testid="recheck-github-button"
        >
          {recheckingGitHub ? "Checking GitHub…" : "Re-check GitHub"}
        </button>
      </div>

      <CommitSigningPanel />

      <div className="tokenRow">
        <code className="tokenValue" data-testid="daemon-token">
          {token ? maskToken(token) : "••••"}
        </code>
        <div className="tokenActions">
          <button
            type="button"
            className="ghostButton"
            onClick={() => void copyToken()}
            data-testid="copy-daemon-token"
          >
            Copy
          </button>
          <button
            type="button"
            className="ghostButton"
            onClick={() => void rotate()}
            data-testid="rotate-daemon-token"
          >
            Rotate
          </button>
        </div>
      </div>
    </>
  );
}

export function SettingsView() {
  const { settings } = useDaemonState();
  const [provenance, setProvenance] = useState<Provenance>({});

  useEffect(() => {
    getSettings()
      .then((res) => setProvenance(res.provenance ?? {}))
      .catch(() => {});
  }, []);

  async function putSetting(key: string, value: boolean | number) {
    try {
      const res = await updateSettings({ [key]: value });
      setProvenance(res.provenance ?? {});
    } catch {
      // The control state comes from the daemon's settings_changed event; if
      // the PUT fails we leave the UI as-is and let the next broadcast sync it.
    }
  }

  /** A free-text setting needs its refusal shown: unlike a toggle, the
   * operator cannot see from the control that the value was rejected. */
  async function putTextSetting(key: string, value: string): Promise<string | null> {
    try {
      const res = await updateSettings({ [key]: value });
      setProvenance(res.provenance ?? {});
      return null;
    } catch (err) {
      return err instanceof Error ? err.message : String(err);
    }
  }

  async function clearSetting(key: string): Promise<string | null> {
    try {
      await deleteSetting(key);
      setProvenance((await getSettings()).provenance ?? {});
      return null;
    } catch (err) {
      return err instanceof Error ? err.message : String(err);
    }
  }

  const renotify =
    typeof settings.renotify_interval === "number" ? settings.renotify_interval : 300;

  return (
    <div className="settingsMain">
      <div className="headerRow">
        <h1>Settings</h1>
        <span className="subline">
          reusable model policy, and how attention reaches you
        </span>
      </div>

      <div className="settingsGrid">
        <div className="settingsColumn">
          {/* Global model policy. A profile is what a launch selects; there
              is no saved launch preset between the two any more (ADR-0026). */}
          <ModelProfilesPanel />
        </div>

        <div className="settingsColumn">
          <section className="panel" data-testid="notifications-panel">
            <h2 className="panelTitle">Notifications · per attention tier</h2>
            <TierMatrix settings={settings} provenance={provenance} onChange={putSetting} />
          </section>

          <section className="panel" data-testid="renotify-panel">
            <h2 className="panelTitle">Re-notify</h2>
            <label className="formField">
              <span className="fieldLabel">
                Re-notify interval
                <OverrideTag
                  provenance={provenance}
                  settingKey="renotify_interval"
                  testId="renotify-interval"
                />
              </span>
              <select
                value={renotify}
                onChange={(e) => putSetting("renotify_interval", Number(e.target.value))}
                data-testid="renotify-interval"
              >
                {RENOTIFY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
              <span className="fieldHint">How long to wait before reminding you again</span>
            </label>
          </section>

          <section className="panel" data-testid="watchdogs-panel">
            <h2 className="panelTitle">Watchdogs &amp; thresholds</h2>
            <WatchdogInputs settings={settings} provenance={provenance} onChange={putSetting} />
          </section>

          <section className="panel" data-testid="checkout-root-panel">
            <h2 className="panelTitle">Checkout root</h2>
            <CheckoutRootPanel
              settings={settings}
              provenance={provenance}
              onCommit={(value) => putTextSetting("checkout_root", value)}
              onReset={() => clearSetting("checkout_root")}
            />
          </section>

          <section className="panel" data-testid="daemon-panel">
            <h2 className="panelTitle">Daemon</h2>
            <DaemonPanel />
          </section>
        </div>
      </div>
    </div>
  );
}

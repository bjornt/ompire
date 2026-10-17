# Run the dogfooding QA loop

Run the real daemon, Workshop/LXD, models, browser, signing, and GitHub against
**only the `ompire-test` bot's disposable sandbox repository**. Never use a
personal GitHub identity or a real project's publishing target. For fast,
deterministic offline checks use the [local end-to-end harness](run-local-e2e.md).
Neither replaces the other.

The flow is **define → probe → grant → observe**. An operator provisions the
trusted QA host and explicitly grants one `dogfood` driver task read-only forge
access. That agent observes a separate sandbox task's real publication through
the [forge broker](../reference/forge-broker.md). The operator alone spawns
tasks, answers gates, confirms delivery, merges, and cleans up.

This is a procedure, not a claim that a real-service acceptance run has passed.
Retain actual observations, failures, and coverage gaps for the run you perform.

## Keep the authority boundary intact

| Plane | Holds | Owner and allowed actions |
|---|---|---|
| Management machine | Bot `gh` login with `admin:public_key` and `admin:gpg_key` | Operator creates/rotates/removes the bot identity and transfers it securely |
| Trusted QA host | `.qa-agent/` PAT, SSH/GPG keys and passphrase; daemon bearer token; forge broker; Workshop/LXD | Operator provisions and supervises services; daemon reviews, signs, pushes, and creates PRs |
| Credential-free task sandbox | Its own clone, non-secret routing, model gateway tunnel; `github` tunnel **only when granted** | Agent uses models and brokered repository reads, collects evidence, and requests operator actions |

The QA host is trusted relative to its task containers, but disposable relative
to your management machine. Give it only the bot's sandbox-scoped authority.
The management credential never leaves your machine. Never copy `.qa-agent/`,
source its `env.sh`, mount host credential stores or sockets, or run `gh auth`
inside a task. Never give an agent the daemon token or its tokenized UI URL,
SSH access to the trusted host, or a browser profile authenticated to the daemon.
Even browser automation of the operator UI runs on the trusted side, under the
operator's control, not in the observing task.

The gateway retains provider credentials; the forge broker retains the PAT.
Neither route needs a sandbox `Authorization` header. There is no direct-token
fallback. The driver must not call the daemon API, publish to GitHub, or alter
host services: it requests each such action from the operator and waits.

See [ADR-0011](../../adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md),
[ADR-0015](../../adr/0015-keep-agent-credentials-behind-narrow-brokers.md),
[ADR-0041](../../adr/0041-broker-forge-access-behind-read-only-proxy.md), and
[ADR-0042](../../adr/0042-pin-environment-grants-at-task-acceptance.md).

## 1. Provision the identity — operator, management machine

You need a disposable Ubuntu host with `sudo`, an `ompire-test`-owned sandbox
repository, and `gh` on your management machine logged in **as that bot**:

```sh
gh auth login --web --scopes "admin:public_key,admin:gpg_key"
```

For an existing bot login use
`gh auth refresh --user <bot> --scopes "admin:public_key,admin:gpg_key"`.
Create the fine-grained PAT manually; GitHub has no API for minting it:

| PAT setting | Value |
|---|---|
| Repository access | Only the disposable sandbox repository |
| Repository permissions | Contents RW, Pull requests RW, Issues RW, Actions RW, Workflows RW; Metadata R implicit |
| Account permissions | None; key management stays on the management machine |

From a checkout of Ompire on the management machine:

```sh
scripts/setup-qa-agent.sh --repo <owner>/<sandbox> setup
scripts/setup-qa-agent.sh status
rsync -a .qa-agent/ <qa-host>:~/ompire/.qa-agent/
```

Setup creates SSH/GPG keys, verifies the PAT's repository access before
uploading their public halves, and records the identity. `--repo` is
repeatable for identity management, but this loop's broker allowlists exactly
the one resolved sandbox. Put the Ompire checkout on the host as well; transfer
it if the bot cannot clone upstream. The identity is self-locating, so copying
it needs no regeneration. Transfer only over SSH, never through chat or an
issue. Confirm directory mode `0700` and private-file mode `0600`.

`status` diagnoses bot login, repository reachability, management login,
local/state/remote key agreement, and live SSH authentication. Use
`rotate ssh`, `rotate gpg`, `rotate token`, or `rotate all` on the management
machine when necessary; copy the updated identity to the trusted host and
re-run environment setup. A brokered task ending alone requires no rotation.

### Protect the trusted-side files

| Identity file | Purpose |
|---|---|
| `token`, `.curlrc`, `.headers` | PAT and credential-bearing curl material |
| `id_ed25519`, `gnupg/`, `.gpg-passphrase` | SSH private key, signing key, and its passphrase |
| `env.sh`, `gitconfig` | Trusted publishing environment and Git identity |
| `state.json`, `known_hosts`, public key files | Identity lifecycle metadata and public material |

Keep the whole directory out of Git and out of every task, not just its obvious
secret files. Never reproduce secret values in screenshots, logs, prompts,
transcripts, findings, or PRs. Bot login and public fingerprints are safe
identifiers. The passphrase-protected signing key is warmed by the daemon
wrapper so its gate sees a cached key. The daemon can report a signing
**subkey** fingerprint while configuration records the **primary** fingerprint;
that difference is expected.

## 2. Define the environment — operator, trusted QA host

A complete real-provider model profile is required. Reuse a profile by setting
`OMPIRE_QA_MODEL_PROFILE`, or set `OMPIRE_QA_MODEL` to the provider-qualified
model ID that should fill all four roles. Setup never borrows your own `omp`
configuration or invents a model.

From the QA host's Ompire checkout:

```sh
scripts/setup-qa-env.sh
```

Use `--repo <owner>/<sandbox>` if repository selection is ambiguous and
`--dir PATH` for an identity outside `.qa-agent/`. Optional `--check` also runs
the daemon/frontend suites; it is not the environment readiness probe.
`--skip-workshop` and `--skip-browser` only skip their provisioning on a re-run;
readiness still requires their dependencies.

Setup provisions the toolchain, `gh`, Workshop/LXD, headless browser, frontend,
bot signing configuration, sandbox checkout/project, and daemon. It also:

- writes `~/.config/ompire/qa-forge-broker.sh`, starts the broker on trusted
  loopback port `4100` with only the resolved sandbox allowlisted, and records
  `~/.local/share/ompire/forge-broker.pid` and `forge-broker.log`;
- installs `[environments.dogfood]` in `~/.config/ompire/config.toml`: host
  `host`, the absolute setup script plus resolved `--dir` and `--repo` as
  provision argv, that script with only `--probe` as check argv, and
  capabilities `model → auth-gateway`, `forge → forge-broker`,
  `browser → chrome`;
- declares the `github` tunnel plug on `omp`, slot on `system`, both at
  `localhost:4100`, and their `provision.connect` connection **in the
  environment wiring fragment**, not in repository `workshop.yaml`;
- registers `ompire` against the local Ompire checkout, using a local-path
  `qa-self` fetch remote. `origin` supplies truthful upstream identity only;
  spawning the driver does not fetch the real upstream over the network;
- installs the tracked `dogfood` workflow in the operator's workflow library
  when absent. It has one agent session, requires `dogfood`, and declares no
  review or publication steps. It cannot publish Ompire changes.

Setup refuses a publishing identity or repository owner other than
`ompire-test`; a public repository is still not a safe target unless it is
that bot's disposable sandbox.

Re-running restarts the broker (re-reading the current trusted-side token),
restarts the daemon, and replaces only the script-owned `dogfood` environment
block, preserving unrelated operator configuration. Existing workflow library
entries remain operator-owned; inspect an existing `dogfood` definition if it
differs from the tracked `scripts/qa-dogfood-workflow.yaml`.

If LXD was just installed, log out and back in before spawning: membership in
`lxd` does not apply to the existing session. Review every setup smoke result.
A missing auth gateway can leave a provisioned environment `degraded`; fix it
before attempting a grant.

## 3. Probe readiness — operator, gateway side and browser

The existing `pi-auth-gateway` tunnel in Ompire's `workshop.yaml` remains the
model route; the grant adds the forge route. From the machine running the
model gateway on its own `localhost:4000`:

```sh
scripts/qa-auth-tunnel.sh ubuntu@<qa-host>
```

Keep it running. It reconnects on disconnect; Ctrl-C stops it. On the trusted
QA host, the read-only, non-interactive check is:

```sh
scripts/setup-qa-env.sh --probe
```

`--probe` is mutually exclusive with all other CLI options. It checks identity
file presence/modes without sourcing `env.sh` or printing contents, broker
`/healthz`, gateway reachability on port `4000`, browser status, and container
tooling. It exits nonzero with a concrete blocker and fix until all are ready.
This proves **local readiness, not upstream GitHub authentication**; the real
brokered reads below establish the latter.

The check inherits the non-secret `QA_AGENT_DIR` path from the daemon wrapper,
which sources the selected trusted identity. A direct probe defaults to this
checkout's `.qa-agent/`; for a custom identity use
`QA_AGENT_DIR=/absolute/identity scripts/setup-qa-env.sh --probe`, not
`--probe --dir ...`.

Forward the daemon UI from your trusted operator machine:

```sh
ssh -N -L 4173:127.0.0.1:4173 ubuntu@<qa-host>
```

Setup points the operator to `~/.local/share/ompire/token`; it does not print
a tokenized URL. Load that file only on the trusted side and authenticate the
operator-only browser at `http://127.0.0.1:4173/?token=<daemon-token>` privately.
Do not paste the real URL into chat, command argv, recordings or screenshots;
remove the token query before capturing browser evidence. The token is retained
in that browser's local storage: never copy its profile into a task.
Open **Settings → Environments** and
inspect `dogfood`: host, capabilities, provision command, state, blockers, and
last check. Select **Re-check**. With the broker and auth tunnel available it
must show **Ready**. Without either, it must show **Degraded** with that
blocker and recovery action. Record rendered browser state, not just API JSON.
For automation tooling see [Drive the UI in a browser](run-local-e2e.md#drive-the-ui-in-a-browser),
but run it only on the trusted operator side for this loop.

## 4. Grant the driver — operator, browser

1. Open **Projects** and confirm both `ompire` (the Ompire checkout) and the
   separate bot sandbox project exist with the intended paths/targets.
2. Open **Workflows**, inspect the installed `dogfood`: `environment: dogfood`,
   one session, no publication steps. In **Spawn task**, select that workflow,
   project `ompire`, a unique slug, and the real-provider profile.
3. Prompt the driver to follow this guide from its workspace, observe only
   `<owner>/<sandbox>`, use a chosen task-correlation label, and request all
   operator actions. Explicitly forbid daemon access, credentials, and direct
   forge publishing. Attach the guide with `@docs/develop/how-to/run-the-qa-loop.md`
   only when that revision is committed on the selected base.
4. Inspect **Required environment: dogfood**, all three capability/provider
   pairs, freshness, and blockers. Leave **Grant this task access to dogfood**
   unchecked and submit **Spawn task** once: capture the named missing-grant
   refusal and confirm no task/workspace was created.
5. On the trusted host, stop the broker using its pid file. In the same browser
   preview select **Re-check**: record **Degraded** and the broker blocker.
   Check the grant box and submit: capture the unready refusal with named
   blockers and no task creation. Restore the broker as described under
   [Recovery](#recovery-and-restart), then **Re-check** to **Ready**.
6. Review the preview again, check **Grant this task access to dogfood**, and
   select **Spawn task**. Acceptance probes again; stale readiness is not
   permission. Observe fetch/clone/branch/workshop progress and the streaming
   driver session. Changing workflow/revision clears the grant; review again.
7. Retain the accepted task ID and its recorded
   `execution_inputs.environment_grant` (name, capability/wiring snapshots,
   UTC time, `actor: operator`). The operator may inspect the task response in
   the browser's Network panel; do not give the agent an authenticated daemon
   request or token. The [spawn reference](../../use/reference/task-spawn.md#environment-access)
   defines that durable record.

Do not combine a non-empty selected Workshop additions source with the grant's
wiring: acceptance refuses that conflict. Keep `github` out of static
repository declarations, including the sandbox project; otherwise an ungranted
task could receive ambient forge access.

## 5. Drive and observe the loop

Each authority act below has exactly one owner. The driver waits for the
operator's acknowledgement and public PR number/commit SHA, not a daemon
credential. Keep the driver session alive until observation and inspection are
complete; the minimal workflow ends when its agent supplies its final outcome.

| Step | Owner / actual action | Required observation |
|---|---|---|
| Establish the observer | Agent runs the brokered clone/fetch/API commands below inside its granted sandbox | Allowed reads succeed through `localhost:4100`, with the chosen label |
| Launch the subject | Operator opens **Spawn task**, selects the **sandbox project** and packaged `single-step` workflow, a unique slug/profile, and a harmless sandbox-only change prompt; reviews and submits | Subject has its own container/transcript, the sandbox upstream, and no `dogfood` grant |
| Handle questions/gates | Operator answers session questions in task detail; for non-publishing gates selects a named answer, supplies required feedback, then **Submit decision** | The named workflow route advances; publication answers link to Ship flow rather than authorizing from this control |
| Review | Operator opens the subject's **Review** panel and follows its llmvet URL | `single-step` starts review itself; approved content advances to the approval gate. Do not look for an obsolete manual Start Review step |
| Prepare publication | Operator selects **Continue to Ship flow**, selects **Sign, push, and open a pull request**, reviews/edits commit message, PR title/body, and uses **Save text** if changed | Bot signing identity and GitHub account `@ompire-test`, canonical sandbox target, and text are correct; both preflights are ready |
| Authorize publication | Operator selects **Review this delivery**, inspects reviewed candidate, actions, identities, destination and blockers, then **Confirm: …** only when it names the intended sign/commit, push, and PR effects | Daemon performs the authorized chain; signed SHA and sandbox PR URL exist. The driver never signs, pushes, or creates the PR |
| Verify GitHub | Agent reads the PR and daemon-published SHA through the broker | `commit.verification.verified == true` and `reason == "valid"`; operator can corroborate the **Verified** badge on GitHub's commit page |
| Audit a denial | Agent requests `/api/user`; operator extracts correlated broker log records | Actual `403`, allowed and denied records for the label, and denial `upstream_status: null` |
| Inspect isolation | Agent performs the safe self-inspection below; operator spot-checks from the host | No reusable credentials in environment, argv, files, or transcripts; coverage gaps are explicit |
| Merge | Operator follows the subject's PR link and uses GitHub's **Merge pull request → Confirm merge** in the bot sandbox | Real PR is merged; Ompire polling records `merged` and enables cleanup. There is no Ompire merge button |
| Clean up subject | Operator selects subject Ship flow **Clean up** (or task detail **Clean up workspace**) and accepts its confirmation | Subject container/clone gone; delivery/review history remains |
| Finish and clean up driver | After all evidence, agent supplies its final workflow outcome; operator selects task detail **Clean up workspace** and confirms | Driver container/clone gone and its granted tunnel revoked, as checked below |

For gates, review correction loops, unresolved effects, or intermediate delivery
endings, follow [Review and ship a task](../../use/how-to/review-and-ship.md).
The **driver** has no publication vocabulary; do not attempt to ship it or add
publish steps to make an observation easier.

### Brokered commands — agent, granted sandbox only

Replace placeholders with the one sandbox target, your driver task label, and
later the real subject PR number/SHA supplied by the operator. The label is
non-secret and spoofable audit context, **not authentication**.

```sh
base=http://localhost:4100
repo='<owner>/<sandbox>'
label='dogfood-<driver-task-id>'
checkout="$PWD/qa-observed-sandbox"

curl -q --fail --silent --show-error --max-time 10 \
  -H "X-Ompire-Task: $label" "$base/healthz"
git -c credential.helper= -c core.hooksPath=/dev/null \
  -c "http.extraHeader=X-Ompire-Task: $label" \
  clone "$base/git/$repo.git" "$checkout"
git -C "$checkout" -c credential.helper= -c core.hooksPath=/dev/null \
  -c "http.extraHeader=X-Ompire-Task: $label" fetch origin
curl -q --fail --silent --show-error --max-time 30 \
  -H "X-Ompire-Task: $label" -H 'Accept: application/vnd.github+json' \
  "$base/api/repos/$repo" | jq '{full_name, default_branch}'
```

After the operator confirms publication:

```sh
pr='<subject-pr-number>'
sha='<daemon-published-commit-sha>'
curl -q --fail --silent --show-error --max-time 30 \
  -H "X-Ompire-Task: $label" -H 'Accept: application/vnd.github+json' \
  "$base/api/repos/$repo/pulls/$pr" | jq '{number, state, head: .head.sha, base: .base.repo.full_name}'
curl -q --fail --silent --show-error --max-time 30 \
  -H "X-Ompire-Task: $label" -H 'Accept: application/vnd.github+json' \
  "$base/api/repos/$repo/commits/$sha" | \
  jq '{sha, verification: (.commit.verification | {verified, reason})}'
curl -q --fail --silent --show-error --max-time 30 \
  -H "X-Ompire-Task: $label" -H 'Accept: application/vnd.github+json' \
  "$base/api/repos/$repo/commits/$sha/check-runs" | jq '{total_count}'
```

The signature field is **`commit.verification`**, not top-level
`verification`. Match the SHA to the daemon's published commit/PR head: an old
seed commit's valid signature is not acceptance evidence for this delivery.
An empty `check_runs` array is legitimate. No command uses `gh`, a PAT, or an
`Authorization` header. `curl -q` must be its first option to suppress ambient
curl configuration. Do not change the remote to direct GitHub if a read fails.

### Capture a real denial — agent request, operator log

Agent, still inside the granted sandbox:

```sh
curl -q --silent --show-error --max-time 10 \
  -H "X-Ompire-Task: $label" --output /dev/null --write-out '%{http_code}\n' \
  "$base/api/user"
```

Require `403`, not merely a failed connection. This account-level request is
outside policy and never reaches GitHub. Do not supply credentials to bypass
it. Operator, on the trusted host, select just this run's request records:

```sh
label='dogfood-<driver-task-id>'
jq -R --arg label "$label" \
  'fromjson? | select(.task == $label) | {timestamp, task, method, path, decision, rule, upstream_status, status}' \
  ~/.local/share/ompire/forge-broker.log
```

The combined log also contains a non-JSON readiness line; `fromjson?` skips it.
Retain allowed clone/API records alongside the `/api/user` denial with its
rule, status `403`, and `upstream_status: null`. The supplied label only
correlates this observed run; it does not cryptographically attribute requests.

### Inspect the sandbox without leaking a discovery

Agent, before completing the driver: inspect **your own sandbox**, not the
trusted host. Do not print `env`, raw `/proc/*/environ`, `ps eww`, credential
files, transcript contents, or matching lines. Use a read-only scanner that
reports only category, safe location identifier, and match count:

1. Check environment **names and non-empty presence**, not values, for forge,
   daemon, signing and provider credentials (including `GH_TOKEN`,
   `GITHUB_TOKEN`, `OMPIRE_FORGE_TOKEN`, and provider API-key variables).
   Non-secret gateway routing is not an upstream provider credential.
2. Scan readable `/proc/<pid>/environ` and `/proc/<pid>/cmdline` for all task
   processes in memory; detect non-empty credential assignments, token-shaped
   strings, private-key blocks, and bearer/basic credential material. Report
   inspected process counts and inaccessible/disappearing entries separately.
3. Walk the workspace **including hidden files**, `.git/config`, `.ompire/`,
   temporary artifacts, and the sandbox home/config/session directories.
   Check for `.qa-agent/`, credential-bearing Git URLs, curl/gh credential
   configuration, SSH/GPG private material, daemon-token copies, and provider
   stores. Scan file contents without echoing a match or embedding it in argv.
   Enumerate unreadable/skipped files; do not silently exclude logs or hidden
   agent state.
4. Inspect every session transcript and tool result available to the task,
   including earlier commands and retained outcomes/artifacts, with the same
   non-emitting scan. If a transcript is available only through the operator
   UI, ask the operator to inspect that portion on the trusted side; report it
   as pending until checked, not as absent.

For a non-emitting first pass, run this inside the task. Add any additional
sandbox-local session/transcript directories as arguments. It scans readable
process metadata and all regular files under the workspace/home, including
hidden state; it prints only counts, never content or matching values:

```sh
python3 - "$PWD" "$HOME" <<'PY'
import collections
import os
from pathlib import Path
import re
import sys

counts = collections.Counter()
credential_name = re.compile(
    rb"(?:GH_TOKEN|GITHUB_TOKEN|OMPIRE_FORGE_TOKEN|OMPIRE_TOKEN|"
    rb"[A-Z0-9_]*API_KEY|AWS_SECRET_ACCESS_KEY|SSH_AUTH_SOCK|GPG_AGENT_INFO)="
    rb"([^\x00\s]+)", re.I,
)
material = re.compile(
    rb"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"sk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY|"
    rb"https?://[^\s/@:]+:[^\s/@]+@|Authorization:\s*(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=-]{16,}",
    re.I,
)

def inspect(category, data):
    counts[category + "_inspected"] += 1
    if material.search(data) or credential_name.search(data):
        counts[category + "_suspect"] += 1

inspect("own_environment", b"\x00".join(os.fsencode(k + "=" + v) for k, v in os.environ.items()))
for process in Path("/proc").iterdir():
    if not process.name.isdecimal():
        continue
    for name in ("environ", "cmdline"):
        try:
            inspect("process_" + name, (process / name).read_bytes())
        except OSError:
            counts["process_" + name + "_unreadable_or_exited"] += 1

seen = set()
def walk_error(_):
    counts["directory_unreadable"] += 1

for root in map(Path, sys.argv[1:]):
    if not root.is_dir():
        counts["root_missing_or_unreadable"] += 1
        continue
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        counts["identity_directory_present"] += int(".qa-agent" in dirs)
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                counts["symlink_requires_review"] += 1
                continue
            if not path.is_file():
                continue
            try:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                inspect("file", path.read_bytes())
            except OSError:
                counts["file_unreadable"] += 1

print(dict(sorted(counts.items())))
PY
```

This intentionally reports **suspects**, not secret values or a certificate of
absence. Resolve every suspect without printing its contents, review symlink
targets and inaccessible surfaces, and confirm all session/transcript roots
were covered. Opaque daemon tokens and arbitrary provider credentials need
the trusted-side comparison below; pattern scanning cannot identify them all.

Report counts and coverage, never suspected secret values. Signature/token
patterns alone are not proof of absence. The operator spot-checks the task's
host-side process metadata, clone and session/transcript storage and, where
available, compares actual trusted secret bytes **in memory** against those
surfaces, emitting only match counts. Never copy the expected secret into the
sandbox, command argv, transcript, or a test fixture to perform this check.
If anything matches, stop the loop, keep the value out of findings, and have
the operator investigate/rotate on the management plane. A coverage gap is an
unresolved acceptance issue, not a passing security result.

## 6. Prove restart, no-grant control, and revocation

All host/UI actions here belong to the operator; only broker probes inside a
live task belong to that task's agent.

- **Broker failure/recovery:** while the driver is live, stop the broker by its
  pid file. Agent repeats its labeled read: it must fail to connect, with no
  credential fallback. Operator restarts the wrapper at the same port and
  re-checks readiness; agent repeats the read successfully through the existing
  tunnel. Save before/after results.
- **Daemon restart:** record the grant, stop the daemon using `daemon.pid`,
  and start its normal `qa-daemon.sh` wrapper with the pid/log handling below.
  Reopen the task in the browser and compare the accepted grant snapshots,
  timestamp and actor. A surviving container retains its forge route; have the
  driver repeat a labeled read. Review/PR history survives too; restart must
  not publish anything by itself.
- **No-grant control:** the separate sandbox subject has no environment grant.
  Ensure its static Workshop/additions declarations do not contain `github`.
  Have that task attempt a bounded `curl -q --fail --max-time 5
  http://localhost:4100/healthz`: it must have no route/listener, not broker
  `403`. Record actual connection failure from inside its container. This is
  distinct from refusing an ungranted `dogfood` spawn, which creates no task.
- **Cleanup revocation:** record the driver container's identity and successful
  labeled read before cleanup. After the driver finishes, operator uses
  **Clean up workspace** and confirms removal. Confirm the task is archived,
  its clone no longer exists, the exact Workshop/LXD container is absent, and
  executing another probe in that removed container fails because it no longer
  exists. The broker must remain healthy on the trusted host; stopping the
  broker globally is not evidence of task-scoped revocation. Host port `4100`
  remains available to trusted processes, not to a removed task.

## Verify GitHub identity preflight — operator only

Preserve this separate publishing failure/recovery check on the disposable
sandbox subject before normal delivery; it is not the environment probe or an
agent-side broker check.

1. Record the subject clone's `HEAD` and worktree state from the trusted host.
2. Stop the normal daemon and launch it once with a deliberately invalid
   `GH_TOKEN` in its trusted launch environment. The ordinary QA wrapper
   sources `env.sh` and would replace that override: use the same daemon
   launch/configuration with the invalid override applied **after** sourcing
   the identity. Keep the value out of argv, recordings and task prompts.
3. In the browser, confirm chrome and Settings show `gh auth` safely. In Ship
   flow choose the PR answer and **Review this delivery**: the GitHub blocker
   must keep **Confirm: …** disabled. Verify that preview creates no delivery
   record, commit, `HEAD` change, or worktree change.
4. The workflow's **local signed commit** answer must still be offered without
   forge availability; its own signing requirements still apply. Do not confirm
   this alternate ending if continuing the PR-path check.
5. Stop the deliberately misconfigured daemon and restart the normal wrapper.
   **Re-check GitHub** in Settings and Ship flow must name `@ompire-test` and
   the canonical sandbox target ready. Complete normal confirmed publication.

Keep GitHub API eligibility distinct from the SSH/HTTPS identity used by
`git push`. Capture safe statuses and identities only, never a token or an
authorization-bearing URL.

## Recovery and restart

Run these commands only on the **trusted QA host**. Setup normally manages
pid/log files; for a stopped broker, restart it with the same handling:

```sh
nohup ~/.config/ompire/qa-forge-broker.sh >>~/.local/share/ompire/forge-broker.log 2>&1 &
echo $! >~/.local/share/ompire/forge-broker.pid
curl -q --fail --show-error --max-time 5 http://127.0.0.1:4100/healthz
```

Stop with `kill "$(cat ~/.local/share/ompire/forge-broker.pid)"` and wait for
that process to exit before restarting; never start a competing listener. The
wrapper executes the broker; it is not a service manager or automatic crash
respawner. Fixed port `4100` lets surviving task tunnels work again. Retry an
interrupted read/fetch; no durable broker state needs reconciliation.

For a stopped daemon use:

```sh
nohup ~/.config/ompire/qa-daemon.sh >>~/.local/share/ompire/daemon.log 2>&1 &
echo $! >~/.local/share/ompire/daemon.pid
```

After trusted-side token rotation, re-run `setup-qa-env.sh` with the original
identity/repository choices. Restart is required: the broker loads token and
allowlist at startup, not per request. Protect broker logs as potentially
sensitive repository evidence even though credentials are scrubbed.

| Observation | Fix / boundary |
|---|---|
| Identity missing or permissions unsafe | Re-copy the complete identity to the trusted host; restore `0700` directory / `0600` private files. Never fix by copying it into a task |
| PAT sees no repositories / multiple repositories | Correct its sandbox selection or pass `--repo <owner>/<sandbox>` on provisioning |
| Environment degraded: gateway on `:4000` missing | Run `qa-auth-tunnel.sh` on the gateway side, then **Re-check** |
| Environment degraded: broker missing / `/healthz` fails | Check `forge-broker.pid`/`forge-broker.log`; restart the generated wrapper and **Re-check** |
| Broker local health succeeds, repository reads fail | Local probe does not authenticate upstream; inspect safe upstream status and PAT expiry/repo permissions on the trusted side |
| Broker `403` | Fix the method/path/repository or unsafe redirect; account reads, writes, GraphQL and pushes are deliberately denied. Never add a sandbox PAT |
| Existing task gets connection refused on `:4100` | Restore broker and task tunnel at the fixed endpoint; repeat read/fetch. Do not regrant or inject credentials |
| Browser/tooling blocker | Run `scripts/setup-browser.sh --status`, provision missing Chrome/Workshop/LXD, and re-check; skip flags do not waive readiness |
| LXD permission denied after installation | Start a new login session with the `lxd` group |
| No environment / script missing after moving checkout | Re-run setup from the new trusted checkout to regenerate absolute paths, then restart/re-check |
| Grant/additions conflict | Remove the non-empty selected additions source and review a new preview; do not move the forge plug into static YAML |
| `ompire` fetch contacts upstream / wrong existing registration | Inspect Projects and correct the existing registration to the local checkout with `fetch_remote: qa-self`; provisioning skips existing registrations |
| Daemon startup dies | Inspect `~/.local/share/ompire/daemon.log`; restore the normal wrapper launch environment |
| GitHub account differs from bot | Restart `qa-daemon.sh` against the intended trusted identity; management `gh` login is not a task credential |
| SSH `ls-remote` fails | On management machine run identity `status`, then rotate/re-copy the SSH key if necessary |
| GPG `locked` / `agent_unavailable` / `no_key` / `ambiguous` | Warm cached key with trusted identity instructions, start `gpg-agent`, or correct/select the configured primary key; restart/re-check |
| GitHub signature `bad_email` | Enable **Keep my email addresses private** in the bot's GitHub email settings, then publish a new commit |
| Placeholder frontend page | Re-run setup to build `frontend/dist`; `--skip-browser` controls browser provisioning, not frontend building |

## Record evidence

Record run date, host/checkouts, driver and subject task IDs, workflow revisions,
public sandbox identity, and actual observed results in the active change's
artifacts (including `epics/<epic>/changes/<change>/` for an epic child). Keep
browser evidence for ready/degraded/grant/refusal, the immutable grant, model
execution, brokered clone/fetch/API reads and published SHA verification,
correlated denial log, inspection coverage, restart/control results, and
cleanup/revocation. A command invocation or green local test is not real-service
acceptance. Mark unavailable services and failed observations honestly; do not
replace them with fixtures or fabricated evidence.

## Teardown — operator

Clean tasks through the UI first so each clone and container, including its
tunnels, is removed. On the trusted host:

```sh
kill "$(cat ~/.local/share/ompire/forge-broker.pid)"
kill "$(cat ~/.local/share/ompire/daemon.pid)"
lxc list --project workshop.ubuntu
```

Check for leftover task containers (`qa-agent-*`); `workshop list` is scoped to
a project's workshops and is not a substitute. Stop `qa-auth-tunnel.sh` on the
gateway machine. Remove only this disposable run's task clones, sandbox
checkout, trusted identity copy, daemon state/config and wrappers, or destroy
the whole QA host. Do not remove your management login or unrelated projects.

On the **management machine**:

```sh
scripts/setup-qa-agent.sh teardown
# Also delete the local identity directory, if no longer needed:
scripts/setup-qa-agent.sh teardown --delete-local
```

Teardown removes only SSH/GPG keys recorded in `state.json`, warns about other
`qa-agent-*` keys, and refuses to delete `/` or `$HOME`. The PAT cannot be
revoked through the API: delete it manually in the bot's
[fine-grained token settings](https://github.com/settings/tokens?type=beta)
(and any OAuth device token in [Applications](https://github.com/settings/applications)).
Finally run `scripts/setup-qa-agent.sh status`: an invalid token and missing
remote keys, not merely a completed teardown command, prove revocation.

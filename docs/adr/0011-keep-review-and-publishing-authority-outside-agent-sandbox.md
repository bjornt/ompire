# ADR 0011: Keep review and publishing authority outside the agent sandbox

- Status: Accepted
- Date: 2026-08-14

## Context

Ompire accepts source changes from a coding agent running in a task-specific sandbox, but accepting and publishing those changes require different authority. The agent and everything it can influence—its tools, dependency scripts, repository contents, generated files, and prompts—are untrusted. Giving that environment the ability to decide that its own work passed review, sign commits, push branches, or create pull requests would let the reviewed party mediate the evidence and would expose credentials to the part of the system most likely to execute untrusted code.

Review must cover the complete task delta, including both committed checkpoints and uncommitted working-tree changes. The review tool consumes working-tree diffs rather than an arbitrary Git revision range. Agent checkpoint commits can therefore hide part of the delta unless the trusted side temporarily exposes all changes relative to the task's merge base. That temporary Git mutation must survive daemon failure without losing the task's real branch tip or touching its working tree.

Publishing has a related but broader authority problem. The task clone contains agent-produced commits and files, while commit signing, branch push, and pull-request creation require the operator's host Git configuration, signing agent, and forge credentials. The agent can use its task context to propose a commit message and pull-request text, but those strings are untrusted suggestions, not authorization. The trusted side must determine the repository destination, rewrite or create the published commits, verify the result when retaining commit structure, and perform every credential-bearing external action.

The review boundary was implemented on 2026-07-22. Daemon-controlled signed commit, push, and pull-request automation completed the boundary on 2026-08-14; retain-mode rewriting later preserved the same boundary. The implementation, the recorded design, and the durable product direction agree that review and publishing authority stay outside the sandbox. This ADR backfills the accepted decision using the date on which the complete review-and-publish boundary was first implemented.

The current publisher uses the operator's host identity, while the product direction prefers a dedicated bot identity by default and requires stronger durable provenance for privileged side effects. Publisher identity, delegation policy, and audit retention are separate decisions requiring reconciliation. They do not alter the authority boundary recorded here: whichever trusted identity is selected, its credentials and privileged operations remain outside the agent sandbox.

## Decision

Ompire keeps authoritative review and all publishing authority in the trusted control plane, outside the agent sandbox.

The daemon runs the real review tool against the host side of the task clone. The reviewed agent cannot invoke the authoritative review, alter its exit status or output in transit, or claim approval on its own. For a review tool that reads working-tree diffs, the daemon exposes the complete task delta by temporarily moving the clone's Git head to the merge base while leaving the working tree unchanged. Before moving the head, it records the original revision in a durable, Ompire-owned Git ref. It restores the exact revision and removes the ref on every normal exit, cancellation, error, and startup recovery path. Review comments may be returned to the agent as untrusted work instructions; approval remains a trusted review result.

The daemon also performs the publishing sequence on the host side of the task clone. It derives the base branch, push destination, and pull-request repository from trusted project and task state rather than agent output. It may ask the agent to draft commit and pull-request text, but the operator or trusted workflow controls the final values and the daemon passes them to Git and forge tools as data.

Published commits are never agent checkpoint commits used unchanged. In squash mode, the daemon creates one new signed commit for the complete task delta. In retain mode, it rewrites every commit in the task range under the selected trusted author, committer, and signing identity, then verifies that commit count is unchanged and every rewritten commit has an acceptable signature before any push. Destructive temporary Git operations are protected by a durable Ompire-owned ref and have deterministic restoration paths.

Signing uses a host-side credential agent or an equivalently narrow trusted broker. Raw signing secrets do not enter daemon configuration, the task workspace, the agent environment, prompts, transcripts, or workflow state. The daemon may probe whether a signing credential is available without opening an interactive prompt and must fail closed before publication when credential state or signature verification is unacceptable.

Push and pull-request creation run as daemon-supervised host processes with the selected trusted credentials. The daemon constrains their repository, branch, and operation from trusted control-plane state, captures their results directly, and does not delegate those commands or credentials to the agent. Publisher identity and attribution policy may change through a separate decision without moving this execution boundary into the sandbox.

The invariant is that the sandbox may produce source changes and draft publication text, but it never supplies the authoritative review verdict, holds publishing credentials, or executes signing, push, pull-request, or equivalent privileged forge operations. Those actions are performed and checked by the trusted control plane against the host-visible task workspace.

## Consequences

The party whose work is under review cannot mediate the approval signal. The review tool sees the same working tree mounted into the task environment, while daemon-controlled Git positioning makes committed checkpoints and uncommitted edits visible as one delta. Comments can still flow back through the existing agent session without making the agent authoritative for the verdict.

Host credentials remain outside the sandbox. Repository code, dependency hooks, and agent tools cannot directly access signing keys, GitHub authentication, or a daemon control credential merely because the task is publishable. The daemon can use existing host credential agents and forge tooling without copying raw secrets into task state. A compromised sandbox can propose malicious content, including commit or pull-request text, but cannot select its own destination or acquire publication authority; the trusted side must still treat every proposed string and workspace file as untrusted input.

The trusted control plane becomes security-critical. It must construct subprocess argument vectors without a shell, constrain paths and remotes from trusted records, avoid executing repository-controlled hooks during privileged Git operations unless explicitly allowed, validate external command results, and fail closed when review, identity, credential, or signature state is ambiguous. Host-side execution narrows credential exposure but does not by itself make untrusted repository content safe.

The complete-delta review requires temporary Git-state manipulation until the review tool can consume a revision range directly. Durable private refs make this recoverable, but every startup and failure path must restore them before the workspace resumes normal use. Squash and retain publication likewise rewrite Git history and may require force-with-lease when a task is republished. Retain mode additionally requires a clean, linear task range and one signing operation per commit; an expired signing-agent cache can make the whole rewrite fail and restore.

Using a host signing agent avoids storing a passphrase, but introduces an operational gate: a locked, missing, or unknown signing key stops publication. Unlocking happens through a trusted host interaction, not through an agent prompt or task process. This is less convenient than passing a secret to the daemon or sandbox and deliberately so.

Agent-generated publication text remains useful because the agent has task context, yet drafting is non-authoritative and may fail or require manual replacement. The control plane must keep final repository routing, identity selection, signature policy, and external side effects independent of the draft channel.

This ADR does not resolve which trusted publishing identity should be the default, how human delegation is represented, or how review and publication provenance survives cleanup. Those policies must preserve this boundary when reconciled: identities may be operator, bot, or narrowly delegated service identities, but their credentials and privileged actions cannot move into the agent sandbox.

The decision should be revisited if review tooling can consume an immutable range without temporary Git positioning, if publishing moves to a separate trusted service, or if a capability broker can safely expose a narrower operation than the current host process. Such changes may reduce host-side mechanics, but they must preserve independent review, trusted destination selection, credential non-disclosure, and fail-closed verification.

## Alternatives considered

### Let the agent run review and report the verdict

Running review inside the existing session would reduce daemon process management and make feedback easy to consume. It was rejected because the reviewed party would control the command invocation and the path by which exit status and output reach the control plane. A compromised agent or repository process could omit part of the diff, fabricate approval, or alter review output. Review may involve an independent agent as a declared workflow participant, but the acceptance result and any human review tool remain supervised by the trusted control plane.

### Give the sandbox signing and forge credentials

Direct credentials would let the agent commit, push, and open a pull request with fewer host-side steps. It was rejected because arbitrary repository code and agent tools would then share the authority to alter remote repositories and forge state. Scoping a token to one repository reduces blast radius but does not prevent unintended pushes, pull requests, comments, or credential disclosure within that repository. Narrow capability brokers may replace direct host commands later, but raw credentials and general publication authority remain outside the sandbox.

### Publish agent checkpoint commits unchanged

Shipping existing commits avoids rewriting cost and preserves their exact hashes. It was rejected because checkpoints are created inside the untrusted environment, may be unsigned, may carry the wrong author or committer identity, and may not correspond to the complete reviewed delta. Squash mode creates one trusted commit; retain mode preserves messages and structure while rewriting identity and signatures, then verifies count and signatures before push.

### Keep review and publishing manual outside Ompire

Manual terminal commands naturally keep credentials away from the sandbox and were the state before automation. They were rejected as the managed path because they cannot reliably enforce that the reviewed delta is the published delta, make restoration and destination checks operator memory, and leave workflow state disconnected from privileged side effects. Manual intervention remains a recovery mechanism, not the architectural authority path.

### Pass a signing passphrase through the daemon

Loopback pinentry or a UI-supplied passphrase could unlock signing without a separate host action. It was rejected because the secret would transit additional process memory and could be exposed through logs, errors, prompts, or future state handling. Ompire instead uses the host credential agent's cache and stops when the selected key is unavailable; a future broker must provide an operation, not disclose the secret behind it.

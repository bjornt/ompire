# ADR 0017: Use a dedicated bot as the default publishing identity

- Status: Proposed
- Date: 2026-08-23

## Context

Ompire performs publication as an automation system: it rewrites or creates
commits, signs them, pushes branches, opens pull requests, and may later act on
reviews or tickets. Those actions need an identity for two distinct purposes.
An authorization identity determines whose delegated authority permits the
action; an execution identity appears as the Git author, committer, signer, and
forge actor. Treating those identities as incidental process configuration
makes automated work look like direct human work and leaves recovery unable to
prove which authority an interrupted operation was meant to use.

The trusted-control-plane boundary is already settled. Commit signing, push,
and forge operations run outside the agent sandbox, and the agent never
receives their credentials. Identity selection must preserve that boundary. It
must also work for supervised runs, where an operator approves a transition,
and unattended runs, where the allowed identities and capabilities are fixed
before execution. Human approval of an automated action does not by itself
make the human the action's execution identity.

The current ship path does not model a publishing identity. It inherits the
Git name and email from the task clone, resolves a signing key from daemon or
Git configuration, uses the host's Git credential resolution for push, and
invokes the configured forge client with its ambient authentication. Normal
feature documentation consequently describes operator-authored commits. The
QA deployment can launch the same daemon under a dedicated bot's Git, signing,
SSH, and forge credentials, demonstrating that a bot is operationally
possible, but this is a deployment-wide environment convention rather than an
explicit policy or durable task fact. Nothing ensures that commit, push, and
pull-request identities agree or that restart recovery preserves the identity
originally authorized for an operation.

The durable product direction instead makes a dedicated bot the default for
automated commits and forge activity. It permits a narrow exception when
remediating an existing pull request after a trusted human review request and
project policy delegates use of that human's identity for the existing branch.
The current implementation and current feature documentation do not enforce
that default or represent the exception. This ADR records the proposed
resolution and remains proposed until identity policy, credential selection,
publication, and durable provenance agree on it.

## Decision

Ompire uses a dedicated automation identity as the default execution identity
for publishing. For GitHub, that identity is a dedicated bot account; other
forges use an equivalent service identity. Its credentials are scoped to the
repositories and operations required by project policy and remain in the
trusted control plane or a narrow credential broker.

The effective publishing identity is selected by trusted policy before a run
may perform authority-bearing work. It is part of the run's immutable execution
snapshot, not an agent input, a value inferred late from ambient host state, or
an unrestricted choice at the publish button. A supervised approval records
the human as authorizing actor while the bot remains the execution identity. An
unattended run may publish only through an identity and capability set that was
pre-authorized for that run.

By default, all parts of one publication use the selected automation identity:
Git author and committer metadata, commit signature, push authentication,
pull-request creation, and subsequent automated forge comments or state
changes. The control plane validates that the configured Git, signing, and
forge credentials resolve to the policy-selected identity before the first
external effect and fails closed on a missing, ambiguous, or inconsistent
identity. Credential unavailability never causes an implicit fallback to the
operator or another ambient host identity.

A project policy may delegate a human operator identity only to remediate an
existing pull request when a trusted human reviewer requested the change and
the policy permits that delegation for the specific project, pull request, and
branch. The delegation does not authorize new repositories, branches, pull
requests, or unrelated forge actions. The trusted control plane performs the
commit and push; the agent still receives no credential. The automation
identity posts or owns the accompanying automated reply so reviewers can see
that Ompire performed the work. Any broader exception requires a new
architectural decision rather than an unrecorded configuration override.

For every privileged publication operation, Ompire durably distinguishes the
authorizing actor, the selected execution identity, and the agent or session
that produced the proposed change. Recovery reuses that recorded identity and
operation intent or stops for reconciliation; it does not repeat an external
effect under a different identity. Git and forge results retain enough identity
and lineage to explain the automation after task-workspace cleanup or history
rewriting.

The invariant is that automated publication is visibly attributable to a
least-privilege automation identity by default. Human identity use is explicit,
pre-authorized, narrowly scoped, durably attributable, and executed outside the
agent sandbox; ambient credentials and agent requests never select or expand
publishing authority.

## Consequences

Commits, pull requests, and forge activity identify Ompire as automation rather
than implying that the local operator performed the work directly. Human
approval remains visible as authorization provenance instead of being
conflated with Git authorship or forge execution. Reviewers and repository
owners can apply bot-specific branch rules, permissions, filtering, and audit
policy consistently.

A dedicated identity narrows the impact of credential compromise relative to a
human account with unrelated repository access. Each deployment must still
provision and maintain the bot account, repository grants, signing key, push
credential, forge token, and any service identities for other integrations.
Tokens expire, keys rotate, accounts can be suspended, and branch protections
may treat bots differently. Those conditions stop affected publication until
the selected identity is restored; they do not justify falling back to the
operator.

Identity becomes explicit control-plane state and policy rather than a side
effect of how the daemon was launched. Projects need an allowed automation
identity and narrowly stated exceptions. Runs need an immutable identity
snapshot. Publication preflight must verify correspondence among Git metadata,
signing keys, transport credentials, and forge accounts without disclosing
secrets. Durable history must record authorization, execution, and resulting
external identities. This adds schema, configuration, credential-broker,
recovery, and operator-setup work.

Existing installations that publish with ambient operator credentials require
a deliberate migration. They may continue to describe current behavior while
this ADR is proposed, but they do not satisfy the decision merely because the
operator happens to use a bot in a shell wrapper. Migration must provision the
bot, grant least privilege, select it through trusted policy, make identity
checks fail closed, and persist it before enabling the new default. Existing
published commits are historical facts and are not rewritten solely to change
attribution.

The human-remediation exception supports repositories where an existing branch
is owned by a human or policy requires the reviewer-requested fix to retain
that identity. It also increases policy and audit complexity. The control plane
must authenticate the review request as trusted, constrain the exact branch and
pull request, record the delegation, and prevent that credential from becoming
a general interactive capability. If those facts cannot be established, the
operation uses the bot where permitted or stops.

This decision depends on durable authority-bearing provenance for reliable
recovery. Until that record exists, a daemon failure can leave uncertainty
about which identity performed an external effect. Implementations must stop on
such ambiguity rather than infer identity from the current host environment or
retry under newly available credentials.

The decision should be revisited if a forge provides a first-class workload
identity that offers stronger per-run attribution and capability scoping than a
long-lived bot account, or if project policy can represent automated authorship
without a separate account while preserving least privilege and visible
attribution. A replacement must still separate authorization from execution,
keep credentials outside the sandbox, fix authority before execution, prevent
implicit fallback, and preserve durable provenance.

## Alternatives considered

### Use the operator's identity for all publication

Using the local operator's existing Git, signing, and forge configuration keeps
setup small and matches the current documented ship path. The operator is also
a natural approver in supervised use. It was rejected as the default because
automated work then appears human-authored, unattended runs inherit a person's
often broader authority, credential rotation and offboarding are coupled to a
human account, and approval is confused with execution. A human identity
remains available only through the narrowly scoped remediation delegation.

### Use a bot for every operation with no human delegation

One automation identity for every commit, push, and forge action is simpler to
reason about and gives the clearest attribution. It was rejected as an absolute
rule because remediation of an existing human-owned pull-request branch may be
permitted only through explicit delegation of that human's push identity. The
exception is intentionally narrower than a configurable alternative default:
it is tied to a trusted review request, existing pull request, project, and
branch, while the bot retains visible automation attribution in the forge.

### Let the agent publish under its own identity

An identity per agent or session could make the immediate producer visible and
avoid rewriting authorship. It was rejected because the producing agent and
repository code are inside the untrusted sandbox. Giving that environment
signing, push, or forge credentials would collapse the independent publishing
boundary and let untrusted input expand remote authority. Ompire records the
producing session as provenance while the trusted control plane publishes under
the selected automation identity.

### Choose from ambient credentials at publication time

Inheriting whichever Git, SSH, signing-agent, and forge credentials are active
makes local deployments flexible and already allows an operator to launch the
daemon as a bot. It was rejected as the architecture because the selected
identity is not explicit, components can resolve to different principals, a
restart can change the effective identity, and unattended authority cannot be
shown to have been pre-authorized. Ambient mechanisms may implement access to a
selected identity, but they cannot define policy or provide an implicit
fallback.

# ADR 0023: Admit `checkout_root` as a bounded daemon-writable setting

- Status: Accepted
- Date: 2026-08-28

## Context

[ADR-0013](0013-layer-daemon-writable-settings-over-operator-configuration.md) allows a small allowlist of preferences to be edited through the web UI and stored as registry overrides, and draws an explicit line around that allowlist. A setting is eligible only if changing it through the authenticated local control plane does "not alter the daemon's security, identity, credential, filesystem, network-listening, or process-launch boundary". Startup and infrastructure settings stay operator-owned and TOML-only "unless a later architectural decision deliberately makes a specific setting daemon-writable". [ADR-0021](0021-admit-signing-key-selection-as-bounded-daemon-writable-setting.md) is the first such decision, for the signing key.

`checkout_root` names the parent directory a project's checkout path is derived from. Before [ADR-0022](0022-create-or-adopt-base-checkouts-without-mutating-them.md) it only supplied a default string for a row nobody validated, so it was inert. It is not inert now: clone mode derives its destination from this value, and the daemon creates a directory there. That makes it a filesystem-boundary setting, squarely inside ADR-0013's clause, and it cannot be added to the allowlist silently. This record is the deliberate decision ADR-0013 requires.

The operational problem is small but real, and it is exactly the problem clone mode creates. Clone mode's destination is derived rather than supplied, which is what bounds the daemon's new authority to create a repository on the host. The cost of that bound is that the operator has no way to say where — unless the root itself is something they can set. Leaving it in `config.toml` means an operator who keeps repositories somewhere other than `~/proj` must edit a file and restart the daemon before they can use the feature at all, and must discover that requirement from documentation rather than from the form that is about to create the directory.

Choosing a parent directory is not the same kind of act as supplying a credential or naming a binary. It does not introduce a capability, reach a secret, or change what the daemon executes. It selects where one already-decided operation puts its output, within a filesystem the operator already controls.

## Decision

Ompire admits `checkout_root` to the daemon-writable settings allowlist, bounded so the exception does not generalize.

An override must be an absolute path once `~` is expanded, must contain no `..` segment, must not be the filesystem root, and must be neither the daemon's task root nor inside it. The task-root exclusion is the substantive one: task cleanup deletes inside that root ([ADR-0006](0006-give-every-task-a-separate-clone-and-workshop.md)), and a base checkout placed there would be destroyed by ordinary task teardown. The stored value is the normalized path, and an invalid value is rejected before anything is written.

Resolution layers as `registry override → config.toml → the value already resolved on the daemon's configuration object`. The bottom layer is deliberately not a second hard-coded constant: startup already turns "the operator's `config.toml` value, or the product default `~/proj`" into one path, and the settings store must agree with the daemon about which path that is rather than compute its own.

A change applies to the next clone-mode registration. It never moves a checkout, never rewrites a stored `checkout_path`, and never invalidates a project that already exists. A project's checkout path is captured at registration and is a fact about that project from then on.

The setting selects a destination for an operation whose authority is already bounded by ADR-0022 — derived path, refusal of any pre-existing target, staging and rename, no deletion of a base checkout. It does not widen that authority. It may not be read as precedent for making other path, command, or credential settings web-editable.

The invariant is that the UI may choose where a bounded, already-authorized filesystem operation puts its result, within paths that cannot collide with the daemon's own deletion territory. It may not choose what that operation is, what it executes, or what it may remove.

## Consequences

An operator who keeps repositories in `~/src` or `/srv/code` can point clone mode there from the form's own settings page, and see which layer the current value comes from. On a host where `~/proj` is fine, nothing needs configuring, and `config.toml` remains the way to fix the value for a reproducible setup.

The blast radius of a compromised browser session or bearer token grows by exactly one directory choice: it could cause the next cloned checkout to land somewhere unexpected under the operator's account. It cannot cause a deletion, because clone mode refuses a pre-existing destination and never removes a base checkout, and it cannot reach the task root, because the validator excludes it. That residual capability is real and is the price of the exception.

Because the change applies only forward, an operator who moves the root will have projects whose checkouts live in two places. That is the honest outcome — the alternative is Ompire moving repositories on the host — but it does mean the effective root is not a reliable way to find every checkout. The project's own `checkout_path` is.

The bottom-layer rule adds a small asymmetry to the settings store: one key resolves its default from the configuration object rather than from the defaults table. That is deliberate and worth the irregularity, because the alternative is two independently-computed answers to "where do checkouts go" that agree only by convention.

This decision should be revisited if checkout location becomes a per-project rather than per-daemon choice, if Ompire becomes multi-user and settings need scoping, or if project setup grows authority to modify or remove checkouts — in which case the bound described here would no longer be sufficient on its own. Any replacement must keep the value validated before storage, keep it disjoint from paths the daemon deletes in, and keep existing projects' paths stable across a change.

## Alternatives considered

### Keep `checkout_root` TOML-only

Leaving the setting where it was would have preserved ADR-0013's line with no amendment, and clone mode would still work with its default. It was rejected because it makes the product's answer to "put it somewhere else" a file edit plus a daemon restart, discovered only from documentation, at the exact moment the operator is looking at a form that shows the destination. The boundary ADR-0013 protects is authority, and this setting does not add any: the directory-creating operation exists either way.

### Let clone mode accept a destination path per project

Accepting a path in the create form would have made the setting unnecessary and given the operator maximum flexibility. It was rejected in ADR-0022 for the reason that applies here too: it hands the daemon's one filesystem-creating operation an unbounded, client-supplied target. A validated root that many projects share is a far smaller surface than an arbitrary path per project, and it is the thing an operator actually wants to decide once.

### Allow any absolute path as the root

Validating only absoluteness would have been simpler and would still have rejected the obviously broken values. It was rejected because a root inside the task root is not obviously broken — it looks like tidy organization — and would place base checkouts where task cleanup deletes. The failure would appear as a repository vanishing during ordinary teardown, which is exactly the kind of destructive surprise the exclusion is cheap to prevent.

### Make the task root web-editable at the same time

The task root is the other path setting, and admitting both together would have looked consistent. It was rejected because they are not comparable: the task root is where the daemon *deletes*, and changing it while tasks exist would strand live workspaces and point teardown at a new location. Eligibility is per setting, as ADR-0013 requires, and this record admits one.

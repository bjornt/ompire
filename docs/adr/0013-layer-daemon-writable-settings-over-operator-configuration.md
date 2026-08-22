# ADR 0013: Layer daemon-writable settings over operator configuration

- Status: Accepted
- Date: 2026-08-17

## Context

Ompire has two distinct configuration authorities. The operator owns startup and infrastructure configuration, including values that determine where and how the daemon runs. The daemon also exposes a small set of user preferences that must be editable through the web UI, take effect while the daemon is running, and survive restart. Some preferences already had equivalents in operator configuration, while others existed only as built-in behavior.

Making one store serve both roles creates unsafe ownership. Rewriting the operator's TOML would require the daemon to parse and render a human-maintained file without losing comments, formatting, or constructs it does not own. A failed or partial write could also make the daemon's next startup fail. Conversely, keeping UI changes only in memory would make the interface misleading because preferences would disappear on restart.

Using two stores introduces its own ambiguity unless precedence and scope are explicit. The system must distinguish a value explicitly supplied by the operator from a built-in default, define what removing a UI override means, and prevent the UI from gaining control over infrastructure settings merely because both kinds of value pass through the same configuration object at startup.

Ompire already has an owner-private, migrated registry for daemon-managed state. It provides transactional persistence and an established backup and recovery boundary without adding another operator-facing configuration file. The settings implementation was completed and verified on 2026-08-17. The implementation and archived design agree on the ownership boundary and precedence, so this ADR backfills that accepted decision using the recorded completion date.

## Decision

Ompire stores daemon-writable settings as overrides in its local registry and resolves every eligible setting in this order:

1. registry override;
2. an explicit value in the operator's TOML configuration;
3. the built-in default.

The daemon never rewrites the operator's TOML configuration. Removing a registry override reveals the next available lower layer rather than copying or synthesizing a replacement value. Resolution reports each effective value's provenance so an operator can tell whether the value comes from an override, operator configuration, or a default.

Only an explicit, centrally defined allowlist of preference keys may have registry overrides. Each key has control-plane validation, and a multi-key update is validated in full before any value is persisted. Unknown keys and invalid values are rejected. Registry values remain simple typed scalars; this mechanism is not a general-purpose document store or an alternate path for arbitrary daemon configuration.

Startup and infrastructure settings remain operator-owned and TOML-only unless a later architectural decision deliberately makes a specific setting daemon-writable. Eligibility requires that the setting be safe to change through the authenticated local control plane, have defined live-application and restart semantics, and not alter the daemon's security, identity, credential, filesystem, network-listening, or process-launch boundary merely through a UI preference update.

The same layered resolution governs startup and subsequent reads. Live consumers receive the effective values, not raw overrides, so restarting the daemon does not change precedence or interpretation. The registry remains inside the same owner-private storage and migration boundary as other local control-plane state.

The invariant is that the operator's configuration file remains untouched and authoritative whenever no registry override exists, while daemon-writable preferences remain durable and take precedence only within their explicit allowlist.

## Consequences

Operators can seed preferences in TOML for reproducible setup and then adjust eligible values through the UI without editing files or restarting the daemon. UI changes survive restart, and deleting an override predictably restores the operator's value or the product default. Provenance makes this layering visible instead of presenting an effective value as though it had only one authority.

The daemon cannot corrupt startup configuration while applying a preference. Comments and formatting remain under operator control, and infrastructure settings cannot accidentally become web-editable. The allowlist also limits the effect of a compromised browser session or bearer token: it may change exposed preferences, but this settings mechanism does not grant control over bind addresses, paths, credentials, external commands, or other TOML-only infrastructure. This does not remove the need to protect the registry, token, and local account boundary.

The registry becomes part of preference backup and recovery. Restoring only TOML intentionally loses UI overrides and falls back to operator values or defaults; restoring the registry restores those overrides. Losing or deleting an override is therefore recoverable without making the daemon unstartable, but operators who require exact preference continuity must back up both stores.

The cost is a more complex read model. Maintainers must preserve explicit-source information from TOML rather than confusing a configuration object's already-filled default with an operator choice. Every newly daemon-writable setting needs a default, validator, provenance, persistence behavior, live-application semantics, and tests for precedence and deletion fallback. A value supported by TOML is not automatically eligible for a registry override.

Two visible authorities can surprise an operator when a registry override masks a later TOML edit. Provenance and explicit override deletion are therefore part of the contract. The system must not silently copy effective values into the registry, because doing so would turn lower-layer changes into stale, unexplained overrides.

Registry migrations and writes add a small operational dependency to settings management. A malformed registry value indicates a control-plane bug or external database tampering rather than user input and may prevent settings resolution; writes must continue to be validated and transactional. Built-in defaults and TOML remain the safe fallback when overrides are absent.

This decision should be revisited if Ompire becomes a multi-user service requiring scoped settings and authorization, if configuration must be distributed across hosts, or if operators require one declarative source that can reproduce all daemon-managed state. Any replacement must retain a clear ownership boundary, deterministic precedence, safe recovery, and protection against UI changes corrupting startup configuration.

## Alternatives considered

### Rewrite the operator's TOML configuration

Writing UI changes directly to TOML would provide one persistent file and avoid layered reads. It was rejected because the daemon would take ownership of a human-maintained artifact, could destroy comments or formatting, and could render its own next startup invalid. It would also blur the boundary between preferences and infrastructure configuration.

### Add a separate settings TOML file

A daemon-managed or preference-specific TOML file would keep the main configuration untouched. It was rejected because it creates another operator-facing file to document, secure, back up, and order relative to the main configuration. The registry already provides daemon-owned persistence and migrations, so an additional file adds authority without adding capability.

### Keep UI settings only in memory

In-memory values would be simple to apply live and would avoid persistent schema. It was rejected because preferences would disappear on daemon restart, making the UI state non-durable and recovery behavior surprising. Durable user preferences belong in the daemon-owned registry.
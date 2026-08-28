# ADR 0021: Admit signing-key selection as a bounded daemon-writable setting

- Status: Accepted
- Date: 2026-08-28

## Context

[ADR-0013](0013-layer-daemon-writable-settings-over-operator-configuration.md) allows a small allowlist of preferences to be edited through the web UI and stored as registry overrides. It draws an explicit line around that allowlist: a setting is eligible only if changing it through the authenticated local control plane does "not alter the daemon's security, identity, credential, filesystem, network-listening, or process-launch boundary". Startup and infrastructure settings stay operator-owned and TOML-only "unless a later architectural decision deliberately makes a specific setting daemon-writable".

`gpg_signing_key` names the OpenPGP key the daemon signs published commits with. It selects a publishing identity, so it falls inside that clause and cannot be added to the allowlist silently. This record is the deliberate decision ADR-0013 requires.

The operational problem is concrete. Before this decision the signing key could only be named in `config.toml`, which is read once at startup, so choosing or changing a key required editing a file and restarting the daemon. The probe also had no way to enumerate keys: it resolved one configured identifier, and if that identifier was absent or ambiguous the operator saw an undifferentiated failure. An operator whose host holds more than one signing key had no way to see the choices, and no way to make the choice where the consequence of it is visible.

Choosing a signing key is not the same kind of act as supplying a credential. Every candidate is a key the operator's own host keyring already holds, placed there by the operator outside Ompire. Selecting among them changes which of the operator's existing identities signs; it cannot introduce a key, move signing off the host agent, or disclose secret material. The GPG agent still holds the passphrase and still performs every private-key operation. That asymmetry is what makes a bounded version of this setting admissible where a general "identity settings are web-editable" rule would not be.

There is a second, sharper reason the selection must be explicit and trusted. Signing happens inside the per-task clone, which is writable by the agent under review. Git resolves the signing key, the signature format, and — most dangerously — the signing *program* from that clone's local configuration. A `git commit -S` with no explicit key therefore lets untrusted repository content choose the signing identity, and a clone-local `gpg.program` makes the daemon execute an arbitrary binary on the host, outside the sandbox, under the operator's account. A signing key that the control plane resolves but does not pass through to Git is not actually the key that signs.

## Decision

Ompire admits `gpg_signing_key` to the daemon-writable settings allowlist, bounded so that the exception does not generalize.

An override may only name a key the daemon has itself enumerated from the host keyring as signing-capable and usable. The stored value is a full OpenPGP fingerprint, because only a fingerprint identifies exactly one key; key IDs and user-ID substrings remain a `config.toml` convenience for a human-written file, not a storable selection. Membership is validated against a live enumeration before any value is persisted, and a rejected selection persists nothing.

The daemon enumerates candidates and classifies the selected key using only non-prompting GPG queries. It publishes public identifiers — fingerprint, key ID, user ID, keygrip, validity dates — and never secret key material, a passphrase, or an agent socket path. Resolution layers as `registry override → config.toml → git config user.signingkey → automatic detection`, and automatic detection selects a key only when exactly one usable candidate exists. Several usable candidates with no selection is a distinct, named state that refuses to guess.

A selection that no longer resolves to a usable key fails closed and names the missing key. The daemon does not fall back to another key, because silently signing as an identity the operator did not choose is worse than refusing.

The resolved key is passed explicitly to every signing operation, together with the signature format and the signing program, which are read from operator-owned Git configuration rather than from the task clone. Signature verification runs under the same trusted configuration. After signing and before any push, the daemon verifies that every commit it produced carries a signature made by the selected fingerprint.

The invariant is that the web UI may choose among identities the host already holds, under a validated bound, and that the chosen identity is the one that demonstrably signs. It may not introduce an identity, reach a credential, relocate signing, or widen the settings allowlist to other identity or credential values by precedent.

## Consequences

An operator can see which key will sign, choose among several, and change that choice without editing a file or restarting the daemon. Because the daemon auto-detects a lone usable key, the common single-key host needs no configuration at all. Ambiguity becomes an explicit, actionable state instead of an opaque failure, which is what the product's attention model asks of every stop.

The security boundary is narrower than before this decision, not wider. Passing the key, format, and program explicitly removes a path by which agent-writable repository content could redirect the signing identity or execute a chosen binary on the host, and post-signing verification turns the intended identity into a checked one. The settings surface gains one identity-selecting key, but that key's blast radius is bounded by the host keyring: a compromised browser session or bearer token can change which of the operator's own keys signs, and nothing more. That residual capability is real and is the price of the exception.

The cost is a probe that must do more work and more honest classification. It enumerates keys on every check, distinguishes an absent tool from an unreachable agent from a cold cache from an unprotected key, and must not prompt on any of those paths. Colon-record and agent-response parsing become behavior the project has to keep correct against real GnuPG rather than a single cached flag.

Validation is split across two layers, and deliberately so. The settings store validates the fingerprint's form and stays free of subprocess dependencies; the REST boundary validates membership against the live keyring. A key can still be removed between validation and use, so the ship gate re-probes immediately before committing and refuses an unresolvable selection.

This decision should be revisited if signing moves behind a capability broker that can name an operation without naming a key, if Ompire becomes multi-user and settings need scoping and authorization, or if publishing identity policy ([ADR-0017](0017-use-dedicated-bot-as-default-publishing-identity.md)) makes the signing identity a per-project rather than per-daemon choice. Any replacement must keep selection bounded to identities the host already holds, keep credentials out of the control plane, and keep the signed result verifiable against the intended identity.

## Alternatives considered

### Keep the signing key TOML-only

Leaving `gpg_signing_key` in `config.toml` would have preserved ADR-0013's line without amendment. It was rejected because it cannot express the problem: with several usable keys the operator must discover the identifiers themselves, outside the product, and every change costs a file edit and a restart. It also leaves the daemon unable to tell "no key" from "several keys" from "wrong key", which is precisely the diagnosis the operator needs at the moment shipping stops. The boundary that matters is credential access, and this setting does not cross it.

### Accept any operator-supplied key identifier as the override

Storing whatever string the UI submitted, and letting GPG resolve it at signing time, would have been simpler and would match `config.toml`'s flexibility. It was rejected because an unvalidated identifier can silently match a different key later, or match several, and the failure would surface as a signature by an unexpected identity rather than as a refusal. Binding the stored value to an enumerated fingerprint makes the selection mean one key at the moment it is made and keeps the exception bounded to the host keyring.

### Auto-select a key when the keyring holds several

Picking the newest, or the first usable candidate, would have removed the ambiguous state and made shipping work without operator input. It was rejected because the signing identity is attribution: it appears on published commits and in the forge's verification badge. Guessing it would violate the requirement to fail closed when authority is ambiguous, and the operator would discover the wrong choice only after commits carrying it had landed.

### Let the daemon hold the passphrase so any key could be used unattended

Storing or forwarding a passphrase would have removed the locked state entirely and made key choice inconsequential. It was rejected for the reasons [ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md) already records: the secret would transit additional process memory and could be exposed through logs, errors, or future state handling. Ompire selects among keys the agent already holds and stops when the selected one is unavailable.

### Trust the task clone's Git signing configuration

Reading `user.signingkey`, `gpg.format`, and `gpg.program` from the clone, as `git commit -S` does by default, would have required no explicit arguments. It was rejected because the clone is written by the untrusted agent. That configuration can redirect the signing identity, change the signature format, and name the program Git executes on the host — the last of which is arbitrary code execution outside the sandbox under the operator's account. The control plane supplies all three and verifies the result.

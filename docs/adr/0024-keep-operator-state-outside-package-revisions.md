# ADR 0024: Keep operator state outside package revisions

- Status: Accepted
- Date: 2026-09-03

## Context

Ompire ships as a classic snap that runs a per-user daemon. Snapd gives an
installed snap two per-user directories: `$SNAP_USER_DATA`, scoped to the
installed revision at `~/snap/<name>/<revision>`, and `$SNAP_USER_COMMON`,
shared by every revision at `~/snap/<name>/common`.

The daemon's data directory holds the entire control-plane store — the SQLite
database and its write-ahead-log sidecars, the bearer token, and the audit
log. [ADR-0005](0005-persist-local-state-with-sqlite-core-and-alembic.md)
makes that store the single logical unit an operator protects and recovers.

The daemon originally defaulted to `$SNAP_USER_DATA`. Snapd copies that
directory forward on refresh, so ordinary upgrades appeared to work, and the
problem stayed invisible. The location is nonetheless a function of the
installed revision, and the copy-forward hides three consequences of that:

- `snap revert` points the daemon at the previous revision's copy. Everything
  done since the upgrade is silently replaced by older state — no error, no
  warning, an emptier UI.
- Snapd retains a bounded number of revisions (two by default). Pruning a
  revision deletes the copy of the store that revision owned.
- Every refresh duplicates the whole database.

Product principle 5 in [`VISION.md`](../VISION.md), *Work is durable and
resumable*, requires that a run's meaning outlive the process that produced
it, and the Reliability and operations section makes the daemon the source of
truth for orchestration state. Whether a task's history survives should not
depend on which revision happens to be current, or on the operator not running
`snap revert`.

Installs already exist that hold their store in a revision directory, so
choosing a different location also has to decide what happens to theirs.

## Decision

The location of the operator's store is a function of the user and the snap,
never of the installed revision.

- Under the snap the data directory is the revision-independent per-user
  common directory, `$SNAP_USER_COMMON`. The revision directory is not a
  storage location.
- `$SNAP_USER_DATA` remains only as a fallback for a snapd context that does
  not export the common directory, so the daemon never silently relocates its
  store to the host's XDG directory while running inside a snap.
- On startup, before schema migration, the daemon carries an existing
  revision-scoped store into the common directory exactly once. It does so
  only when the common directory is the effective data directory — never a
  directory the operator named in `config.toml` — and holds no database, and
  the revision directory does.
- The carry moves the database together with its write-ahead log, the bearer
  token verbatim, and the audit log. The database is placed last, so a store
  is never half-carried: an incomplete destination must not be able to look
  complete on the next start.
- The source is never modified or deleted. It is the operator's fallback copy.
- A carry that cannot complete is a startup failure naming both directories.
  The daemon does not substitute an empty database for the operator's own.

The invariant is that no start of the daemon may present an empty store where
a previous install's store exists, and that no packaging operation —
refresh, revert, or revision pruning — is a data event.

## Consequences

Revert and revision pruning stop being able to destroy or silently substitute
operator state. Refresh no longer duplicates the database. The operator has
one directory to back up whose path does not change, which is what
[ADR-0005](0005-persist-local-state-with-sqlite-core-and-alembic.md)'s
"one logical unit to protect and recover" needs in order to mean anything in a
packaged install.

The carry-forward is permanent startup code. It has to keep working for as
long as any install might still hold a revision-scoped store, which in practice
is indefinitely, because an operator can refresh from an arbitrarily old
revision. The cost is bounded: three guard conditions, and it disables itself
the moment the destination holds a database.

Correctness of the carry rests on there being no concurrent writer, which
holds because snapd stops the old user daemon before starting the new one and
the carry runs before this process opens the database. A future packaging
change that let two daemons share one data directory would invalidate that
assumption and this decision would need revisiting.

Retaining the source means a store that was carried exists in two places until
the operator removes one. That is deliberate: the fallback matters most
immediately after an automated move of the operator's database, which is
exactly when it is least proven. It also means an operator who reverts to a
daemon predating this decision still finds state where that daemon looks for
it, at the cost of the two diverging from that point.

Operators who already worked around the revision-scoped store by setting an
explicit `data_dir` keep their directory and get no carry-forward. A directory
the operator named is theirs, and writing another install's state into it
would be a worse failure than doing nothing.

## Alternatives considered

### Keep `$SNAP_USER_DATA` and rely on snapd's copy-forward

The copy-forward genuinely does preserve state across a normal refresh, which
is the common case, and it requires no code at all.

Rejected because it treats a copy as a substitute for a stable location. It
leaves `snap revert` able to silently swap the operator's database for an
older one and leaves revision pruning able to delete a copy, and neither is
something the operator is warned about or can reasonably predict. A store the
packaging layer moves on the operator's behalf is not a durable store; it is
one that has been lucky.

### Use the host XDG directory even under the snap

This would give snap and non-snap installs one location and delete the whole
question, along with the carry-forward code.

Rejected because it puts snap-managed state outside the snap's own per-user
tree, where `snap remove` and `snap save` do not see it — an operator removing
the snap would leave the store behind, and a snapshot of the snap would not
contain it. It also collides with a from-source install of the same daemon on
the same account, which contributors routinely run.

### Use the system-wide `$SNAP_COMMON`

`/var/snap/ompire/common` is equally revision-independent and would survive
the same operations.

Rejected because the daemon is a per-user service and the store is
operator-private: ADR-0005 restricts the database and its sidecars to the
operator account. A system-wide directory would either mix two operators'
control planes or need per-user subdirectories underneath it, reinventing what
`$SNAP_USER_COMMON` already provides with the right ownership.

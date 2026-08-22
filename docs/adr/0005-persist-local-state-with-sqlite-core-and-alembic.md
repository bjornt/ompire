# ADR 0005: Persist local control-plane state in SQLite using SQLAlchemy Core and Alembic

- Status: Accepted
- Date: 2026-07-18

## Context

Ompire is a single-operator local daemon that must retain control-plane state across browser disconnects and daemon restarts. Projects, task identity and execution state, workflow records, named sessions, templates, settings, and publishing state need transactions and schema constraints, but the expected workload is modest and does not justify operating a separate database service. Keeping this state in one local database also gives installation, access control, backup, and recovery a single boundary.

The daemon serves concurrent command, observation, workflow, and recovery activity. SQLite fits the deployment model, while write-ahead logging allows readers to continue while the single writer commits. The database and its journal files contain prompts, repository locations, workflow outcomes, and other operator-private data, so both the database directory and all database files must remain accessible only to the operator.

The schema evolves as the control plane gains durable concepts. Startup must bring an existing database to a schema understood by the running daemon before normal operation begins. Those changes need to be ordered, reviewable, reproducible, and compatible with SQLite's restricted direct schema alteration. Runtime query behavior also needs to remain explicit enough for operator audit without duplicating parameter binding, transaction, and schema-description machinery.

The current daemon implements one owner-private SQLite database, enables write-ahead logging on runtime connections, expresses its schema and queries with SQLAlchemy Core, and applies checked-in Alembic migrations before constructing the running application. The original decision and its dependency tradeoff were recorded on 2026-07-18; this ADR backfills that accepted decision using the recorded acceptance date. It covers the storage mechanism, not which control-plane facts must be durable; changes to the durability boundary require a separate decision.

## Decision

Ompire persists local control-plane state in one owner-private SQLite database and uses the following storage boundary:

- Runtime database access uses SQLAlchemy Core tables and statements. Ompire does not use SQLAlchemy's object-relational mapper.
- SQLAlchemy `MetaData` is the authoritative description of the current application schema used by runtime queries and migration development.
- Every runtime SQLite connection enables write-ahead logging.
- Schema transitions are checked-in, sequential Alembic revisions configured for SQLite batch migrations where direct alteration is unavailable.
- The daemon upgrades the database to the latest reviewed revision during startup, before serving requests or starting control-plane work. It does not create or mutate schema opportunistically from request handling.
- The database directory, main database, and SQLite sidecar files are restricted to the operator account.

The invariant is that a released daemon accesses one local SQLite schema explicitly through SQLAlchemy Core and reaches that release's schema only through reviewed Alembic migrations before normal operation. Schema metadata and the migration chain must agree; neither ORM-generated persistence nor ad hoc runtime DDL may become a second schema authority.

## Consequences

Ompire needs no separately installed or supervised database server. Transactions and explicit schema constraints protect control-plane records in one store. A single database simplifies local lifecycle and gives operators one logical unit to protect and recover. Write-ahead logging lets dashboard reads and background observation proceed without unnecessarily blocking the active writer under the expected workload.

Core table and statement construction keeps joins, updates, transaction boundaries, and stored representations visible in the code while retaining parameter binding and dialect-aware SQL construction. Avoiding the ORM also avoids identity-map, lazy-loading, and object-lifecycle behavior in the trusted control plane. The accepted cost is a larger dependency footprint than direct `sqlite3` use and more explicit row-to-domain conversion in registry code.

Automatic startup migration gives every running process a known schema and removes a manual operator step. A missing, divergent, or failed revision is therefore a startup failure rather than a condition the daemon can ignore. Migrations must preserve existing operator data, account for SQLite's table-rebuild semantics, and be reviewed with the application change that needs them. Autogeneration may assist development, but generated revisions are not authoritative until reviewed and checked in.

SQLite remains a single-writer database. Long transactions and write-heavy features can delay unrelated state changes, so transactions must stay bounded and no architecture should assume horizontal daemon writers. WAL creates sidecar files that are part of the live database's security and recovery boundary; file copying while the daemon is active is not by itself a guaranteed consistent backup procedure.

This ADR does not promise that every authority-bearing event is currently durable. Adding or changing durable records can extend the schema through migrations, but deciding which events, histories, and external side effects must survive restart is a separate architectural concern.

This decision should be revisited through a superseding ADR if Ompire requires multiple concurrent daemon writers, remote or multi-user database access, high write throughput, online failover, or recovery guarantees that a single local SQLite database cannot provide. A replacement must preserve explicit, reviewed schema evolution and an owner-auditable query boundary.

## Alternatives considered

### Python `sqlite3` with hand-written schema and migrations

The standard library driver would remove SQLAlchemy and Alembic from the trusted dependency set and expose executed SQL directly. It was rejected because Ompire's schema is expected to evolve across installed versions. Reimplementing revision tracking, transactional upgrades, SQLite table rebuilds, parameterized query composition, and schema comparison would create project-specific migration machinery with a higher risk of data-loss defects. SQLAlchemy Core and Alembic provide those mechanisms without requiring ORM semantics.

### SQLAlchemy ORM

The ORM could reduce repetitive row mapping and express relationships through domain objects. It was rejected because the control-plane store is small and authority-bearing: explicit statements, selected columns, and transaction scopes are easier to audit than implicit unit-of-work behavior, relationship loading, and identity-map state. SQLAlchemy Core retains the portable statement and migration ecosystem without adding that hidden lifecycle.
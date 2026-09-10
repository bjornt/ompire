"""The shared SQLite write reservation.

One primitive, owned by no domain: `BEGIN IMMEDIATE` up front, so a read made
inside the block cannot go stale before the matching write commits, then
commit or roll back on exit. Every registry mutation that has a check-then-
write pair — profile deletion and project assignment, launch acceptance,
result retention, delivery journaling, workflow transitions — admits through
here rather than growing a second reservation with subtly different rules.

This module is platform code: it imports the standard library and SQLAlchemy
only, and nothing may import product state through it. Keeping it here is what
lets owned modules share one local consistency boundary without depending on
each other (ADR-0038).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from sqlalchemy import Connection, Engine


@contextlib.contextmanager
def reserved_write(engine: Engine) -> Iterator[Connection]:
    """Open a SQLite write reservation, then commit or roll back.

    `BEGIN IMMEDIATE` takes the database's write lock up front, so a read made
    inside this block cannot go stale before the matching write commits. That
    is what keeps profile deletion and project assignment from racing into a
    project whose default names a profile that no longer exists: whichever
    transaction starts first finishes first, and the other sees its result.

    A plain `engine.begin()` is not enough — pysqlite defers `BEGIN` until the
    first DML statement, so a preflight `SELECT` would run outside the
    reservation. The driver's implicit transaction handling is switched off for
    the duration and restored before the connection returns to the pool.

    Deliberately narrow: only the reference check and its write belong inside.
    Filesystem work, git, setup scheduling, and event publication stay outside,
    and this does not turn on SQLite foreign-key enforcement for other tables.
    """
    with engine.connect() as conn:
        dbapi = conn.connection.dbapi_connection
        assert dbapi is not None  # a live Connection always has one
        prior = dbapi.isolation_level
        dbapi.isolation_level = None
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.exec_driver_sql("ROLLBACK")
                raise
            else:
                conn.exec_driver_sql("COMMIT")
        finally:
            dbapi.isolation_level = prior

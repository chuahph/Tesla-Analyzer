"""Database engine, session handling and schema creation."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextvars import ContextVar

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


# Guards the schema migration declined to install, recorded at boot.
#
# _ensure_unique_index refuses rather than fails the boot when the data cannot
# satisfy an index, and said so with print() — which lands in the host's log
# and nowhere a phone can reach. A protection that is silently absent is the
# exact failure shape this app keeps finding in itself, so the refusal is kept
# here and reported by /api/health. Populated per process at init_db, and a
# note is only cleared by a restart, which is also what installing the index
# requires.
SCHEMA_DECLINED: list[dict[str, object]] = []


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    # Render/Heroku-style URLs use the legacy "postgres://" scheme, which
    # SQLAlchemy 2.x no longer accepts — normalise it.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path == ":memory:":
            # Share one connection so every session sees the same in-memory DB.
            return create_engine(
                url, connect_args={"check_same_thread": False},
                poolclass=StaticPool, future=True,
            )
        # Ensure the parent directory exists for file-based SQLite DBs.
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        return create_engine(
            url, connect_args={"check_same_thread": False}, future=True
        )
    # Hosted databases drop idle connections; pre-ping revalidates them.
    return create_engine(url, pool_pre_ping=True, future=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# Temporary instrumentation for the Sep 2026 Neon/Supabase egress investigation.
# Table sizes ruled out an oversized blob (largest table 576 kB) and the
# project has only one branch, so the remaining candidates are that SOME
# query pulls far more rows than its caller needs, or that a write sends far
# more bytes than its caller needs — both invisible from the provider's own
# console, which reports bytes moved but not which statement moved them.
# rowcount alone found the first kind (a missing LIMIT) but is blind to the
# second: an UPDATE that overwrites a large JSON blob reports rowcount=1,
# identical to one that changes a single flag. param_bytes closes that gap —
# an approximation of what's actually sent over the wire for this statement's
# bound values, cheap (str() of what SQLAlchemy already built, no query of its
# own) but sized correctly relative to other statements in the same batch.
#
# Both of those are blind to a third kind: a SELECT's bound params are tiny
# (a vehicle_id, a timestamp) no matter how much data comes back, so a query
# returning thousands of narrow rows — e.g. /api/driving-matrix's 90-day
# Sentry history — looked cheap by every earlier measure while still moving
# real bytes on the wire. result_bytes estimates that side: cursor.description
# gives each column's Postgres type OID without fetching a single row, so a
# fixed-width type (int, bool, timestamp) is sized exactly and a variable one
# (text, json, numeric) gets a conservative flat estimate — not exact, but
# enough to tell "a few KB" from "a few hundred KB" per page load, which
# nothing else here can currently do.
# Safe to leave running. Remove once the heavy one is found.
_QUERY_LOG: ContextVar[list[tuple[int, int, int, str]] | None] = ContextVar("_QUERY_LOG", default=None)

# Postgres type OID -> wire size in bytes, for the fixed-width types worth
# telling apart. Anything not listed (text, varchar, json/jsonb, numeric,
# arrays, ...) is variable-length and falls back to _DEFAULT_COL_BYTES —
# genuinely unknowable without fetching the row, which this must not do.
_PG_FIXED_COL_BYTES = {
    16: 1,     # bool
    20: 8,     # int8 / bigint
    21: 2,     # int2 / smallint
    23: 4,     # int4 / integer
    700: 4,    # float4 / real
    701: 8,    # float8 / double precision
    1082: 4,   # date
    1083: 8,   # time
    1114: 8,   # timestamp
    1184: 8,   # timestamptz
}
# A short state string ("Idle", "Armed"), a name, a small JSON fragment — 20
# bytes undercounts a big blob and overcounts a short flag, but it is the same
# flat guess for every variable-length column, so totals stay comparable
# across queries rather than precise for any one of them.
_DEFAULT_COL_BYTES = 20
# Per-row wire overhead: a 4-byte row length plus a 4-byte length prefix per
# column value, matching libpq's row/column framing closely enough to matter
# on a query that returns thousands of narrow rows.
_ROW_OVERHEAD_BYTES = 4


def _estimate_result_bytes(cursor, rows: int) -> int:
    if rows <= 0:
        return 0
    try:
        description = cursor.description
    except Exception:  # noqa: BLE001 — instrumentation must never break a query
        return 0
    if not description:
        return 0
    row_bytes = _ROW_OVERHEAD_BYTES
    for col in description:
        type_code = col[1] if len(col) > 1 else None
        row_bytes += 4 + _PG_FIXED_COL_BYTES.get(type_code, _DEFAULT_COL_BYTES)
    return row_bytes * rows


@event.listens_for(engine, "after_cursor_execute")
def _log_query_rows(conn, cursor, statement, parameters, context, executemany):
    log = _QUERY_LOG.get()
    if log is None:
        return
    try:
        rows = cursor.rowcount
    except Exception:  # noqa: BLE001 — instrumentation must never break a query
        rows = -1
    try:
        # str() on the whole structure rather than branching on dict vs.
        # tuple vs. executemany's list-of-either: the first version of this
        # branched, and on Postgres it measured 1 byte for an UPDATE that
        # was actually writing a multi-KB JSON blob — the DBAPI-level shape
        # SQLAlchemy hands the event for an ORM-flush UPDATE did not match
        # what local testing against sqlite predicted. str() of the whole
        # object is slower but cannot be fooled by a shape assumption.
        #
        # That fix ALSO still measured 1 byte in production against the
        # exact same statement, which means ``parameters`` itself is not
        # what it's assumed to be here — not a formatting bug, a wrong
        # assumption about what this event hands over. type()+repr() below
        # is a one-shot diagnostic to see the actual object instead of
        # guessing a third time; remove alongside the rest of this
        # instrumentation once that's understood.
        param_bytes = len(str(parameters)) if parameters else 0
        param_shape = f"{type(parameters).__name__}:{executemany}:{parameters!r:.120}"
    except Exception:  # noqa: BLE001 — instrumentation must never break a query
        param_bytes = -1
        param_shape = "ERR"
    try:
        result_bytes = _estimate_result_bytes(cursor, rows)
    except Exception:  # noqa: BLE001 — instrumentation must never break a query
        result_bytes = -1
    log.append((rows if rows is not None else -1, param_bytes, result_bytes,
               f"{statement[:160]} <<{param_shape}>>"))


def _ensure_column(table: str, column: str, ddl_type: str, default_sql: str) -> None:
    """Defensively add a column to an already-existing table if it's missing —
    a minimal stand-in for a migration tool (no Alembic in this project).
    create_all() only creates missing *tables*; it never alters ones that
    already exist, so a column added to a model after a database has already
    been created would otherwise silently never appear there. Only ever
    additive (a new column with a default) — never destructive."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table(table):
        return  # brand new table — create_all() above already gave it every column
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type} DEFAULT {default_sql}"))


def _ensure_unique_index(table: str, name: str, columns: tuple[str, ...]) -> None:
    """Add a unique index, but only once the data can actually satisfy it.

    The drives table has no database-level guard against the same journey
    being written twice, and promotion runs from three places — the dashboard
    read, the telemetry ingest, and the cron. Identity is a read-then-write
    check, which two concurrent requests can both pass, and FastAPI runs these
    endpoints in a threadpool so the concurrency is real. A unique index turns
    that silent duplicate into a failed insert, which every automatic caller
    already rolls back and retries on the next tick.

    Checked before created, and never forced. A duplicate pair already in the
    table would make CREATE fail, and failing the boot over it would be worse
    than the duplicate: the app would be down instead of slightly wrong. So a
    table that cannot take the index keeps its data and says so in the log,
    and /api/data/duplicate-trips is the tool for clearing the way.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table(table):
        return
    if any(ix["name"] == name for ix in inspector.get_indexes(table)):
        return
    cols = ", ".join(columns)
    with engine.begin() as conn:
        dupes = conn.execute(text(
            f"SELECT COUNT(*) FROM (SELECT {cols} FROM {table} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1) d")).scalar() or 0
        if dupes:
            why = (f"{dupes} duplicate {cols} groups — clear them with "
                   f"/api/data/duplicate-trips?apply=true and restart")
            print(f"[schema] {table}: {name} not created. {why}.")
            SCHEMA_DECLINED.append(
                {"table": table, "index": name, "why": why})
            return
        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))


def _drop_column(table: str, column: str) -> None:
    """Remove a column that nothing maps any more.

    The one destructive operation in this file, and deliberately the only one.
    ``_ensure_column`` above is additive because a column added by mistake
    costs nothing and a column dropped by mistake costs the rows in it — so
    this exists only for a field that has already been removed from the models
    and is being cleared out behind that removal, never as a way to change a
    schema that is still in use.

    Idempotent, and safe to run from several workers at once: the column is
    checked first, and Postgres is asked with IF EXISTS on top of that so a
    race between two startups cannot turn into a 500. SQLite has supported
    DROP COLUMN since 3.35; a database old enough to refuse is left as it is
    rather than failing the boot over a column nothing reads.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table(table):
        return
    if column not in {c["name"] for c in inspector.get_columns(table)}:
        return
    guard = " IF EXISTS" if engine.dialect.name == "postgresql" else ""
    try:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN{guard} {column}"))
    except Exception:
        # A drop that cannot happen must not stop the app starting. Nothing
        # reads this column; leaving it costs a little disk and no behaviour.
        pass


def _widen_to_text(table: str, column: str) -> None:
    """Relax a bounded VARCHAR to unbounded TEXT on an existing table.

    The additive-only ``_ensure_column`` above can't do this: the column is
    already there, just too small. Widening is the one ALTER that is always
    safe — every value that fitted before still fits — and on Postgres a
    varchar-to-text change is metadata-only. SQLite never enforced the bound
    in the first place, so it is a no-op there.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table(table):
        return
    if engine.dialect.name != "postgresql":
        return
    current = {c["name"]: c["type"] for c in inspector.get_columns(table)}
    kind = current.get(column)
    if kind is None or getattr(kind, "length", None) is None:
        return  # missing, or already unbounded
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT"))


def init_db() -> None:
    """Create all tables. Models must be imported before calling this."""
    from . import models  # noqa: F401  (registers models on Base.metadata)

    # This run's findings replace the last one's. init_db is idempotent and
    # gets called again after the duplicates are cleared, and a note that
    # outlived its cause would send the reader to fix something already fixed.
    SCHEMA_DECLINED.clear()
    Base.metadata.create_all(bind=engine)
    # Runtime state values outgrew their original 2048-char bound (see
    # models.Setting.value) — and the failure mode was a 500 on every sync,
    # not a truncated string.
    _widen_to_text("settings", "value")
    _ensure_column("drives", "idle_min", "FLOAT", "0.0")
    _ensure_column("drives", "idle_tracked", "BOOLEAN", "FALSE")
    _ensure_column("drives", "start_area", "VARCHAR(120)", "''")
    _ensure_column("drives", "end_area", "VARCHAR(120)", "''")
    _ensure_column("drives", "start_coords", "VARCHAR(40)", "''")
    _ensure_column("drives", "end_coords", "VARCHAR(40)", "''")
    _ensure_column("drives", "tag", "VARCHAR(20)", "''")
    _ensure_column("drives", "cost_override", "FLOAT", "NULL")
    _ensure_column("drives", "energy_estimated", "BOOLEAN", "FALSE")
    _ensure_column("drives", "tail_trim_sec", "FLOAT", "NULL")
    _ensure_column("drives", "start_lost_km", "FLOAT", "NULL")
    _ensure_column("drives", "end_lost_km", "FLOAT", "NULL")
    _ensure_column("drives", "start_recovered_km", "FLOAT", "NULL")
    _ensure_column("drives", "start_park_min", "FLOAT", "NULL")
    _ensure_column("drives", "climate_min", "FLOAT", "NULL")
    # NULL, not the mean: a trip closed before the outside-temperature
    # averaging landed has no second reading, and copying out_temp into it
    # would manufacture a zero bias for trips whose bias is unknown.
    _ensure_column("drives", "out_temp_end_c", "FLOAT", "NULL")
    _ensure_column("drives", "ended_on", "VARCHAR(12)", "NULL")
    # Where a trip's kilometres, minutes and kWh went by speed, and how often
    # it stopped. Null on every row written before these existed, which the
    # matrix has to handle rather than assume away — most of the history has no
    # profile and never will.
    _ensure_column("drives", "speed_profile", "TEXT", "NULL")
    _ensure_column("drives", "stop_count", "INTEGER", "NULL")
    _ensure_column("drives", "start_gap_sec", "FLOAT", "NULL")
    _ensure_column("drives", "end_gap_sec", "FLOAT", "NULL")
    _ensure_column("drives", "end_est_km", "FLOAT", "NULL")
    _ensure_column("drives", "end_est_verified", "BOOLEAN", "NULL")
    _ensure_column("arrival_tail_samples", "place", "VARCHAR(120)", "''")
    _ensure_column("drives", "start_odo_km", "FLOAT", "NULL")
    _ensure_column("drives", "end_odo_km", "FLOAT", "NULL")
    # Telemetry taking over the drive history. source names which path put
    # these figures here; shadow_start_ts links the row to the streamed trip
    # it came from, so promoting twice corrects the same row instead of
    # writing a second one. polled_km/polled_kwh keep what polling had said
    # before telemetry overwrote it — without them the comparison that
    # decides which source to believe would be reading telemetry against
    # itself and agreeing perfectly for ever.
    _ensure_column("drives", "source", "VARCHAR(12)", "''")
    _ensure_column("drives", "shadow_start_ts", "FLOAT", "NULL")
    _ensure_column("drives", "polled_km", "FLOAT", "NULL")
    _ensure_column("drives", "polled_kwh", "FLOAT", "NULL")
    _ensure_column("drives", "recovered_km", "FLOAT", "NULL")
    _ensure_column("drives", "recovered_kwh", "FLOAT", "NULL")
    _ensure_column("drives", "recovered_via", "VARCHAR(20)", "NULL")
    _ensure_column("drives", "recovered_at", "VARCHAR(32)", "NULL")
    _ensure_column("charges", "is_free", "BOOLEAN", "FALSE")
    _ensure_column("charges", "billed_kwh", "FLOAT", "0.0")
    _ensure_column("charges", "implied_capacity_kwh", "FLOAT", "NULL")
    _ensure_column("charges", "capacity_samples", "INTEGER", "NULL")
    _ensure_column("charges", "price_source", "VARCHAR(10)", "''")
    _ensure_column("charges", "source", "VARCHAR(12)", "''")
    _ensure_column("charges", "shadow_start_ts", "FLOAT", "NULL")
    _ensure_column("charges", "polled_kwh", "FLOAT", "NULL")
    _ensure_column("charges", "energy_source", "VARCHAR(16)", "''")
    # 0, not NULL: "not set" and "set to zero" mean the same thing here (fall
    # back to the global pace), so there is nothing for NULL to carry.
    _ensure_column("places", "departure_pace_kmh", "FLOAT", "0.0")
    _ensure_column("places", "parked_draw_w", "FLOAT", "0.0")
    _ensure_column("places", "arrival_tail_km", "FLOAT", "0.0")
    # NULL default (not FALSE) — "unknown" (older reading, car didn't report
    # it) must stay distinguishable from a confirmed off.
    _ensure_column("battery_readings", "sentry_mode", "BOOLEAN", "NULL")
    _ensure_column("battery_readings", "sentry_state", "VARCHAR(28)", "NULL")
    _ensure_column("battery_readings", "climate_on", "BOOLEAN", "NULL")
    _ensure_column("battery_readings", "cabin_overheat_protection", "VARCHAR(10)", "NULL")
    _ensure_column("battery_readings", "cabin_overheat_protection_actively_cooling", "BOOLEAN", "NULL")
    # One journey, one row. Promotion decides whether a trip is already
    # recorded by reading the table and then writing to it, which two
    # concurrent requests can both get through — and it runs from the
    # dashboard read, the telemetry ingest and the cron, so a page load
    # landing while a batch arrives is all it takes. Two cars cannot start a
    # trip in the same second, so this costs nothing and turns a silent
    # duplicate into a failed insert the caller already retries after.
    _ensure_unique_index("drives", "ux_drives_vehicle_start",
                         ("vehicle_id", "start_time"))
    # dashcam_state existed to test one thing: whether Tesla leaked a Sentry
    # TRIGGER through the polled API indirectly, a clip being written standing
    # in for an alarm state the API does not publish. Telemetry answered it
    # outright — SentryMode carries Aware and Panic at ten seconds — so the
    # field went from the models and these drop the columns behind it.
    #
    # Asked for explicitly after the trade was put plainly: the rows in these
    # two columns are the cost, and they are worth nothing, because the
    # question they were recorded for has a better answer now.
    _drop_column("battery_readings", "dashcam_state")
    _drop_column("security_events", "dashcam_state")
    # center_display_state followed it, for the same reason and one more of
    # its own. It was the other half of the same probe, so it lost its purpose
    # when SentryMode's Aware and Panic states answered the question. And it
    # had no telemetry path even in principle: CenterDisplay is streamed, but
    # as an enum with no documented correspondence to the integers this column
    # held, so the stream could only ever write None into it. A column one of
    # the two sources cannot fill, holding a value neither of them reads, is
    # not a column.
    _drop_column("battery_readings", "center_display_state")
    _drop_column("security_events", "center_display_state")



def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

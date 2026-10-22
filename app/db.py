"""Database engine, session factory and a dialect-aware upsert helper."""

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator

from app import config


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """A datetime column that always comes back timezone-aware in UTC.

    Postgres keeps time zones; SQLite silently drops them. Without this, comparing a
    value read from SQLite with datetime.now(timezone.utc) raises a TypeError.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def make_engine(url: str | None = None):
    url = url or config.DATABASE_URL
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind=None) -> None:
    """Create all tables. Fine for now; switch to Alembic migrations in a later phase."""
    from app import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(bind or engine)


def upsert(session, model, rows: Iterable[dict], key_cols: list[str]) -> int:
    """Insert rows, or update them if a row with the same key already exists.

    This makes every ingest run idempotent: re-running a day never creates duplicates.
    Works on both PostgreSQL and SQLite.
    """
    rows = list(rows)
    if not rows:
        return 0

    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert

    # Send in chunks so a two-year backfill doesn't build one giant statement.
    for start in range(0, len(rows), 1000):
        chunk = rows[start : start + 1000]
        stmt = insert(model).values(chunk)
        update_cols = {c: stmt.excluded[c] for c in chunk[0] if c not in key_cols}
        stmt = stmt.on_conflict_do_update(index_elements=key_cols, set_=update_cols)
        session.execute(stmt)
    return len(rows)

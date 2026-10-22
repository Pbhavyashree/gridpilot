"""Database tables.

All timestamps are timezone-aware UTC and mark the START of an interval.
Data is stored at its native resolution (15 minutes for day-ahead prices since 2025);
resampling to hourly happens in one place: app/queries.py.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class Price(Base):
    __tablename__ = "prices"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)  # energy_charts | awattar
    eur_mwh: Mapped[float] = mapped_column(Float)


class Generation(Base):
    __tablename__ = "generation"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    fuel: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "Wind onshore"
    mw: Mapped[float] = mapped_column(Float)


class CarbonIntensity(Base):
    __tablename__ = "carbon_intensity"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    g_per_kwh: Mapped[float] = mapped_column(Float)


class Weather(Base):
    """Weather FORECAST for one location and hour (made about 2 days before, see app/ingest/weather.py)."""

    __tablename__ = "weather"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    location: Mapped[str] = mapped_column(String(32), primary_key=True)
    wind_100m: Mapped[float | None] = mapped_column(Float, nullable=True)
    radiation: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_2m: Mapped[float | None] = mapped_column(Float, nullable=True)


class Forecast(Base):
    """One forecast value: made at `made_at`, for the hour starting at `target_ts`."""

    __tablename__ = "forecasts"

    made_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    target_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    metric: Mapped[str] = mapped_column(String(16), primary_key=True)  # price | carbon
    model: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[float] = mapped_column(Float)


class Job(Base):
    """A flexible job that must finish by its deadline.

    Lifecycle:  planned -> running -> succeeded | failed
                planned -> missed     (the worker wasn't running and the deadline can't be met)
                planned -> cancelled
    Once its window has passed and real data exists, realised cost and CO2 are filled in
    ("settled"), so planned and actual can be compared.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    task: Mapped[str] = mapped_column(String(64))
    duration_hours: Mapped[int] = mapped_column(Integer)
    power_kw: Mapped[float] = mapped_column(Float, default=1.0)
    earliest_start: Mapped[datetime] = mapped_column(UTCDateTime)
    deadline: Mapped[datetime] = mapped_column(UTCDateTime)
    objective: Mapped[str] = mapped_column(String(16))  # cost | carbon | blend
    carbon_weight: Mapped[float] = mapped_column(Float, default=0.5)

    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    planned_start: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    planned_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    replan_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_cost_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_co2_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # snapshot for /explain

    actual_start: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    realised_cost_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    realised_co2_kg: Mapped[float | None] = mapped_column(Float, nullable=True)


class Incident(Base):
    """Something GridPilot noticed is wrong with itself. Opened by the monitor, resolved when it clears."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), index=True)  # e.g. "ingest:prices"; one open incident per key
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))  # warning | critical
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | resolved
    title: Mapped[str] = mapped_column(String(200))
    note: Mapped[str] = mapped_column(Text)  # Markdown write-up: what, since when, evidence, what was done
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    checks: Mapped[int] = mapped_column(Integer, default=1)  # how many monitor runs saw it


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dataset: Mapped[str] = mapped_column(String(32))  # prices | generation
    source: Mapped[str] = mapped_column(String(32))
    rows: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))  # ok | failed | fallback
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

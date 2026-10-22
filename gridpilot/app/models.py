"""Database tables.

All timestamps are timezone-aware UTC and mark the START of an interval.
Data is stored at its native resolution (15 minutes for day-ahead prices since 2025);
resampling to hourly happens in one place: app/queries.py.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


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

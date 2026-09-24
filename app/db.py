"""Database: SQLAlchemy-modeller og session.

SQLite som standard. Huset har omkring 320 senge og en horisont på 120 dage —
det fylder nogle få megabyte om året, så der er ingen grund til noget tungere.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint,
    create_engine, inspect, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

SCHEMA_VERSION = 3


def utcnow() -> datetime:
    """Naiv UTC — samme form som alt andet i basen.

    `datetime.utcnow()` er udfaset i Python 3.12 og forsvinder. Men vi kan ikke
    bare skifte til `datetime.now(timezone.utc)`: den returnerer et
    tidszonebevidst tidspunkt, og hvert eneste tidsstempel der allerede ligger i
    rms.db er naivt. En sammenligning mellem de to former kaster TypeError —
    og først når koden kører, ikke ved import. Derfor beholder vi naiv UTC og
    skifter kun kilden.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class DayState(Base):
    """Det vi ved om én ankomstdato lige nu — begge lagre."""
    __tablename__ = "day_state"

    day: Mapped[date] = mapped_column(Date, primary_key=True)

    rooms_otb: Mapped[int] = mapped_column(Integer, default=0)
    beds_otb: Mapped[int] = mapped_column(Integer, default=0)
    blocked_rooms: Mapped[int] = mapped_column(Integer, default=0)
    blocked_beds: Mapped[int] = mapped_column(Integer, default=0)

    comp_room: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_bed: Mapped[float | None] = mapped_column(Float, nullable=True)

    current_room_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_bed_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    flex_private_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flex_private_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    booked_room_revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    booked_bed_revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    room_type_otb: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor_room_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    anchor_bed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    anchor_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_room_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    locked_bed_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    otb_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    comp_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Event(Base):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(160))
    uplift: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(160), default="")


class Competitor(Base):
    __tablename__ = "competitor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    review_score: Mapped[float] = mapped_column(Float, default=8.0)
    note: Mapped[str] = mapped_column(String(240), default="")


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    n_days: Mapped[int] = mapped_column(Integer, default=0)
    revpab: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")


class Recommendation(Base):
    __tablename__ = "recommendation"
    __table_args__ = (UniqueConstraint("run_id", "day", name="uq_run_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    lead_days: Mapped[int] = mapped_column(Integer, default=0)

    # lager og allokering
    flex_to_private: Mapped[int] = mapped_column(Integer, default=0)
    room_capacity: Mapped[int] = mapped_column(Integer, default=0)
    bed_capacity: Mapped[int] = mapped_column(Integer, default=0)

    # prognose
    rooms_otb: Mapped[int] = mapped_column(Integer, default=0)
    beds_otb: Mapped[int] = mapped_column(Integer, default=0)
    room_occ_now: Mapped[float] = mapped_column(Float, default=0.0)
    bed_occ_now: Mapped[float] = mapped_column(Float, default=0.0)
    room_forecast: Mapped[float] = mapped_column(Float, default=0.0)
    bed_forecast: Mapped[float] = mapped_column(Float, default=0.0)
    forecast_rooms: Mapped[float] = mapped_column(Float, default=0.0)
    forecast_beds: Mapped[float] = mapped_column(Float, default=0.0)

    # faktorer
    f_pace_rooms: Mapped[float] = mapped_column(Float, default=1.0)
    f_pace_beds: Mapped[float] = mapped_column(Float, default=1.0)
    f_market_rooms: Mapped[float] = mapped_column(Float, default=1.0)
    f_market_beds: Mapped[float] = mapped_column(Float, default=1.0)
    f_event: Mapped[float] = mapped_column(Float, default=1.0)
    event_names: Mapped[str] = mapped_column(String(320), default="")

    # priser
    base_room: Mapped[float] = mapped_column(Float, default=0.0)
    base_bed: Mapped[float] = mapped_column(Float, default=0.0)
    room_price: Mapped[float] = mapped_column(Float, default=0.0)
    bed_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_room_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_bed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    room_types: Mapped[str] = mapped_column(Text, default="{}")
    net_room: Mapped[float] = mapped_column(Float, default=0.0)
    net_bed: Mapped[float] = mapped_column(Float, default=0.0)
    revpab: Mapped[float] = mapped_column(Float, default=0.0)

    revenue_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # prisstige (None i faktormodellen)
    room_rung: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bed_rung: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ladder: Mapped[str | None] = mapped_column(Text, nullable=True)

    warnings: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="pending")
    decided_by: Mapped[str] = mapped_column(String(80), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    action: Mapped[str] = mapped_column(String(60))
    day: Mapped[date | None] = mapped_column(Date, nullable=True)
    old_value: Mapped[str] = mapped_column(String(80), default="")
    new_value: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


_engine = None
SessionLocal = None


class SchemaMismatch(RuntimeError):
    """Databasen stammer fra version 1 og kan ikke læses af version 2."""


def init_db(database_url: str):
    global _engine, SessionLocal
    if database_url.startswith("sqlite:///"):
        target = database_url.replace("sqlite:///", "", 1)
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(
        database_url, future=True,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )

    # Version 1 gemte ét lager pr. dato. Version 2 gemmer to. Der er ingen
    # meningsfuld automatisk migrering — den gamle base ved ikke hvor mange
    # senge der var solgt. Sig det tydeligt i stedet for at fejle underligt.
    inspector = inspect(_engine)
    if "day_state" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("day_state")}
        if "rooms_otb" not in columns:
            raise SchemaMismatch(
                "Databasen er fra version 1 af prismotoren. Flyt data/rms.db til side "
                "(fx 'mv data/rms.db data/rms-v1.db') og start forfra — version 2 "
                "holder styr på både værelser og senge, og de tal findes ikke i den gamle base."
            )

    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    # Version 2 -> 3: nullable additions preserve all existing prices and history.
    additions = {
        "day_state": {"flex_private_min": "INTEGER", "flex_private_max": "INTEGER",
                      "booked_room_revenue": "FLOAT", "booked_bed_revenue": "FLOAT",
                      "room_type_otb": "TEXT", "anchor_room_price": "FLOAT",
                      "anchor_bed_price": "FLOAT", "anchor_at": "DATETIME"},
        "recommendation": {"revenue_basis": "VARCHAR(32)", "room_rung": "INTEGER",
                           "bed_rung": "INTEGER", "ladder": "TEXT"},
    }
    inspector = inspect(_engine)
    with _engine.begin() as connection:
        for table, fields in additions.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, sql_type in fields.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
    return _engine


def get_session():
    if SessionLocal is None:
        raise RuntimeError("init_db() skal kaldes før get_session()")
    return SessionLocal()


def log(session, action: str, *, actor="system", day=None, old="", new="", detail=""):
    session.add(AuditLog(
        actor=actor, action=action, day=day,
        old_value=str(old), new_value=str(new), detail=detail,
    ))

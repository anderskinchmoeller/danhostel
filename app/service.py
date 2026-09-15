"""Kørslen: hent data, beregn, gem, og skriv eventuelt tilbage.

Hver kørsel gemmes som et nyt sæt forslag. Låste datoer bruger låsepriser.
Kun friske, mulige datoer beregnes; eksport/skrivning validerer igen.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

from sqlalchemy import select

from . import db
from .adapters import AdapterUnavailable, PricePush, get_pms_adapter, get_rateshop_adapter
from .config import Settings
from .engine import DayInput, EventUplift, Params, price_day, quality_index


# --------------------------------------------------------------------------
# Ind- og udlæsning af tilstand
# --------------------------------------------------------------------------

def upsert_inventory(session, rows, actor="system") -> int:
    now = datetime.utcnow()
    n = 0
    for row in rows:
        state = session.get(db.DayState, row.day)
        if state is None:
            state = db.DayState(day=row.day)
            session.add(state)
        state.rooms_otb = row.rooms_otb
        state.beds_otb = row.beds_otb
        state.blocked_rooms = row.blocked_rooms
        state.blocked_beds = row.blocked_beds
        if not state.anchor_at or now - state.anchor_at >= timedelta(hours=24):
            state.anchor_room_price = state.current_room_price or row.current_room_price
            state.anchor_bed_price = state.current_bed_price or row.current_bed_price
            state.anchor_at = now
        if state.anchor_room_price is None and row.current_room_price:
            state.anchor_room_price = row.current_room_price
        if state.anchor_bed_price is None and row.current_bed_price:
            state.anchor_bed_price = row.current_bed_price
        for field in ("flex_private_min", "flex_private_max", "booked_room_revenue", "booked_bed_revenue"):
            setattr(state, field, getattr(row, field))
        state.room_type_otb = json.dumps(row.room_type_otb)
        if row.current_room_price:
            state.current_room_price = row.current_room_price
        if row.current_bed_price:
            state.current_bed_price = row.current_bed_price
        state.otb_updated = now
        n += 1
    db.log(session, "import_inventory", actor=actor, new=n, detail=f"{n} datoer opdateret")
    return n


def upsert_comp(session, rows, actor="system") -> int:
    now = datetime.utcnow()
    n = 0
    for row in rows:
        state = session.get(db.DayState, row.day)
        if state is None:
            state = db.DayState(day=row.day)
            session.add(state)
        if row.comp_room is not None:
            state.comp_room = row.comp_room
        if row.comp_bed is not None:
            state.comp_bed = row.comp_bed
        state.comp_updated = now
        n += 1
    db.log(session, "import_comp", actor=actor, new=n, detail=f"{n} datoer opdateret")
    return n


def effective_params(session, settings: Settings) -> Params:
    """Kvalitetsindekset genberegnes hver kørsel, så en ændret review-score slår
    igennem uden at nogen skal huske at rette en fil."""
    params = settings.params
    competitors = session.scalars(select(db.Competitor)).all()
    weighted = [(c.weight, c.review_score) for c in competitors if c.weight > 0]
    own = next((c for c in competitors if c.name == "__own__"), None)
    if weighted and own:
        return replace(params, quality_index=quality_index(own.review_score, weighted))
    return params


def load_events(session) -> list:
    return [
        EventUplift(start=e.start, end=e.end, name=e.name, uplift=e.uplift)
        for e in session.scalars(select(db.Event)).all()
    ]


def build_inputs(states, settings: Settings) -> list:
    return [
        DayInput(
            day=s.day,
            rooms_otb=s.rooms_otb,
            beds_otb=s.beds_otb,
            comp_room=s.comp_room,
            comp_bed=s.comp_bed,
            current_room_price=(s.anchor_room_price if s.anchor_at and
                datetime.utcnow() - s.anchor_at < timedelta(hours=24) and s.anchor_room_price
                else s.current_room_price),
            current_bed_price=(s.anchor_bed_price if s.anchor_at and
                datetime.utcnow() - s.anchor_at < timedelta(hours=24) and s.anchor_bed_price
                else s.current_bed_price),
            flex_private_min=s.flex_private_min, flex_private_max=s.flex_private_max,
            booked_room_revenue=s.booked_room_revenue, booked_bed_revenue=s.booked_bed_revenue,
            room_type_otb=json.loads(s.room_type_otb or "{}"),
            locked_room_price=s.locked_room_price if s.locked else None,
            locked_bed_price=s.locked_bed_price if s.locked else None,
            blocked_rooms=s.blocked_rooms,
            blocked_beds=s.blocked_beds,
        )
        for s in states
    ]


# --------------------------------------------------------------------------
# Selve kørslen
# --------------------------------------------------------------------------

def run_pricing(settings: Settings, trigger: str = "manual", actor: str = "system",
                today: date | None = None) -> dict:
    today = today or date.today()
    start = today
    end = today + timedelta(days=settings.horizon_days)

    session = db.get_session()
    run = db.Run(trigger=trigger, status="running")
    session.add(run)
    session.commit()

    notes: list = []
    try:
        # 1. Hent fra adaptere hvis de kan levere. Ellers kør på det der allerede
        #    ligger i basen — uploadet i dashboardet eller hentet i går.
        try:
            rows = get_pms_adapter(settings.pms_adapter).fetch_inventory(start, end)
            upsert_inventory(session, rows, actor=f"adapter:{settings.pms_adapter}")
            notes.append(f"Belægning hentet: {len(rows)} datoer")
        except AdapterUnavailable as exc:
            notes.append(f"Belægning: {exc}")

        try:
            rows = get_rateshop_adapter(settings.rateshop_adapter).fetch_comp_prices(start, end)
            upsert_comp(session, rows, actor=f"adapter:{settings.rateshop_adapter}")
            notes.append(f"Konkurrentpriser hentet: {len(rows)} datoer")
        except AdapterUnavailable as exc:
            notes.append(f"Konkurrentpriser: {exc}")

        session.commit()

        # 2. Dødmandsknap. Hellere gårsdagens priser end priser på tomme data.
        states = session.scalars(
            select(db.DayState).where(db.DayState.day >= start, db.DayState.day <= end)
            .order_by(db.DayState.day)
        ).all()
        if not states:
            raise AdapterUnavailable("Ingen data for horisonten — upload belægning først")

        fresh_states = [s for s in states if inventory_is_fresh(s, settings)]
        skipped = [s.day.isoformat() for s in states if s not in fresh_states]
        if skipped:
            notes.append("Fastfrosset: gamle/manglende belægningsdata for " + ", ".join(skipped))
        missing = (end - start).days + 1 - len(states)
        if missing:
            notes.append(f"{missing} datoer uden data: ingen prisforslag")

        params = effective_params(session, settings)
        events = load_events(session)
        recs = []
        for item in build_inputs(fresh_states, settings):
            try:
                recs.append(price_day(item, params, events, today=today))
            except ValueError as exc:
                notes.append(f"Fastfrosset {item.day}: {exc}")
        if not recs:
            run.status = "frozen"
            run.finished = datetime.utcnow()
            run.note = " | ".join(notes)
            db.log(session, "freeze", actor=actor, detail=run.note)
            session.commit()
            return {"status": "frozen", "run_id": run.id, "note": run.note}

        by_day = {s.day: s for s in states}
        locked = {s.day: s for s in states if s.locked}
        revpab_total = 0.0
        for rec in recs:
            state = locked.get(rec.day)
            status = "pending"
            room_price, bed_price = rec.room_price, rec.bed_price
            warnings = list(rec.warnings)
            if state:
                status = "locked"
                warnings.append("Manuelt låst — automatikken rører ikke denne dato")

            revpab_total += rec.revpab
            session.add(db.Recommendation(
                run_id=run.id, day=rec.day, lead_days=rec.lead_days,
                flex_to_private=rec.flex_to_private,
                room_capacity=rec.room_capacity, bed_capacity=rec.bed_capacity,
                rooms_otb=rec.rooms_otb, beds_otb=rec.beds_otb,
                room_occ_now=rec.room_occ_now, bed_occ_now=rec.bed_occ_now,
                room_forecast=rec.room_forecast, bed_forecast=rec.bed_forecast,
                forecast_rooms=rec.forecast_rooms, forecast_beds=rec.forecast_beds,
                f_pace_rooms=rec.f_pace_rooms, f_pace_beds=rec.f_pace_beds,
                f_market_rooms=rec.f_market_rooms, f_market_beds=rec.f_market_beds,
                f_event=rec.f_event, event_names=", ".join(rec.event_names),
                base_room=rec.base_room, base_bed=rec.base_bed,
                room_price=room_price, bed_price=bed_price,
                current_room_price=by_day[rec.day].current_room_price,
                current_bed_price=by_day[rec.day].current_bed_price,
                room_types=json.dumps(rec.room_types),
                net_room=rec.net_room, net_bed=rec.net_bed, revpab=rec.revpab,
                revenue_basis=rec.revenue_basis,
                warnings=" · ".join(warnings), status=status,
            ))

        run.n_days = len(recs)
        run.revpab = revpab_total / len(recs) if recs else 0.0
        run.status = "done"
        run.finished = datetime.utcnow()
        run.note = " | ".join(notes)
        db.log(session, "run", actor=actor, new=len(recs), detail=run.note)
        session.commit()

        result = {"status": "done", "run_id": run.id, "n_days": len(recs),
                  "quality_index": params.quality_index,
                  "avg_revpab": run.revpab, "notes": notes}

        if settings.auto_publish:
            result["publish"] = publish_run(settings, run.id, actor="auto")
        return result

    except Exception as exc:  # noqa: BLE001 — en kørsel må aldrig vælte servicen
        run.status = "failed"
        run.finished = datetime.utcnow()
        run.note = f"{type(exc).__name__}: {exc}"
        db.log(session, "run_failed", actor=actor, detail=run.note)
        session.commit()
        return {"status": "failed", "run_id": run.id, "error": run.note}
    finally:
        session.close()


def inventory_is_fresh(state, settings):
    return bool(state.otb_updated and timedelta(0) <= datetime.utcnow() - state.otb_updated
                <= timedelta(hours=settings.stale_hours))


def delivery_error(session, settings, run, recs):
    if not settings.params.inventory.confirmed:
        return "Lageret er ikke bekræftet. Afstem rumtyper og sengetal med Picasso før eksport/skrivning."
    if not run or run.status != "done":
        return "Kørslen er ikke klar til eksport/skrivning"
    for rec in recs:
        state = session.get(db.DayState, rec.day)
        if rec.status not in ("approved", "published"):
            return f"{rec.day}: prisen er ikke godkendt"
        if not state or not inventory_is_fresh(state, settings):
            return f"{rec.day}: belægningsdata er for gamle eller mangler; beregn igen"
        if state.locked:
            return f"{rec.day}: datoen er manuelt låst"
        if rec.decided_at and state.otb_updated > rec.decided_at:
            return f"{rec.day}: belægning ændret efter godkendelse; beregn igen"
        if state.otb_updated > run.finished:
            return f"{rec.day}: belægning ændret efter beregning; beregn igen"
    return None


def publish_run(settings: Settings, run_id: int, actor: str = "system",
                only_approved: bool = True) -> dict:
    """Skriv priser til PMS. I skyggedrift sker det kun for godkendte datoer."""
    session = db.get_session()
    try:
        query = select(db.Recommendation).where(db.Recommendation.run_id == run_id)
        if only_approved:
            query = query.where(db.Recommendation.status == "approved")
        else:
            query = query.where(db.Recommendation.status.in_(["approved", "pending"]))
        recs = session.scalars(query.order_by(db.Recommendation.day)).all()
        if not recs:
            return {"ok": 0, "failed": [], "note": "Ingen godkendte datoer at skrive"}

        error = delivery_error(session, settings, session.get(db.Run, run_id), recs)
        if error:
            db.log(session, "publish_blocked", actor=actor, detail=error)
            session.commit()
            return {"ok": 0, "failed": [r.day.isoformat() for r in recs], "note": error}
        pushes = [
            PricePush(day=r.day, room_price=r.room_price, bed_price=r.bed_price,
                      room_types=json.loads(r.room_types or "{}"))
            for r in recs
        ]
        try:
            outcome = get_pms_adapter(settings.pms_adapter).push_prices(pushes)
        except AdapterUnavailable as exc:
            db.log(session, "publish_unavailable", actor=actor, detail=str(exc))
            session.commit()
            return {"ok": 0, "failed": [], "note": str(exc)}

        failed = set(outcome.get("failed", []))
        now = datetime.utcnow()
        for rec in recs:
            if rec.day.isoformat() in failed:
                continue
            state = session.get(db.DayState, rec.day)
            old = state.current_room_price if state else None
            if state:
                if not state.anchor_at or now - state.anchor_at >= timedelta(hours=24):
                    state.anchor_at = now
                    state.anchor_room_price = state.current_room_price or rec.room_price
                    state.anchor_bed_price = state.current_bed_price or rec.bed_price
                state.current_room_price = rec.room_price
                state.current_bed_price = rec.bed_price
            rec.status = "published"
            rec.decided_at = now
            rec.decided_by = actor
            db.log(session, "publish", actor=actor, day=rec.day,
                   old=old if old is not None else "",
                   new=f"{rec.room_price:.0f}/{rec.bed_price:.0f}")
        session.commit()
        return outcome
    finally:
        session.close()

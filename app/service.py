"""Kørslen: hent data, beregn, gem, og skriv eventuelt tilbage.

Hver kørsel gemmes som et nyt sæt forslag. Låste datoer bruger låsepriser.
Kun friske, mulige datoer beregnes; eksport/skrivning validerer igen.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from pathlib import Path

from . import db, level
from .adapters import AdapterUnavailable, PricePush, get_pms_adapter, get_rateshop_adapter
from .config import BASE_DIR, Settings
from .engine import DayInput, EventUplift, Params, flex_bounds, price_day, quality_index


# --------------------------------------------------------------------------
# Ind- og udlæsning af tilstand
# --------------------------------------------------------------------------

def upsert_inventory(session, rows, actor="system", observed_at=None) -> int:
    from .validation import append
    now = db.utcnow()
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
        state.otb_updated = observed_at or now
        append(session, "inventory", row.day, observed_at or now, {
            "rooms_otb": row.rooms_otb, "beds_otb": row.beds_otb,
            "blocked_rooms": row.blocked_rooms if observed_at is None else None,
            "blocked_beds": row.blocked_beds if observed_at is None else None,
            "room_type_otb": row.room_type_otb,
            "booked_room_revenue": row.booked_room_revenue,
            "booked_bed_revenue": row.booked_bed_revenue,
            "imported_room_price": row.current_room_price,
            "imported_bed_price": row.current_bed_price,
        }, actor=actor, source="Indlæst belægning; ikke bekræftelse af prisændring")
        n += 1
    db.log(session, "import_inventory", actor=actor, new=n, detail=f"{n} datoer opdateret")
    return n


def upsert_comp(session, rows, actor="system", observed_at: datetime | None = None) -> int:
    now = observed_at or db.utcnow()
    n = 0
    for row in rows:
        state = session.get(db.DayState, row.day)
        if state is None:
            state = db.DayState(day=row.day)
            session.add(state)
        # Each row is a snapshot: blank means unavailable, never a fresh old quote.
        state.comp_room = row.comp_room
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


def build_inputs(states, settings: Settings, *, now: datetime | None = None) -> list:
    now = now or db.utcnow()
    local_now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(settings.timezone))
    remaining = 1 - (local_now.hour * 3600 + local_now.minute * 60 + local_now.second) / 86400
    def fresh_market(s):
        return bool(s.comp_updated and timedelta(0) <= now - s.comp_updated
                    <= timedelta(hours=settings.stale_hours))
    return [
        DayInput(
            day=s.day,
            rooms_otb=s.rooms_otb,
            beds_otb=s.beds_otb,
            comp_room=s.comp_room if fresh_market(s) else None,
            comp_bed=s.comp_bed if fresh_market(s) else None,
            same_day_fraction=remaining if s.day == local_now.date() else None,
            market_notes=(("Gamle konkurrentpriser udeladt — opdatér markedet",)
                          if s.comp_updated and not fresh_market(s) else ()),
            current_room_price=(s.anchor_room_price if s.anchor_at and
                db.utcnow() - s.anchor_at < timedelta(hours=24) and s.anchor_room_price
                else s.current_room_price),
            current_bed_price=(s.anchor_bed_price if s.anchor_at and
                db.utcnow() - s.anchor_at < timedelta(hours=24) and s.anchor_bed_price
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
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
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
        inputs = build_inputs(fresh_states, settings)
        if params.ladder is not None:
            inputs = with_ladder_history(session, inputs, today, run.id,
                                         timezone=settings.timezone)
        if params.v4_active:
            params, level_note = update_demand_level(params, inputs, today)
            if level_note:
                notes.append(level_note)
        recs = []
        for item in inputs:
            try:
                recs.append(price_day(item, params, events, today=today))
            except ValueError as exc:
                notes.append(f"Fastfrosset {item.day}: {exc}")
        if not recs:
            run.status = "frozen"
            run.finished = db.utcnow()
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
                room_rung=rec.room_rung, bed_rung=rec.bed_rung,
                ladder=json.dumps({"rooms": rec.room_ladder, "beds": rec.bed_ladder},
                                  ensure_ascii=False) if rec.room_ladder else None,
                warnings=" · ".join(warnings), status=status,
            ))

        from .forecast_validation import save_checkpoints
        saved = save_checkpoints(session, run, recs, inputs, params, today, settings.timezone,
                                 revenue_settings=settings.raw.get("validation", {}))
        notes.append(f"Gemte {saved} nye prognoser ved 30/14/7/1 dage")
        run.n_days = len(recs)
        if params.v4_active:
            # Save the comparison with the run's actual data, levels and clock.
            # Files are only written when the user downloads the CSV.
            from .skyggedrift import compare_inputs
            run.shadow_json = json.dumps(compare_inputs(params, inputs, events, today,
                                                        recommendations=recs), ensure_ascii=False)
        run.revpab = revpab_total / len(recs) if recs else 0.0
        run.status = "done"
        run.finished = db.utcnow()
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
        # A failed run must not retain partial proposals or consume a checkpoint.
        session.rollback()
        run.status = "failed"
        run.finished = db.utcnow()
        run.note = f"{type(exc).__name__}: {exc}"
        db.log(session, "run_failed", actor=actor, detail=run.note)
        session.commit()
        return {"status": "failed", "run_id": run.id, "error": run.note}
    finally:
        session.close()


def with_ladder_history(session, inputs, today: date, current_run_id: int,
                        pace_window: int = 7, timezone: str = "Europe/Copenhagen") -> list:
    """Giv stigen hukommelse: forrige trin og belægningen for ca. en uge siden.

    Forrige trin er det seneste færdige forslag fra før den lokale dags start.
    Alle dagens kørsler bruger samme trin og udglatning som udgangspunkt; nye
    data kan ændre forslaget uden at gentagne klik bruger endnu et dagstrin.
    Tempo-snapshottet er
    det forslag der ligger tættest på `pace_window` dage tilbage, inden for
    5-10 dage. Ældre eller nyere snapshots giver et for støjende vindue.
    """
    if not inputs:
        return inputs
    days = [i.day for i in inputs]
    rows = session.execute(
        select(db.Recommendation, db.Run.started)
        .join(db.Run, db.Run.id == db.Recommendation.run_id)
        .where(db.Run.status == "done", db.Run.id != current_run_id,
               db.Run.id < current_run_id if current_run_id > 0 else True,
               db.Recommendation.day >= min(days), db.Recommendation.day <= max(days))
        .order_by(db.Recommendation.run_id.desc())
    ).all()
    latest: dict = {}
    prior: dict = {}
    for rec, started in rows:
        local_day = started.replace(tzinfo=ZoneInfo("UTC")).astimezone(
            ZoneInfo(timezone)).date()
        age = (today - local_day).days
        if age > 0:
            latest.setdefault(rec.day, rec)
        if 5 <= age <= 10:
            best = prior.get(rec.day)
            if best is None or abs(age - pace_window) < abs(best[1] - pace_window):
                prior[rec.day] = (rec, age)
    out = []
    for item in inputs:
        prev = latest.get(item.day)
        snap = prior.get(item.day)
        lad = json.loads(prev.ladder) if prev and prev.ladder else {}
        rooms, beds = lad.get("rooms") or {}, lad.get("beds") or {}
        out.append(replace(
            item,
            prev_room_rung=prev.room_rung if prev else None,
            prev_bed_rung=prev.bed_rung if prev else None,
            prev_room_position=rooms.get("smoothed_position"),
            prev_bed_position=beds.get("smoothed_position"),
            rooms_otb_prior=snap[0].rooms_otb if snap else None,
            beds_otb_prior=snap[0].beds_otb if snap else None,
            room_capacity_prior=snap[0].room_capacity if snap else None,
            bed_capacity_prior=snap[0].bed_capacity if snap else None,
            prior_days=snap[1] if snap else None,
        ))
    return out


def update_demand_level(params, inputs, today: date):
    """Juster det adaptive efterspørgselsniveau på dagens prognosefejl.

    Snapshottene fra `with_ladder_history` er allerede der: for hver dato ved vi
    hvad der stod på bøgerne for ca. en uge siden og hvad der står nu. Den
    forventede pickup i samme vindue er forskellen mellem fordelingens
    forventning dengang og nu. Forholdet mellem de to summer er signalet.

    Niveauet gemmes på disk og ikke i databasen, så det overlever en
    skemaændring og kan læses og rettes i hånden af et menneske der er uenig.
    """
    path = Path(params.v4.level_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    state = level.load(path)
    if state.updated and state.updated >= today.isoformat():
        return replace(params, demand_level=(state.rooms, state.beds)), ""
    pairs = {"rooms": [], "beds": []}
    exclusions = {key: {"blocked": 0, "capacity_limited": 0} for key in pairs}
    for item in inputs:
        if item.prior_days is None or item.rooms_otb_prior is None:
            continue
        lead = (item.day - today).days
        # Intraday observations are incomplete; the training cube is end-of-day.
        if lead <= 0:
            continue
        inv = params.inventory
        try:
            lower, upper = flex_bounds(inv, rooms_otb=item.rooms_otb, beds_otb=item.beds_otb,
                blocked_rooms=item.blocked_rooms, blocked_beds=item.blocked_beds,
                private_min=item.flex_private_min, private_max=item.flex_private_max)
        except ValueError:
            for counts in exclusions.values():
                counts["capacity_limited"] += 1
            continue
        for key, model_name, now, before in (
                ("rooms", "rum", item.rooms_otb, item.rooms_otb_prior),
                ("beds", "senge", item.beds_otb, item.beds_otb_prior)):
            if before is None:
                continue
            beds = key == "beds"
            blocked = item.blocked_beds if beds else item.blocked_rooms
            capacity = (inv.max_bed_capacity-lower*inv.beds_per_flex_room if beds else
                        inv.private_rooms+upper) - blocked
            prior_capacity = item.bed_capacity_prior if beds else item.room_capacity_prior
            # Sales at a capacity limit are a lower bound on demand. Do not
            # interpret constrained pickup as evidence that demand fell.
            # Historical recommendation capacity is only an exclusion signal:
            # it does not prove channel availability throughout the window.
            if blocked:
                exclusions[key]["blocked"] += 1
                continue
            if (capacity <= 0 or now >= .98 * capacity or
                    (prior_capacity is not None and prior_capacity > 0 and before >= .98 * prior_capacity)):
                exclusions[key]["capacity_limited"] += 1
                continue
            # Estimate the window at the unadjusted historical level. Binding
            # its endpoints to today's OTB would put the observed pickup into
            # both the numerator and denominator of the learning signal.
            model = params.demand_models[model_name]
            expected = max(0.0, model.expected(lead + item.prior_days, item.day)
                           - model.expected(lead, item.day))
            # Cancellations offset new bookings across the whole window.
            pairs[key].append((expected, now - before))
    excluded = sum(sum(counts.values()) for counts in exclusions.values())
    if not pairs["rooms"] and not pairs["beds"] and not excluded:
        return replace(params, demand_level=(state.rooms, state.beds)), ""
    with level.locked(path):
        state = level.load(path)
        updated = level.update(state, rooms=pairs["rooms"], beds=pairs["beds"], today=today,
                               alpha=params.v4.level_alpha, low=params.v4.level_min,
                               high=params.v4.level_max, exclusions=exclusions)
        if updated is not state:
            level.save(updated, path)
            updated = level.load(path)
    note = ""
    if abs(updated.rooms - state.rooms) > 0.005 or abs(updated.beds - state.beds) > 0.005:
        note = (f"Efterspørgselsniveau: rum {state.rooms:.2f} → {updated.rooms:.2f}, "
                f"senge {state.beds:.2f} → {updated.beds:.2f}")
    if excluded:
        note += (" · " if note else "") + f"Læring: {excluded} produktvinduer udeladt pga. kapacitet/blokering"
    return replace(params, demand_level=(updated.rooms, updated.beds)), note


def inventory_is_fresh(state, settings):
    return bool(state.otb_updated and timedelta(0) <= db.utcnow() - state.otb_updated
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
    """Skriv priser til PMS. Ved manuel kontrol sker det kun for godkendte datoer."""
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
        now = db.utcnow()
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

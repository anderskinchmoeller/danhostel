"""FastAPI-service: dashboard, upload, kørsel, godkendelse og gruppetilbud."""

from __future__ import annotations

import csv
import io
import json
import math
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from . import db, picasso_belaegning, service
from .adapters import parse_comp, parse_inventory
from .config import load_settings
from .sanity import check_inventory
from .engine import DayInput, group_quote, price_range, quality_index

BASE_DIR = Path(__file__).resolve().parent
settings = load_settings()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
scheduler = BackgroundScheduler(timezone=settings.timezone)

security = HTTPBasic(auto_error=False)


def current_user(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Kodeordsbeskyttelse. Slås til ved at sætte RMS_USER og RMS_PASSWORD.

    Servicen kan sætte priser på et rigtigt hostel. Lad den ikke stå åben på
    internettet uden det her.
    """
    if not settings.basic_auth_user:
        return "åben"
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login kræves",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, settings.basic_auth_user)
    ok_pass = secrets.compare_digest(credentials.password, settings.basic_auth_password or "")
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Forkert brugernavn eller kodeord",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def nightly():
    service.run_pricing(settings, trigger="scheduled", actor="scheduler")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.database_url)
    seed_competitors()
    scheduler.add_job(
        nightly,
        CronTrigger(hour=settings.run_cron_hour, minute=settings.run_cron_minute),
        id="nightly", replace_existing=True, misfire_grace_time=3600,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Prismotor", lifespan=lifespan)
templates.env.globals["freshness"] = lambda: freshness()


def seed_competitors():
    session = db.get_session()
    try:
        if session.scalars(select(db.Competitor)).first():
            return
        session.add_all([
            db.Competitor(name="__own__", weight=0.0, review_score=8.0,
                          note="Egen score. Hostelworld 8,0 af 615 anmeldelser."),
            db.Competitor(name="CABINN Aarhus", weight=0.20, review_score=7.8,
                          note="EKSEMPEL — indtast aktuel score"),
            db.Competitor(name="City Sleep-In", weight=0.30, review_score=7.6,
                          note="EKSEMPEL — nærmeste hostel, vejer tungt på senge"),
            db.Competitor(name="Milling Hotel Ritz", weight=0.10, review_score=8.3,
                          note="EKSEMPEL — indtast aktuel score"),
            db.Competitor(name="Airbnb, privatværelse", weight=0.25, review_score=8.5,
                          note="Ca. 2.500 aktive udlejninger i Aarhus (AirDNA)"),
            db.Competitor(name="Tilføj selv", weight=0.15, review_score=8.0, note=""),
        ])
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------
# Hjælpere
# --------------------------------------------------------------------------

DK_DAYS = ["Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn"]


def latest_run(session):
    return session.scalars(
        select(db.Run).where(db.Run.status.in_(["done", "frozen"])).order_by(desc(db.Run.id))
    ).first()


def recommendations_for(session, run_id: int, days: int = 90):
    cutoff = date.today() + timedelta(days=days)
    return session.scalars(
        select(db.Recommendation)
        .where(db.Recommendation.run_id == run_id, db.Recommendation.day <= cutoff)
        .order_by(db.Recommendation.day)
    ).all()


def age_text(delta: timedelta) -> str:
    minutes = delta.total_seconds() / 60
    if minutes < 90:
        return f"for {max(0, int(minutes))} min. siden"
    hours = minutes / 60
    if hours < 48:
        return f"for {int(hours)} timer siden"
    return f"for {int(hours / 24)} dage siden"


def freshness() -> dict:
    """Hvor gammelt er det man kigger på?

    Når outputtet kun læses på en skærm, er en frossen side og en frisk side
    visuelt ens. Det her er forskellen — og dermed den alarm servicen ellers
    ikke har.
    """
    try:
        session = db.get_session()
    except Exception:  # noqa: BLE001 — en defekt base må ikke vælte forsiden
        return {"finished": None, "age_text": "ukendt", "stale": True,
                "limit": settings.stale_hours, "status": None}
    try:
        run = latest_run(session)
        finished = run.finished if run else None
        if not finished:
            return {"finished": None, "age_text": "aldrig", "stale": True,
                    "limit": settings.stale_hours,
                    "status": run.status if run else None}
        age = db.utcnow() - finished
        return {
            "finished": finished,
            "age_text": age_text(age),
            "stale": age > timedelta(hours=settings.stale_hours),
            "limit": settings.stale_hours,
            "status": run.status,
        }
    finally:
        session.close()


def decorate(rec):
    room_delta = rec.room_price - rec.current_room_price if rec.current_room_price else None
    bed_delta = rec.bed_price - rec.current_bed_price if rec.current_bed_price else None
    return {
        "r": rec,
        "weekday": DK_DAYS[rec.day.weekday()],
        "is_weekend": rec.day.weekday() in (4, 5),
        "room_types": json.loads(rec.room_types or "{}"),
        "room_delta": room_delta,
        "room_delta_pct": (room_delta / rec.current_room_price * 100) if room_delta is not None else None,
        "bed_delta": bed_delta,
        "warning_list": [w for w in (rec.warnings or "").split(" · ") if w],
        "ladder": json.loads(rec.ladder) if rec.ladder else None,
    }


# --------------------------------------------------------------------------
# Sider
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, days: int = 60, user: str = Depends(current_user)):
    session = db.get_session()
    try:
        run = latest_run(session)
        rows = [decorate(r) for r in recommendations_for(session, run.id, days)] if run else []
        pending = sum(1 for r in rows if r["r"].status == "pending")
        flagged = sum(1 for r in rows if r["warning_list"])
        avg_room = (sum(r["r"].room_price for r in rows) / len(rows)) if rows else 0
        avg_bed = (sum(r["r"].bed_price for r in rows) / len(rows)) if rows else 0
        avg_revpab = (sum(r["r"].revpab for r in rows) / len(rows)) if rows else 0
        avg_flex = (sum(r["r"].flex_to_private for r in rows) / len(rows)) if rows else 0
        return templates.TemplateResponse(request, "dashboard.html", {
            "rows": rows, "run": run, "settings": settings, "inv": settings.params.inventory,
            "pending": pending, "flagged": flagged, "days": days,
            "avg_room": avg_room, "avg_bed": avg_bed, "avg_revpab": avg_revpab,
            "avg_flex": avg_flex, "user": user,
        })
    finally:
        session.close()


@app.post("/run")
def trigger_run(user: str = Depends(current_user)):
    service.run_pricing(settings, trigger="manual", actor=user)
    return RedirectResponse("/", status_code=303)


@app.post("/decide")
def decide(action: str = Form(...), day: str = Form(""), scope: str = Form("one"),
           user: str = Depends(current_user)):
    session = db.get_session()
    try:
        run = latest_run(session)
        if not run:
            return RedirectResponse("/", status_code=303)
        query = select(db.Recommendation).where(db.Recommendation.run_id == run.id)
        if scope == "one" and day:
            query = query.where(db.Recommendation.day == date.fromisoformat(day))
        else:
            query = query.where(db.Recommendation.status == "pending")
        new_status = "approved" if action == "approve" else "rejected"
        now = db.utcnow()
        for rec in session.scalars(query).all():
            if rec.status in ("locked", "published"):
                continue
            rec.status = new_status
            rec.decided_by = user
            rec.decided_at = now
            db.log(session, new_status, actor=user, day=rec.day,
                   new=f"{rec.room_price:.0f}/{rec.bed_price:.0f}")
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/lock")
def lock_day(day: str = Form(...), room_price: str = Form(""), bed_price: str = Form(""),
             unlock: str = Form(""), user: str = Depends(current_user)):
    session = db.get_session()
    try:
        target = date.fromisoformat(day)
        state = session.get(db.DayState, target) or db.DayState(day=target)
        session.add(state)
        if unlock:
            state.locked = False
            state.locked_room_price = None
            state.locked_bed_price = None
            db.log(session, "unlock", actor=user, day=target)
        else:
            try:
                fixed_room = float(room_price.replace(",", ".")) if room_price else None
                fixed_bed = float(bed_price.replace(",", ".")) if bed_price else None
                if any(v is not None and (not math.isfinite(v) or v <= 0)
                       for v in (fixed_room, fixed_bed)):
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "Låsepriser skal være positive beløb")
            state.locked = True
            state.locked_room_price = fixed_room
            state.locked_bed_price = fixed_bed
            db.log(session, "lock", actor=user, day=target,
                   new=f"{state.locked_room_price or '-'}/{state.locked_bed_price or '-'}")
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/publish")
def publish(user: str = Depends(current_user)):
    session = db.get_session()
    try:
        run = latest_run(session)
        run_id = run.id if run else None
    finally:
        session.close()
    if run_id:
        outcome = service.publish_run(settings, run_id, actor=user)
        if outcome.get("note"):
            raise HTTPException(409, outcome["note"])
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Gruppeforespørgsler
# --------------------------------------------------------------------------

@app.get("/grupper", response_class=HTMLResponse)
def groups_page(request: Request, rooms: int = 0, start: str = "", nights: int = 1,
                user: str = Depends(current_user)):
    session = db.get_session()
    quotes, total_min, total_normal = [], 0.0, 0.0
    error = ""
    try:
        if rooms and start:
            try:
                first = date.fromisoformat(start)
            except ValueError:
                first = None
                error = "Ugyldig startdato"
            if first:
                days = [first + timedelta(days=i) for i in range(max(1, nights))]
                states = {
                    s.day: s for s in session.scalars(
                        select(db.DayState).where(db.DayState.day.in_(days))
                    ).all()
                }
                params = service.effective_params(session, settings)
                events = service.load_events(session)
                inputs = [
                    DayInput(
                        day=d,
                        rooms_otb=states[d].rooms_otb if d in states else 0,
                        beds_otb=states[d].beds_otb if d in states else 0,
                        comp_room=states[d].comp_room if d in states else None,
                        comp_bed=states[d].comp_bed if d in states else None,
                        blocked_rooms=states[d].blocked_rooms if d in states else 0,
                        blocked_beds=states[d].blocked_beds if d in states else 0,
                    )
                    for d in days
                ]
                recs = price_range(inputs, params, events)
                quotes = group_quote(recs, rooms, params)
                total_min = sum(q.minimum_rate for q in quotes) * rooms
                total_normal = sum(q.transient_price for q in quotes) * rooms
                missing = [d for d in days if d not in states]
                if missing:
                    error = (f"{len(missing)} af datoerne har ingen belægningsdata — "
                             "de regnes som tomme, og minimumsprisen bliver for lav.")
    finally:
        session.close()

    return templates.TemplateResponse(request, "groups.html", {
        "settings": settings, "quotes": quotes, "rooms": rooms, "start": start,
        "nights": nights, "total_min": total_min, "total_normal": total_normal,
        "error": error, "days_dk": DK_DAYS, "user": user,
    })


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request, message: str = "", error: str = "",
              user: str = Depends(current_user)):
    session = db.get_session()
    try:
        states = session.scalars(
            select(db.DayState).where(db.DayState.day >= date.today())
            .order_by(db.DayState.day).limit(20)
        ).all()
        return templates.TemplateResponse(request, "data.html", {
            "states": states, "message": message, "error": error,
            "settings": settings, "inv": settings.params.inventory, "user": user,
        })
    finally:
        session.close()


@app.post("/data/upload")
async def upload(kind: str = Form(...), file: UploadFile = File(...),
                 override: str = Form(""), user: str = Depends(current_user)):
    data = await file.read()
    notes: list[str] = []
    session = db.get_session()
    try:
        if kind == "inventory" and data.startswith(b"%PDF"):
            # Picassos Arrivals-rapport (Rooms spec.) direkte, uden CSV-mellemtrin.
            raw, notes = picasso_belaegning.csv_from_pdf(data)
        else:
            raw = data.decode("utf-8-sig", errors="replace")
        if kind == "inventory":
            rows = parse_inventory(raw)
            findings = check_inventory(rows, settings.params)
            if findings and not override:
                detail = " · ".join(f.code for f in findings)
                db.log(session, "import_blocked", actor=user,
                       detail=f"{file.filename}: {detail}")
                session.commit()
                blocked = ("Importen er stoppet — filen ser ikke rigtig ud. "
                           + " ".join(f.message for f in findings))
                return RedirectResponse(f"/data?error={quote(blocked)}", status_code=303)
            n = service.upsert_inventory(session, rows, actor=user)
            msg = f"{n} datoer med belægning importeret fra {file.filename}"
            if findings:
                db.log(session, "import_override", actor=user,
                       detail=f"{file.filename}: " + " · ".join(f.code for f in findings))
                msg += (f" — trumfet igennem trods {len(findings)} "
                        f"{'bemærkning' if len(findings) == 1 else 'bemærkninger'}. "
                        "Det står i loggen.")
            if notes:
                msg += " — " + " ".join(notes)
        else:
            rows = parse_comp(raw)
            n = service.upsert_comp(session, rows, actor=user)
            msg = f"{n} datoer med konkurrentpriser importeret fra {file.filename}"
        session.commit()
        return RedirectResponse(f"/data?message={quote(msg)}", status_code=303)
    except ValueError as exc:
        return RedirectResponse(f"/data?error={quote(str(exc))}", status_code=303)
    finally:
        session.close()


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request, user: str = Depends(current_user)):
    session = db.get_session()
    try:
        events = session.scalars(select(db.Event).order_by(db.Event.start)).all()
        return templates.TemplateResponse(request, "events.html", {
            "events": events, "settings": settings, "user": user,
        })
    finally:
        session.close()


@app.post("/events/add")
def add_event(name: str = Form(...), start: str = Form(...), end: str = Form(...),
              uplift: str = Form("0.10"), source: str = Form(""),
              user: str = Depends(current_user)):
    session = db.get_session()
    try:
        session.add(db.Event(
            name=name, start=date.fromisoformat(start), end=date.fromisoformat(end),
            uplift=float(uplift.replace(",", ".")), source=source,
        ))
        db.log(session, "event_add", actor=user, detail=f"{name} {start}–{end}")
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/events", status_code=303)


@app.post("/events/delete")
def delete_event(event_id: int = Form(...), user: str = Depends(current_user)):
    session = db.get_session()
    try:
        ev = session.get(db.Event, event_id)
        if ev:
            db.log(session, "event_delete", actor=user, detail=ev.name)
            session.delete(ev)
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/events", status_code=303)


# --------------------------------------------------------------------------
# Indstillinger
# --------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(current_user)):
    session = db.get_session()
    try:
        comps = session.scalars(select(db.Competitor).order_by(db.Competitor.id)).all()
        own = next((c for c in comps if c.name == "__own__"), None)
        weighted = [(c.weight, c.review_score) for c in comps if c.weight > 0]
        idx = quality_index(own.review_score, weighted) if own and weighted else 1.0
        logs = session.scalars(select(db.AuditLog).order_by(desc(db.AuditLog.id)).limit(40)).all()
        runs = session.scalars(select(db.Run).order_by(desc(db.Run.id)).limit(10)).all()
        return templates.TemplateResponse(request, "settings.html", {
            "comps": [c for c in comps if c.name != "__own__"], "own": own,
            "quality_index": idx, "settings": settings, "params": settings.params,
            "inv": settings.params.inventory, "logs": logs, "runs": runs, "user": user,
        })
    finally:
        session.close()


@app.post("/settings/competitors")
async def update_competitors(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    session = db.get_session()
    try:
        for comp in session.scalars(select(db.Competitor)).all():
            name = form.get(f"name_{comp.id}")
            weight = form.get(f"weight_{comp.id}")
            score = form.get(f"score_{comp.id}")
            if name and comp.name != "__own__":
                comp.name = name
            if weight not in (None, ""):
                comp.weight = float(str(weight).replace(",", "."))
            if score not in (None, ""):
                comp.review_score = float(str(score).replace(",", "."))
        db.log(session, "competitors_update", actor=user)
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/settings", status_code=303)


# --------------------------------------------------------------------------
# Eksport og API
# --------------------------------------------------------------------------

@app.get("/export.csv")
def export_csv(only_approved: bool = True, user: str = Depends(current_user)):
    """Priser til manuel import i Picasso, indtil API'et er på plads."""
    session = db.get_session()
    try:
        run = latest_run(session)
        if not run:
            raise HTTPException(404, "Ingen kørsel endnu")
        recs = recommendations_for(session, run.id, settings.horizon_days)
        if only_approved:
            recs = [r for r in recs if r.status in ("approved", "published")]

        error = service.delivery_error(session, settings, run, recs)
        if error:
            raise HTTPException(409, error)
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        types = list(settings.params.room_types)
        writer.writerow(["dato", "vaerelsespris", "sengepris"] + types
                        + ["flex_forslag_til_private", "status", "bemaerkning"])
        for rec in recs:
            prices = json.loads(rec.room_types or "{}")
            writer.writerow(
                [rec.day.isoformat(), f"{rec.room_price:.2f}", f"{rec.bed_price:.2f}"]
                + [f"{prices.get(t, 0):.2f}" for t in types]
                + [rec.flex_to_private, rec.status, rec.warnings]
            )
        buffer.seek(0)
        filename = f"priser_{date.today().isoformat()}.csv"
        return StreamingResponse(
            iter([buffer.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        session.close()


@app.get("/api/prices")
def api_prices(days: int = 90, user: str = Depends(current_user)):
    session = db.get_session()
    try:
        run = latest_run(session)
        if not run:
            return JSONResponse({"run": None, "prices": []})
        recs = recommendations_for(session, run.id, days)
        return {
            "run": {"id": run.id, "started": run.started.isoformat(),
                    "status": run.status, "avg_revpab": round(run.revpab, 2)},
            "inventory": {
                "private_rooms": settings.params.inventory.private_rooms,
                "flex_rooms": settings.params.inventory.flex_rooms,
                "dorm_beds": settings.params.inventory.dorm_beds,
                "total_beds": settings.params.inventory.total_beds,
                "confirmed": settings.params.inventory.confirmed,
            },
            "prices": [
                {
                    "date": r.day.isoformat(),
                    "room_price": r.room_price,
                    "bed_price": r.bed_price,
                    "room_types": json.loads(r.room_types or "{}"),
                    "status": r.status,
                    "allocation": {
                        "flex_to_private": r.flex_to_private,
                        "room_capacity": r.room_capacity,
                        "bed_capacity": r.bed_capacity,
                    },
                    "forecast": {
                        "rooms": round(r.room_forecast, 4),
                        "beds": round(r.bed_forecast, 4),
                        "rooms_now": round(r.room_occ_now, 4),
                        "beds_now": round(r.bed_occ_now, 4),
                    },
                    "factors": {
                        "base_room": r.base_room, "base_bed": r.base_bed,
                        "pace_rooms": r.f_pace_rooms, "pace_beds": r.f_pace_beds,
                        "market_rooms": r.f_market_rooms, "market_beds": r.f_market_beds,
                        "event": r.f_event,
                    },
                    "ladder": json.loads(r.ladder) if r.ladder else None,
                    "revpab": round(r.revpab, 2),
                    "revenue_basis": r.revenue_basis or "estimated",
                    "warnings": [w for w in (r.warnings or "").split(" · ") if w],
                }
                for r in recs
            ],
        }
    finally:
        session.close()


@app.get("/api/group-quote")
def api_group_quote(rooms: int, start: str, nights: int = 1,
                    user: str = Depends(current_user)):
    session = db.get_session()
    try:
        first = date.fromisoformat(start)
        days = [first + timedelta(days=i) for i in range(max(1, nights))]
        states = {s.day: s for s in session.scalars(
            select(db.DayState).where(db.DayState.day.in_(days))).all()}
        params = service.effective_params(session, settings)
        events = service.load_events(session)
        inputs = [
            DayInput(
                day=d,
                rooms_otb=states[d].rooms_otb if d in states else 0,
                beds_otb=states[d].beds_otb if d in states else 0,
                comp_room=states[d].comp_room if d in states else None,
                comp_bed=states[d].comp_bed if d in states else None,
            )
            for d in days
        ]
        quotes = group_quote(price_range(inputs, params, events), rooms, params)
        return {
            "rooms": rooms,
            "nights": len(days),
            "minimum_total": round(sum(q.minimum_rate for q in quotes) * rooms, 2),
            "transient_total": round(sum(q.transient_price for q in quotes) * rooms, 2),
            "dates": [q.as_dict() for q in quotes],
        }
    finally:
        session.close()


@app.get("/health")
def health():
    session = db.get_session()
    try:
        run = latest_run(session)
        stale = True
        if run and run.finished:
            stale = (db.utcnow() - run.finished) > timedelta(hours=36)
        return {
            "status": "degraded" if stale else "ok",
            "last_run": run.finished.isoformat() if run and run.finished else None,
            "last_run_status": run.status if run else None,
            "auto_publish": settings.auto_publish,
        }
    finally:
        session.close()

"""Read-only policy replay. Does not learn, approve, export, or post prices."""
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from time import monotonic

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app import db, engine, forecast_validation, forecast_monitor, service
from app.config import load_settings


out = Path(__file__).resolve().parent
today = date(2026, 10, 23)
settings = load_settings()
connection = create_engine('sqlite:///file:data/rms.db?mode=ro&uri=true')
started = monotonic()
with Session(connection) as session:
    params = service.effective_params(session, settings)
    states = session.scalars(select(db.DayState).where(db.DayState.day.between(
        today, today+timedelta(days=settings.horizon_days))).order_by(db.DayState.day)).all()
    run_id = session.scalar(select(func.max(db.Run.id))) or 0
    items = service.with_ladder_history(session,
        service.build_inputs(states, settings, now=db.utcnow()),
        today, run_id+1, timezone=settings.timezone)
    events = service.load_events(session)
    experiments = session.scalar(select(func.count()).select_from(db.ABExperiment))
    live, _, _ = forecast_validation.live_rows(session, today)
    monitor = forecast_monitor.recent(live, today)
rows, frozen, violations = [], [], []
for item in items:
    try:
        robust = engine.price_day(item, params, events, today)
        nominal = engine.price_day(item, replace(params, v4=replace(params.v4,
            uncertainty_mode='diagnostics')), events, today)
    except ValueError as exc:
        frozen.append(dict(day=item.day.isoformat(), reason=str(exc)))
        continue
    inv = params.inventory
    if (robust.room_capacity+item.blocked_rooms-inv.private_rooms)*inv.beds_per_flex_room + \
            robust.bed_capacity+item.blocked_beds != inv.max_bed_capacity:
        violations.append(dict(day=item.day.isoformat(), rule='shared capacity'))
    for beds, ladder, price, capacity, otb in (
            (False, robust.room_ladder, robust.room_price, robust.room_capacity, item.rooms_otb),
            (True, robust.bed_ladder, robust.bed_price, robust.bed_capacity, item.beds_otb)):
        assert ladder['bid']['capacity_remaining'] == capacity-otb
        assert ladder['bid']['price'] == price
        rungs = params.ladder.rungs_beds if beds else params.ladder.rungs_rooms
        floor = ladder['rung_prices'][engine._discount_minimum(params, rungs)]
        lock = item.locked_bed_price if beds else item.locked_room_price
        if lock is None and price < floor:
            violations.append(dict(day=item.day.isoformat(), rule='discount floor'))
        stress = ladder['bid']['uncertainty']
        nominal_e = params.v4.elasticity_beds if beds else params.v4.elasticity_rooms
        same = next(r for r in stress['rows'] if r['elasticity'] == nominal_e)
        assert abs(same['expected_contribution']-ladder['bid']['expected_revenue']) <= .061
    rows.append(dict(day=item.day.isoformat(), nominal=nominal.as_dict(), robust=robust.as_dict()))
summary = dict(dates=len(items), computed=len(rows), frozen=frozen, violations=violations,
    changed_rooms=sum(r['nominal']['room_price'] != r['robust']['room_price'] for r in rows),
    changed_beds=sum(r['nominal']['bed_price'] != r['robust']['bed_price'] for r in rows),
    changed_flex=sum(r['nominal']['flex_to_private'] != r['robust']['flex_to_private'] for r in rows),
    total_room_price_change=sum(r['robust']['room_price']-r['nominal']['room_price'] for r in rows),
    total_bed_price_change=sum(r['robust']['bed_price']-r['nominal']['bed_price'] for r in rows),
    active_experiments=experiments, monitor=monitor,
    elapsed_seconds=round(monotonic()-started,2),
    interpretation='Mechanical replay; scenario results are not causal or realized revenue gains')
(out/'verification.json').write_text(json.dumps(dict(summary=summary, rows=rows), ensure_ascii=False,
    indent=2, allow_nan=False))
print(json.dumps(summary, ensure_ascii=False, indent=2))

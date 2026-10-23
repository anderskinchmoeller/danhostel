"""Evidence coverage and reported economics; never infer causal lift from correlations."""
import json
from sqlalchemy import select
from . import db, validation


def coverage(session, run):
    recs = session.scalars(select(db.Recommendation).where(db.Recommendation.run_id == run.id)).all() if run else []
    states = {s.day: s for s in session.scalars(select(db.DayState).where(
        db.DayState.day.in_([r.day for r in recs])))} if recs else {}
    room = sum(states[r.day].booked_room_revenue is not None for r in recs if r.day in states)
    bed = sum(states[r.day].booked_bed_revenue is not None for r in recs if r.day in states)
    both = sum(states[r.day].booked_room_revenue is not None and states[r.day].booked_bed_revenue is not None
               for r in recs if r.day in states)
    assignments = {"rooms": {"control": 0, "alternative": 0, "overridden": 0},
                   "beds": {"control": 0, "alternative": 0, "overridden": 0}}
    sensitivity = []
    for rec in recs:
        ladders = json.loads(rec.ladder or "{}")
        for product, counts in assignments.items():
            stress = ((ladders.get(product) or {}).get("bid") or {}).get("uncertainty")
            if stress and stress.get("rows"):
                sensitivity.append(dict(day=rec.day, product=product, **stress))
            assignment = (ladders.get(product) or {}).get("exploration")
            if assignment and assignment.get("distinct_prices"):
                if not assignment.get("assignment_preserved") or rec.status == "locked":
                    counts["overridden"] += 1
                else:
                    counts[assignment["assigned_arm"]] += 1
    return dict(total=len(recs), room_known=room, bed_known=bed, both_known=both,
                missing=len(recs)-both, estimated=sum(r.revenue_basis != "booked_plus_forecast" for r in recs),
                assignments=assignments, sensitivity=sensitivity)


def final_results(session, start, end, today):
    records = session.scalars(select(db.ValidationRecord).where(
        db.ValidationRecord.kind == "outcome", db.ValidationRecord.day.between(start, end),
        db.ValidationRecord.day < today).order_by(db.ValidationRecord.observed_at, db.ValidationRecord.id)).all()
    final = {}
    for record in records:
        data = json.loads(record.payload)
        if data.get("final"):
            final[record.day] = (record, data)
    rows = []
    for day, (record, data) in sorted(final.items()):
        revenue = validation.actual_revenue(data)
        contribution, per_bed = validation.contribution(data)
        available = data.get("available_beds")
        hotel_total=data.get('hotelmanager_lodging_total')
        overview_only=revenue is None and hotel_total is not None
        if overview_only:
            revenue=hotel_total
        rows.append(dict(day=day, source=record.source, data=data, revenue=revenue,
                         overview_only=overview_only,
                         contribution=contribution, contribution_per_bed=per_bed,
                         revpab=revenue/available if revenue is not None and available else None))
    return rows

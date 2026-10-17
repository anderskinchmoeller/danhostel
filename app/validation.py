"""Evidence linking suggested prices, reported applied prices and actual results."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import db
from .adapters.csv_adapter import parse_date, parse_number
from .config import BASE_DIR

PDF_ARCHIVE_ROOT = BASE_DIR / "data" / "validation_pdfs"

LABELS = {
    "day": "dato", "observed_at": "tidspunkt", "run_id": "koersel",
    "baseline_room": "normal_vaerelsespris", "baseline_bed": "normal_sengepris",
    "applied_room": "lagt_ind_vaerelsespris", "applied_bed": "lagt_ind_sengepris",
    "window_start": "periode_fra", "window_end": "periode_til",
    "new_rooms": "nye_vaerelsesbookinger", "new_beds": "nye_sengebookinger",
    "cancelled_rooms": "annullerede_vaerelser", "cancelled_beds": "annullerede_senge",
    "final_rooms": "endeligt_solgte_vaerelser", "final_beds": "endeligt_solgte_senge",
    "room_revenue": "faktisk_vaerelsesomsaetning", "bed_revenue": "faktisk_sengeomsaetning",
    "commission": "faktisk_provision", "variable_cost": "faktiske_variable_omkostninger",
    "available_beds": "tilgaengelige_senge", "group_rooms": "gruppe_vaerelser",
    "group_beds": "gruppe_senge", "blocked_rooms": "blokerede_vaerelser",
    "blocked_beds": "blokerede_senge", "events": "events", "note": "note",
    "final": "afsluttet",
}
PRICE_FIELDS = ("baseline_room", "baseline_bed", "applied_room", "applied_bed")
COUNTS = ("new_rooms", "new_beds", "cancelled_rooms", "cancelled_beds", "final_rooms",
          "final_beds", "available_beds", "group_rooms", "group_beds", "blocked_rooms", "blocked_beds")
MONEY = ("room_revenue", "bed_revenue", "commission", "variable_cost")
FIELDS = {
    "applied": ("day", "observed_at", "run_id", *PRICE_FIELDS, "note"),
    "outcome": ("day", "observed_at", "window_start", "window_end", *COUNTS, *MONEY,
                "events", "final", "note"),
}


def timestamp(value, tz):
    try:
        result = datetime.fromisoformat(value.strip())
        if result.tzinfo is None:
            result = result.replace(tzinfo=ZoneInfo(tz))
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Tidspunkter skal indeholde dato og klokkeslæt") from exc


def prepare(kind, raw, tz, *, now=None):
    if kind not in FIELDS:
        raise ValueError("Ukendt registreringstype")
    now = now or db.utcnow()
    day = parse_date(raw.get("day", ""))
    observed = timestamp(raw.get("observed_at", ""), tz)
    if observed > now + timedelta(minutes=1):
        raise ValueError("Tidspunktet må ikke ligge i fremtiden")
    payload = {}
    for key in FIELDS[kind]:
        value = "" if raw.get(key) is None else str(raw[key]).strip()
        if not value or key in ("day", "observed_at"):
            continue
        if key in PRICE_FIELDS + COUNTS + MONEY + ("run_id",):
            number = parse_number(value)
            if key in PRICE_FIELDS and number <= 0:
                raise ValueError(f"{LABELS[key]} skal være positiv")
            if key in COUNTS + ("run_id",):
                if number < 0 or not number.is_integer():
                    raise ValueError(f"{LABELS[key]} skal være et helt tal på mindst nul")
                number = int(number)
            if key in ("commission", "variable_cost") and number < 0:
                raise ValueError(f"{LABELS[key]} må ikke være negativ")
            payload[key] = number
        elif key in ("window_start", "window_end"):
            payload[key] = timestamp(value, tz).isoformat()+"Z"
        elif key == "final":
            if value.lower() not in ("1", "0", "true", "false", "ja", "nej"):
                raise ValueError("afsluttet skal være ja eller nej")
            payload[key] = value.lower() in ("1", "true", "ja")
        else:
            payload[key] = value
    if kind == "applied":
        if not any(key in payload for key in ("applied_room", "applied_bed")):
            raise ValueError("Angiv mindst én pris, der faktisk er lagt ind")
    else:
        if not any(key in payload for key in COUNTS + MONEY):
            raise ValueError("Angiv mindst ét faktisk resultat")
        flow = any(key in payload for key in ("new_rooms", "new_beds", "cancelled_rooms", "cancelled_beds"))
        if flow or any(k in payload for k in ("window_start", "window_end")):
            if not all(k in payload for k in ("window_start", "window_end")):
                raise ValueError("Nye bookinger og annulleringer kræver både periode fra og til")
            start, end = (timestamp(payload[k], tz) for k in ("window_start", "window_end"))
            if not start < end <= observed:
                raise ValueError("Perioden skal slutte efter start og senest ved registreringstidspunktet")
        if payload.get("final"):
            if day >= observed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).date():
                raise ValueError("Opholdsdatoen skal være afsluttet før det oplyste tidspunkt")
            if any(k not in payload for k in ("final_rooms", "final_beds")):
                raise ValueError("Afsluttede datoer kræver solgte værelser og senge, også nul")
        for group, total in (("group_rooms", "final_rooms"), ("group_beds", "final_beds")):
            if group in payload and total in payload and payload[group] > payload[total]:
                raise ValueError("Gruppeantal må ikke overstige det samlede salg")
    return day, observed, payload


def append(session, kind, day, observed, payload, *, actor, source, run_id=None):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(json.dumps(
        [kind, day.isoformat(), observed.isoformat(), run_id, payload], sort_keys=True,
        ensure_ascii=False).encode()).hexdigest()
    if session.scalar(select(db.ValidationRecord.id).where(db.ValidationRecord.fingerprint == fingerprint)):
        return False
    values = dict(day=day, kind=kind, observed_at=observed, actor=actor,
                  source=source, run_id=run_id, payload=body, fingerprint=fingerprint)
    if session.get_bind().dialect.name == "sqlite":
        result = session.execute(sqlite_insert(db.ValidationRecord).values(**values)
            .on_conflict_do_nothing(index_elements=["fingerprint"]))
        return result.rowcount == 1
    session.add(db.ValidationRecord(**values))
    session.flush()
    return True


def record(session, kind, prepared, *, actor, source):
    day, observed, payload = prepared
    payload = dict(payload)
    run_id = payload.pop("run_id", None) or None
    if kind == "applied" and run_id is not None:
        rec = session.scalar(select(db.Recommendation).where(
            db.Recommendation.run_id == run_id, db.Recommendation.day == day))
        run = session.get(db.Run, run_id)
        if rec is None or run is None or run.status != "done":
            raise ValueError("Kørslen indeholder ikke et prisforslag for denne dato")
        if run.finished and run.finished > observed:
            raise ValueError("Prisen kan ikke være lagt ind før den valgte beregning var færdig")
        payload.update(model_room=rec.room_price, model_bed=rec.bed_price,
                       model_events=rec.event_names,
                       model_status=rec.status,
                       model_forecast_rooms=rec.forecast_rooms, model_forecast_beds=rec.forecast_beds)
        payload["manual_override"] = rec.status == "locked" or any(payload[k] != getattr(rec, attr)
            for k, attr in (("applied_room", "room_price"), ("applied_bed", "bed_price")) if k in payload)
        ladders = json.loads(rec.ladder or "{}")
        for product, key, price_key in (("rooms", "room", "applied_room"), ("beds", "bed", "applied_bed")):
            assignment = (ladders.get(product) or {}).get("exploration")
            if assignment is not None and price_key in payload:
                payload["exploration_"+key] = dict(assignment,
                    confirmed_applied_price=payload[price_key],
                    applied_matches_assignment=abs(payload[price_key]-assignment["assigned_price"]) <= .01)
    added = append(session, kind, day, observed, payload, actor=actor, source=source, run_id=run_id)
    if added:
        db.log(session, "validation_" + kind, actor=actor, day=day, detail=source)
    return added


def parse_csv(text, kind, tz):
    if kind not in FIELDS:
        raise ValueError("Ukendt registreringstype")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=";,\t").delimiter
    except csv.Error:
        delimiter = ";"
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter)
    aliases = {label: key for key, label in LABELS.items()}
    headers = {h: aliases.get(h.strip(), h.strip()) for h in reader.fieldnames or []}
    if any(v not in FIELDS[kind] for v in headers.values()) or len(set(headers.values())) != len(headers):
        raise ValueError("Ukendte eller gentagne kolonner — brug CSV-skabelonen")
    prepared = []
    for line, row in enumerate(reader, 2):
        if not any(row.values()):
            continue
        try:
            if None in row:
                raise ValueError("For mange felter")
            prepared.append(prepare(kind, {headers[k]: v for k, v in row.items()}, tz))
        except ValueError as exc:
            raise ValueError(f"Linje {line}: {exc}") from exc
    if not prepared:
        raise ValueError("CSV-filen indeholder ingen registreringer")
    return prepared


def actual_revenue(payload):
    if any(payload.get(k) is None for k in ('room_revenue','bed_revenue')):
        return None
    return round(payload['room_revenue'] + payload['bed_revenue'] + payload.get('revenue_adjustment',0),2)


def contribution(payload):
    if not payload.get("final") or any(k not in payload for k in MONEY):
        return None, None
    total = round(actual_revenue(payload) - payload["commission"] - payload["variable_cost"], 2)
    available = payload.get("available_beds")
    return total, total / available if available else None


def arrivals_time(text, tz):
    from . import picasso_belaegning as pdf
    printed = pdf.PRINTED_TIME.search(text)
    if not printed:
        raise ValueError("Arrivals-PDF'en mangler udskriftstidspunkt; resultatloggen kræver dato og klokkeslæt")
    observed = datetime.strptime(printed.group(1)+" "+printed.group(2), "%d-%m-%Y %H:%M")
    observed = observed.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).replace(tzinfo=None)
    if observed > db.utcnow()+timedelta(minutes=1):
        raise ValueError("PDF'ens udskriftstidspunkt ligger i fremtiden")
    return observed


def evidence_payload(kind, day, observed, payload):
    """Active-status reports cannot establish totals for already departed stays."""
    payload = dict(payload)
    if kind == "pdf":
        local_day = observed.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo(payload.get("timezone", "Europe/Copenhagen"))).date()
        payload["coverage_complete"] = day >= local_day
        if not payload["coverage_complete"]:
            payload.setdefault("reported_active_rooms", payload.get("rooms_otb"))
            payload.setdefault("reported_active_beds", payload.get("beds_otb"))
            payload.setdefault("active_room_types", payload.get("room_type_otb"))
            payload["rooms_otb"] = payload["beds_otb"] = None
            payload["room_type_otb"] = payload["reservation_amount_allocated"] = None
            prefix = "Historisk nat: rapporten viser kun stadig aktive reservationer; "
            if not payload.get("note", "").startswith(prefix):
                payload["note"] = (prefix + "afrejste gæster mangler. Ingen samlet belægning "
                    "eller omsætning kan udledes. " + payload.get("note", ""))
    return payload


def record_arrivals(session, data, *, filename, tz, actor, text=None):
    """Preserve dated, auditable PDF evidence without inventing publication or revenue."""
    from collections import Counter, defaultdict
    from pathlib import Path
    from . import picasso_belaegning as pdf

    text = text if text is not None else pdf.pdf_text(data)
    start, end, rows = pdf.parse(text)
    observed = arrivals_time(text, tz)
    digest = hashlib.sha256(data).hexdigest()
    counts = defaultdict(Counter)
    totals = defaultdict(float)
    missing = Counter()
    for row in rows:
        if row["st"] not in pdf.COUNTED:
            continue
        if row["days"] <= 0:
            continue
        for n in range(row["days"]):
            night = row["arrival"]+timedelta(days=n)
            if not start <= night <= end:
                continue
            counts[night][row["type"]] += row["pcs"]
            if row["total_price"] is None:
                missing[night] += row["pcs"]
            else:
                # This is only a uniform allocation of Total Price for comparison.
                totals[night] += row["total_price"] / row["days"]
    otb = pdf.on_the_books(start, end, rows)
    added = 0
    for day, (rooms, beds) in otb.items():
        payload = {
            "rooms_otb": rooms, "beds_otb": beds, "room_type_otb": dict(counts[day]),
            "reservation_amount_allocated": round(totals[day], 2) if not missing[day] else None,
            "amount_missing_units": missing[day], "pdf_hash": digest,
            "period_start": start.isoformat(), "period_end": end.isoformat(), "timezone": tz,
            "note": "Total Price fordelt jævnt over opholdsnætterne. Ikke bogført logiomsætning "
                    "og ikke dokumentation for en offentlig salgspris. "
                    + " ".join(pdf.notes(start, rows, observed.replace(tzinfo=timezone.utc).astimezone(
                        ZoneInfo(tz)).date(), pdf.printed_on(text))),
        }
        added += append(session, "pdf", day, observed,
                        evidence_payload("pdf", day, observed, payload), actor=actor,
                        source="Arrivals-PDF: "+filename)
    if added:
        # Keep the original document as evidence; no guest names are copied into the log.
        archive = PDF_ARCHIVE_ROOT
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / (digest+".pdf")
        try:
            with target.open("xb") as fh:
                fh.write(data)
        except FileExistsError:
            pass
        db.log(session, "validation_pdf", actor=actor, new=added,
               detail=json.dumps({"filename": filename, "sha256": digest,
                                  "printed_at": observed.isoformat()+"Z"}, ensure_ascii=False))
    return added


def overview(session, start, end):
    recs = session.scalars(select(db.Recommendation).join(db.Run, db.Run.id == db.Recommendation.run_id)
        .where(db.Recommendation.day.between(start, end), db.Run.status == "done")
        .order_by(db.Recommendation.run_id.desc())).all()
    records = session.scalars(select(db.ValidationRecord).where(db.ValidationRecord.day.between(start, end))
        .order_by(db.ValidationRecord.observed_at.desc(), db.ValidationRecord.id.desc())).all()
    days = {}
    for rec in recs:
        days.setdefault(rec.day, {"day": rec.day, "rec": rec, "history": []})
    for r in records:
        row = days.setdefault(r.day, {"day": r.day, "rec": None, "history": []})
        data = evidence_payload(r.kind, r.day, r.observed_at, json.loads(r.payload))
        row["history"].append({"record": r, "data": data})
        if r.kind != "pdf" or data["coverage_complete"]:
            row.setdefault(r.kind, {"record": r, "data": data})
    for row in days.values():
        outcome = row.get("outcome", {}).get("data", {})
        row["contribution"], row["per_bed"] = contribution(outcome)
    return [days[d] for d in sorted(days)]

"""Human-confirmed prices and reported actuals; no writes to Picasso."""
import csv
import io
import json
import re
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select

from . import db, economics, validation, picasso_results, forecast_charts, forecast_validation as forecasts


def router(settings, templates, current_user):
    routes = APIRouter()

    def period(start, end):
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        first = validation.parse_date(start) if start else today - timedelta(days=30)
        last = validation.parse_date(end) if end else today + timedelta(days=120)
        if last < first or (last-first).days > 366:
            raise ValueError("Vælg en periode på højst 367 dage")
        return first, last

    @routes.get("/resultater/oekonomi")
    def economics_page(request: Request, start: str = "", end: str = "", user: str = Depends(current_user)):
        try:
            first, last = period(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        with db.get_session() as session:
            run = session.scalar(select(db.Run).where(db.Run.status == "done").order_by(db.Run.id.desc()))
            coverage = economics.coverage(session, run)
            results = economics.final_results(session, first, last, today)
            return templates.TemplateResponse(request, "economics.html", {
                "settings": settings, "user": user, "coverage": coverage, "rows": results, "run": run,
                "start": first.isoformat(), "end": last.isoformat(),
                "revenue_dates": sum(r["revenue"] is not None for r in results),
                "contribution_dates": sum(r["contribution"] is not None for r in results),
            })

    def chart_data(source, start, end, measure, lead):
        if source not in ("", "live", "historical") or measure not in forecast_charts.MEASURES or lead not in forecasts.CHECKPOINTS:
            raise HTTPException(422, "Ukendt kilde, måling eller prognosehorisont")
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        with db.get_session() as session:
            live, pending = forecast_charts.live_rows(session, today)
        source = source or ("live" if live else "historical")
        if source == "historical":
            first = validation.parse_date(start) if start else datetime(2025, 3, 8).date()
            last = validation.parse_date(end) if end else datetime(2025, 12, 31).date()
            rows = forecast_charts.historical_rows()
        else:
            first, last = period(start, end)
            rows = live
        if last < first or (last-first).days > 366:
            raise HTTPException(422, "Vælg en periode på højst 367 dage")
        return dict(forecast_charts.diagnostics(rows, first, last, measure, lead),
            source=source, start=first.isoformat(), end=last.isoformat(), measure=measure, lead=lead,
            pending=pending, measures=forecast_charts.MEASURES, horizons=forecasts.CHECKPOINTS)

    @routes.get("/resultater/afvigelser")
    def deviations(request: Request, source: str = "", start: str = "", end: str = "", measure: str = "rum",
                   lead: int = 7, user: str = Depends(current_user)):
        try:
            data = chart_data(source, start, end, measure, lead)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return templates.TemplateResponse(request, "deviations.html", dict(data, settings=settings, user=user))

    @routes.get("/resultater/afvigelser.csv")
    def deviations_export(source: str = "", start: str = "", end: str = "", measure: str = "rum", lead: int = 7,
                          user: str = Depends(current_user)):
        try:
            data = chart_data(source, start, end, measure, lead)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(["kilde","opholdsdato","maaling","dage_foer","model","faktisk","forskel_model_minus_faktisk","reference","koersel","prognose_gemt_utc"])
        for row in data["rows"]:
            values = [data["source"],row["day"],measure,lead,row["forecast"],row["actual"],
                      row["forecast"]-row["actual"],row.get("baseline"),row.get("run_id"),row.get("observed_at")]
            writer.writerow([str(v).replace(".",",") if isinstance(v,float) else v for v in values])
        return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="prognoseafvigelser.csv"'})

    @routes.get("/resultater/prognoser")
    def forecast_page(request: Request, segment: str = "all", user: str = Depends(current_user)):
        if segment not in forecasts.SEGMENTS:
            raise HTTPException(422, "Ukendt gruppe")
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        with db.get_session() as session:
            rows, n_snapshots, n_dates = forecasts.live_rows(session, today)
        history = json.loads(forecasts.REPORT_PATH.read_text()) if forecasts.REPORT_PATH.exists() else None
        return templates.TemplateResponse(request, "forecasts.html", {
            "settings": settings, "user": user, "segment": segment, "segments": forecasts.SEGMENTS,
            "live": [r for r in forecasts.score(rows) if r["segment"] == segment],
            "history": history, "historical": [r for r in history["metrics"] if r["segment"] == segment] if history else [],
            "n_snapshots": n_snapshots, "n_dates": n_dates, "n_scored": len(rows),
            "products": forecasts.PRODUCTS,
        })

    @routes.get("/resultater/prognoser.csv")
    def forecast_export(user: str = Depends(current_user)):
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        with db.get_session() as session:
            rows, _, _ = forecasts.live_rows(session, today)
        return Response(forecasts.csv_details(rows), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="prognosevalidering_drift.csv"'})

    @routes.get("/resultater")
    def page(request: Request, start: str = "", end: str = "", message: str = "", error: str = "",
             user: str = Depends(current_user)):
        try:
            first, last = period(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with db.get_session() as session:
            rows = validation.overview(session, first, last)
            runs = session.scalars(select(db.Run).where(db.Run.status == "done")
                                   .order_by(db.Run.id.desc()).limit(60)).all()
            now = datetime.now(ZoneInfo(settings.timezone)).replace(microsecond=0)
            return templates.TemplateResponse(request, "results.html", {
                "settings": settings, "user": user, "rows": rows, "runs": runs,
                "report_pairs": session.scalars(select(db.ValidationRecord).where(
                    db.ValidationRecord.kind == "report_pair").order_by(db.ValidationRecord.id.desc()).limit(20)).all(),
                "start": first.isoformat(), "end": last.isoformat(), "message": message,
                "error": error, "now": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "today": now.date().isoformat(), "labels": {
                    **{k: v.replace("_", " ") for k, v in validation.LABELS.items()},
                    "model_room": "Modellens værelsespris", "model_bed": "Modellens sengepris",
                    "manual_override": "Afviger fra prisforslag", "model_events": "Events ved beregningen",
                    "model_status": "Forslagets status ved registreringen",
                    "model_forecast_rooms": "Prognose værelser", "model_forecast_beds": "Prognose senge",
                    "rooms_otb": "Værelser på bøgerne", "beds_otb": "Senge på bøgerne",
                    "room_type_otb": "Solgt pr. rumtype", "imported_room_price": "Pris oplyst i belægningsfil",
                    "imported_bed_price": "Sengepris oplyst i belægningsfil",
                    "booked_room_revenue": "Oplyst bogført værelsesomsætning",
                    "booked_bed_revenue": "Oplyst bogført sengeomsætning",
                    "reservation_amount_allocated": "Reservationsbeløb fordelt pr. nat (skøn)",
                    "amount_missing_units": "Enheder uden reservationsbeløb",
                    "period_start": "Rapportperiode fra", "period_end": "Rapportperiode til",
                    "coverage_complete": "Komplet dækning af aktive reservationer",
                    "reported_active_rooms": "Stadig aktive værelsesenheder (ufuldstændig historik)",
                    "reported_active_beds": "Stadig aktive sengeenheder (ufuldstændig historik)",
                    "active_room_types": "Rumtyper på stadig aktive reservationer",
                    "timezone": "Tidszone", "revenue_adjustment": "Ufordelt afrunding i samlet omsætning",
                    "reported_lodging_total": "Logi i alt fra produktudtræk", "breakfast_excluded": "Morgenmad fratrukket",
                    "result_report_id": "Rapportpar til salgstal", "revenue_report_id": "Rapportpar til omsætning",
                    "report_counts_confirmed": "Endeligt salg kontrolleret", "revenue_date_confirmed": "Opholdsdato og fradrag kontrolleret",
                    "revenue_basis": "Grundlag for omsætning", "carried_forward_from_record": "Oplysninger videreført fra registrering",
                },
                "counts": validation.COUNTS, "money": validation.MONEY,
                "time_local": lambda t: t.replace(tzinfo=ZoneInfo("UTC")).astimezone(
                    ZoneInfo(settings.timezone)).strftime("%d-%m-%Y %H:%M:%S"),
            })

    @routes.post("/resultater/rapporter")
    async def report_upload(hotelmanager: UploadFile = File(...), products: UploadFile = File(...),
                            user: str = Depends(current_user)):
        with db.get_session() as session:
            try:
                hotel_data = await hotelmanager.read(20 * 1024 * 1024 + 1)
                product_data = await products.read(20 * 1024 * 1024 + 1)
                if any(len(data) > 20 * 1024 * 1024 for data in (hotel_data, product_data)):
                    raise ValueError("Hver PDF må højst fylde 20 MB")
                record = picasso_results.stage(session, hotel_data, product_data, settings, user,
                    hotel_name=hotelmanager.filename, product_name=products.filename)
                session.commit()
                return RedirectResponse(f"/resultater/rapporter/{record.id}", status_code=303)
            except (ValueError, OSError) as exc:
                session.rollback()
                message = str(exc) if isinstance(exc, ValueError) else "Kildefilerne kunne ikke gemmes; ingen resultater blev registreret"
                return RedirectResponse("/resultater?error="+quote(message), status_code=303)

    def report_record(session, report_id):
        record = session.get(db.ValidationRecord, report_id)
        if record is None or record.kind != "report_pair":
            raise HTTPException(404, "Rapportpar ikke fundet")
        return record

    @routes.get("/resultater/rapporter/{report_id}")
    def report_review(request: Request, report_id: int, message: str = "", error: str = "",
                      user: str = Depends(current_user)):
        with db.get_session() as session:
            record = report_record(session, report_id)
            payload = json.loads(record.payload)
            outcomes = session.scalars(select(db.ValidationRecord).where(
                db.ValidationRecord.kind == "outcome", db.ValidationRecord.day == record.day)
                .order_by(db.ValidationRecord.observed_at.desc(), db.ValidationRecord.id.desc())).all()
            latest = json.loads(outcomes[0].payload) if outcomes else {}
            return templates.TemplateResponse(request, "report_review.html", {
                "settings": settings, "user": user, "record": record, "payload": payload,
                "report": picasso_results.review(payload), "message": message, "error": error,
                "latest": latest,
                "time_local": lambda value: validation.timestamp(value, settings.timezone).replace(
                    tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(settings.timezone)).strftime("%d-%m-%Y %H:%M"),
            })

    @routes.post("/resultater/rapporter/{report_id}/bekraeft")
    def report_confirm(report_id: int, final_counts: bool = Form(False), accept_revenue: bool = Form(False),
                       date_confirmed: bool = Form(False), note: str = Form(""),
                       commission: str = Form(""), variable_cost: str = Form(""), available_beds: str = Form(""),
                       user: str = Depends(current_user)):
        with db.get_session() as session:
            record = report_record(session, report_id)
            try:
                added = picasso_results.confirm(session, record, actor=user, final_counts=final_counts,
                    accept_revenue=accept_revenue, date_confirmed=date_confirmed, note=note,
                    supplements=dict(commission=commission, variable_cost=variable_cost, available_beds=available_beds))
                session.commit()
                message = "Kontrolleret resultat gemt" if added else "Resultatet er allerede gemt"
                return RedirectResponse(f"/resultater/rapporter/{report_id}?message="+quote(message), status_code=303)
            except ValueError as exc:
                session.rollback()
                return RedirectResponse(f"/resultater/rapporter/{report_id}?error="+quote(str(exc)), status_code=303)

    @routes.post("/resultater/registrer")
    async def record(request: Request, kind: str = Form(...), user: str = Depends(current_user)):
        with db.get_session() as session:
            try:
                raw = dict(await request.form())
                prepared = validation.prepare(kind, raw, settings.timezone)
                added = validation.record(session, kind, prepared, actor=user, source="Manuelt registreret")
                session.commit()
                message = "Registreringen er gemt" if added else "Registreringen findes allerede"
                return RedirectResponse("/resultater?message="+quote(message), status_code=303)
            except ValueError as exc:
                session.rollback()
                return RedirectResponse("/resultater?error="+quote(str(exc)), status_code=303)

    @routes.post("/resultater/import")
    async def upload(kind: str = Form(...), file: UploadFile = File(...), user: str = Depends(current_user)):
        with db.get_session() as session:
            try:
                rows = validation.parse_csv((await file.read()).decode("utf-8-sig"), kind, settings.timezone)
                count = sum(validation.record(session, kind, row, actor=user,
                            source="CSV: "+(file.filename or "uden navn")) for row in rows)
                session.commit()
                return RedirectResponse("/resultater?message="+quote(
                    f"{count} nye registreringer gemt; {len(rows)-count} dubletter sprunget over"), status_code=303)
            except ValueError as exc:
                session.rollback()
                return RedirectResponse("/resultater?error="+quote(str(exc)), status_code=303)

    @routes.get("/resultater/skabelon.csv")
    def template(kind: str, user: str = Depends(current_user)):
        if kind not in validation.FIELDS:
            raise HTTPException(422, "Ukendt registreringstype")
        buffer = io.StringIO()
        csv.writer(buffer, delimiter=";").writerow([validation.LABELS[k] for k in validation.FIELDS[kind]])
        return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="resultatlog_{kind}.csv"'})

    @routes.get("/resultater/export.csv")
    def export(start: str = "", end: str = "", user: str = Depends(current_user)):
        try:
            first, last = period(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with db.get_session() as session:
            rows = validation.overview(session, first, last)
            fields = ["dato", "model_koersel", "model_vaerelsespris", "model_sengepris",
                      "priser_lagt_ind_tidspunkt", "priser_kilde", "priser_registreret_af",
                      *[validation.LABELS[k] for k in validation.PRICE_FIELDS],
                      "resultat_tidspunkt", "resultat_kilde", "afsluttet",
                      "periode_fra", "periode_til", "pdf_tidspunkt", "pdf_kilde",
                      "pdf_vaerelser", "pdf_senge", "pdf_rumtyper", "pdf_reservationsbeloeb_fordelt",
                      *[validation.LABELS[k] for k in validation.COUNTS + validation.MONEY],
                      "omsaetning_afrunding_ufordelt", "logi_i_alt", "morgenmad_fratrukket", "rapportpar_id", "logi_pr_rumtype_json",
                      "daekningsbidrag", "daekningsbidrag_pr_tilgaengelig_seng", "events", "note"]
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=";")
            writer.writeheader()
            for row in rows:
                a, o = row.get("applied", {}), row.get("outcome", {})
                applied, outcome = a.get("data", {}), o.get("data", {})
                pdf = row.get("pdf", {})
                pdf_data = pdf.get("data", {})
                rec = row["rec"]
                data = {"dato": row["day"].isoformat(),
                    "model_koersel": a["record"].run_id if a else rec.run_id if rec else None,
                    "model_vaerelsespris": applied.get("model_room") if a else rec.room_price if rec else None,
                    "model_sengepris": applied.get("model_bed") if a else rec.bed_price if rec else None,
                    "priser_lagt_ind_tidspunkt": a["record"].observed_at.isoformat()+"Z" if a else None,
                    "priser_kilde": a["record"].source if a else None,
                    "priser_registreret_af": a["record"].actor if a else None,
                    "resultat_tidspunkt": o["record"].observed_at.isoformat()+"Z" if o else None,
                    "resultat_kilde": o["record"].source if o else None,
                    "afsluttet": outcome.get("final"),
                    "periode_fra": outcome.get("window_start"), "periode_til": outcome.get("window_end"),
                    "pdf_tidspunkt": pdf["record"].observed_at.isoformat()+"Z" if pdf else None,
                    "pdf_kilde": pdf["record"].source if pdf else None,
                    "pdf_vaerelser": pdf_data.get("rooms_otb"), "pdf_senge": pdf_data.get("beds_otb"),
                    "pdf_rumtyper": json.dumps(pdf_data["room_type_otb"]) if pdf_data else None,
                    "pdf_reservationsbeloeb_fordelt": pdf_data.get("reservation_amount_allocated"),
                    "omsaetning_afrunding_ufordelt": outcome.get("revenue_adjustment"),
                    "logi_i_alt": validation.actual_revenue(outcome),
                    "morgenmad_fratrukket": outcome.get("breakfast_excluded"),
                    "rapportpar_id": outcome.get("result_report_id"),
                    "logi_pr_rumtype_json": json.dumps(outcome["amounts_by_type"], ensure_ascii=False) if "amounts_by_type" in outcome else None,
                    "daekningsbidrag": row["contribution"],
                    "daekningsbidrag_pr_tilgaengelig_seng": row["per_bed"],
                    "events": outcome.get("events", applied.get("model_events", rec.event_names if rec else None)),
                    "note": " | ".join(v for v in (applied.get("note"), outcome.get("note")) if v),
                }
                data.update({validation.LABELS[k]: applied.get(k) for k in validation.PRICE_FIELDS})
                data.update({validation.LABELS[k]: outcome.get(k) for k in validation.COUNTS + validation.MONEY})
                writer.writerow({k: str(v).replace(".", ",") if isinstance(v, float) else v for k, v in data.items()})
            return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="resultatlog.csv"'})

    @routes.get("/resultater/historik.csv")
    def history(start: str = "", end: str = "", user: str = Depends(current_user)):
        try:
            first, last = period(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with db.get_session() as session:
            records = session.scalars(select(db.ValidationRecord).where(
                db.ValidationRecord.day.between(first, last)).order_by(db.ValidationRecord.id)).all()
            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter=";")
            writer.writerow(["id", "dato", "type", "opgjort_tidspunkt_utc", "gemt_tidspunkt_utc",
                             "kilde", "bruger", "koersel", "oplysninger_json"])
            for r in records:
                writer.writerow([r.id, r.day.isoformat(), r.kind, r.observed_at.isoformat()+"Z",
                    r.recorded_at.isoformat()+"Z", r.source, r.actor, r.run_id,
                    json.dumps(validation.evidence_payload(r.kind, r.day, r.observed_at,
                                                          json.loads(r.payload)), ensure_ascii=False)])
            return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="resultatlog_historik.csv"'})

    @routes.get("/resultater/pdf/{digest}")
    def original_pdf(digest: str, user: str = Depends(current_user)):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(404, "PDF ikke fundet")
        path = validation.PDF_ARCHIVE_ROOT / (digest+".pdf")
        if not path.is_file():
            raise HTTPException(404, "PDF ikke fundet")
        return FileResponse(path, media_type="application/pdf", filename="Picasso-rapport.pdf",
                            content_disposition_type="inline")

    return routes

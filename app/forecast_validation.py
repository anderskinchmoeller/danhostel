"""Frozen operational checkpoints and strictly out-of-time sales validation.

Actual sales are measurable; price counterfactuals and unconstrained demand are not.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import statistics
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from . import cube, db, demand, forecast_metrics

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "data" / "forecast_validation_history.json"
OBSERVATIONS_PATH = ROOT / "data" / "forecast_validation_observations.json"
CHECKPOINTS = (1, 7, 14, 30)
PRODUCTS = {"rum": ("Værelser", "final_rooms", "group_rooms"),
            "senge": ("Senge", "final_beds", "group_beds")}
SEGMENTS = {"all": "Alle", "weekday": "Hverdage", "weekend": "Weekender",
            "groups": "Med grupper", "no_groups": "Uden grupper", "unknown_groups": "Grupper ukendt",
            "busy": "Travle (mindst 90 % solgt)", "ordinary": "Under 90 % solgt",
            "censored": "Historisk kapacitetsmærket", "uncensored": "Uden kapacitetsmærke"}


def baseline(rows, product):
    """Simple weekday/month mean, trained only on completed training dates."""
    cells = {}
    for r in rows:
        if r["lead"] == 0:
            cells.setdefault(f"{r['ugedag']}|{r['maaned']}", []).append(r[f"endelig_{product}"])
    return {k: statistics.mean(v) for k, v in cells.items()}


@lru_cache(maxsize=4)
def live_baselines(asof, mtime):
    # Dates after the cutoff can never supply the reference for that forecast.
    path = ROOT / "kali" / "cube.csv"
    rows = [r for r in cube.load(path) if r["dato"] < asof
            and not "2025-01-01" <= r["dato"] <= "2025-03-07"]
    return {p: baseline(rows, p) for p in PRODUCTS}


def save_checkpoints(session, run, recs, inputs, params, today, tz, revenue_settings=None):
    if not params.v4_active:
        return 0
    observed = db.utcnow()
    # A caller simulating another day must not manufacture prospective evidence.
    if observed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).date() != today:
        return 0
    path = ROOT / "kali" / "cube.csv"
    refs = live_baselines(today.isoformat(), path.stat().st_mtime_ns) if path.exists() else {}
    fingerprint = hashlib.sha256(json.dumps({p: asdict(params.demand_models[p]) for p in PRODUCTS},
                                            sort_keys=True).encode()).hexdigest()
    by_day = {r.day: r for r in inputs}
    added = 0
    for rec in recs:
        lead = (rec.day - today).days
        if lead not in CHECKPOINTS:
            continue
        item = by_day[rec.day]
        for index, (product, (_, final_key, _)) in enumerate(PRODUCTS.items()):
            rooms = product == "rum"
            cap = (params.inventory.max_room_capacity - item.blocked_rooms if rooms else
                   params.inventory.max_bed_capacity - item.blocked_beds)
            otb = item.rooms_otb if rooms else item.beds_otb
            value = demand.sales_forecast(params.demand_models[product], otb, lead, rec.day,
                                          cap, params.demand_level[index])
            saved_point = rec.forecast_rooms if rooms else rec.forecast_beds
            if abs(value["forecast"] - saved_point) > 1e-7:
                raise ValueError("Valideringsprognosen afviger fra prisberegningen")
            ref = refs.get(product, {}).get(f"{rec.day.weekday()}|{rec.day.month}")
            value.update(baseline=ref, model="v4-referencepris", model_hash=fingerprint,
                         level=params.demand_level[index], weekend=params.is_weekend(rec.day),
                         events=list(rec.event_names), baseline_cutoff=today.isoformat(),
                         baseline_source="Historisk gennemsnit for ugedag og måned; renovering udeladt")
            if rooms:
                policy = revenue_settings or {}
                basis = policy.get("price_vat_basis", "unknown")
                total = rec.revpab * params.inventory.total_beds
                # The current engine may combine actual booked values and prices.
                # Their tax bases must not be silently mixed or inferred.
                known_booked = bool(item.booked_room_revenue or item.booked_bed_revenue)
                expected = (total if basis == "ex_vat" else total / 1.25 if basis == "incl_25" else None)
                if known_booked or not policy.get("accommodation_only_confirmed", False):
                    expected = None
                value["revenue_checkpoint"] = dict(expected_raw=total, revpab_raw=rec.revpab,
                    available_beds=params.inventory.total_beds, basis=basis, estimate_basis=rec.revenue_basis,
                    expected_ex_vat=expected, mixed_booked_values=known_booked,
                    accommodation_only_confirmed=policy.get("accommodation_only_confirmed", False))
            result = session.execute(insert(db.ForecastSnapshot).values(
                day=rec.day, lead=lead, product=product, run_id=run.id, observed_at=observed,
                payload=json.dumps(value, ensure_ascii=False)).on_conflict_do_nothing(
                    index_elements=["day", "lead", "product"]))
            added += result.rowcount
    return added


def tags(row):
    out = ["all", "weekend" if row["weekend"] else "weekday"]
    group = row.get("groups")
    out.append("unknown_groups" if group is None else "groups" if group > 0 else "no_groups")
    if row.get("capacity", 0) > 0:
        out.append("busy" if row["actual"] / row["capacity"] >= .9 else "ordinary")
    if row.get("censored") is not None:
        out.append("censored" if row["censored"] else "uncensored")
    return out


def score(rows):
    """One stay date per product/checkpoint; paired bootstrap resamples dates."""
    buckets = {}
    for row in rows:
        for segment in tags(row):
            buckets.setdefault((row["product"], row["lead"], segment), []).append(row)
    out = []
    for (product, lead, segment), items in sorted(buckets.items()):
        errors = [r["forecast"] - r["actual"] for r in items]
        paired = [r for r in items if r.get("baseline") is not None]
        diffs = [abs(r["baseline"]-r["actual"]) - abs(r["forecast"]-r["actual"]) for r in paired]
        ci = forecast_metrics.inference([(r["day"],abs(r["baseline"]-r["actual"])-abs(r["forecast"]-r["actual"]))
                                         for r in paired], lead)["ci"]
        coverages = {}
        widths = {}
        for level in ("50", "80", "90"):
            eligible = [r for r in items if level in r.get("intervals", {})]
            coverages[level] = (sum(r["intervals"][level][0] <= r["actual"] <= r["intervals"][level][1]
                                    for r in eligible) / len(eligible)) if eligible else None
            widths[level] = statistics.fmean(r["intervals"][level][1]-r["intervals"][level][0]
                                             for r in eligible) if eligible else None
        out.append(dict(product=product, lead=lead, segment=segment, n=len(items), n_baseline=len(paired),
                        mae=statistics.fmean(abs(e) for e in errors), bias=statistics.fmean(errors),
                        baseline_mae=statistics.fmean(abs(r["baseline"]-r["actual"]) for r in paired) if paired else None,
                        paired_model_mae=statistics.fmean(abs(r["forecast"]-r["actual"]) for r in paired) if paired else None,
                        advantage=statistics.fmean(diffs) if diffs else None, advantage_ci=ci,
                        coverage=coverages, interval_width=widths))
    return out


def live_rows(session, today):
    finals = {}
    records = session.scalars(select(db.ValidationRecord).where(
        db.ValidationRecord.kind == "outcome", db.ValidationRecord.day < today).order_by(
            db.ValidationRecord.observed_at, db.ValidationRecord.id)).all()
    for record in records:
        payload = json.loads(record.payload)
        if payload.get("final"):
            finals[record.day] = payload
    snapshots = session.scalars(select(db.ForecastSnapshot).order_by(db.ForecastSnapshot.day)).all()
    rows = []
    for snapshot in snapshots:
        actual = finals.get(snapshot.day)
        _, final_key, group_key = PRODUCTS[snapshot.product]
        if actual is None or actual.get(final_key) is None:
            continue
        payload = json.loads(snapshot.payload)
        rows.append(dict(payload, day=snapshot.day.isoformat(), lead=snapshot.lead, product=snapshot.product,
                         run_id=snapshot.run_id, observed_at=snapshot.observed_at.isoformat()+"Z",
                         actual=actual[final_key], groups=actual.get(group_key), censored=None))
    return rows, len(snapshots), len({s.day for s in snapshots})


def historical_report(rows, params):
    train = [r for r in rows if r["dato"] < "2025-01-01"]
    test = [r for r in rows if "2025-03-07" < r["dato"] < "2026-01-01" and r["lead"] in CHECKPOINTS]
    models = demand.fit_all(train)
    refs = {p: baseline(train, p) for p in PRODUCTS}
    observations = []
    for row in test:
        day = date.fromisoformat(row["dato"])
        for product in PRODUCTS:
            cap = params.inventory.max_room_capacity if product == "rum" else params.inventory.max_bed_capacity
            value = demand.sales_forecast(models[product], row[f"otb_{product}"], row["lead"], day, cap)
            # Historical cube stores the group split as OTB group + remaining group.
            groups = row.get(f"otb_{product}_gruppe", 0) + row.get(f"rest_{product}_gruppe", 0)
            observations.append(dict(value, day=row["dato"], lead=row["lead"], product=product,
                actual=row[f"endelig_{product}"], groups=groups, weekend=params.is_weekend(day),
                censored=bool(row["censureret"]), baseline=refs[product].get(f"{day.weekday()}|{day.month}")))
    model_hash = hashlib.sha256(json.dumps({p: asdict(models[p]) for p in PRODUCTS}, sort_keys=True).encode()).hexdigest()
    return dict(created_at=db.utcnow().isoformat()+"Z", training="2024", test="2025-03-08 – 2025-12-31",
                model_hash=model_hash, levels=[1.0, 1.0],
                capacity={"rum": params.inventory.max_room_capacity, "senge": params.inventory.max_bed_capacity},
                training_dates=len({r["dato"] for r in train}), test_dates=len({r["day"] for r in observations}),
                caveats=["Fast model trænet på 2024. Ingen efterfølgende læring eller niveauer fra 2026 i testen.",
                         "Måler observeret salg, ikke skjult efterspørgsel eller omsætningsgevinst.",
                         "Grupper er historikkens regel: mindst fire enheder på samme reservation/ankomst.",
                         "Kapacitetsmærket gælder hele datoen, hvis et af lagrene er mindst 98 % solgt.",
                         "Maksimal nuværende kapacitet bruges; historiske blokeringer og ændret flexfordeling er ukendte.",
                         "Intervallerne beskriver salg ved referenceprisen. Prisændringer kan påvirke afvigelserne.",
                         "Travle datoer og grupper opdeles efter facit, kun til efterfølgende fejlanalyse.",
                         "95 %-interval bruger kalenderbaseret Newey–West/HAC med lag max(7, horisont−1), mindst 60 datoer og 8 blokke; tilnærmet punktvist interval."],
                metrics=score(observations)), observations


def bed_regime_report(rows, params, *, training_from="2025-03-08", test_from="2025-10-01"):
    """Development comparison, separate from the frozen 2024→2025 report.

    Both candidates use only observations before the test period. Results
    informed model selection; they are not an independent approval test.
    """
    date.fromisoformat(training_from)
    date.fromisoformat(test_from)
    train = [r for r in rows if r["dato"] < test_from]
    test = [r for r in rows if test_from <= r["dato"] < "2026-01-01" and r["lead"] in CHECKPOINTS]
    metrics = []
    baseline_model = None
    for label, training in (("all_history", train),
                             ("recent_beds", [r for r in train if r["dato"] >= training_from])):
        model = demand.fit(training, kind="senge", column="rest_senge", final_column="endelig_senge",
                           exclude_ranges=(("2025-01-01", "2025-03-07"),))
        if baseline_model is None:
            baseline_model = model
        else:
            # Use the production recipe, including seasonal factors for
            # months absent from the recent training period.
            model.season.update({m: value for m, value in baseline_model.season.items()
                                 if m not in model.season})
        observations = []
        for row in test:
            day = date.fromisoformat(row["dato"])
            value = demand.sales_forecast(model, row["otb_senge"], row["lead"], day,
                                           params.inventory.max_bed_capacity)
            observations.append(dict(value, day=row["dato"], lead=row["lead"], product="senge",
                actual=row["endelig_senge"], groups=None, censored=bool(row["censureret"]),
                weekend=params.is_weekend(day), baseline=None))
        metrics.extend(dict(m, candidate=label) for m in score(observations) if m["segment"] == "all")
    return dict(created_at=db.utcnow().isoformat()+"Z", training_from=training_from,
        training_through=(date.fromisoformat(test_from)-timedelta(days=1)).isoformat(),
        test_from=test_from, test_through="2025-12-31", levels=[1.0, 1.0],
        purpose="Development comparison; used for selection, not an independent acceptance test",
        caveats=["Ingen træningsobservationer fra testperioden.",
                 "Rekonstrueret historik; blokeringer, flex og ændringshistorik er ukendte.",
                 "Måler salg ved historiske priser, ikke priseffekt eller omsætningsgevinst.",
                 "Nyere fremadrettede resultater skal fortsat indsamles."], metrics=metrics)


def csv_details(rows):
    buffer = io.StringIO()
    fields = ["day", "product", "lead", "run_id", "observed_at", "forecast", "actual", "baseline",
              "capacity", "groups", "weekend", "censored", "intervals", "model_hash"]
    writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row, intervals=json.dumps(row.get("intervals", {}))))
    return buffer.getvalue().encode("utf-8-sig")


def main():
    from .config import load_settings
    report, rows = historical_report(cube.load(ROOT / "kali" / "cube.csv"), load_settings().params)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    tmp.replace(REPORT_PATH)
    observations_tmp = OBSERVATIONS_PATH.with_suffix(".tmp")
    observations_tmp.write_text(json.dumps(dict(model_hash=report["model_hash"],rows=rows),ensure_ascii=False))
    observations_tmp.replace(OBSERVATIONS_PATH)
    output = ROOT / "outputs" / ("prognosevalidering-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True)
    (output / "historisk.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (output / "observationer.csv").write_bytes(csv_details(rows))
    print(f"Træning: {report['training_dates']} datoer. Test: {report['test_dates']} datoer. Niveauer 1,0.")
    for line in report["metrics"]:
        if line["segment"] == "all":
            print(f"{PRODUCTS[line['product']][0]:10} {line['lead']:2} dage: n={line['n']}, MAE={line['mae']:.2f}, "
                  f"reference={line['baseline_mae']:.2f}, dækning 80 %={line['coverage']['80']:.1%}")
    print(f"Gemte rapport og observationer: {output}")


if __name__ == "__main__":
    main()

"""Recent operational forecast checks; no drift-test p-values or price-effect claims."""
from datetime import date, timedelta
import statistics


def recent(rows, today, *, window_days=28, min_dates=14):
    """One observation per product/horizon/stay date; no future or missing outcomes.

    Thresholds are review rules, not calibrated hypothesis tests. Repeated daily
    checks do not estimate causal revenue lift or automatically retrain a model.
    """
    start = today - timedelta(days=window_days)
    unique = {}
    for row in rows:
        day = date.fromisoformat(row["day"])
        if start <= day < today and row.get("actual") is not None:
            unique[(row["product"], row["lead"], row["day"])] = row
    out = []
    for product in ("rum", "senge"):
        for lead in (1, 7, 14, 30):
            items = [r for (p, h, _), r in unique.items() if p == product and h == lead]
            errors = [r["forecast"]-r["actual"] for r in items]
            paired = [r for r in items if r.get("baseline") is not None]
            intervals = [r for r in items if "80" in r.get("intervals", {})]
            coverage = statistics.fmean(r["intervals"]["80"][0] <= r["actual"] <=
                                       r["intervals"]["80"][1] for r in intervals) if intervals else None
            reasons = []
            status = "insufficient" if len(items) < min_dates else "no_flag"
            if status != "insufficient":
                actual_mean = statistics.fmean(r["actual"] for r in items)
                if abs(statistics.fmean(errors)) > max(1, .2 * actual_mean):
                    reasons.append("Vedvarende over-/underskøn i det seneste vindue")
                if len(intervals) >= min_dates and coverage < .6:
                    reasons.append("Under 60 % af resultaterne ligger i modellens 80 %-interval")
                if len(paired) >= min_dates:
                    model_mae = statistics.fmean(abs(r["forecast"]-r["actual"]) for r in paired)
                    base_mae = statistics.fmean(abs(r["baseline"]-r["actual"]) for r in paired)
                    if model_mae > max(base_mae * 1.2, base_mae + .5):
                        reasons.append("Større fejl end simpel reference på de samme datoer")
                if reasons:
                    status = "review"
            out.append(dict(product=product, lead=lead, n=len(items), status=status,
                mae=statistics.fmean(abs(e) for e in errors) if errors else None,
                bias=statistics.fmean(errors) if errors else None, coverage80=coverage,
                n_intervals=len(intervals), n_paired=len(paired), reasons=reasons,
                model_versions=len({r.get("model_hash") for r in items})))
    return dict(start=start.isoformat(), end=(today-timedelta(days=1)).isoformat(),
                min_dates=min_dates, rows=out, purpose="Operational review, not a significance test")

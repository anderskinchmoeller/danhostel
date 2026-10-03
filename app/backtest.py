"""Backtest: version 3 mod version 4 på den tilstand der faktisk stod på bøgerne.

Roadmappens Tier 3 foreslår at køre modellen på historikken. Med bookingkuben
kan det gøres rigtigt: ikke mod en gennemsnitskurve, men mod det der stod på
bøgerne den enkelte dag, for hver dato og hvert lead time.

Backtesten svarer på to spørgsmål, og det er vigtigt at holde dem adskilt.

**Hvor godt rammer prognosen?** Det kan måles uden at antage noget om priser.
Version 3 giver ét tal; version 4 giver en fordeling. Begge sammenlignes med
det der faktisk skete, og version 4 bedømmes også på dækning: lander de
faktiske tal inden for 10-90 %-intervallet i 80 % af tilfældene? Rammer
dækningen ved siden af, er fordelingen for smal eller for bred, og bid price
bliver tilsvarende forkert.

**Hvor meget mere tjener den?** Det kan IKKE måles her. Historikken indeholder
kun de priser der faktisk blev taget, så enhver sammenligning af omsætning
hviler på en antagelse om hvordan efterspørgslen ville have reageret på en
anden pris. Tallet beregnes alligevel, fordi en prisvej uden et omsætningstal
er svær at vurdere — men det er antagelsen der driver resultatet, ikke dataene.
Det rigtige svar kommer fra eksplorationen, ikke herfra.

Kør: python -m app.backtest
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import replace
from datetime import date

from . import bidprice, cube, demand
from .config import load_settings
from .engine import DayInput, forecast_occupancy, price_day

LEADS = (0, 3, 7, 14, 21, 30, 45, 60, 90, 120)


def forecast_comparison(rows, models, params, *, leads=LEADS) -> list:
    """Prognosepræcision pr. lead time. Ingen antagelser om priser."""
    inv = params.inventory
    capacity = inv.max_room_capacity
    by_lead = {lead: {"v3": [], "v4": [], "inside": 0, "n": 0} for lead in leads}
    for row in rows:
        lead = row["lead"]
        if lead not in by_lead or row["censureret"]:
            continue
        day = date.fromisoformat(row["dato"])
        actual = row["endelig_rum"]
        otb = row["otb_rum"]
        weekend = params.is_weekend(day)

        v3 = forecast_occupancy(otb / capacity, lead, weekend, params.booking_curve) * capacity
        v4 = otb + models["rum"].expected(lead, day)
        low = otb + models["rum"].quantile(0.10, lead, day)
        high = otb + models["rum"].quantile(0.90, lead, day)

        bucket = by_lead[lead]
        bucket["v3"].append(abs(v3 - actual))
        bucket["v4"].append(abs(v4 - actual))
        bucket["inside"] += int(low <= actual <= high)
        bucket["n"] += 1

    out = []
    for lead in leads:
        bucket = by_lead[lead]
        if not bucket["n"]:
            continue
        out.append({
            "lead": lead, "n": bucket["n"],
            "v3_mae": statistics.mean(bucket["v3"]),
            "v4_mae": statistics.mean(bucket["v4"]),
            "daekning_80": bucket["inside"] / bucket["n"],
        })
    return out


def price_path(rows_by_date, day: str, params, *, max_lead: int, today_offset: int = 0) -> dict:
    """Kør prissætningen dag for dag ned mod ankomst, som den ville være kørt."""
    arrival = date.fromisoformat(day)
    prev_rung = prev_position = None
    prices, rungs = [], []
    for lead in range(min(max_lead, 120), -1, -1):
        row = rows_by_date[day].get(lead)
        if row is None:
            continue
        item = DayInput(day=arrival, rooms_otb=int(row["otb_rum"]),
                        beds_otb=int(row["otb_senge"]),
                        prev_room_rung=prev_rung, prev_room_position=prev_position)
        today = arrival.toordinal() - lead
        rec = price_day(item, params, today=date.fromordinal(today))
        prev_rung = rec.room_rung
        prev_position = (rec.room_ladder or {}).get("smoothed_position")
        prices.append(rec.room_price)
        rungs.append(rec.room_rung)
    changes = sum(1 for a, b in zip(prices, prices[1:]) if abs(a - b) > 0.01)
    return {"dato": day, "priser": prices, "trin": rungs, "skift": changes,
            "gns_pris": statistics.mean(prices) if prices else 0.0,
            "slutpris": prices[-1] if prices else 0.0}


def revenue_under_assumption(path: dict, final_units: float, capacity: int,
                             reference_price: float, elasticity: float) -> float:
    """Omsætning hvis efterspørgslen havde reageret med den antagne elasticitet.

    Antagelsen er hele resultatet. Den er ikke målt på egne gæster, og tallet
    skal læses som "hvad modellen tror", ikke som "hvad der ville være sket".
    """
    if not path["priser"]:
        return 0.0
    price = path["slutpris"]
    scale = (price / reference_price) ** (-elasticity) if reference_price > 0 else 1.0
    return price * min(final_units * scale, capacity)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sammenlign version 3 og version 4 på historikken")
    ap.add_argument("kube", nargs="?", default="kali/cube.csv")
    ap.add_argument("--model", default="config/demand_model.json")
    ap.add_argument("--datoer", type=int, default=60, help="antal datoer i prisvejen")
    ap.add_argument("--max-lead", type=int, default=60)
    ap.add_argument("--ud-af-stikproeve", action="store_true",
                    help="estimér fordelingen på 2024 og mål den på 2025")
    args = ap.parse_args(argv)

    rows = cube.load(args.kube)
    if args.ud_af_stikproeve:
        # En model målt på de data den selv er estimeret på, ser altid god ud.
        # Her estimeres den på 2024 og bedømmes på et år den aldrig har set.
        train = [r for r in rows if r["dato"] < "2025-01-01"]
        rows = [r for r in rows if r["dato"] >= "2025-03-08"]
        models = demand.fit_all(train)
        print(f"Ud af stikprøve: estimeret på {len(train)} rækker fra 2024, "
              f"målt på {len(rows)} rækker fra 2025 (renoveringen udeladt)")
        print()
    else:
        models = demand.load(args.model)
    settings = load_settings()
    p3 = settings.params
    v4cfg = replace(p3.v4 or bidprice.V4Config(), enabled=True)
    p4 = replace(p3, v4=v4cfg, demand_models=models)
    if not p4.v4_active:
        print("Version 4 kunne ikke aktiveres — mangler stigen eller modelfilen")
        return 1

    print("PROGNOSEPRÆCISION — værelser, i enheder. Ingen antagelser om priser.")
    print(f"{'lead':>5}{'n':>8}{'v3 MAE':>9}{'v4 MAE':>9}{'bedre':>8}{'dækning 80 %':>14}")
    for line in forecast_comparison(rows, models, p3):
        better = (line["v3_mae"] - line["v4_mae"]) / line["v3_mae"] if line["v3_mae"] else 0
        print(f"{line['lead']:>5}{line['n']:>8}{line['v3_mae']:>9.1f}{line['v4_mae']:>9.1f}"
              f"{better:>+8.0%}{line['daekning_80']:>14.0%}")
    print("# Dækning tæt på 80 % betyder at fordelingen har den rigtige bredde.")
    print("# Ligger den under, er den for smal, og bid price bliver for sikker på sig selv.")
    print()

    by_date: dict = {}
    for row in rows:
        by_date.setdefault(row["dato"], {})[row["lead"]] = row
    days = sorted(by_date)
    step = max(1, len(days) // max(1, args.datoer))
    sample = days[::step][:args.datoer]

    stats = {"v3": {"skift": [], "pris": [], "oms": []},
             "v4": {"skift": [], "pris": [], "oms": []}}
    capacity = p3.inventory.max_room_capacity
    for day in sample:
        final_units = by_date[day][0]["endelig_rum"]
        for name, params in (("v3", p3), ("v4", p4)):
            path = price_path(by_date, day, params, max_lead=args.max_lead)
            if not path["priser"]:
                continue
            reference = statistics.mean(path["priser"])
            stats[name]["skift"].append(path["skift"])
            stats[name]["pris"].append(path["slutpris"])
            stats[name]["oms"].append(revenue_under_assumption(
                path, final_units, capacity, reference, v4cfg.elasticity_rooms))

    print(f"PRISVEJ — {len(sample)} datoer, {args.max_lead} dage ned mod ankomst")
    print(f"{'':>6}{'prisskift':>11}{'slutpris':>10}{'omsætning*':>12}")
    for name in ("v3", "v4"):
        s = stats[name]
        if not s["pris"]:
            continue
        print(f"{name:>6}{statistics.mean(s['skift']):>11.1f}"
              f"{statistics.mean(s['pris']):>10.0f}{statistics.mean(s['oms']):>12.0f}")
    print("# *Omsætningen hviler på den ANTAGNE elasticitet "
          f"({v4cfg.elasticity_rooms}), ikke på data.")
    print("# Den er ikke et bevis for en gevinst. Det kommer fra eksplorationen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

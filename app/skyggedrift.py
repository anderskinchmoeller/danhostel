"""Version 3 og version 4 side om side på dagens faktiske belægning.

Skyggedrift betyder at se forskellen, før man stoler på den. Uden en konkret
visning bliver de fire uger til "kør videre og håb"; med den bliver de til en
liste over datoer hvor de to versioner er uenige, og en vurdering af hvem der
havde ret.

Kører på databasens nuværende belægning — altså det du uploadede i morges — og
viser begge versioners forslag for hver dato i horisonten.

    python -m app.skyggedrift                 # datoer hvor de er uenige
    python -m app.skyggedrift --alle          # hele horisonten
    python -m app.skyggedrift --csv log.csv   # til regneark, så uenighederne kan følges

Kolonnen `forskel` er v4 minus v3 i kroner. Positiv betyder at version 4 vil
tage mere for værelset end version 3 ville.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import date, timedelta

from sqlalchemy import select

from . import db, service
from .config import load_settings
from .engine import price_day

DAYS_DK = ("man", "tir", "ons", "tor", "fre", "lør", "søn")


def compare(settings, today: date | None = None) -> list[dict]:
    """Kør begge versioner på den belægning der står i databasen lige nu."""
    today = today or date.today()
    end = today + timedelta(days=settings.horizon_days)
    session = db.get_session()
    try:
        states = session.scalars(
            select(db.DayState).where(db.DayState.day >= today, db.DayState.day <= end)
            .order_by(db.DayState.day)
        ).all()
        if not states:
            raise SystemExit("Ingen belægningsdata. Upload dagens fil først.")
        params = service.effective_params(session, settings)
        events = service.load_events(session)
        inputs = service.build_inputs(states, settings)
        inputs = service.with_ladder_history(session, inputs, today, current_run_id=-1)
    finally:
        session.close()

    if not params.v4_active:
        raise SystemExit(
            "Version 4 er ikke aktiv. Sæt pricing.v4.enabled: true i config.yaml, "
            "og byg fordelingen med:\n"
            "  python -m app.cube && python -m app.demand")

    # Samme parametre, kun motoren skiftes ud. Så er forskellen modellens og
    # ikke en bivirkning af to forskellige konfigurationer.
    p4 = params
    p3 = replace(params, v4=replace(params.v4, enabled=False))

    rows = []
    for item in inputs:
        try:
            a = price_day(item, p3, events, today=today)
            b = price_day(item, p4, events, today=today)
        except ValueError:
            continue    # fastfrosne datoer håndteres af kørslen selv
        rows.append({
            "dato": item.day.isoformat(),
            "dag": DAYS_DK[item.day.weekday()],
            "lead": a.lead_days,
            "otb_rum": a.rooms_otb,
            "otb_senge": a.beds_otb,
            "v3_rum": a.room_price, "v4_rum": b.room_price,
            "v3_seng": a.bed_price, "v4_seng": b.bed_price,
            "v3_flex": a.flex_to_private, "v4_flex": b.flex_to_private,
            "forskel": b.room_price - a.room_price,
            "bid_price": round((b.room_ladder or {}).get("bid", {}).get("bid_price", 0), 0),
            "uenige": (abs(b.room_price - a.room_price) > 0.01
                       or abs(b.bed_price - a.bed_price) > 0.01
                       or a.flex_to_private != b.flex_to_private),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Sammenlign version 3 og version 4 på dagens belægning")
    ap.add_argument("--alle", action="store_true", help="vis hele horisonten")
    ap.add_argument("--csv", help="skriv også til en CSV-fil")
    ap.add_argument("--dage", type=int, default=None, help="begræns horisonten")
    args = ap.parse_args(argv)

    settings = load_settings()
    if args.dage:
        settings = replace(settings, horizon_days=args.dage)
    engine = db.init_db(settings.database_url)
    try:
        rows = compare(settings)
    finally:
        engine.dispose()
    shown = rows if args.alle else [r for r in rows if r["uenige"]]

    print(f"{len(rows)} datoer, {sum(r['uenige'] for r in rows)} hvor versionerne er uenige")
    print()
    print(f"{'dato':12}{'dag':5}{'lead':>5}{'otb':>9}"
          f"{'v3 rum':>8}{'v4 rum':>8}{'forskel':>9}"
          f"{'v3 sg':>7}{'v4 sg':>7}{'flex':>9}{'bid':>6}")
    for r in shown:
        flex = f"{r['v3_flex']}→{r['v4_flex']}" if r["v3_flex"] != r["v4_flex"] else str(r["v4_flex"])
        print(f"{r['dato']:12}{r['dag']:5}{r['lead']:>5}"
              f"{str(r['otb_rum']) + '/' + str(r['otb_senge']):>9}"
              f"{r['v3_rum']:>8.0f}{r['v4_rum']:>8.0f}{r['forskel']:>+9.0f}"
              f"{r['v3_seng']:>7.0f}{r['v4_seng']:>7.0f}{flex:>9}{r['bid_price']:>6.0f}")

    if rows:
        diffs = [r["forskel"] for r in rows]
        up = sum(1 for d in diffs if d > 0)
        down = sum(1 for d in diffs if d < 0)
        print()
        print(f"Version 4 vil højere på {up} datoer, lavere på {down}, "
              f"ens på {len(diffs) - up - down}.")
        print(f"Gennemsnitlig forskel: {sum(diffs) / len(diffs):+.0f} kr. pr. værelse.")
        print()
        print("Kig især på de datoer hvor forskellen er størst, og på dem hvor")
        print("flex-forslaget er forskelligt. Skriv ned hvem du mener havde ret —")
        print("det er den eneste måde de fire uger bliver til viden.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
            writer.writeheader()
            for r in rows:
                writer.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v)
                                 for k, v in r.items()})
        print(f"\nSkrevet til {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

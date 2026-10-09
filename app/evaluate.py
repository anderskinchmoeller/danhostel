"""Hvilken version er bedst — og hvad kan overhovedet bevises?

Spørgsmålet deler sig i to, og de to dele har vidt forskellig status.

**Prognosen kan afgøres på data.** Hvor mange værelser ender der med at blive
solgt? Det ved vi for hver eneste historisk dato. En model estimeret på 2024
alene kan bedømmes på 2025, som den aldrig har set, mod version 3 og mod to
trivielle referencer. Forskellen kan gives et usikkerhedsinterval. Det er et
bevis i den forstand ordet kan bruges her.

**Omsætningen kan ikke.** Historikken indeholder kun de priser der faktisk blev
taget. Enhver sammenligning af omsætning kræver en antagelse om, hvordan
gæsterne ville have reageret på en anden pris, og den antagelse er ikke målt.
Modulet her beregner derfor ikke ét omsætningstal, men hele kurven over
elasticiteter — og viser hvor skillelinjen ligger. Det er ikke et bevis for en
gevinst; det er et bevis for, hvad gevinsten afhænger af.

Til sidst regnes hvor meget data eksplorationen skal bruge, før elasticiteten
er målt godt nok til at afgøre sagen.

Kør: python -m app.evaluate
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
from dataclasses import replace
from datetime import date

from . import bidprice, cube, demand
from .config import load_settings
from .engine import DayInput, forecast_occupancy, price_day

LEADS = (3, 7, 14, 30, 60, 90, 120)
ELASTICITIES = (0.8, 1.0, 1.3, 1.6, 2.0, 2.5)
RENOVATION = ("2025-01-01", "2025-03-07")


# --------------------------------------------------------------------------
# 1. Prognosepræcision med usikkerhed
# --------------------------------------------------------------------------

def _baseline_history(train_rows) -> dict:
    """Simpleste rimelige reference: gennemsnittet for den ugedag i den måned.

    Enhver model skal slå den. Gør den ikke det, er al maskineriet pynt.
    """
    sums, counts = {}, {}
    for row in train_rows:
        if row["lead"]:
            continue
        key = (row["ugedag"], row["maaned"])
        sums[key] = sums.get(key, 0.0) + row["endelig_rum"]
        counts[key] = counts.get(key, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


def errors_by_date(test_rows, models, params, history: dict, leads=LEADS) -> dict:
    """Absolutte fejl pr. (dato, lead) for fire metoder.

    Fejlene holdes samlet pr. dato, fordi lead times inden for samme dato er
    stærkt korrelerede. Bootstrappen nedenfor trækker derfor datoer, ikke
    enkeltobservationer — ellers bliver usikkerheden kunstigt lille.
    """
    capacity = params.inventory.max_room_capacity
    out: dict = {}
    for row in test_rows:
        lead = row["lead"]
        if lead not in leads or row["censureret"]:
            continue
        day = date.fromisoformat(row["dato"])
        actual, otb = row["endelig_rum"], row["otb_rum"]
        v3 = forecast_occupancy(otb / capacity, lead, params.is_weekend(day),
                                params.booking_curve) * capacity
        v4 = otb + models["rum"].expected(lead, day, otb=otb)
        naive = otb
        hist = history.get((row["ugedag"], row["maaned"]), actual)
        out.setdefault(row["dato"], {}).setdefault(lead, {}).update({
            "v3": abs(v3 - actual), "v4": abs(v4 - actual),
            "nu": abs(naive - actual), "historik": abs(hist - actual),
        })
    return out


def bootstrap_difference(errors: dict, lead: int, *, draws: int = 2000,
                         seed: int = 20261002) -> dict:
    """Forskellen i gennemsnitlig fejl mellem v3 og v4, med 95 %-interval.

    Klyngebootstrap over datoer. Intervallet svarer på: ville forskellen holde
    på et andet år med samme slags datoer?
    """
    days = [d for d in errors if lead in errors[d]]
    pairs = [(errors[d][lead]["v3"], errors[d][lead]["v4"]) for d in days]
    if len(pairs) < 30:
        return {}
    observed = statistics.mean(a - b for a, b in pairs)
    rng = random.Random(seed)
    means = []
    n = len(pairs)
    for _ in range(draws):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.mean(a - b for a, b in sample))
    means.sort()
    wins = sum(1 for a, b in pairs if b < a)
    return {
        "n": n, "forskel": observed,
        "lav": means[int(0.025 * draws)], "hoej": means[int(0.975 * draws)],
        "andel_v4_bedst": wins / n,
    }


def calibration(test_rows, models, leads=LEADS, levels=(0.5, 0.8, 0.9)) -> list:
    """Rammer fordelingen den bredde den lover?

    Et 80 %-interval skal indeholde det faktiske tal i 80 % af tilfældene. Er
    dækningen lavere, er fordelingen for smal, og bid price bliver for sikker
    på sig selv — den vil presse prisen op på knaphed der ikke er der.
    """
    out = []
    for level in levels:
        low_q, high_q = (1 - level) / 2, 1 - (1 - level) / 2
        inside = total = 0
        for row in test_rows:
            if row["lead"] not in leads or row["censureret"]:
                continue
            day = date.fromisoformat(row["dato"])
            otb = row["otb_rum"]
            low = otb + models["rum"].quantile(low_q, row["lead"], day, otb=otb)
            high = otb + models["rum"].quantile(high_q, row["lead"], day, otb=otb)
            inside += int(low <= row["endelig_rum"] <= high)
            total += 1
        out.append({"lovet": level, "faktisk": inside / max(1, total), "n": total})
    return out


# --------------------------------------------------------------------------
# 2. Omsætning: hele kurven i stedet for ét tal
# --------------------------------------------------------------------------

def price_path(by_date, day: str, params, max_lead: int) -> dict:
    """Prisen version X ville have slået op, dag for dag ned mod ankomst."""
    arrival = date.fromisoformat(day)
    prev_rung = prev_position = None
    prices = {}
    for lead in range(max_lead, -1, -1):
        row = by_date[day].get(lead)
        if row is None:
            continue
        item = DayInput(day=arrival, rooms_otb=int(row["otb_rum"]),
                        beds_otb=int(row["otb_senge"]),
                        prev_room_rung=prev_rung, prev_room_position=prev_position)
        try:
            rec = price_day(item, params, today=date.fromordinal(arrival.toordinal() - lead))
        except ValueError:
            return {}
        prev_rung = rec.room_rung
        prev_position = (rec.room_ladder or {}).get("smoothed_position")
        prices[lead] = rec.room_price
    return prices


def revenue(prices: dict, by_date, day: str, capacity: int, elasticity: float) -> float:
    """Omsætning hvis gæsterne havde reageret med den givne elasticitet.

    Bookingerne tælles dér hvor de faktisk kom ind: pickup mellem lead L+1 og L
    ganges med den pris modellen havde slået op på lead L. Den opnåede pris den
    dato bruges som reference, så efterspørgslen skaleres i forhold til det der
    faktisk skete — ikke i forhold til et gennemsnit.
    """
    rows = by_date[day]
    final = rows[0]["endelig_rum"]
    achieved = rows[0]["otb_oms_rum"] / final if final > 0 else 0.0
    if achieved <= 0:
        return 0.0
    total, sold = 0.0, 0.0
    for lead in sorted(prices, reverse=True):
        before = rows.get(lead + 1)
        pickup = rows[lead]["otb_rum"] - (before["otb_rum"] if before else 0.0)
        if pickup <= 0:
            continue
        price = prices[lead]
        scaled = pickup * (price / achieved) ** (-elasticity)
        room = max(0.0, capacity - sold)
        taken = min(scaled, room)
        total += taken * price
        sold += taken
    return total


def revenue_sweep(paths: dict, by_date, capacity: int,
                  elasticities=ELASTICITIES, *, draws: int = 1000,
                  seed: int = 20261002) -> list:
    """Omsætning pr. elasticitet, med bootstrap-interval over datoer.

    Intervallet er usikkerheden fra udvalget af datoer. Det siger intet om
    usikkerheden på elasticiteten — den er grunden til at der er en hel tabel
    og ikke ét tal.
    """
    days = sorted(d for d in paths["v3"] if paths["v3"][d] and paths["v4"].get(d))
    out = []
    for elasticity in elasticities:
        pairs = [(revenue(paths["v3"][d], by_date, d, capacity, elasticity),
                  revenue(paths["v4"][d], by_date, d, capacity, elasticity))
                 for d in days]
        v3 = statistics.mean(a for a, _ in pairs)
        v4 = statistics.mean(b for _, b in pairs)
        rng = random.Random(seed)
        n = len(pairs)
        diffs = []
        for _ in range(draws):
            sample = [pairs[rng.randrange(n)] for _ in range(n)]
            base = statistics.mean(a for a, _ in sample)
            diffs.append((statistics.mean(b for _, b in sample) - base) / base if base else 0.0)
        diffs.sort()
        out.append({"elasticitet": elasticity, "v3": v3, "v4": v4,
                    "forskel": (v4 - v3) / v3 if v3 else 0.0,
                    "lav": diffs[int(0.025 * draws)], "hoej": diffs[int(0.975 * draws)]})
    return out


def revenue_sweep_pair(paths: dict, by_date, capacity: int, base: str, challenger: str,
                       elasticities=ELASTICITIES, *, draws: int = 1000,
                       seed: int = 20261002) -> list:
    """Sammenlign to prisveje under samme elasticitetsantagelser."""
    days = sorted(d for d in set(paths[base]) & set(paths[challenger])
                  if paths[base][d] and paths[challenger][d])
    out = []
    for elasticity in elasticities:
        pairs = [(revenue(paths[base][d], by_date, d, capacity, elasticity),
                  revenue(paths[challenger][d], by_date, d, capacity, elasticity))
                 for d in days]
        b = statistics.mean(a for a, _ in pairs)
        c = statistics.mean(v for _, v in pairs)
        rng = random.Random(seed)
        n = len(pairs)
        diffs = []
        for _ in range(draws):
            sample = [pairs[rng.randrange(n)] for _ in range(n)]
            base_mean = statistics.mean(a for a, _ in sample)
            diffs.append((statistics.mean(v for _, v in sample) - base_mean) / base_mean
                         if base_mean else 0.0)
        diffs.sort()
        out.append({"elasticitet": elasticity, base: b, challenger: c,
                    "forskel": (c - b) / b if b else 0.0,
                    "lav": diffs[int(0.025 * draws)], "hoej": diffs[int(0.975 * draws)]})
    return out


# --------------------------------------------------------------------------
# 3. Hvor meget data skal der til, før sagen kan afgøres?
# --------------------------------------------------------------------------

def power(test_rows, *, rung_step: float = 0.10, target_se: float = 0.20) -> dict:
    """Hvor mange eksplorationsdatoer skal der til for at måle elasticiteten?

    Version 5-forsøget fordeler datoer 5 % op og 5 % ned, altså ca. 10 %
    prisforskel mellem de to grupper. Elasticiteten estimeres ved at regressere
    log pickup på log pris.
    Standardfejlen er

        SE = sigma(log pickup) / (sigma(log pris) x kvadratroden af n)

    med sigma(log pris) = halvdelen af trinafstanden ved 50/50-randomisering.
    """
    values = [r["rest_rum"] for r in test_rows
              if r["lead"] == 14 and r["rest_rum"] > 0 and not r["censureret"]]
    if len(values) < 30:
        return {}
    logs = [math.log(v) for v in values]
    sigma = statistics.pstdev(logs)
    sigma_price = rung_step / 2
    n = (sigma / (sigma_price * target_se)) ** 2
    return {"sigma_log_pickup": sigma, "sigma_log_pris": sigma_price,
            "target_se": target_se, "n": n}


# --------------------------------------------------------------------------
# 4. Kan elasticiteten estimeres på historikken?
# --------------------------------------------------------------------------

def _ols(x: Sequence[float], y: Sequence[float]) -> tuple:
    """Simpel regression med standardfejl. Ingen afhængigheder."""
    mx, my = statistics.mean(x), statistics.mean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx
    resid = [b - (my + beta * (a - mx)) for a, b in zip(x, y)]
    se = math.sqrt(sum(r * r for r in resid) / (len(x) - 2) / sxx)
    return beta, se


def endogeneity(rows: Sequence[dict], exclude_ranges=(RENOVATION,)) -> dict:
    """Vis hvorfor elasticiteten ikke kan estimeres på historikken.

    Priserne i historikken blev ikke sat tilfældigt. De blev sat af et menneske
    eller af den gamle model, som hævede prisen netop når efterspørgslen var høj.
    Pris og efterspørgsel bevæger sig derfor sammen, og en regression af solgte
    værelser på pris måler den sammenhæng — ikke gæsternes prisfølsomhed.

    Det åbenlyse modsvar er at kontrollere for ugedag og måned. Det virker ikke:
    selv på en bestemt lørdag i juli vidste den der satte prisen, om netop den
    lørdag var travl. Præcis den information er den, der ikke står i data.

    Funktionen regner begge dele, så det kan ses frem for at skulle tros.
    """
    def excluded(day: str) -> bool:
        return any(low <= day <= high for low, high in exclude_ranges)

    final = [r for r in rows if r["lead"] == 0 and r["endelig_rum"] > 0
             and not r["censureret"] and not excluded(r["dato"])]
    log_price = [math.log(r["otb_oms_rum"] / r["endelig_rum"]) for r in final]
    log_sold = [math.log(r["endelig_rum"]) for r in final]

    naive, naive_se = _ols(log_price, log_sold)

    # Samme regression inden for celler af ugedag x måned.
    cells: dict = {}
    for index, row in enumerate(final):
        cells.setdefault((row["ugedag"], row["maaned"]), []).append(index)
    dp, dq = [], []
    for members in cells.values():
        if len(members) < 3:
            continue
        mp = statistics.mean(log_price[i] for i in members)
        mq = statistics.mean(log_sold[i] for i in members)
        for i in members:
            dp.append(log_price[i] - mp)
            dq.append(log_sold[i] - mq)
    controlled, controlled_se = _ols(dp, dq)

    return {"n": len(final), "naive": naive, "naive_se": naive_se,
            "controlled": controlled, "controlled_se": controlled_se,
            "cells": len(cells),
            "correlation": statistics.correlation(log_price, log_sold),
            "price_sd_history": statistics.pstdev(log_price),
            "price_sd_exploration": 0.035}


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Afgør hvilken version der er bedst")
    ap.add_argument("kube", nargs="?", default="kali/cube.csv")
    ap.add_argument("--datoer", type=int, default=50, help="datoer i prisvejen")
    ap.add_argument("--max-lead", type=int, default=45)
    args = ap.parse_args(argv)

    rows = cube.load(args.kube)
    train = [r for r in rows if r["dato"] < "2025-01-01"]
    test = [r for r in rows if r["dato"] > RENOVATION[1]]
    models = demand.fit_all(train)
    settings = load_settings()
    p3 = settings.params
    v4cfg = replace(p3.v4 or bidprice.V4Config(), enabled=True)
    p4 = replace(p3, v4=v4cfg, demand_models=models)
    v5cfg = replace(v4cfg, elasticity_measured=True)
    p5 = replace(p4, v4=v5cfg)
    capacity = p3.inventory.max_room_capacity

    print("OPSÆTNING")
    print(f"  Estimeret på 2024: {len({r['dato'] for r in train})} datoer")
    print(f"  Bedømt på 2025 efter renoveringen: {len({r['dato'] for r in test})} datoer")
    print("  Modellen har aldrig set testperioden.")
    print()

    history = _baseline_history(train)
    errors = errors_by_date(test, models, p3, history)

    print("1. PROGNOSEPRÆCISION — gennemsnitlig absolut fejl i antal værelser")
    print(f"{'lead':>5}{'nu':>8}{'historik':>10}{'v3':>8}{'v4':>8}"
          f"{'v4 bedre end v3':>18}{'95 %-interval':>20}{'v4 vinder':>11}")
    for lead in LEADS:
        days = [d for d in errors if lead in errors[d]]
        if not days:
            continue
        means = {k: statistics.mean(errors[d][lead][k] for d in days)
                 for k in ("nu", "historik", "v3", "v4")}
        boot = bootstrap_difference(errors, lead)
        interval = f"[{boot['lav']:+.2f}; {boot['hoej']:+.2f}]" if boot else "-"
        print(f"{lead:>5}{means['nu']:>8.1f}{means['historik']:>10.1f}"
              f"{means['v3']:>8.1f}{means['v4']:>8.1f}"
              f"{boot.get('forskel', 0):>+18.2f}{interval:>20}"
              f"{boot.get('andel_v4_bedst', 0):>11.0%}")
    print("  'nu' = antag at der ikke kommer flere. 'historik' = gennemsnittet for")
    print("  den ugedag i den måned. Et interval der ikke rummer 0 betyder at")
    print("  forskellen ville holde på et andet år med samme slags datoer.")
    print()

    print("2. KALIBRERING — holder fordelingen det den lover?")
    print(f"{'lovet':>8}{'faktisk':>10}{'n':>9}")
    for line in calibration(test, models):
        print(f"{line['lovet']:>8.0%}{line['faktisk']:>10.0%}{line['n']:>9}")
    print()

    by_date: dict = {}
    for row in test:
        by_date.setdefault(row["dato"], {})[row["lead"]] = row
    days = sorted(d for d in by_date if 0 in by_date[d] and by_date[d][0]["endelig_rum"] > 0)
    step = max(1, len(days) // max(1, args.datoer))
    sample = days[::step][:args.datoer]
    paths = {name: {day: price_path(by_date, day, params, args.max_lead)
                    for day in sample}
             for name, params in (("v3", p3), ("v4", p4), ("v5", p5))}

    changes = {}
    for name, price_paths in paths.items():
        counted = [
            sum(1 for a, b in zip(list(p.values()), list(p.values())[1:])
                if abs(a - b) > 0.01)
            for p in price_paths.values() if p
        ]
        changes[name] = statistics.mean(counted) if counted else 0.0
    print(f"3. OMSÆTNING — {len(sample)} datoer, {args.max_lead} dage ned mod ankomst")
    print(f"   Prisskift pr. dato: v3 {changes['v3']:.1f}, v4 {changes['v4']:.1f}")
    print(f"{'elasticitet':>12}{'v3':>10}{'v4':>10}{'forskel':>10}{'95 %-interval':>20}")
    for line in revenue_sweep(paths, by_date, capacity):
        interval = f"[{line['lav']:+.1%}; {line['hoej']:+.1%}]"
        print(f"{line['elasticitet']:>12.1f}{line['v3']:>10.0f}{line['v4']:>10.0f}"
              f"{line['forskel']:>+10.1%}{interval:>20}")
    print("  Dette er IKKE et bevis. Hver række er en antagelse om gæsterne.")
    print("  Tabellen viser hvilken antagelse der skal holde, for at hver version vinder.")
    print()

    print("3B. V4 MOD V5 — v5 = v4 med målt elasticitet og uden midlertidig rabatbinding")
    print(f"   Prisskift pr. dato: v4 {changes['v4']:.1f}, v5 {changes['v5']:.1f}")
    print(f"{'elasticitet':>12}{'v4':>10}{'v5':>10}{'forskel':>10}{'95 %-interval':>20}")
    for line in revenue_sweep_pair(paths, by_date, capacity, "v4", "v5"):
        interval = f"[{line['lav']:+.1%}; {line['hoej']:+.1%}]"
        print(f"{line['elasticitet']:>12.1f}{line['v4']:>10.0f}{line['v5']:>10.0f}"
              f"{line['forskel']:>+10.1%}{interval:>20}")
    print("  Dette tester ikke en færdig v5 på historiske gæster. Det tester effekten af")
    print("  den v5-beslutning der først må tages efter forsøget: at behandle")
    print("  elasticiteten som målt og fjerne v4's midlertidige rabatloft.")
    print()

    e = endogeneity(rows)
    print("4. KAN ELASTICITETEN ESTIMERES PÅ HISTORIKKEN?")
    print(f"   {e['n']} datoer. Den ønskede elasticitet er negativ "
          f"(højere pris → færre solgte).")
    print(f"   Naiv regression:              {e['naive']:+.2f}  (SE {e['naive_se']:.2f})"
          f"{'   FORKERT FORTEGN' if e['naive'] > 0 else ''}")
    print(f"   Med kontrol for ugedag×måned: {e['controlled']:+.2f}  "
          f"(SE {e['controlled_se']:.2f})"
          f"{'   STADIG FORKERT FORTEGN' if e['controlled'] > 0 else ''}")
    print(f"   Korrelation log pris / log solgt: {e['correlation']:+.2f}")
    print("   Prisen blev sat højt netop når efterspørgslen var høj. Regressionen")
    print("   måler den sammenhæng, ikke gæsternes prisfølsomhed. Standardfejlen er")
    print("   lille, så mere data gør kun estimatet mere sikkert på noget forkert.")
    print()

    p = power(test)
    if p:
        print("5. HVOR MEGET DATA SKAL DER TIL?")
        print(f"   Spredning på log pickup: {p['sigma_log_pickup']:.2f}")
        print(f"   Prisvariation fra eksplorationen: {p['sigma_log_pris']:.3f} i log")
        print(f"   Datoer for en standardfejl på {p['target_se']:.2f}: {p['n']:.0f}")
        print(f"   Ved 120 datoer i horisonten: {p['n'] / 120:.0f} kørselsdage, "
              f"hvis eksplorationen rammer alle datoer.")
        print("   Realistisk rammer den en mindre del, så regn med en sæson.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Kalibrér bookingkurver fra en komplet, afsluttet reservationshistorik.

python -m app.calibration reservationer.csv --rooms 36 --beds 280 \
    --start 2024-01-01 --end 2025-12-31

Kræver bookingdato, ankomstdato, enhed (room/bed). Valgfrie naetter,
quantity, pris (pr. enhed pr. nat), status og cancelled_at. Annulleringer
kræver dato; nul-salgsdatoer medtages i den valgte driftsperiode.
Resultatet kræver validering mod egne data før brug i live prissætning.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from dataclasses import dataclass

from .adapters.csv_adapter import _map_headers, _reader, parse_date, parse_number

LEAD_BUCKETS = [0, 2, 4, 8, 15, 31, 61]
WEEKEND = (4, 5)

RES_ALIASES = {
    "booked": {"bookingdato", "booking_date", "created", "oprettet", "reservationsdato"},
    "arrival": {"ankomstdato", "ankomst", "arrival", "arrival_date", "checkin", "check_in"},
    "nights": {"naetter", "nætter", "nights", "los", "antal_naetter"},
    "price": {"pris", "price", "rate", "adr", "beloeb", "beløb"},
    "quantity": {"quantity", "antal", "units", "antal_enheder"},
    "status": {"status", "booking_status", "reservationsstatus"},
    "cancelled": {"cancelled_at", "canceled_at", "annulleringsdato", "afbestillingsdato"},
    "unit": {"enhed", "type", "kategori", "unit", "category", "roomtype", "vaerelsestype"},
}


def _headers(row):
    cols = _map_headers(row)
    for i, raw in enumerate(row):
        key = raw.strip().lower().replace(" ", "_")
        for field, names in RES_ALIASES.items():
            if key in names:
                cols[field] = i
    return cols


@dataclass(frozen=True)
class Reservation:
    booked: date
    arrival: date
    nights: int
    price: float | None
    unit: str
    quantity: int = 1
    cancelled: date | None = None


def _unit(value):
    v = value.strip().lower()
    if v in {"room", "værelse", "vaerelse", "private"}:
        return "room"
    if v in {"bed", "seng", "dorm", "dorm_bed", "dormseng"}:
        return "bed"
    raise ValueError(f"Ukendt enhed {value!r}; brug room eller bed (ikke værelsesnavn)")


def read_reservations(text: str) -> tuple:
    rows = list(_reader(text))
    if not rows:
        raise ValueError("Reservationsfilen er tom")
    cols = _headers(rows[0])
    missing = [f for f in ("booked", "arrival", "unit") if f not in cols]
    if missing:
        raise ValueError(f"Mangler kolonne(r): {', '.join(missing)}")
    out = []
    for line_no, row in enumerate(rows[1:], 2):
        if not any(c.strip() for c in row):
            continue
        try:
            def cell(name, default=""):
                return row[cols[name]].strip() if name in cols else default
            def positive_int(name):
                value = parse_number(cell(name) or "1")
                if value <= 0 or not value.is_integer():
                    raise ValueError(f"{name} skal være et positivt heltal")
                return int(value)
            booked, arrival = parse_date(cell("booked")), parse_date(cell("arrival"))
            if booked > arrival:
                raise ValueError("Bookingdato ligger efter ankomst")
            nights, quantity = positive_int("nights"), positive_int("quantity")
            price = parse_number(cell("price")) if cell("price") else None
            if price is not None and price < 0:
                raise ValueError("Pris må ikke være negativ")
            status = cell("status").lower().replace("-", "_").replace(" ", "_")
            cancelled = parse_date(cell("cancelled")) if cell("cancelled") else None
            if status in {"cancelled", "canceled", "annulleret", "afbestilt", "no_show", "noshow"}:
                if cancelled is None:
                    raise ValueError("Annullering/no-show kræver annulleringsdato")
            elif status not in {"", "confirmed", "booked", "checked_in", "checked_out",
                                 "completed", "bekræftet", "aktiv"}:
                raise ValueError(f"Ukendt reservationsstatus: {status}")
            if cancelled and cancelled < booked:
                raise ValueError("Annullering ligger før booking")
            out.append(Reservation(booked, arrival, nights, price, _unit(cell("unit")),
                                   quantity, cancelled))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Linje {line_no}: {exc}") from exc
    return out, True


def build_units_by_day(reservations, unit: str, start=None, end=None) -> dict:
    """Use one complete, closed operating interval, including zero-sale dates.

    Cancellation history reconstructs on-the-books snapshots at each cutoff.
    Prices must be per unit per night; quantity expands group reservations.
    """
    if not reservations:
        return {}
    start = start or min(r.arrival for r in reservations)
    end = end or max(r.arrival + timedelta(days=r.nights - 1) for r in reservations)
    if start > end:
        raise ValueError("Startdato ligger efter slutdato")
    by_day = {start + timedelta(days=n): [] for n in range((end - start).days + 1)}
    for r in reservations:
        if r.unit != unit:
            continue
        for n in range(r.nights):
            stay = r.arrival + timedelta(days=n)
            if stay in by_day:
                by_day[stay].extend([(r.booked, r.price, r.cancelled)] * r.quantity)
    return by_day


def _active(entries, cutoff):
    return [(booked, price) for booked, price, cancelled in entries
            if booked <= cutoff and (cancelled is None or cancelled > cutoff)]


def booking_curve(by_day: dict, capacity: int) -> list:
    if capacity <= 0:
        raise ValueError("Kapacitet skal være positiv")
    samples = {(b, w): [] for b in LEAD_BUCKETS for w in (False, True)}
    for stay, entries in by_day.items():
        is_weekend = stay.weekday() in WEEKEND
        for bucket in LEAD_BUCKETS:
            cutoff = stay - timedelta(days=bucket)
            sold = len(_active(entries, cutoff))
            samples[(bucket, is_weekend)].append(min(1.0, sold / capacity))

    curve = []
    for bucket in LEAD_BUCKETS:
        hv, we = samples[(bucket, False)], samples[(bucket, True)]
        curve.append({
            "lead_days_from": bucket,
            "weekday": round(statistics.median(hv), 3) if hv else 0.0,
            "weekend": round(statistics.median(we), 3) if we else 0.0,
        })
    return curve


def demand_factors(by_day: dict, capacity: int) -> tuple:
    by_month, by_weekday = defaultdict(list), defaultdict(list)
    for stay, entries in by_day.items():
        occ = min(1.0, len(_active(entries, stay)) / capacity)
        by_month[stay.month].append(occ)
        by_weekday[stay.weekday()].append(occ)

    all_occ = [o for v in by_month.values() for o in v]
    overall = statistics.mean(all_occ) if all_occ else 1.0

    def factor(values, floor=0.75, ceil=1.35):
        if not values or overall <= 0:
            return 1.0
        # Belægning oversættes dæmpet til pris. Fuldt gennemslag ville gøre
        # prisen lige så volatil som efterspørgslen, og det tåler ingen gæst.
        ratio = statistics.mean(values) / overall
        return round(max(floor, min(ceil, 1 + 0.5 * (ratio - 1))), 3)

    season = {m: factor(by_month.get(m, [])) for m in range(1, 13)}
    weekday_raw = {d: factor(by_weekday.get(d, [])) for d in range(7)}
    anchor = weekday_raw.get(2) or 1.0
    weekday = {d: round(v / anchor, 3) for d, v in weekday_raw.items()}
    return season, weekday


def suggest_base(by_day: dict, capacity: int) -> float | None:
    """Medianpris på dage hvor belægningen landede mellem 75 og 85 procent."""
    prices = []
    for stay, entries in by_day.items():
        active = _active(entries, stay)
        if 0.75 <= len(active) / capacity <= 0.85:
            prices.extend(p for _, p in active if p is not None)
    return round(statistics.median(prices), -1) if prices else None


def _print_curve(name: str, curve: list):
    print(f"{name}:")
    for point in curve:
        print(f"  - {{lead_days_from: {point['lead_days_from']}, "
              f"weekday: {point['weekday']}, weekend: {point['weekend']}}}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Kalibrér prismotoren på egen historik")
    ap.add_argument("csv", help="reservationshistorik fra Picasso")
    ap.add_argument("--rooms", type=int, required=True,
                    help="maks. antal private værelser (private_rooms + flex_rooms)")
    ap.add_argument("--beds", type=int, required=True,
                    help="maks. antal senge (dorm_beds + flex_rooms x beds_per_flex_room)")
    ap.add_argument("--start", type=date.fromisoformat, required=True,
                    help="første dato i en komplet historisk eksport (YYYY-MM-DD)")
    ap.add_argument("--end", type=date.fromisoformat, required=True,
                    help="sidste afsluttede driftsdato (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    if args.rooms <= 0 or args.beds <= 0:
        ap.error("--rooms og --beds skal være positive")
    if args.start > args.end or args.end >= date.today():
        ap.error("Perioden skal være afsluttet og start skal være før/lig slut")

    with open(args.csv, encoding="utf-8-sig") as fh:
        try:
            reservations, _ = read_reservations(fh.read())
        except ValueError as exc:
            ap.error(str(exc))
    if not reservations:
        raise SystemExit("Ingen brugbare rækker i filen")

    rooms_by_day = build_units_by_day(reservations, "room", args.start, args.end)
    beds_by_day = build_units_by_day(reservations, "bed", args.start, args.end)
    if not any(rooms_by_day.values()) and not any(beds_by_day.values()):
        raise SystemExit("Ingen reservationer i den valgte periode")

    span = f"{min(rooms_by_day):%d-%m-%Y} til {max(rooms_by_day):%d-%m-%Y}"
    print(f"# Kalibreret på {len(reservations)} reservationer, {span}")
    print("# Kurver beskriver historisk pickup, ikke valideret priselasticitet.")
    print("# Kapacitet skal være konstant; udelad lukkede perioder og adskil kapacitetsændringer.")
    print(f"# Kapacitet brugt: {args.rooms} værelser, {args.beds} senge")
    room_base = suggest_base(rooms_by_day, args.rooms)
    print(f"bar_base: {room_base:.0f}" if room_base
          else "# bar_base: ingen prisdata på datoer med 75–85 % belægning")
    if beds_by_day:
        bed_base = suggest_base(beds_by_day, args.beds)
        print(f"bed_base: {bed_base:.0f}" if bed_base
              else "# bed_base kunne ikke beregnes")

    season, weekday = demand_factors(rooms_by_day, args.rooms)
    print("season:")
    for m in range(1, 13):
        print(f"  {m}: {season[m]}")
    print("weekday:")
    for d in range(7):
        print(f"  {d}: {weekday[d]}")

    if beds_by_day:
        _, weekday_beds = demand_factors(beds_by_day, args.beds)
        print("weekday_beds:")
        for d in range(7):
            print(f"  {d}: {weekday_beds[d]}")

    _print_curve("booking_curve", booking_curve(rooms_by_day, args.rooms))
    if beds_by_day:
        _print_curve("booking_curve_beds", booking_curve(beds_by_day, args.beds))
    return 0


if __name__ == "__main__":
    sys.exit(main())

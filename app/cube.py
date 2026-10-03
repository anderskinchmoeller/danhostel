"""Bookingkuben — on-the-books-tilstanden på enhver historisk dag.

Kalibreringen (app/calibration.py) bruger reservationshistorikken til at trække
*gennemsnitlige* bookingkurver ud. Det er en lille del af det der ligger i
filen. Hver linje har både oprettelsesdato og annulleringsdato, og derfor kan
tilstanden rekonstrueres eksakt: hvor meget stod der på bøgerne til natten den
14. juli, set fra den 3. juni?

    OTB(d, L) = antal enheder booket til natten d, set fra dag t = d - L
              = linjer hvor  oprettet <= t
                       og   (ikke annulleret  eller  annulleret > t)
                       og    ankomst <= d < afrejse

Kuben er (dato x lead time) for hele historikken. Af den falder tre ting ud,
som kurverne ikke kan give:

1. **Fordelingen af resterende efterspørgsel**, ikke kun dens middelværdi.
   Det er grundlaget under app/demand.py og dermed under bid price.
2. **Prognosefejl pr. lead time**, målt mod det der faktisk skete.
3. **Backtest på rigtig tilstand** — ikke mod en gennemsnitskurve, men mod det
   der rent faktisk stod på bøgerne den dag.

To forbehold, som skal med hver gang kuben bruges:

**Ændringer fanges ikke.** Flyttes en reservation fra 12. til 19. juli, ser
kuben den som om den altid lå den 19. Picasso gemmer ikke ændringshistorik i de
tre rapporter. Effekten er lille, men systematisk: pickup ser tidligere ud.

**Censurering.** På datoer der blev udsolgt er den resterende efterspørgsel
afkortet — den sande efterspørgsel var højere. Ved 45 % belægning rammer det
få datoer, men de er flaget, så de kan udelades når de en dag bliver flere.

Ren beregning: ingen database, intet netværk. Kun læsning af CSV og skrivning
af CSV.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Iterator

MAX_LEAD = 120
"""Hvor langt ude kuben går. Samme horisont som prismotoren prissætter på."""

GROUP_MIN_UNITS = 4
"""Fra hvor mange enheder på samme reservation en booking regnes som gruppe.

Sovesalene sælges mest samlet til grupper, de private rum enkeltvis. De to har
hverken samme form eller samme klumpethed, og en blandet kurve beskriver ingen
af dem. Tærsklen er et valg, ikke en måling: reservationshistorikken har ingen
arrangementskolonne, så antal enheder pr. reservationsnummer er det nærmeste
vi kommer. Juster den her, ikke spredt i koden.
"""

FIELDS = (
    "dato", "lead", "ugedag", "maaned",
    "otb_rum", "otb_senge",
    "otb_rum_gruppe", "otb_senge_gruppe",
    "otb_oms_rum", "otb_oms_senge",
    "endelig_rum", "endelig_senge",
    "rest_rum", "rest_senge",
    "rest_rum_gruppe", "rest_senge_gruppe",
    "censureret",
)


# --------------------------------------------------------------------------
# Indlæsning
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Line:
    """En reservationslinje som kuben bruger den."""
    booked: date
    arrival: date
    nights: int
    quantity: int
    price: float | None
    is_bed: bool
    cancelled: date | None
    is_group: bool


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


def normalise_ref(ref: str) -> str:
    """Reservationsnumre skrives forskelligt i Picassos rapporter.

    `094613` i Rooms spec. og CXL, `94613` i Weekplan, og delreservationer får
    `/001`, `/002`. Uden normalisering tæller samme reservation som flere.
    """
    ref = (ref or "").strip().split("/")[0]
    return ref.lstrip("0") or "0"


def read_lines(path: str | Path, *, group_min_units: int = GROUP_MIN_UNITS) -> list[Line]:
    """Læs reservationshistorikken og markér gruppebookinger.

    Gruppemarkeringen sker pr. (reservationsnummer, ankomstdato), så en
    gruppereservation delt på tyve linjer tælles som én blok.
    """
    raw: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            booked = _parse_date(row.get("bookingdato", ""))
            arrival = _parse_date(row.get("ankomstdato", ""))
            if booked is None or arrival is None:
                continue
            try:
                nights = int(row.get("naetter") or 0)
                quantity = int(row.get("quantity") or 0)
            except ValueError:
                continue
            if nights <= 0 or quantity <= 0:
                continue
            if booked > arrival:
                # Oprettet efter ankomst giver ingen mening; behandl som walk-in.
                booked = arrival
            raw.append({
                "booked": booked, "arrival": arrival, "nights": nights,
                "quantity": quantity,
                "price": float(row["pris"]) if row.get("pris") else None,
                "is_bed": (row.get("enhed") or "").strip().lower() == "bed",
                "cancelled": _parse_date(row.get("cancelled_at", "")),
                "key": (normalise_ref(row.get("ref", "")), arrival),
            })

    units = defaultdict(int)
    for item in raw:
        units[item["key"]] += item["quantity"]

    return [
        Line(booked=i["booked"], arrival=i["arrival"], nights=i["nights"],
             quantity=i["quantity"], price=i["price"], is_bed=i["is_bed"],
             cancelled=i["cancelled"],
             is_group=units[i["key"]] >= group_min_units)
        for i in raw
    ]


# --------------------------------------------------------------------------
# Opbygning
# --------------------------------------------------------------------------

class _Night:
    """Akkumulator for én ankomstdato: seks differensrækker over lead time.

    En reservationslinje står på bøgerne i et sammenhængende interval af lead
    times — fra den blev oprettet til den blev annulleret. Derfor lægges den
    ind som +qty i den ene ende og -qty i den anden, og rækken summeres til
    sidst. Det gør opbygningen lineær i antal linjer i stedet for i linjer
    gange lead times.
    """

    __slots__ = ("rooms", "beds", "rooms_group", "beds_group", "rev_rooms", "rev_beds")

    def __init__(self, size: int):
        self.rooms = [0.0] * size
        self.beds = [0.0] * size
        self.rooms_group = [0.0] * size
        self.beds_group = [0.0] * size
        self.rev_rooms = [0.0] * size
        self.rev_beds = [0.0] * size

    def add(self, low: int, high: int, line: Line) -> None:
        qty = float(line.quantity)
        revenue = qty * (line.price or 0.0)
        if line.is_bed:
            targets = [(self.beds, qty), (self.rev_beds, revenue)]
            if line.is_group:
                targets.append((self.beds_group, qty))
        else:
            targets = [(self.rooms, qty), (self.rev_rooms, revenue)]
            if line.is_group:
                targets.append((self.rooms_group, qty))
        for row, value in targets:
            row[low] += value
            row[high + 1] -= value

    def rows(self) -> tuple:
        out = []
        for row in (self.rooms, self.beds, self.rooms_group, self.beds_group,
                    self.rev_rooms, self.rev_beds):
            running, cumulative = 0.0, []
            for value in row[:-1]:
                running += value
                cumulative.append(running)
            out.append(cumulative)
        return tuple(out)


def build(lines: Iterable[Line], *, max_lead: int = MAX_LEAD,
          room_capacity: int | None = None,
          bed_capacity: int | None = None,
          censor_at: float = 0.98, trim_to_arrivals: bool = True) -> Iterator[dict]:
    """Byg kuben. Udbytter én række pr. (dato, lead), lead 0..max_lead.

    `trim_to_arrivals` skærer datoerne til ankomstperioden. Lange ophold rækker
    ind i måneden efter den sidste ankomst, men de datoer har kun de gæster med
    der nåede at ankomme inden periodens slutning, og ser derfor kunstigt tomme
    ud. Skal man se hele opholdet — fx i en test — slås den fra.
    """
    lines = list(lines)
    if not lines:
        return
    first_arrival = min(line.arrival for line in lines)
    last_arrival = max(line.arrival for line in lines)
    size = max_lead + 2
    nights: dict[date, _Night] = {}

    for line in lines:
        for offset in range(line.nights):
            day = line.arrival + timedelta(days=offset)
            # Interval af lead times hvor linjen stod på bøgerne:
            #   oprettet <= d - L   ->  L <= (d - oprettet)
            #   d - L < annulleret  ->  L > (d - annulleret)
            high = min(max_lead, (day - line.booked).days)
            if high < 0:
                continue
            low = 0
            if line.cancelled is not None:
                low = max(0, (day - line.cancelled).days + 1)
                if low > high:
                    continue
            bucket = nights.get(day)
            if bucket is None:
                bucket = nights[day] = _Night(size)
            bucket.add(low, high, line)

    for day in sorted(nights):
        if trim_to_arrivals and not first_arrival <= day <= last_arrival:
            continue
        rooms, beds, rooms_g, beds_g, rev_r, rev_b = nights[day].rows()
        final_rooms, final_beds = rooms[0], beds[0]
        final_rooms_g, final_beds_g = rooms_g[0], beds_g[0]
        censored = int(
            (room_capacity is not None and final_rooms >= censor_at * room_capacity)
            or (bed_capacity is not None and final_beds >= censor_at * bed_capacity)
        )
        for lead in range(max_lead + 1):
            yield {
                "dato": day.isoformat(),
                "lead": lead,
                "ugedag": day.weekday(),
                "maaned": day.month,
                "otb_rum": round(rooms[lead], 3),
                "otb_senge": round(beds[lead], 3),
                "otb_rum_gruppe": round(rooms_g[lead], 3),
                "otb_senge_gruppe": round(beds_g[lead], 3),
                "otb_oms_rum": round(rev_r[lead], 2),
                "otb_oms_senge": round(rev_b[lead], 2),
                "endelig_rum": round(final_rooms, 3),
                "endelig_senge": round(final_beds, 3),
                "rest_rum": round(final_rooms - rooms[lead], 3),
                "rest_senge": round(final_beds - beds[lead], 3),
                "rest_rum_gruppe": round(final_rooms_g - rooms_g[lead], 3),
                "rest_senge_gruppe": round(final_beds_g - beds_g[lead], 3),
                "censureret": censored,
            }


def write(rows: Iterable[dict], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def load(path: str | Path) -> list[dict]:
    """Læs en skrevet kube tilbage med tal som tal."""
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            item = {"dato": row["dato"]}
            for key in FIELDS[1:]:
                value = float(row[key])
                item[key] = int(value) if key in ("lead", "ugedag", "maaned", "censureret") else value
            out.append(item)
    return out


# --------------------------------------------------------------------------
# Kommandolinje
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Byg bookingkuben ud fra reservationshistorikken")
    ap.add_argument("reservationer", nargs="?",
                    default="kali/reservationer_2024_2025.csv")
    ap.add_argument("--ud", default="kali/cube.csv")
    ap.add_argument("--max-lead", type=int, default=MAX_LEAD)
    ap.add_argument("--gruppe-min", type=int, default=GROUP_MIN_UNITS,
                    help="enheder pr. reservation før den regnes som gruppe")
    ap.add_argument("--rum-kapacitet", type=int, default=None,
                    help="til censureringsflaget; udelades det, flages intet")
    ap.add_argument("--seng-kapacitet", type=int, default=None)
    args = ap.parse_args(argv)

    lines = read_lines(args.reservationer, group_min_units=args.gruppe_min)
    groups = sum(1 for line in lines if line.is_group)
    rows = build(lines, max_lead=args.max_lead,
                 room_capacity=args.rum_kapacitet,
                 bed_capacity=args.seng_kapacitet)
    written = write(rows, args.ud)
    dates = written // (args.max_lead + 1)
    print(f"{len(lines)} reservationslinjer, heraf {groups} i gruppebookinger "
          f"({groups / max(1, len(lines)):.0%})")
    print(f"{written} rækker over {dates} datoer skrevet til {args.ud}")
    print("# Ændringer af dato fanges ikke af kuben: en flyttet reservation ser")
    print("# ud som om den altid lå på den nye dato. Pickup ser derfor en anelse")
    print("# tidligere ud end den var.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

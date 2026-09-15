"""CSV-adapter — den der virker fra dag ét.

To filformater, begge med semikolon eller komma som separator og dansk eller
ISO-datoformat:

  Belægning:    dato;solgte_vaerelser;solgte_senge[;blokerede_vaerelser;blokerede_senge;vaerelsespris;sengepris]
  Konkurrenter: dato;medianpris_vaerelse[;medianpris_seng]

Kolonnenavne genkendes på dansk og engelsk, med og uden æøå. Rækkefølgen er
ligegyldig, og kolonner der ikke genkendes ignoreres.

Kapaciteten står i config.yaml, ikke i filen — huset får ikke flere senge fra
den ene dag til den anden. Det filen skal fortælle er hvor meget der er solgt
og hvor meget der er blokeret.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from .base import AdapterUnavailable, CompRow, InventoryRow, PricePush

DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d")

ALIASES = {
    "flex_private_min": {"flex_private_min", "flex_private_minimum"},
    "flex_private_max": {"flex_private_max", "flex_private_maximum"},
    "booked_room_revenue": {"booked_room_revenue", "bogfoert_vaerelsesomsaetning"},
    "booked_bed_revenue": {"booked_bed_revenue", "bogfoert_sengeomsaetning"},
    "room_type_otb": {"room_type_otb", "solgte_pr_type"},
    "day": {"dato", "date", "ankomst", "ankomstdato", "arrival", "arrival_date", "stay_date"},
    "rooms_otb": {
        "solgte_vaerelser", "solgte_værelser", "solgte", "vaerelser_solgt", "værelser_solgt",
        "rooms_sold", "rooms_otb", "otb_rooms", "sold_rooms", "reserverede_vaerelser",
    },
    "beds_otb": {
        "solgte_senge", "senge_solgt", "beds_sold", "beds_otb", "otb_beds", "sold_beds",
        "dorm_solgt", "dormsenge_solgt",
    },
    "blocked_rooms": {
        "blokerede_vaerelser", "blokerede_værelser", "blokerede", "blocked_rooms",
        "grupper_vaerelser", "ooo_rooms", "allotment_rooms",
    },
    "blocked_beds": {"blokerede_senge", "blocked_beds", "grupper_senge", "ooo_beds"},
    "current_room_price": {
        "vaerelsespris", "værelsespris", "pris", "price", "rate", "bar",
        "current_room_price", "room_rate",
    },
    "current_bed_price": {"sengepris", "bed_price", "current_bed_price", "dorm_pris", "bed_rate"},
    "comp_room": {
        "medianpris_vaerelse", "medianpris_værelse", "medianpris", "comp_room",
        "konkurrentpris", "comp_median", "market_median", "median",
    },
    "comp_bed": {
        "medianpris_seng", "comp_bed", "konkurrentpris_seng", "median_bed", "bed_median",
    },
    "n_properties": {"antal", "n", "properties", "antal_hoteller", "count"},
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9æøå_]", "", name.strip().lower().replace(" ", "_"))


def _map_headers(headers: Sequence[str]) -> dict:
    out = {}
    for i, raw in enumerate(headers):
        key = _norm(raw)
        for field, names in ALIASES.items():
            if key in names and field not in out:
                out[field] = i
                break
    return out


def parse_date(value: str) -> date:
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Ukendt datoformat: {value!r}")


def parse_number(value: str) -> float:
    value = (value or "").strip().replace("kr.", "").replace("kr", "").replace("\xa0", "")
    value = value.replace(" ", "")
    if not value:
        raise ValueError("tom værdi")
    if "," in value and "." in value:          # dansk format 1.234,50
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Tal skal være endelige")
    return number


def _reader(text: str):
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    return csv.reader(io.StringIO(text), delimiter=delimiter)


def _cell(row, cols, field):
    idx = cols.get(field)
    if idx is None or len(row) <= idx or not row[idx].strip():
        return None
    return row[idx]


def parse_room_type_otb(raw):
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(n, int) or isinstance(n, bool) or n < 0
        for k, n in value.items()
    ):
        raise ValueError("room_type_otb skal være et objekt med heltallige antal pr. type")
    return value


def parse_inventory(text: str) -> list:
    rows = list(_reader(text))
    if not rows:
        raise ValueError("Filen er tom")
    cols = _map_headers(rows[0])
    if "day" not in cols:
        raise ValueError("Mangler datokolonne. Forventede fx: dato;solgte_vaerelser;solgte_senge")
    if "rooms_otb" not in cols or "beds_otb" not in cols:
        raise ValueError(
            "Mangler kolonne med solgte enheder. Forventede fx: "
            "dato;solgte_vaerelser;solgte_senge"
        )

    out = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            def num(field, default=0):
                raw = _cell(row, cols, field)
                if raw is None:
                    if field in ("rooms_otb", "beds_otb"):
                        raise ValueError(f"{field} må ikke være tom")
                    return default
                value = parse_number(raw)
                if value < 0 or not value.is_integer():
                    raise ValueError(f"{field} skal være et helt tal på mindst nul")
                return int(value)

            def price(field):
                raw = _cell(row, cols, field)
                value = parse_number(raw) if raw else None
                if value is not None and value < 0:
                    raise ValueError(f"{field} må ikke være negativ")
                return value

            out.append(InventoryRow(
                day=parse_date(row[cols["day"]]),
                rooms_otb=num("rooms_otb"),
                beds_otb=num("beds_otb"),
                blocked_rooms=num("blocked_rooms"),
                blocked_beds=num("blocked_beds"),
                current_room_price=price("current_room_price"),
                current_bed_price=price("current_bed_price"),
                flex_private_min=num("flex_private_min", None),
                flex_private_max=num("flex_private_max", None),
                booked_room_revenue=price("booked_room_revenue"),
                booked_bed_revenue=price("booked_bed_revenue"),
                room_type_otb=parse_room_type_otb(_cell(row, cols, "room_type_otb")),
            ))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Linje {line_no}: {exc}") from exc
    if not out:
        raise ValueError("Ingen datarækker fundet")
    if len({r.day for r in out}) != len(out):
        raise ValueError("Belægning må kun indeholde én række pr. dato")
    return out


def parse_comp(text: str) -> list:
    rows = list(_reader(text))
    if not rows:
        raise ValueError("Filen er tom")
    cols = _map_headers(rows[0])
    if "day" not in cols:
        raise ValueError("Mangler datokolonne. Forventede fx: dato;medianpris_vaerelse;medianpris_seng")
    if "comp_room" not in cols and "comp_bed" not in cols:
        raise ValueError(
            "Mangler priskolonne. Forventede fx: dato;medianpris_vaerelse;medianpris_seng"
        )

    out = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            room = _cell(row, cols, "comp_room")
            bed = _cell(row, cols, "comp_bed")
            n = _cell(row, cols, "n_properties")
            out.append(CompRow(
                day=parse_date(row[cols["day"]]),
                comp_room=parse_number(room) if room else None,
                comp_bed=parse_number(bed) if bed else None,
                n_properties=int(parse_number(n)) if n else 0,
            ))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Linje {line_no}: {exc}") from exc
    if not out:
        raise ValueError("Ingen datarækker fundet")
    return out


class CsvPMSAdapter:
    """Læser belægning fra en mappe. Nyeste fil vinder.

    Sæt `inbox` til en mappe hvor Picasso lægger sin natlige eksport — eller
    lad den være tom og brug upload i dashboardet i stedet.
    """

    name = "csv"

    def __init__(self, inbox: str | Path | None = None, pattern: str = "*.csv"):
        self.inbox = Path(inbox) if inbox else None
        self.pattern = pattern

    def _latest(self) -> Path:
        if not self.inbox or not self.inbox.exists():
            raise AdapterUnavailable("Ingen indbakke konfigureret — brug upload i dashboardet")
        files = sorted(self.inbox.glob(self.pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise AdapterUnavailable(f"Ingen filer i {self.inbox}")
        return files[0]

    def fetch_inventory(self, start: date, end: date) -> Sequence[InventoryRow]:
        path = self._latest()
        rows = parse_inventory(path.read_text(encoding="utf-8-sig"))
        return [r for r in rows if start <= r.day <= end]

    def push_prices(self, prices: Sequence[PricePush]) -> dict:
        raise AdapterUnavailable(
            "Servicen skriver ikke priser til Picasso. "
            "Hent /export.csv, eller brug ændringerne på dashboardet."
        )


class ManualRateShopAdapter:
    """Konkurrentpriser indtastet i hånden eller uploadet som CSV.

    Ikke elegant, men det virker fra dag ét og koster ingenting. For et hostel
    er det desuden mindre tab end for et hotel: rate shopping vejer alligevel
    kun 0,45 på værelser og 0,30 på senge i denne model, fordi produkterne ikke
    er sammenlignelige på samme måde.
    """

    name = "manual"

    def fetch_comp_prices(self, start: date, end: date) -> Sequence[CompRow]:
        raise AdapterUnavailable(
            "Manuel rate shopping: upload konkurrentpriser i dashboardet"
        )

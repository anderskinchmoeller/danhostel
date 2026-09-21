"""Lav belægningsfilen ud fra Picassos Arrivals → Rooms spec. (PDF).

python -m app.picasso_belaegning kali/Arrivals_on_21-09-2026_to_19-01-202710.pdf

Skriver dato;solgte_vaerelser;solgte_senge for hver dato i rapportens periode.
Rumtyper der starter med B er enkeltsenge; alle andre er hele rum (også
sovesale solgt samlet). Kun Confirmed, Guaranteed og In-House tælles.

Rapporten udvælger på ankomstdato. Gæster der ankom før periodens start og
stadig bor der, kommer kun med hvis perioden starter før deres ankomst, så
start den ca. 30 dage tilbage og slå In-House til.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROW = re.compile(
    r"^\s*(?P<room>\d{3,5})?\s+(?P<type>[A-Z][A-Z0-9]{0,5})\s*(?P<st>[A-Z])\s+"
    r".*?(?P<ref>\d{5,6}(?:/\d{3})?)\s+(?P<arr>\d\d-\d\d)\s+\d\d:\d\d\s+(?P<dep>\d\d-\d\d)\s+"
    r"(?P<days>\d+)\s+(?P<pcs>\d+)\s+(?P<pax>\d+)"
)
PERIOD = re.compile(r"Arrivals on period:\s*(\d\d-\d\d-\d{4})\s*to\s*(\d\d-\d\d-\d{4})")
# Kun bindende bookinger tæller: Confirmed, Guaranteed, In-House. Tentative,
# Provisional og WaitingList er optioner; medregnet giver de overbooking (fx
# 77 af 70 rum den 20-11-2026). Annulleret, no-show og afrejst tæller aldrig.
COUNTED = {"C", "G", "I"}


def _dmy(text: str) -> date:
    d, m, y = map(int, text.split("-"))
    return date(y, m, d)


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    return "\n".join(p.extract_text(extraction_mode="layout") for p in PdfReader(path).pages)


def parse(text: str, today: date | None = None) -> tuple[date, date, list[dict]]:
    today = today or date.today()
    m = PERIOD.search(text)
    if not m:
        raise ValueError("Fandt ikke 'Arrivals on period' – er det en Rooms spec.-rapport?")
    start, end = _dmy(m.group(1)), _dmy(m.group(2))
    if (end - start).days > 330:
        raise ValueError("Perioden er for lang til at gætte årstal; hold den under 11 måneder")
    rows = []
    for line in text.splitlines():
        r = ROW.match(line)
        if not r:
            continue
        d, mo = map(int, r["arr"].split("-"))
        arrival = date(start.year, mo, d)
        if arrival < start - timedelta(days=31):  # krydser nytår
            arrival = date(start.year + 1, mo, d)
        if r["st"] == "I":
            # In-house er ankommet. Langtidsgæster kan være ankommet før
            # rapportperioden (fx 365 nætter fra et tidligere år).
            while arrival > today:
                arrival = date(arrival.year - 1, mo, d)
            # Days er loftet ved 365 i rapporten; afrejsedatoen er den sikre.
            dd_, dm_ = map(int, r["dep"].split("-"))
            depart = date(today.year, dm_, dd_)
            if depart <= today:
                depart = date(today.year + 1, dm_, dd_)
            days = (depart - arrival).days
        else:
            days = int(r["days"])
        rows.append({"type": r["type"], "st": r["st"], "ref": r["ref"], "arrival": arrival,
                     "days": days, "pcs": int(r["pcs"])})
    if not rows:
        raise ValueError("Ingen reservationslinjer fundet i filen")
    return start, end, rows


def on_the_books(start: date, end: date, rows: list[dict]) -> dict:
    rooms, beds = Counter(), Counter()
    for r in rows:
        if r["st"] not in COUNTED:
            continue
        pool = beds if r["type"].startswith("B") else rooms
        for n in range(r["days"]):
            pool[r["arrival"] + timedelta(days=n)] += r["pcs"]
    days = (end - start).days + 1
    return {start + timedelta(n): (rooms[start + timedelta(n)], beds[start + timedelta(n)])
            for n in range(days)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Belægningsfil fra Picasso Rooms spec. (PDF)")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", help="CSV-fil (standard: belaegning_<dato>.csv ved siden af PDF'en)")
    args = ap.parse_args(argv)
    src = Path(args.pdf)
    try:
        start, end, rows = parse(pdf_text(src))
    except ValueError as exc:
        ap.error(str(exc))
    otb = on_the_books(start, end, rows)
    today = date.today()
    out = Path(args.out) if args.out else src.with_name(f"belaegning_{today:%Y-%m-%d}.csv")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("dato;solgte_vaerelser;solgte_senge\n")
        for d, (r, b) in otb.items():
            if d >= today:
                fh.write(f"{d.isoformat()};{r};{b}\n")
    kept = [v for d, v in otb.items() if d >= today]
    status = Counter(r["st"] for r in rows)
    print(f"{len(rows)} linjer læst ({dict(status)}), periode {start} til {end}")
    if start > today - timedelta(days=14):
        print("ADVARSEL: perioden starter mindre end 14 dage tilbage; gæster der ankom "
              "før og stadig bor der, mangler i de første nætter.")
    options = sum(r["pcs"] for r in rows if r["st"] in {"T", "P", "W"})
    if options:
        print(f"Ikke medregnet: {options} enheder på tentative/foreløbige/venteliste-bookinger")
    print(f"Skrev {len(kept)} datoer til {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

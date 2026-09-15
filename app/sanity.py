"""Rimelighedstjek på uploadet belægning.

Guardrails fanger priser der er absurde. De fanger ikke input der er plausibelt
og forkert — og det er den dyre fejl: servicen kører igennem uden en lyd,
konkluderer noget forkert om huset, og prissætter videre.

Tjekkene her er bevidst grove. De vurderer ikke om tallene er rigtige, for det
kan ingen maskine afgøre. De vurderer om tallene overhovedet kan passe. En
kolonne der er nul hele horisonten igennem, et spring på 40 procentpoint på ét
døgn, flere solgte værelser end huset har: det er ikke lav belægning, det er en
fil der er læst forkert.

Et fund stopper importen. Et menneske kan trumfe det igennem, og så står både
fundet og navnet i loggen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MAX_OCC_JUMP = 0.40          # procentpoint på ét døgn
MIN_ROWS_FOR_ZERO_CHECK = 7  # under en uge kan sagtens være nul hele vejen
PAST_DAYS_ALLOWED = 2        # eksporten må gerne indeholde i går
FUTURE_DAYS_ALLOWED = 730
PRICE_LOW_FACTOR = 0.5       # under halvdelen af gulvet er ikke en lav pris
PRICE_HIGH_FACTOR = 2.0      # over det dobbelte af loftet er ikke en høj pris
MAX_LISTED_DATES = 3


@dataclass(frozen=True)
class Finding:
    """Ét fund. `code` er til loggen, `message` er til mennesket."""
    code: str
    message: str


def _dk(day: date) -> str:
    return day.strftime("%d-%m-%Y")


def _dates(days, limit: int = MAX_LISTED_DATES) -> str:
    listed = [_dk(d) for d in days[:limit]]
    rest = len(days) - len(listed)
    text = ", ".join(listed)
    return f"{text} og {rest} flere" if rest > 0 else text


def _occ(sold: int, blocked: int, capacity: int) -> float:
    if capacity <= 0:
        return 0.0
    return (sold + blocked) / capacity


def check_inventory(rows, params, today: date | None = None) -> list[Finding]:
    """Returnér de fund der bør stoppe importen. Tom liste = filen ser rimelig ud."""
    if not rows:
        return [Finding("empty", "Filen indeholder ingen datarækker.")]

    today = today or date.today()
    inv = params.inventory
    rows = sorted(rows, key=lambda r: r.day)
    findings: list[Finding] = []

    # 1. En kolonne der er nul hele vejen. Det er sådan en manglende kolonne ser ud.
    if len(rows) >= MIN_ROWS_FOR_ZERO_CHECK:
        for field, label in (("rooms_otb", "solgte værelser"),
                             ("beds_otb", "solgte senge")):
            if all(getattr(r, field) == 0 for r in rows):
                findings.append(Finding(
                    f"zero_{field}",
                    f"Kolonnen for {label} er nul på alle {len(rows)} datoer. "
                    "Enten står huset tomt i hele perioden, eller også mangler "
                    "kolonnen i eksporten og er blevet læst som nul.",
                ))

    # 2. Mere solgt end huset har. Forkert lager eller forkert kolonne.
    for field, blocked_field, capacity, label in (
        ("rooms_otb", "blocked_rooms", inv.max_room_capacity, "værelser"),
        ("beds_otb", "blocked_beds", inv.max_bed_capacity, "senge"),
    ):
        over = [r.day for r in rows
                if getattr(r, field) + getattr(r, blocked_field) > capacity]
        if over:
            findings.append(Finding(
                f"over_capacity_{field}",
                f"Flere {label} solgt eller blokeret end huset har "
                f"({capacity} i config.yaml) på {_dates(over)}. "
                "Enten er lageret forkert sat op, eller også er kolonnerne byttet om.",
            ))

    # 3. Spring i belægning fra én dag til den næste.
    jumps_rooms, jumps_beds = [], []
    for prev, cur in zip(rows, rows[1:]):
        if (cur.day - prev.day).days != 1:
            continue
        room_jump = abs(_occ(cur.rooms_otb, cur.blocked_rooms, inv.max_room_capacity)
                        - _occ(prev.rooms_otb, prev.blocked_rooms, inv.max_room_capacity))
        bed_jump = abs(_occ(cur.beds_otb, cur.blocked_beds, inv.max_bed_capacity)
                       - _occ(prev.beds_otb, prev.blocked_beds, inv.max_bed_capacity))
        if room_jump > MAX_OCC_JUMP:
            jumps_rooms.append(cur.day)
        if bed_jump > MAX_OCC_JUMP:
            jumps_beds.append(cur.day)
    for days, label, code in ((jumps_rooms, "værelsesbelægningen", "jump_rooms"),
                              (jumps_beds, "sengebelægningen", "jump_beds")):
        if days:
            findings.append(Finding(
                code,
                f"{label.capitalize()} springer mere end "
                f"{MAX_OCC_JUMP * 100:.0f} procentpoint på ét døgn ved {_dates(days)}. "
                "Det kan være en gruppeankomst — eller en fil med forskubbede rækker.",
            ))

    # 4. Priser uden for et forventeligt interval. Fanger enheds- og kolonnefejl.
    for field, low, high, label in (
        ("current_room_price", params.price_floor * PRICE_LOW_FACTOR,
         params.price_ceiling * PRICE_HIGH_FACTOR, "værelsespriser"),
        ("current_bed_price", params.bed_floor * PRICE_LOW_FACTOR,
         params.bed_ceiling * PRICE_HIGH_FACTOR, "sengepriser"),
    ):
        odd = [r.day for r in rows
               if getattr(r, field) is not None and not (low <= getattr(r, field) <= high)]
        if odd:
            findings.append(Finding(
                f"price_range_{field}",
                f"Nuværende {label} ligger uden for {low:.0f}–{high:.0f} kr. på "
                f"{_dates(odd)}. Tjek om kolonnen er den rigtige, og om beløbene "
                "er i kroner.",
            ))

    # 5. Datoer uden for horisonten. Fanger dd/mm mod mm/dd.
    stray = [r.day for r in rows
             if (today - r.day).days > PAST_DAYS_ALLOWED
             or (r.day - today).days > FUTURE_DAYS_ALLOWED]
    if stray:
        findings.append(Finding(
            "date_range",
            f"Datoer langt uden for horisonten: {_dates(stray)}. "
            "Ofte et datoformat der er læst med måned og dag byttet om.",
        ))

    return findings

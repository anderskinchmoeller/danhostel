"""Adaptivt efterspørgselsniveau — så modellen kan se et strukturelt skift.

`prismotor-risici.md` afsnit 2: modellen kan ikke se et strukturelt skift, fordi
den uden signal falder tilbage på historikken. `picasso-datagrundlag-2025.md`
afsnit 5 dokumenterer at et sådant skift *er sket* — august −21 %, oktober −25 %,
november −35 % — og at ingen ved hvorfor.

Niveauet her lukker hullet uden at nogen skal finde årsagen. Efter hver kørsel
sammenlignes den pickup der faktisk kom med den pickup fordelingen forventede,
og niveauet justeres en lille smule:

    niveau_ny = niveau_gammel x (faktisk / forventet) ^ alfa

Alfa er lille (0,07) og niveauet er bundet til [0,6 ; 1,6]. Det er med vilje:
en enkelt skæv uge skal ikke flytte noget, et skift på 25 % skal fanges på
nogle få uger. Uden grænserne kan en fejl i dataene — en manglende kolonne i
Picasso-eksporten, som risikovurderingens afsnit 1 beskriver — trække niveauet
i bund, og så prissætter modellen efter en efterspørgsel der aldrig forsvandt.

Niveauet erstatter ikke undersøgelsen af hvorfor efteråret 2025 faldt. Det
betyder kun, at modellen ikke er blind over for det næste skift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

DEFAULT_ALPHA = 0.07
DEFAULT_MIN = 0.6
DEFAULT_MAX = 1.6
MIN_EXPECTED_UNITS = 20.0
"""Under så lidt forventet pickup i vinduet justeres der ikke.

Forholdet mellem faktisk og forventet er meningsløst, når nævneren er lille:
tre bookinger mod forventet to er ikke et niveauskift, det er en tirsdag.
"""


@dataclass
class Level:
    """Niveauet pr. lager, med en kort historik så justeringer kan ses."""
    rooms: float = 1.0
    beds: float = 1.0
    updated: str = ""
    history: list = field(default_factory=list)

    def get(self, kind: str) -> float:
        return self.beds if kind.startswith("senge") else self.rooms

    def to_dict(self) -> dict:
        return {"rooms": round(self.rooms, 4), "beds": round(self.beds, 4),
                "updated": self.updated, "history": self.history[-60:]}

    @classmethod
    def from_dict(cls, raw: dict) -> "Level":
        return cls(rooms=float(raw.get("rooms", 1.0)), beds=float(raw.get("beds", 1.0)),
                   updated=raw.get("updated", ""), history=list(raw.get("history", [])))


def adjust(current: float, expected: float, actual: float, *,
           alpha: float = DEFAULT_ALPHA, low: float = DEFAULT_MIN,
           high: float = DEFAULT_MAX,
           min_expected: float = MIN_EXPECTED_UNITS) -> tuple:
    """Ét skridt. Returnerer (nyt niveau, begrundelse)."""
    if expected < min_expected:
        return current, f"for lidt signal ({expected:.0f} forventede enheder)"
    if actual < 0:
        actual = 0.0
    ratio = (actual + 1.0) / (expected + 1.0)   # +1: en uge med nul salg må ikke nulstille niveauet
    updated = current * (ratio ** alpha)
    updated = max(low, min(high, updated))
    if updated >= high - 1e-9 or updated <= low + 1e-9:
        return updated, f"ved grænsen ({updated:.2f}) — kontrollér datagrundlaget"
    return updated, f"faktisk/forventet {ratio:.2f} over {expected:.0f} forventede enheder"


def update(level: Level, *, rooms: Sequence[tuple] = (), beds: Sequence[tuple] = (),
           today: date | None = None, alpha: float = DEFAULT_ALPHA,
           low: float = DEFAULT_MIN, high: float = DEFAULT_MAX) -> Level:
    """`rooms` og `beds` er par af (forventet, faktisk) pickup i enheder.

    Parrene summeres først: ét samlet forhold over alle datoer i vinduet er
    langt mindre støjende end et gennemsnit af dato-forhold, hvor en enkelt
    dato med nul forventet kan dominere.
    """
    today = today or date.today()
    entry = {"dato": today.isoformat()}
    new_rooms, why_rooms = adjust(level.rooms, sum(e for e, _ in rooms),
                                  sum(a for _, a in rooms), alpha=alpha, low=low, high=high)
    new_beds, why_beds = adjust(level.beds, sum(e for e, _ in beds),
                                sum(a for _, a in beds), alpha=alpha, low=low, high=high)
    entry["rum"] = {"fra": round(level.rooms, 4), "til": round(new_rooms, 4), "hvorfor": why_rooms}
    entry["senge"] = {"fra": round(level.beds, 4), "til": round(new_beds, 4), "hvorfor": why_beds}
    return Level(rooms=new_rooms, beds=new_beds, updated=today.isoformat(),
                 history=list(level.history) + [entry])


def load(path: str | Path) -> Level:
    path = Path(path)
    if not path.exists():
        return Level()
    try:
        return Level.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        # Et ulæseligt niveau skal ikke stoppe prissætningen; 1,00 er ingen justering.
        return Level()


def save(level: Level, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(level.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

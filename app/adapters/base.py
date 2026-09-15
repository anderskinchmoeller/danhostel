"""Adaptere.

Servicen kender ikke Picasso. Den kender en grænseflade, og bag den kan der
sidde en CSV-fil i dag og et REST-API om to måneder, uden at prislogikken
ændrer sig en linje.

Bemærk at rækkerne bærer begge lagre. Et hostel sælger både værelser og senge,
og en adapter der kun leverer det ene efterlader den halve model blind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, Sequence


@dataclass(frozen=True)
class InventoryRow:
    """Solgte enheder og blokeringer for én ankomstdato.

    Den fysiske kapacitet står i config.yaml — den ændrer sig ikke fra dag til
    dag. Det der ændrer sig er hvor meget der er solgt, og hvor meget der er
    blokeret til grupper eller ude af drift.
    """
    day: date
    rooms_otb: int = 0
    beds_otb: int = 0
    blocked_rooms: int = 0
    blocked_beds: int = 0
    current_room_price: float | None = None
    current_bed_price: float | None = None
    flex_private_min: int | None = None
    flex_private_max: int | None = None
    booked_room_revenue: float | None = None
    booked_bed_revenue: float | None = None
    room_type_otb: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CompRow:
    """Konkurrenternes medianpriser for én ankomstdato.

    Værelser og senge har hver sin comp set: privatværelser sammenlignes med
    budgethoteller, senge med andre hostels.
    """
    day: date
    comp_room: float | None = None
    comp_bed: float | None = None
    n_properties: int = 0


@dataclass(frozen=True)
class PricePush:
    day: date
    room_price: float
    bed_price: float
    room_types: dict


class PMSAdapter(Protocol):
    name: str

    def fetch_inventory(self, start: date, end: date) -> Sequence[InventoryRow]:
        ...

    def push_prices(self, prices: Sequence[PricePush]) -> dict:
        """Skriv priser tilbage. Returnerer {'ok': n, 'failed': [...]}."""


class RateShopAdapter(Protocol):
    name: str

    def fetch_comp_prices(self, start: date, end: date) -> Sequence[CompRow]:
        ...


class AdapterUnavailable(RuntimeError):
    """Adapteren kan ikke levere data lige nu. Kaldes af dødmandsknappen."""

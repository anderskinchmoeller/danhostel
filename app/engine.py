"""Prismodellen, version 3 — bygget til et hostel, ikke til et hotel.

Tre ting adskiller den fra version 1:

1. **Den forudsiger i stedet for at reagere.** F_pace måler ikke længere hvor du
   står i dag mod hvor du plejer at stå, men hvor du *lander* mod hvor du gerne
   vil lande. En dato der er 20 % solgt 40 dage ude er ikke svag — den er på vej
   til 78 %, og prisen skal ikke falde.

2. **Den prissætter to lagre, ikke ét.** Private værelser og dormsenge fyldes
   ikke samtidigt og bookes ikke med samme varsel. De får hver sin bookingkurve,
   hver sin prognose og hver sin pris.

3. **Den træffer beslutningen om flex-rummene.** Et rum der kan sælges enten som
   familieværelse eller som fire senge er ikke et prisspørgsmål — det er et
   allokeringsspørgsmål. Modellen regner marginalværdien af begge veje og
   foreslår en fordeling. Konkrete rum og hele ophold kontrolleres i Picasso.

Nøgletallet er RevPAB: omsætning pr. tilgængelig seng på tværs af hele huset.
Et hostel der optimerer værelsespris alene optimerer den forkerte størrelse.

Ren beregning: ingen database, intet netværk, ingen sideeffekter.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Iterable, Sequence


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def round_to(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def round_within(value: float, low: float, high: float, step: float) -> float:
    """Rund af, men aldrig ud over grænserne.

    Uden dette kan afrundingen bryde et prisgulv: 165 kr. rundet til nærmeste
    ti bliver 160 — fem kroner under gulvet, hver eneste nat. Små fejl af den
    slags er dyre, fordi de rammer systematisk og i samme retning.
    """
    if low > high:
        raise ValueError("Prisgrænser overlapper ikke")
    if step > 0 and math.ceil(low / step) > math.floor(high / step):
        # A narrow interval may contain no rounded price; bounds take priority.
        cent_low, cent_high = math.ceil(low * 100) / 100, math.floor(high * 100) / 100
        if cent_low > cent_high:
            raise ValueError("Prisintervallet indeholder intet gyldigt ørebeløb")
        return clamp(round(value, 2), cent_low, cent_high)
    rounded = round_to(clamp(value, low, high), step)
    if step > 0:
        if rounded < low:
            rounded = math.ceil(low / step) * step
        if rounded > high:
            rounded = math.floor(high / step) * step
    return rounded


# --------------------------------------------------------------------------
# Lager
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Inventory:
    """Husets lager, delt i tre.

    private_rooms   altid private (fx værelserne med eget bad)
    flex_rooms      kan sælges som familieværelse ELLER som senge
    dorm_beds       faste sovesale, sælges altid pr. seng
    """
    private_rooms: int = 20
    flex_rooms: int = 16
    beds_per_flex_room: int = 4
    dorm_beds: int = 216
    private_beds: int | None = None
    private_room_types: dict = field(default_factory=dict)
    confirmed: bool = False

    def __post_init__(self):
        counts = (self.private_rooms, self.flex_rooms, self.dorm_beds)
        if any(n < 0 for n in counts) or self.beds_per_flex_room <= 0:
            raise ValueError("Lagerantal skal være positive eller nul")
        if self.private_beds is not None and self.private_beds < self.private_rooms:
            raise ValueError("Private senge kan ikke være færre end private værelser")
        if self.private_room_types and (
            any(not isinstance(n, int) or n < 0 for n in self.private_room_types.values())
            or sum(self.private_room_types.values()) != self.private_rooms
        ):
            raise ValueError("Private værelsestyper skal summere til private_rooms")
        if self.confirmed and (self.private_beds is None or
                               (self.private_rooms and not self.private_room_types)):
            raise ValueError("Bekræftet lager kræver private_beds og private_room_types")
        if self.total_beds <= 0:
            raise ValueError("Lageret skal indeholde mindst én seng")

    @property
    def max_room_capacity(self) -> int:
        return self.private_rooms + self.flex_rooms

    @property
    def max_bed_capacity(self) -> int:
        return self.dorm_beds + self.flex_rooms * self.beds_per_flex_room

    @property
    def total_beds(self) -> int:
        """Alle senge i huset, uanset hvordan de sælges.

        Faktisk antal private senge plus dorm/flex-senge. To senge pr. privat
        rum bruges kun som ubekræftet eksempel, aldrig som bekræftet lager.
        """
        return (self.private_beds if self.private_beds is not None else self.private_rooms * 2) + self.max_bed_capacity


# --------------------------------------------------------------------------
# Bookingkurver
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CurvePoint:
    lead_days_from: int
    weekday: float
    weekend: float


@dataclass(frozen=True)
class BookingCurve:
    """Hvor stor en andel der plejer at være solgt X dage før ankomst."""
    points: Sequence[CurvePoint]

    def reference(self, lead_days: int, is_weekend: bool) -> float:
        lead = max(0, lead_days)
        chosen = self.points[0]
        for point in self.points:
            if point.lead_days_from <= lead:
                chosen = point
            else:
                break
        return chosen.weekend if is_weekend else chosen.weekday

    def final(self, is_weekend: bool) -> float:
        """Referencebelægningen ved ankomst — det kurven ender på."""
        return self.reference(0, is_weekend)

    @classmethod
    def rooms(cls) -> "BookingCurve":
        """Private værelser: familier og par booker med varsel."""
        return cls([
            CurvePoint(0, 0.72, 0.80),
            CurvePoint(2, 0.63, 0.72),
            CurvePoint(4, 0.52, 0.62),
            CurvePoint(8, 0.38, 0.48),
            CurvePoint(15, 0.24, 0.32),
            CurvePoint(31, 0.12, 0.18),
            CurvePoint(61, 0.05, 0.08),
        ])

    @classmethod
    def beds(cls) -> "BookingCurve":
        """Dormsenge: backpackere booker sent. Kurven er fladere langt ude og
        stejlere de sidste to uger. Det er derfor senge og værelser ikke kan
        dele prognose."""
        return cls([
            CurvePoint(0, 0.70, 0.78),
            CurvePoint(2, 0.58, 0.66),
            CurvePoint(4, 0.44, 0.52),
            CurvePoint(8, 0.28, 0.35),
            CurvePoint(15, 0.15, 0.20),
            CurvePoint(31, 0.07, 0.10),
            CurvePoint(61, 0.03, 0.04),
        ])


def forecast_occupancy(occ_now: float, lead_days: int, is_weekend: bool,
                       curve: BookingCurve) -> float:
    """Prognose for slutbelægning ud fra pickup.

    To klassiske metoder, blandet efter hvor langt der er til ankomst:

      multiplikativ  occ_now x (ref_slut / ref_nu)   — præcis tæt på ankomst
      additiv        occ_now + (ref_slut - ref_nu)   — robust langt ude

    Langt ude bygger den multiplikative metode på få bookinger og bliver
    voldsom: to bookinger for meget bliver til tyve for meget. Derfor vægtes
    den ind i takt med at referencen vokser.
    """
    ref_now = curve.reference(lead_days, is_weekend)
    ref_final = curve.final(is_weekend)
    if ref_final <= 0:
        return occ_now

    additive = occ_now + (ref_final - ref_now)
    if ref_now <= 0:
        return clamp(additive, 0.0, 1.0)

    multiplicative = occ_now * (ref_final / ref_now)
    weight = clamp(ref_now / ref_final, 0.0, 1.0)
    return clamp(weight * multiplicative + (1 - weight) * additive, 0.0, 1.0)


# --------------------------------------------------------------------------
# Parametre
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Params:
    inventory: Inventory = field(default_factory=Inventory)

    bar_base: float = 645.0      # dobbeltværelse uden bad, onsdag, skuldersæson
    bed_base: float = 270.0      # dormseng, samme referencedag

    season: dict = field(default_factory=lambda: {
        1: 0.82, 2: 0.85, 3: 0.90, 4: 0.95, 5: 1.05, 6: 1.15,
        7: 1.20, 8: 1.15, 9: 1.05, 10: 0.95, 11: 0.88, 12: 0.85,
    })
    weekday: dict = field(default_factory=lambda: {
        0: 0.95, 1: 0.98, 2: 1.00, 3: 1.02, 4: 1.08, 5: 1.12, 6: 0.90,
    })
    # Senge er mindre weekendfølsomme end værelser — backpackere rejser hele ugen.
    weekday_beds: dict = field(default_factory=lambda: {
        0: 0.97, 1: 0.99, 2: 1.00, 3: 1.01, 4: 1.05, 5: 1.07, 6: 0.94,
    })
    weekend_days: tuple = (4, 5)

    room_types: dict = field(default_factory=lambda: {
        "enkelt_uden_bad": 0.80,
        "dobbelt_uden_bad": 1.00,
        "dobbelt_med_bad": 1.18,
        "familie_4": 1.55,
    })

    # Prognose mod mål. Højere k = hårdere reaktion på afvigelsen.
    target_occupancy_rooms: float = 0.85
    target_occupancy_beds: float = 0.80
    k_forecast: float = 0.90
    k_forecast_beds: float = 0.80
    pace_min: float = 0.85
    pace_max: float = 1.20

    # Rate shopping vejer mindre for et hostel end for et hotel: produkterne er
    # ikke sammenlignelige på samme måde. En seng i et sekssengsrum med god
    # stemning er ikke samme vare som en seng ti minutter væk.
    k_market: float = 0.45
    k_market_bed: float = 0.30
    market_min: float = 0.85
    market_max: float = 1.15

    event_max: float = 1.45
    max_daily_change: float = 0.15

    variable_cost: float = 95.0       # pr. solgt privat værelse
    variable_cost_bed: float = 45.0   # pr. solgt seng
    commission: float = 0.16
    price_floor: float = 375.0
    price_ceiling: float = 1400.0
    bed_floor: float = 165.0
    bed_ceiling: float = 600.0
    rounding: float = 10.0

    quality_index: float = 1.00
    booking_curve: BookingCurve = field(default_factory=BookingCurve.rooms)
    booking_curve_beds: BookingCurve = field(default_factory=BookingCurve.beds)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() in self.weekend_days


# --------------------------------------------------------------------------
# Input og output
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EventUplift:
    start: date
    end: date
    name: str
    uplift: float


@dataclass(frozen=True)
class DayInput:
    day: date
    rooms_otb: int = 0
    beds_otb: int = 0
    comp_room: float | None = None
    comp_bed: float | None = None
    current_room_price: float | None = None
    current_bed_price: float | None = None
    blocked_rooms: int = 0
    blocked_beds: int = 0
    flex_private_min: int | None = None
    flex_private_max: int | None = None
    booked_room_revenue: float | None = None
    booked_bed_revenue: float | None = None
    room_type_otb: dict = field(default_factory=dict)
    locked_room_price: float | None = None
    locked_bed_price: float | None = None


@dataclass
class Recommendation:
    day: date
    lead_days: int

    # lager og allokering
    flex_to_private: int
    room_capacity: int
    bed_capacity: int

    # prognose
    rooms_otb: int
    beds_otb: int
    room_occ_now: float
    bed_occ_now: float
    room_forecast: float
    bed_forecast: float
    forecast_rooms: float
    forecast_beds: float

    # faktorer
    f_pace_rooms: float
    f_pace_beds: float
    f_market_rooms: float
    f_market_beds: float
    f_event: float
    event_names: list

    # priser
    base_room: float
    base_bed: float
    room_price: float
    bed_price: float
    room_types: dict
    net_room: float
    net_bed: float

    # resultat
    revpab: float
    warnings: list
    revenue_basis: str = "estimated"

    def as_dict(self) -> dict:
        out = asdict(self)
        out["day"] = self.day.isoformat()
        return out

    def explain(self) -> str:
        return (
            f"værelse {self.base_room:.0f} x pace {self.f_pace_rooms:.3f} "
            f"x marked {self.f_market_rooms:.3f} x event {self.f_event:.2f} "
            f"= {self.room_price:.0f} kr. · seng {self.bed_price:.0f} kr. · "
            f"prognose {self.room_forecast:.0%} værelser / {self.bed_forecast:.0%} senge · "
            f"RevPAB {self.revpab:.0f} kr."
        )


# --------------------------------------------------------------------------
# Delberegninger
# --------------------------------------------------------------------------

def event_factor(day: date, events: Iterable[EventUplift], params: Params) -> tuple:
    uplift, names = 0.0, []
    for ev in events:
        if ev.start <= day <= ev.end:
            uplift += ev.uplift
            names.append(ev.name)
    return min(params.event_max, 1.0 + uplift), names


def base_prices(day: date, params: Params) -> tuple:
    season = params.season.get(day.month, 1.0)
    room = round(params.bar_base * season * params.weekday.get(day.weekday(), 1.0))
    bed = round(params.bed_base * season * params.weekday_beds.get(day.weekday(), 1.0))
    return room, bed


def market_factor(comp: float | None, base: float, k: float, params: Params) -> tuple:
    if not comp or base <= 0:
        return 1.0, False
    target = comp * params.quality_index
    return clamp(1.0 + k * (target / base - 1.0), params.market_min, params.market_max), True


def allocate_flex(inv: Inventory, forecast_rooms: float, forecast_beds: float,
                  family_price: float, bed_price: float, *, rooms_otb: int = 0,
                  beds_otb: int = 0, blocked_rooms: int = 0, blocked_beds: int = 0,
                  private_min: int | None = None, private_max: int | None = None,
                  commission: float = 0, room_cost: float = 0, bed_cost: float = 0) -> int:
    """Enumerate feasible allocations, comparing total incremental contribution.

    Existing sales and blocks are hard constraints. PMS bounds can reserve whole
    flex rooms throughout existing stays. Without room identities this remains
    an advisory allocation, never a command to move guests or change inventory.
    """
    lower = max(0, rooms_otb + blocked_rooms - inv.private_rooms,
                private_min if private_min is not None else 0)
    upper = min(inv.flex_rooms,
                (inv.max_bed_capacity - beds_otb - blocked_beds) // inv.beds_per_flex_room,
                private_max if private_max is not None else inv.flex_rooms)
    if lower > upper:
        raise ValueError("Bookinger, blokeringer og flex-grænser kan ikke rummes samtidigt")
    room_net = family_price * (1 - commission) - room_cost
    bed_net = bed_price * (1 - commission) - bed_cost

    def value(n):
        room_capacity = inv.private_rooms + n - blocked_rooms
        bed_capacity = inv.dorm_beds + (inv.flex_rooms - n) * inv.beds_per_flex_room - blocked_beds
        # Fixed-room sales do not change with n; compare only extra flex sales.
        private_sales = max(0, min(forecast_rooms, room_capacity) -
                            max(rooms_otb, inv.private_rooms - blocked_rooms))
        bed_sales = max(0, min(forecast_beds, bed_capacity) - beds_otb)
        return private_sales * room_net + bed_sales * bed_net

    return max(range(lower, upper + 1), key=value)


# --------------------------------------------------------------------------
# Hovedberegning
# --------------------------------------------------------------------------

def price_day(item: DayInput, params: Params, events: Iterable[EventUplift] = (),
              today: date | None = None) -> Recommendation:
    today = today or date.today()
    lead = (item.day - today).days
    weekend = params.is_weekend(item.day)
    inv = params.inventory
    warnings: list = []
    if not inv.confirmed:
        warnings.append("Lageret er et ubekræftet eksempel — afstem med Picasso før live brug")
    if inv.flex_rooms:
        warnings.append("Flex er et dagsforslag: kontrollér konkrete rum og alle opholdsnætter i Picasso")
    counts = (item.rooms_otb, item.beds_otb, item.blocked_rooms, item.blocked_beds)
    if any(n < 0 for n in counts):
        raise ValueError("Bookinger og blokeringer må ikke være negative")
    if item.rooms_otb + item.blocked_rooms > inv.max_room_capacity or \
       item.beds_otb + item.blocked_beds > inv.max_bed_capacity:
        raise ValueError("Bookinger og blokeringer overstiger kapaciteten")

    # 1. Prognose på maksimalt lager, så tallet ikke afhænger af allokeringen
    max_rooms = max(0, inv.max_room_capacity - item.blocked_rooms)
    max_beds = max(0, inv.max_bed_capacity - item.blocked_beds)

    rooms_otb = item.rooms_otb
    beds_otb = item.beds_otb
    room_occ_now = rooms_otb / max_rooms if max_rooms else 0
    bed_occ_now = beds_otb / max_beds if max_beds else 0

    room_forecast = forecast_occupancy(room_occ_now, lead, weekend, params.booking_curve)
    bed_forecast = forecast_occupancy(bed_occ_now, lead, weekend, params.booking_curve_beds)
    forecast_rooms = room_forecast * max_rooms
    forecast_beds = bed_forecast * max_beds

    # 2. Foreløbige priser, så allokeringen har noget at regne på
    base_room, base_bed = base_prices(item.day, params)
    f_event, event_names = event_factor(item.day, events, params)
    f_market_rooms, has_room_comp = market_factor(item.comp_room, base_room, params.k_market, params)
    f_market_beds, has_bed_comp = market_factor(item.comp_bed, base_bed, params.k_market_bed, params)
    if not has_room_comp:
        warnings.append("Ingen konkurrentpris på værelser — F_marked sat til 1,000")
    if not has_bed_comp:
        warnings.append("Ingen konkurrentpris på senge — F_marked sat til 1,000")

    # 4. Pris mod prognose i forhold til mål.
    #
    # Knapheden måles mod det MAKSIMALE lager, ikke mod det allokerede. Et
    # privat værelse er ikke knapt, så længe der står flex-rum der kan laves om.
    # Målte vi mod den allokerede kapacitet, ville allokeringen — som netop
    # fylder lageret ud til efterspørgslen — gøre hver eneste dato udsolgt på
    # papiret og sende prisen i loft hver gang.
    room_gap = room_forecast - params.target_occupancy_rooms
    bed_gap = bed_forecast - params.target_occupancy_beds

    f_pace_rooms = clamp(1 + params.k_forecast * room_gap, params.pace_min, params.pace_max)
    f_pace_beds = clamp(1 + params.k_forecast_beds * bed_gap, params.pace_min, params.pace_max)

    raw_room = base_room * f_pace_rooms * f_market_rooms * f_event
    raw_bed = base_bed * f_pace_beds * f_market_beds * f_event

    room_price = round_within(raw_room, params.price_floor, params.price_ceiling, params.rounding)
    bed_price = round_within(raw_bed, params.bed_floor, params.bed_ceiling, params.rounding)

    # Linking products is advisory: privacy and dorm beds are distinct products.
    # Apply all hard price bounds last; never override the daily brake afterwards.
    room_price = (item.locked_room_price if item.locked_room_price is not None else
                  _brake(room_price, item.current_room_price, params, warnings,
                         "værelsespris", params.price_floor, params.price_ceiling))
    bed_price = (item.locked_bed_price if item.locked_bed_price is not None else
                 _brake(bed_price, item.current_bed_price, params, warnings,
                        "sengepris", params.bed_floor, params.bed_ceiling))
    if any(not math.isfinite(p) or p <= 0 for p in (room_price, bed_price)):
        raise ValueError("Priser skal være endelige positive beløb")
    room_prices = {
        code: round_to(room_price * factor, params.rounding)
        for code, factor in params.room_types.items()
    }
    family_price = room_prices.get("familie_4", room_price)
    if bed_price * inv.beds_per_flex_room < family_price * 0.9:
        warnings.append("Dorm underbyder familieværelse — kontrollér produktforskel; sengepris fastholdt")

    minimum = item.flex_private_min or 0
    if item.room_type_otb and inv.private_room_types:
        for code, count in item.room_type_otb.items():
            if code != "familie_4" and count > inv.private_room_types.get(code, 0):
                raise ValueError("Bookede rumtyper overstiger det faste lager")
        minimum = max(minimum, item.room_type_otb.get("familie_4", 0) -
                      inv.private_room_types.get("familie_4", 0))
    flex_to_private = allocate_flex(
        inv, forecast_rooms, forecast_beds, family_price, bed_price,
        rooms_otb=rooms_otb, beds_otb=beds_otb,
        blocked_rooms=item.blocked_rooms, blocked_beds=item.blocked_beds,
        private_min=minimum, private_max=item.flex_private_max,
        commission=params.commission, room_cost=params.variable_cost,
        bed_cost=params.variable_cost_bed,
    )
    room_capacity = max(0, inv.private_rooms + flex_to_private - item.blocked_rooms)
    bed_capacity = max(0, inv.dorm_beds + (inv.flex_rooms - flex_to_private) *
                       inv.beds_per_flex_room - item.blocked_beds)

    net_room = room_price * (1 - params.commission) - params.variable_cost
    net_bed = bed_price * (1 - params.commission) - params.variable_cost_bed

    # 7. RevPAB — husets samlede nøgletal
    revpab, revenue_basis = revenue_forecast(
        item, params, room_prices, bed_price, forecast_rooms, forecast_beds,
        room_capacity, bed_capacity, flex_to_private,
    )
    if revenue_basis == "estimated":
        warnings.append("RevPAB er et skøn: mangler bekræftet typemiks eller bogført værelses-/sengeomsætning")

    # 8. Advarsler
    if net_room < 0:
        warnings.append("Værelsespris under variabel omkostning")
    if net_bed < 0:
        warnings.append("Sengepris under variabel omkostning")
    if room_price <= params.price_floor:
        warnings.append("Værelsespris på prisgulv")
    if room_price >= params.price_ceiling:
        warnings.append("Værelsespris på prisloft")
    if room_forecast > 0.97:
        warnings.append("Værelser forventes udsolgt — overvej minimum-ophold")
    if bed_forecast > 0.97:
        warnings.append("Senge forventes udsolgt")
    if room_forecast < params.target_occupancy_rooms - 0.25:
        warnings.append("Værelser langt under mål — kampagne virker bedre end prisfald alene")
    if flex_to_private == inv.flex_rooms and inv.flex_rooms:
        warnings.append("Alle flex-rum er sat til private værelser")

    return Recommendation(
        day=item.day, lead_days=lead,
        flex_to_private=flex_to_private,
        room_capacity=room_capacity, bed_capacity=bed_capacity,
        rooms_otb=rooms_otb, beds_otb=beds_otb,
        room_occ_now=room_occ_now, bed_occ_now=bed_occ_now,
        room_forecast=room_forecast, bed_forecast=bed_forecast,
        forecast_rooms=forecast_rooms, forecast_beds=forecast_beds,
        f_pace_rooms=f_pace_rooms, f_pace_beds=f_pace_beds,
        f_market_rooms=f_market_rooms, f_market_beds=f_market_beds,
        f_event=f_event, event_names=event_names,
        base_room=base_room, base_bed=base_bed,
        room_price=room_price, bed_price=bed_price, room_types=room_prices,
        net_room=net_room, net_bed=net_bed,
        revpab=revpab, warnings=warnings, revenue_basis=revenue_basis,
    )


def _brake(new_price: float, current: float | None, params: Params,
           warnings: list, label: str, floor: float, ceiling: float) -> float:
    if not current:
        return round_within(new_price, floor, ceiling, params.rounding)
    low = max(floor, current * (1 - params.max_daily_change))
    high = min(ceiling, current * (1 + params.max_daily_change))
    if low > high:
        raise ValueError(f"{label}: prisgulv/-loft og ændringsbremse kan ikke overholdes samtidigt")
    braked = round_within(new_price, low, high, params.rounding)
    if abs(braked - new_price) > 0.01:
        warnings.append(f"Ændringsbremse på {label}: {new_price:.0f} → {braked:.0f} kr.")
    return braked


def revenue_forecast(item, params, room_prices, bed_price, forecast_rooms,
                     forecast_beds, room_capacity, bed_capacity, flex_to_private):
    """Booked revenue stays at booked rates; only future sales use new rates.

    Remaining room sales use a capacity-weighted room-type mix. This is a
    forecast, not realized revenue or a measured revenue uplift.
    """
    inv = params.inventory
    types = dict(inv.private_room_types) or {"dobbelt_uden_bad": inv.private_rooms}
    types["familie_4"] = types.get("familie_4", 0) + flex_to_private
    if any(t not in room_prices for t in types):
        raise ValueError("Lagertype mangler prisfaktor")
    known_mix = sum(item.room_type_otb.values()) == item.rooms_otb
    if item.room_type_otb and (not known_mix or any(
            n < 0 or n > types.get(t, 0) for t, n in item.room_type_otb.items())):
        raise ValueError("room_type_otb skal matche solgte værelser og allokerede typer")
    remaining = {t: n - item.room_type_otb.get(t, 0) for t, n in types.items()}
    # Blocks lack type identity: retain an explicit estimate flag when present.
    rate = sum(room_prices[t] * n for t, n in remaining.items()) / max(1, sum(remaining.values()))
    booked_room = item.booked_room_revenue
    booked_bed = item.booked_bed_revenue
    complete = (inv.confirmed and known_mix and not item.blocked_rooms and
                (booked_room is not None or not item.rooms_otb) and
                (booked_bed is not None or not item.beds_otb))
    if booked_room is None:
        booked_room = item.rooms_otb * (item.current_room_price or rate)
    if booked_bed is None:
        booked_bed = item.beds_otb * (item.current_bed_price or bed_price)
    if booked_room < 0 or booked_bed < 0:
        raise ValueError("Bogført omsætning må ikke være negativ")
    future_rooms = max(0, min(forecast_rooms, room_capacity) - item.rooms_otb)
    future_beds = max(0, min(forecast_beds, bed_capacity) - item.beds_otb)
    return ((booked_room + booked_bed + future_rooms * rate + future_beds * bed_price)
            / inv.total_beds, "booked_plus_forecast" if complete else "estimated")


def price_range(items: Iterable[DayInput], params: Params,
                events: Iterable[EventUplift] = (), today: date | None = None) -> list:
    events = list(events)
    return [price_day(item, params, events, today) for item in items]


# --------------------------------------------------------------------------
# Gruppeforespørgsler
# --------------------------------------------------------------------------

@dataclass
class GroupQuote:
    day: date
    forecast_rooms: float
    room_capacity: int
    spare_rooms: float
    displaced_rooms: float
    transient_price: float
    minimum_rate: float

    def as_dict(self) -> dict:
        out = asdict(self)
        out["day"] = self.day.isoformat()
        return out


def group_quote(recommendations: Sequence[Recommendation], rooms_requested: int,
                params: Params) -> list:
    """Hvad er den laveste pris en gruppeforespørgsel må få?

    Når en skoleklasse beder om 20 værelser, er svaret ikke en mavefornemmelse.
    Prognosen ved hvor mange værelser der alligevel ville være blevet solgt til
    almindelig pris. De fortrængte værelser er gruppens reelle omkostning.
    """
    quotes = []
    for rec in recommendations:
        spare = max(0.0, rec.room_capacity - rec.forecast_rooms)
        displaced = max(0.0, rooms_requested - spare)
        displaced_revenue = displaced * rec.room_price * (1 - params.commission)
        minimum = displaced_revenue / max(1, rooms_requested) + params.variable_cost
        quotes.append(GroupQuote(
            day=rec.day,
            forecast_rooms=rec.forecast_rooms,
            room_capacity=rec.room_capacity,
            spare_rooms=spare,
            displaced_rooms=displaced,
            transient_price=rec.room_price,
            minimum_rate=round_within(max(minimum, params.price_floor),
                                      params.price_floor, params.price_ceiling,
                                      params.rounding),
        ))
    return quotes


# --------------------------------------------------------------------------
# Kvalitetsindeks
# --------------------------------------------------------------------------

ADR_PER_REVIEW_POINT = 0.0089
"""Cornell (Anderson 2012): +1 point på et 100-punkts review-indeks svarer til
ca. +0,89 % ADR."""


def quality_index(own_score: float, competitors: Sequence[tuple],
                  low: float = 0.85, high: float = 1.15) -> float:
    total_weight = sum(w for w, _ in competitors)
    if total_weight <= 0:
        return 1.0
    comp_avg = sum(w * s for w, s in competitors) / total_weight
    delta_points = (own_score - comp_avg) * 10.0
    return clamp(1.0 + delta_points * ADR_PER_REVIEW_POINT, low, high)

"""Prismodellen, version 4 — bygget til et hostel, ikke til et hotel.

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
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from functools import lru_cache
from typing import Iterable, Sequence

from . import bidprice
from .bidprice import V4Config
from .demand import DemandModel, sales_forecast
from .ladder import LadderConfig, ladder_step
from .pricing_basis import PricingBasis


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
    # Flex-rummenes Picasso-koder, fx {"V6": 7, "V8": 6}. Tom = ét samlet
    # "familie_4" (den gamle model med én rumtype for alle flex-rum).
    flex_room_types: dict = field(default_factory=dict)
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
        if self.flex_room_types and (
            any(not isinstance(n, int) or n < 0 for n in self.flex_room_types.values())
            or sum(self.flex_room_types.values()) != self.flex_rooms
        ):
            raise ValueError("Flex-rumtyper skal summere til flex_rooms")
        if set(self.flex_room_types) & set(self.private_room_types):
            raise ValueError("En rumtype kan ikke være både privat og flex")
        if self.confirmed and (self.private_beds is None or
                               (self.private_rooms and not self.private_room_types)):
            raise ValueError("Bekræftet lager kræver private_beds og private_room_types")
        if self.total_beds <= 0:
            raise ValueError("Lageret skal indeholde mindst én seng")

    @property
    def flex_mix(self) -> dict:
        """Flex-rummenes typer og antal; uden opdeling ét samlet familie_4."""
        return dict(self.flex_room_types) or {"familie_4": self.flex_rooms}

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

    def interpolated(self, lead_days: int, is_weekend: bool) -> float:
        """Som reference(), men lineært mellem punkterne.

        Kurven er en trappe. Til prognosen er det fint; til at måle tempo over
        en uge er det ikke — så svinger forventet pickup mellem nul og meget,
        alt efter om ugen krydser et trappetrin.
        """
        lead = max(0, lead_days)
        pts = list(self.points)
        val = (lambda p: p.weekend if is_weekend else p.weekday)
        for a, b in zip(pts, pts[1:]):
            if a.lead_days_from <= lead < b.lead_days_from:
                w = (lead - a.lead_days_from) / (b.lead_days_from - a.lead_days_from)
                return val(a) + w * (val(b) - val(a))
        last = pts[-1]
        if lead >= last.lead_days_from:
            # Hale: lineært mod nul ved dobbelt så langt ude som sidste punkt
            span = max(1, last.lead_days_from)
            return max(0.0, val(last) * (1 - (lead - last.lead_days_from) / span))
        return val(pts[0])

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
    # Lineært mellem kurvens punkter: med trappen hoppede prognosen hver gang
    # lead time krydsede et punkt (fx 60 -> 61 dage), og prisen hoppede med.
    ref_now = curve.interpolated(lead_days, is_weekend)
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
    # Sengekoder (Picassos B-typer) som faktor på sengeprisen. Tom = ingen.
    bed_types: dict = field(default_factory=dict)

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
    pricing_basis: PricingBasis = field(default_factory=PricingBasis)
    price_floor: float = 375.0
    price_ceiling: float = 1400.0
    bed_floor: float = 165.0
    bed_ceiling: float = 600.0
    rounding: float = 10.0

    quality_index: float = 1.00
    booking_curve: BookingCurve = field(default_factory=BookingCurve.rooms)
    booking_curve_beds: BookingCurve = field(default_factory=BookingCurve.beds)

    # Prisstige. V4 bruger stigen som prisrum, træghed og forklaring.
    ladder: LadderConfig | None = None

    # Bid price vælger trinnet ud fra forventet omsætning over hele
    # efterspørgselsfordelingen, og målbelægningen indgår ikke.
    v4: V4Config | None = None
    demand_models: dict = field(default_factory=dict)
    demand_level: tuple = (1.0, 1.0)   # (rum, senge) fra app/level.py

    @property
    def v4_active(self) -> bool:
        return bool(self.v4 and self.v4.enabled and self.ladder
                    and {"rum", "senge"} <= set(self.demand_models))

    def is_weekend(self, day: date) -> bool:
        return day.weekday() in self.weekend_days

    def contribution(self, price: float, *, beds: bool = False) -> float:
        return self.pricing_basis.contribution(price, self.commission,
            self.variable_cost_bed if beds else self.variable_cost)


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
    # Til prisstigen: gårsdagens trin og belægningen for ca. en uge siden
    prev_room_rung: int | None = None
    prev_bed_rung: int | None = None
    prev_room_position: float | None = None
    prev_bed_position: float | None = None
    rooms_otb_prior: int | None = None
    beds_otb_prior: int | None = None
    room_capacity_prior: int | None = None
    bed_capacity_prior: int | None = None
    prior_days: int | None = None
    same_day_fraction: float | None = None
    market_notes: tuple = ()


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
    room_ladder: dict | None = None
    bed_ladder: dict | None = None
    flex_steps: list | None = None   # (rum nr., værdi som rum, værdi som senge)

    @property
    def room_rung(self) -> int | None:
        return self.room_ladder["rung"] if self.room_ladder else None

    @property
    def bed_rung(self) -> int | None:
        return self.bed_ladder["rung"] if self.bed_ladder else None

    def as_dict(self) -> dict:
        out = asdict(self)
        out["day"] = self.day.isoformat()
        return out

    def explain(self) -> str:
        if self.room_ladder:
            return (
                f"værelse trin {self.room_ladder['label']} ({self.room_price:.0f} kr., "
                f"tryk {self.room_ladder['score']:+.2f}) · seng trin "
                f"{self.bed_ladder['label']} ({self.bed_price:.0f} kr.) · "
                f"prognose {self.room_forecast:.0%} værelser / {self.bed_forecast:.0%} senge · "
                f"RevPAB {self.revpab:.0f} kr."
            )
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


def flex_bounds(inv: Inventory, *, rooms_otb: int, beds_otb: int,
                blocked_rooms: int, blocked_beds: int,
                private_min: int | None, private_max: int | None) -> tuple:
    """Hvor mange flex-rum der overhovedet må gå til private værelser.

    Solgte rum og blokeringer er hårde bindinger; PMS-grænserne kan reservere
    hele flex-rum gennem et helt ophold. Samme grænser gælder uanset om
    allokeringen afgøres af historisk gennemsnitslogik eller af v4's bid price.
    """
    lower = max(0, rooms_otb + blocked_rooms - inv.private_rooms,
                private_min if private_min is not None else 0)
    upper = min(inv.flex_rooms,
                (inv.max_bed_capacity - beds_otb - blocked_beds) // inv.beds_per_flex_room,
                private_max if private_max is not None else inv.flex_rooms)
    if lower > upper:
        raise ValueError("Bookinger, blokeringer og flex-grænser kan ikke rummes samtidigt")
    return lower, upper


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
    lower, upper = flex_bounds(inv, rooms_otb=rooms_otb, beds_otb=beds_otb,
                               blocked_rooms=blocked_rooms, blocked_beds=blocked_beds,
                               private_min=private_min, private_max=private_max)
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
    warnings: list = list(item.market_notes)
    if not params.pricing_basis.confirmed:
        warnings.append("Modelbidrag er et skøn: bekræft momsgrundlag for priser og "
                        "omkostninger samt provisionsgrundlag i konfigurationen")
    if lead == 0 and item.same_day_fraction is not None and params.v4_active:
        if not math.isfinite(item.same_day_fraction) or not 0 <= item.same_day_fraction <= 1:
            raise ValueError("Resterende andel af ankomstdagen skal være mellem 0 og 1")
        params = replace(params, demand_models={
            name: replace(model, same_day_fraction=item.same_day_fraction)
            for name, model in params.demand_models.items()})
        warnings.append("Ankomstdag: resterende salg er et skøn fra historisk dags-pickup, "
                        "fordelt jævnt frem til midnat; bookingtidspunkter mangler")
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

    if params.v4_active:
        # Reference-price forecast from the very same bound demand models and
        # adaptive levels used by bid price. Price elasticity is applied to
        # candidate prices in _bid_choices, not to this reference forecast.
        forecast_rooms = sales_forecast(params.demand_models["rum"], rooms_otb, lead,
                                        item.day, max_rooms, params.demand_level[0])["forecast"]
        forecast_beds = sales_forecast(params.demand_models["senge"], beds_otb, lead,
                                       item.day, max_beds, params.demand_level[1])["forecast"]
        room_forecast = forecast_rooms / max_rooms if max_rooms else 0
        bed_forecast = forecast_beds / max_beds if max_beds else 0
    else:
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

    room_ladder = bed_ladder = None
    if params.ladder is not None:
        room_ladder, bed_ladder = _ladder(
            item, params, lead, weekend, base_room, base_bed, f_event,
            room_forecast, bed_forecast, room_occ_now, bed_occ_now,
            forecast_rooms, forecast_beds, max_rooms, max_beds)
        raw_room, raw_bed = room_ladder.price, bed_ladder.price
        warnings.extend(f"Stige værelse: {r}" for r in room_ladder.reasons
                        if not r.startswith("Intet tempo"))
        warnings.extend(f"Stige seng: {r}" for r in bed_ladder.reasons
                        if not r.startswith("Intet tempo"))
        if any(r.startswith("Intet tempo") for r in room_ladder.reasons):
            warnings.append("Stige: intet tempo-signal endnu (kræver en kørsel fra ca. en uge siden)")

    room_floor, bed_floor = params.price_floor, params.bed_floor
    if params.v4_active:
        room_floor = max(room_floor, room_ladder.rung_prices[
            _discount_minimum(params, params.ladder.rungs_rooms)])
        bed_floor = max(bed_floor, bed_ladder.rung_prices[
            _discount_minimum(params, params.ladder.rungs_beds)])
    room_price = round_within(raw_room, room_floor, params.price_ceiling, params.rounding)
    bed_price = round_within(raw_bed, bed_floor, params.bed_ceiling, params.rounding)

    # Linking products is advisory: privacy and dorm beds are distinct products.
    # Apply all hard price bounds last; never override the daily brake afterwards.
    room_price = (item.locked_room_price if item.locked_room_price is not None else
                  _brake(room_price, item.current_room_price, params, warnings,
                         "værelsespris", room_floor, params.price_ceiling,
                         room_ladder.rung_prices if room_ladder else None))
    bed_price = (item.locked_bed_price if item.locked_bed_price is not None else
                 _brake(bed_price, item.current_bed_price, params, warnings,
                        "sengepris", bed_floor, params.bed_ceiling,
                        bed_ladder.rung_prices if bed_ladder else None))
    # Stigen skal huske det trin prisen faktisk endte på — efter bremse og lås —
    # ellers starter morgendagens hysterese fra et trin der aldrig blev brugt.
    for ladder, final in ((room_ladder, room_price), (bed_ladder, bed_price)):
        if ladder is not None:
            if ladder.exploration is not None:
                ladder.exploration.update(proposed_price=final,
                    assignment_preserved=abs(ladder.exploration["assigned_price"]-final) <= .01)
            nearest = min(range(ladder.n_rungs),
                          key=lambda i: (abs(ladder.rung_prices[i] - final), -i))
            ladder.off_ladder = abs(ladder.rung_prices[nearest] - final) > 0.01
            if nearest != ladder.rung:
                ladder.reasons.append(f"Endte på trin {nearest + 1} efter bremse/lås")
            ladder.rung, ladder.price = nearest, final
    if any(not math.isfinite(p) or p <= 0 for p in (room_price, bed_price)):
        raise ValueError("Priser skal være endelige positive beløb")
    room_prices = {
        code: round_to(room_price * factor, params.rounding)
        for code, factor in params.room_types.items()
    }
    bed_prices = {
        code: round_to(bed_price * factor, params.rounding)
        for code, factor in params.bed_types.items()
    }
    # Et flex-rum solgt privat: gennemsnitsprisen over flex-typerne, vægtet med
    # antal rum. Uden opdeling er det familie_4 som før.
    flex = inv.flex_mix
    if inv.flex_room_types:
        family_price = (sum(room_prices[c] * n for c, n in flex.items())
                        / max(1, sum(flex.values())))
    else:
        family_price = room_prices.get("familie_4", room_price)
    if bed_price * inv.beds_per_flex_room < family_price * 0.9:
        warnings.append("Dorm underbyder familieværelse — kontrollér produktforskel; sengepris fastholdt")

    lower, upper = _day_flex_bounds(item, params)
    flex_steps = None
    if params.v4_active and params.v4.use_for_flex:
        # Controls and locks can change the optimized prices. Allocate against
        # those actual prices, with the same shared inventory and type mix.
        flex_to_private, flex_steps = _allocation_at_prices(
            item, params, lead, base_room, base_bed, room_price, bed_price,
            oracle=room_ladder.bid.get("uncertainty", {}).get("scenario_oracle"))
    else:
        flex_to_private = allocate_flex(
            inv, forecast_rooms, forecast_beds, params.contribution(family_price),
            params.contribution(bed_price, beds=True),
            rooms_otb=rooms_otb, beds_otb=beds_otb,
            blocked_rooms=item.blocked_rooms, blocked_beds=item.blocked_beds,
            private_min=lower, private_max=upper,
            commission=0, room_cost=0, bed_cost=0,
        )
    room_capacity = max(0, inv.private_rooms + flex_to_private - item.blocked_rooms)
    bed_capacity = max(0, inv.dorm_beds + (inv.flex_rooms - flex_to_private) *
                       inv.beds_per_flex_room - item.blocked_beds)

    if params.v4_active:
        _final_bid(item, params, lead, room_ladder, room_price,
                   room_capacity - rooms_otb, beds=False)
        _final_bid(item, params, lead, bed_ladder, bed_price,
                   bed_capacity - beds_otb, beds=True)
        for label, ladder in (("Værelser", room_ladder), ("Senge", bed_ladder)):
            stress = ladder.bid["uncertainty"]
            if stress["sensitive"]:
                warnings.append(f"{label}: bedste pris afhænger af antaget prisfølsomhed; se økonomi")

    net_room = params.contribution(room_price)
    net_bed = params.contribution(bed_price, beds=True)

    # 7. RevPAB — husets samlede nøgletal
    revpab, revenue_basis = revenue_forecast(
        item, params, room_prices, bed_price, forecast_rooms, forecast_beds,
        room_capacity, bed_capacity, flex_to_private, family_price,
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
        room_price=room_price, bed_price=bed_price,
        room_types={**room_prices, **bed_prices},
        net_room=net_room, net_bed=net_bed,
        revpab=revpab, warnings=warnings, revenue_basis=revenue_basis,
        flex_steps=flex_steps,
        room_ladder=room_ladder.as_dict() if room_ladder else None,
        bed_ladder=bed_ladder.as_dict() if bed_ladder else None,
    )


def _ladder(item, params, lead, weekend, base_room, base_bed, f_event,
            room_forecast, bed_forecast, room_occ_now, bed_occ_now,
            forecast_rooms, forecast_beds, max_rooms, max_beds):
    """Kør prisstigen for værelser og senge.

    Tempo = belægning booket siden forrige snapshot mod det kurven forventede
    i samme vindue. Begge i andel af huset, så værelser og senge kan sammenlignes.
    """
    cfg = params.ladder

    def pickup(otb_now, otb_prior, capacity, curve):
        if otb_prior is None or not item.prior_days or item.prior_days <= 0 or not capacity:
            return None, None
        actual = (otb_now - otb_prior) / capacity
        expected = max(0.0, curve.interpolated(lead, weekend) -
                       curve.interpolated(lead + item.prior_days, weekend))
        return actual, expected

    rp, re_ = pickup(item.rooms_otb, item.rooms_otb_prior, max_rooms, params.booking_curve)
    bp, be = pickup(item.beds_otb, item.beds_otb_prior, max_beds, params.booking_curve_beds)

    room_override = bed_override = None
    room_bid = bed_bid = None
    if params.v4_active:
        room_choice, bed_choice = _bid_choices(item, params, lead, base_room, base_bed)
        room_override = float(room_choice.rung)
        bed_override = float(bed_choice.rung)
        room_bid, bed_bid = room_choice.as_dict(), bed_choice.as_dict()
    protect = not params.v4_active
    seed_room = f"{item.day.isoformat()}|rum" if params.v4_active else None
    seed_bed = f"{item.day.isoformat()}|senge" if params.v4_active else None

    room = ladder_step(
        base=base_room, rungs=cfg.rungs_rooms, cfg=cfg,
        rounder=lambda v: round_within(v, params.price_floor, params.price_ceiling, params.rounding),
        forecast=room_forecast, target=params.target_occupancy_rooms, occ_now=room_occ_now,
        lead_days=lead, pickup=rp, expected_pickup=re_, comp_price=item.comp_room,
        quality_index=params.quality_index, event_factor=f_event,
        capacity=max_rooms, forecast_units=forecast_rooms, previous_rung=item.prev_room_rung,
        previous_position=item.prev_room_position,
        position_override=room_override, scarcity_protect=protect,
        use_ratchet=None if protect else False,
        explore_seed=seed_room, bid=room_bid,
        minimum_rung=_discount_minimum(params, cfg.rungs_rooms))
    bed = ladder_step(
        base=base_bed, rungs=cfg.rungs_beds, cfg=cfg,
        rounder=lambda v: round_within(v, params.bed_floor, params.bed_ceiling, params.rounding),
        forecast=bed_forecast, target=params.target_occupancy_beds, occ_now=bed_occ_now,
        lead_days=lead, pickup=bp, expected_pickup=be, comp_price=item.comp_bed,
        quality_index=params.quality_index, event_factor=f_event,
        capacity=max_beds, forecast_units=forecast_beds, previous_rung=item.prev_bed_rung,
        previous_position=item.prev_bed_position,
        position_override=bed_override, scarcity_protect=protect,
        use_ratchet=None if protect else False,
        explore_seed=seed_bed, bid=bed_bid,
        minimum_rung=_discount_minimum(params, cfg.rungs_beds))
    return room, bed


def _discount_minimum(params: Params, rungs: Sequence[float]) -> int:
    if not params.v4_active or params.v4.elasticity_measured:
        return 0
    return max(0, list(rungs).index(1.0) - params.v4.max_discount_rungs)


def _bid_choices(item: DayInput, params: Params, lead: int,
                 base_room: float, base_bed: float) -> tuple:
    """Choose prices across feasible allocations of the same shared rooms."""
    cfg = params.ladder
    level_rooms, level_beds = params.demand_level
    room_model = _CachedDemand(params.demand_models["rum"].bind(item.rooms_otb))
    bed_model = _CachedDemand(params.demand_models["senge"].bind(item.beds_otb))
    private_factor, flex_factor, private_free = _room_mix(item, params)
    lower, upper = _day_flex_bounds(item, params)
    best, score = None, -math.inf
    actions, contexts = [], {}
    robust = params.v4.uncertainty_mode == "minimax_regret"
    room_es, bed_es = params.v4.elasticities(), params.v4.elasticities(beds=True)
    scenarios = [(r, b) for r in room_es for b in bed_es]
    for n in range(lower, upper + 1):
        rooms, beds = _free_capacity(item, params, n)
        room_args = dict(
            model=room_model,
            rung_prices=rung_prices_for(base_room, cfg.rungs_rooms, params, beds=False),
            reference_rung=list(cfg.rungs_rooms).index(1.0), lead=lead, day=item.day,
            capacity_remaining=rooms, commission=params.commission,
            variable_cost=params.variable_cost, elasticity=params.v4.elasticity_rooms,
            level=level_rooms, price_factor=private_factor,
            fixed_capacity=private_free, flex_price_factor=flex_factor,
            minimum_rung=_discount_minimum(params, cfg.rungs_rooms),
            contribution=params.contribution)
        bed_args = dict(
            model=bed_model,
            rung_prices=rung_prices_for(base_bed, cfg.rungs_beds, params, beds=True),
            reference_rung=list(cfg.rungs_beds).index(1.0), lead=lead, day=item.day,
            capacity_remaining=beds, commission=params.commission,
            variable_cost=params.variable_cost_bed, elasticity=params.v4.elasticity_beds,
            level=level_beds, minimum_rung=_discount_minimum(params, cfg.rungs_beds),
            contribution=lambda p: params.contribution(p, beds=True))
        room = bidprice.choose_rung(**room_args)
        bed = bidprice.choose_rung(**bed_args)
        if robust:
            from .uncertainty import Action
            rt = {e: bidprice.choose_rung(**dict(room_args, elasticity=e)).table for e in room_es}
            bt = {e: bidprice.choose_rung(**dict(bed_args, elasticity=e)).table for e in bed_es}
            contexts[n] = (room_args, bed_args)
            # Each action contains ONE shared allocation, not independently
            # optimized room/bed capacities in different scenarios.
            for r in range(room_args["minimum_rung"], len(room.table)):
                for b in range(bed_args["minimum_rung"], len(bed.table)):
                    actions.append(Action((n, r, b), tuple(
                        rt[er][r][3] + bt[eb][b][3] - params.v4.flex_min_gain * (n-lower)
                        for er, eb in scenarios)))
        objective = (room.expected_revenue + bed.expected_revenue
                     - params.v4.flex_min_gain * (n - lower))
        if objective > score + 1e-9:
            best, score = (room, bed), objective
    if robust:
        from .uncertainty import minimax_regret
        nominal = scenarios.index((params.v4.elasticity_rooms, params.v4.elasticity_beds))
        action, regret = minimax_regret(actions, nominal)
        oracle = [max(a.values[i] for a in actions) for i in range(len(scenarios))]
        n, r, b = action.key
        room_args, bed_args = contexts[n]
        best = (bidprice.choose_rung(**room_args, selected_rung=r),
                bidprice.choose_rung(**bed_args, selected_rung=b))
        for choice in best:
            choice.uncertainty = dict(mode="minimax_regret", scenario_count=len(scenarios),
                joint_worst_regret=round(regret, 2), allocation=n,
                scenario_oracle=oracle, scenarios=scenarios,
                scope="Optimizer before market, smoothing, exploration, brakes and locks",
                basis="Configured stress scenarios, not confidence limits")
    return best


class _CachedDemand:
    """Cache distribution queries only within this date's allocation search."""
    def __init__(self, model):
        self.survival = lru_cache(maxsize=None)(model.survival)


def _day_flex_bounds(item: DayInput, params: Params) -> tuple:
    inv, minimum = params.inventory, item.flex_private_min or 0
    if item.room_type_otb and inv.private_room_types:
        for code, count in item.room_type_otb.items():
            if code in inv.flex_mix and not inv.flex_room_types:
                continue
            if count > inv.private_room_types.get(code, 0) + inv.flex_mix.get(code, 0):
                raise ValueError("Bookede rumtyper overstiger det faste lager")
        minimum = max(minimum, sum(max(0, item.room_type_otb.get(c, 0)
                                      - inv.private_room_types.get(c, 0)) for c in inv.flex_mix))
    return flex_bounds(inv, rooms_otb=item.rooms_otb, beds_otb=item.beds_otb,
        blocked_rooms=item.blocked_rooms, blocked_beds=item.blocked_beds,
        private_min=minimum, private_max=item.flex_private_max)


def _free_capacity(item: DayInput, params: Params, n: int) -> tuple:
    inv = params.inventory
    return (max(0, inv.private_rooms + n - item.blocked_rooms - item.rooms_otb),
            max(0, inv.max_bed_capacity - n * inv.beds_per_flex_room
                - item.blocked_beds - item.beds_otb))


def _room_mix(item: DayInput, params: Params) -> tuple:
    """Fixed rooms fill before extra flex capacity; opening a dorm must not
    reprice the same fixed-room demand as more expensive family-room sales.
    """
    inv = params.inventory
    known = sum(item.room_type_otb.values()) == item.rooms_otb
    private = dict(inv.private_room_types) or {"dobbelt_uden_bad": inv.private_rooms}
    fixed_count = fixed_value = 0
    for code, count in private.items():
        free = max(0, count - (item.room_type_otb.get(code, 0) if known else 0))
        fixed_count += free
        fixed_value += free * params.room_types.get(code, 1.0)
    flex_factor = sum(params.room_types.get(c, 1.0) * count for c, count in inv.flex_mix.items()) / max(1, inv.flex_rooms)
    private_otb = (sum(min(count, item.room_type_otb.get(c, 0)) for c, count in private.items())
                   if known else item.rooms_otb)
    private_free = max(0, inv.private_rooms - item.blocked_rooms - private_otb)
    return fixed_value / max(1, fixed_count), flex_factor, private_free


def _allocation_at_prices(item, params, lead, base_room, base_bed, room_price, bed_price, *, oracle=None):
    lower, upper = _day_flex_bounds(item, params)
    room_model = _CachedDemand(params.demand_models["rum"].bind(item.rooms_otb))
    bed_model = _CachedDemand(params.demand_models["senge"].bind(item.beds_otb))
    private_factor, flex_factor, private_free = _room_mix(item, params)
    room_ref = rung_prices_for(base_room, params.ladder.rungs_rooms, params, beds=False)[
        list(params.ladder.rungs_rooms).index(1.0)]
    bed_ref = rung_prices_for(base_bed, params.ladder.rungs_beds, params, beds=True)[
        list(params.ladder.rungs_beds).index(1.0)]
    robust = params.v4.uncertainty_mode == "minimax_regret"
    room_es = params.v4.elasticities() if robust else (params.v4.elasticity_rooms,)
    bed_es = params.v4.elasticities(beds=True) if robust else (params.v4.elasticity_beds,)
    scenarios = [(r, b) for r in room_es for b in bed_es]
    actions = []
    best, score, steps, previous = lower, -math.inf, [], None
    for n in range(lower, upper + 1):
        rooms, beds = _free_capacity(item, params, n)
        room_sales = bidprice.expected_sales(room_model, lead, item.day, rooms,
            level=params.demand_level[0], multiplier=bidprice.demand_multiplier(
                room_price, room_ref, params.v4.elasticity_rooms))
        bed_sales = bidprice.expected_sales(bed_model, lead, item.day, beds,
            level=params.demand_level[1], multiplier=bidprice.demand_multiplier(
                bed_price, bed_ref, params.v4.elasticity_beds))
        fixed_sales = bidprice.expected_sales(room_model, lead, item.day, min(rooms, private_free),
            level=params.demand_level[0], multiplier=bidprice.demand_multiplier(
                room_price, room_ref, params.v4.elasticity_rooms))
        room_value = (params.contribution(room_price * private_factor) * fixed_sales
                      + params.contribution(room_price * flex_factor) * max(0, room_sales - fixed_sales))
        bed_value = params.contribution(bed_price, beds=True) * bed_sales
        if robust:
            from .uncertainty import Action
            rv, bv = {}, {}
            for e in room_es:
                mult = bidprice.demand_multiplier(room_price, room_ref, e)
                sales = bidprice.expected_sales(room_model, lead, item.day, rooms,
                    level=params.demand_level[0], multiplier=mult)
                fixed = bidprice.expected_sales(room_model, lead, item.day, min(rooms, private_free),
                    level=params.demand_level[0], multiplier=mult)
                rv[e] = (params.contribution(room_price * private_factor) * fixed
                         + params.contribution(room_price * flex_factor) * max(0, sales-fixed))
            for e in bed_es:
                bv[e] = params.contribution(bed_price, beds=True) * bidprice.expected_sales(
                    bed_model, lead, item.day, beds, level=params.demand_level[1],
                    multiplier=bidprice.demand_multiplier(bed_price, bed_ref, e))
            actions.append(Action((n,), tuple(rv[r]+bv[b]-params.v4.flex_min_gain*(n-lower)
                                               for r, b in scenarios)))
        objective = room_value + bed_value - params.v4.flex_min_gain * (n - lower)
        if previous is not None:
            steps.append((n, round(room_value - previous[0], 2), round(previous[1] - bed_value, 2)))
        previous = (room_value, bed_value)
        if objective > score + 1e-9:
            best, score = n, objective
    if robust:
        from .uncertainty import minimax_regret
        nominal = scenarios.index((params.v4.elasticity_rooms, params.v4.elasticity_beds))
        selected, _ = minimax_regret(actions, nominal, oracle=oracle)
        best = selected.key[0]
    return best, steps


def _final_bid(item, params, lead, ladder, price, capacity, *, beds):
    """Describe the posted price and actual shared capacity after every control."""
    model = _CachedDemand(params.demand_models["senge" if beds else "rum"].bind(
        item.beds_otb if beds else item.rooms_otb))
    elasticity = params.v4.elasticity_beds if beds else params.v4.elasticity_rooms
    level = params.demand_level[1 if beds else 0]
    multiplier = bidprice.demand_multiplier(price, ladder.rung_prices[ladder.reference_rung], elasticity)
    factor, flex_factor, private_free = (1.0, 1.0, None) if beds else _room_mix(item, params)
    net = params.contribution(price * factor, beds=beds)
    sales = bidprice.expected_sales(model, lead, item.day, capacity, level=level, multiplier=multiplier)
    revenue, marginal_net = net * sales, net
    if not beds:
        fixed_sales = bidprice.expected_sales(model, lead, item.day, min(capacity, private_free),
                                              level=level, multiplier=multiplier)
        flex_net = params.contribution(price * flex_factor)
        revenue = net * fixed_sales + flex_net * max(0, sales - fixed_sales)
        if capacity > private_free:
            marginal_net = flex_net
        net = revenue / sales if sales > 0 else net
    original = dict(ladder.bid)
    evaluated = bidprice.choose_rung(model=model, rung_prices=ladder.rung_prices,
        reference_rung=ladder.reference_rung, lead=lead, day=item.day,
        capacity_remaining=capacity, commission=params.commission,
        variable_cost=params.variable_cost_bed if beds else params.variable_cost,
        elasticity=elasticity, level=level, price_factor=factor,
        fixed_capacity=private_free, flex_price_factor=flex_factor,
        contribution=lambda p: params.contribution(p, beds=beds))
    ladder.bid.update(rung=ladder.rung, price=price, net_price=round(net, 2),
        expected_sales=round(sales, 2), expected_revenue=round(revenue, 1),
        capacity_remaining=capacity, multiplier=round(multiplier, 3),
        bid_price=round(bidprice.bid_price(model, lead, item.day, capacity, marginal_net,
                                         level=level, multiplier=multiplier), 1),
        optimizer=original, table=evaluated.as_dict()["table"])
    ladder.p_sellout = (model.survival(capacity / multiplier, lead, item.day, level)
                        if capacity > 0 else 1.0)
    ladder.bid["p_sellout"] = round(ladder.p_sellout, 4)
    # Evaluate the FINAL price and allocation, even when an off-ladder lock or
    # a control changed the optimizer's selection. No scenario is a probability.
    rows = []
    reference = ladder.rung_prices[ladder.reference_rung]
    minimum = _discount_minimum(params, params.ladder.rungs_beds if beds else params.ladder.rungs_rooms)
    for e in params.v4.elasticities(beds=beds):
        actual = bidprice.choose_rung(model=model, rung_prices=[*ladder.rung_prices, price],
            reference_rung=ladder.reference_rung, lead=lead, day=item.day,
            capacity_remaining=capacity, commission=params.commission,
            variable_cost=params.variable_cost_bed if beds else params.variable_cost,
            elasticity=e, level=level, price_factor=factor, fixed_capacity=private_free,
            flex_price_factor=flex_factor, contribution=lambda p: params.contribution(p, beds=beds),
            minimum_rung=minimum, selected_rung=len(ladder.rung_prices))
        eligible = actual.table[minimum:len(ladder.rung_prices)]
        optimal = max(eligible, key=lambda row: row[3])
        ref_value = actual.table[ladder.reference_rung][3]
        rows.append(dict(elasticity=e, best_price=optimal[1],
            expected_contribution=round(actual.expected_revenue, 2),
            gain_vs_reference=round(actual.expected_revenue-ref_value, 2),
            regret=round(max(0, optimal[3]-actual.expected_revenue), 2)))
    ladder.bid["uncertainty"] = dict(mode=params.v4.uncertainty_mode,
        basis="Configured stress scenarios, not confidence limits", reference_price=reference,
        rows=rows, sensitive=len({r["best_price"] for r in rows}) > 1,
        contribution_min=min(r["expected_contribution"] for r in rows),
        contribution_max=max(r["expected_contribution"] for r in rows),
        worst_gain_vs_reference=min(r["gain_vs_reference"] for r in rows),
        worst_regret=max(r["regret"] for r in rows),
        scope="Final price and fixed final capacity; alternatives are ladder prices before daily controls")


def rung_prices_for(base: float, rungs: Sequence[float], params: Params,
                    *, beds: bool) -> list:
    """Trinpriserne med gulv, loft og afrunding — samme tal som stigen bruger."""
    low, high = ((params.bed_floor, params.bed_ceiling) if beds
                 else (params.price_floor, params.price_ceiling))
    return [round_within(base * m, low, high, params.rounding) for m in rungs]


def _brake(new_price: float, current: float | None, params: Params,
           warnings: list, label: str, floor: float, ceiling: float,
           rungs: Sequence[float] | None = None) -> float:
    if not current:
        return round_within(new_price, floor, ceiling, params.rounding)
    low = max(floor, current * (1 - params.max_daily_change))
    high = min(ceiling, current * (1 + params.max_daily_change))
    if low > high:
        raise ValueError(f"{label}: prisgulv/-loft og ændringsbremse kan ikke overholdes samtidigt")
    # På stigen bremses til det nærmeste trin inden for bremsen, så prisen
    # bliver på stigen. Kun hvis intet trin ligger i vinduet, forlades den.
    allowed = [p for p in (rungs or ()) if low <= p <= high]
    if allowed:
        braked = min(allowed, key=lambda p: abs(p - new_price))
    else:
        braked = round_within(new_price, low, high, params.rounding)
    if abs(braked - new_price) > 0.01:
        warnings.append(f"Ændringsbremse på {label}: {new_price:.0f} → {braked:.0f} kr.")
    return braked


def revenue_forecast(item, params, room_prices, bed_price, forecast_rooms,
                     forecast_beds, room_capacity, bed_capacity, flex_to_private,
                     family_price=None):
    """Booked revenue stays at booked rates; only future sales use new rates.

    Remaining room sales use a capacity-weighted room-type mix. This is a
    forecast, not realized revenue or a measured revenue uplift.
    """
    inv = params.inventory
    types = dict(inv.private_room_types) or {"dobbelt_uden_bad": inv.private_rooms}
    otb = dict(item.room_type_otb)
    room_prices = dict(room_prices)
    if inv.flex_room_types:
        # Hvilke sovesale der bliver solgt samlet vides ikke på forhånd: flex
        # regnes som én pulje til den vægtede flex-pris.
        types["_flex"] = flex_to_private
        room_prices["_flex"] = (family_price if family_price is not None else
                                sum(room_prices[c] * n for c, n in inv.flex_room_types.items())
                                / max(1, inv.flex_rooms))
        otb["_flex"] = sum(otb.pop(c, 0) for c in inv.flex_room_types)
        if not otb["_flex"]:
            del otb["_flex"]
    elif flex_to_private or "familie_4" in types:
        types["familie_4"] = types.get("familie_4", 0) + flex_to_private
    if any(t not in room_prices for t in types):
        raise ValueError("Lagertype mangler prisfaktor")
    known_mix = sum(otb.values()) == item.rooms_otb
    if otb and (not known_mix or any(
            n < 0 or n > types.get(t, 0) for t, n in otb.items())):
        raise ValueError("room_type_otb skal matche solgte værelser og allokerede typer")
    remaining = {t: n - otb.get(t, 0) for t, n in types.items()}
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
    minimum_rate_certain: float = 0.0
    """Gulvet hvis prognosen holder præcist.

    Bid price-gulvet er den FORVENTEDE fortrængning: nogle gange fyldes datoen,
    nogle gange ikke, og gennemsnittet er lavere end punktprognosen antyder.
    Det er det rigtige tal i forventning. Men et gruppetilbud er bindende, og
    tabet ved at forære en udsolgt dato væk er større end gevinsten ved at tage
    en gruppe med på en halvtom. Derfor står begge tal: det ene er forventningen,
    det andet er hvad det koster hvis prognosen rammer plet. Mennesket der
    afgiver tilbuddet skal se spændet, ikke et gennemsnit af to verdener.
    """

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
    use_bid = params.v4_active and params.v4.use_for_groups
    for rec in recommendations:
        spare = max(0.0, rec.room_capacity - rec.forecast_rooms)
        displaced = max(0.0, rooms_requested - spare)
        basis = params.pricing_basis
        unit_value = (params.contribution(rec.room_price) if basis.confirmed else
                      rec.room_price * (1 - params.commission))
        if use_bid:
            # Fortrængningen er summen af bid price for hver enkelt enhed. De
            # første værelser er næsten gratis, hvis der er rigeligt tilbage;
            # de sidste er dyre. Et gennemsnit rammer ingen af delene.
            free = max(0.0, rec.room_capacity - rec.rooms_otb)
            # Dæmpningen (efterspørgsel ved den opslåede pris) hører til i
            # trinvalget, hvor en fejl koster én nat. Her hører den ikke til,
            # før elasticiteten er målt: et gruppetilbud er bindende, og et
            # gulv regnet på en udæmpet efterspørgsel er det forsigtige valg.
            multiplier = 1.0
            if params.v4.elasticity_measured or not params.v4.group_undamped_until_measured:
                multiplier = ((rec.room_ladder or {}).get("bid", {}).get("multiplier", 1.0)
                              if rec.room_ladder else 1.0)
            displaced_revenue = bidprice.displacement_cost(
                model=params.demand_models["rum"].bind(rec.rooms_otb),
                lead=rec.lead_days, day=rec.day,
                capacity_remaining=free, units=rooms_requested,
                net_price=unit_value,
                level=params.demand_level[0], multiplier=multiplier)
            displaced = displaced_revenue / max(1e-9, unit_value)
        else:
            displaced_revenue = displaced * unit_value
        expected_cost = displaced_revenue / max(1, rooms_requested)
        certain_cost = (max(0.0, rooms_requested - max(0.0, rec.room_capacity - rec.forecast_rooms))
                        * unit_value / max(1, rooms_requested))
        if basis.confirmed:
            minimum = basis.price_for_contribution(expected_cost, params.commission, params.variable_cost)
            certain = basis.price_for_contribution(certain_cost, params.commission, params.variable_cost)
        else:
            minimum = expected_cost + params.variable_cost
            certain = certain_cost + params.variable_cost
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
            minimum_rate_certain=round_within(max(certain, params.price_floor),
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

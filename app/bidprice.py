"""Bid price — den marginale værdi af den sidste ledige enhed.

Version 4 har ingen målbelægning. Den regner forventet omsætning ud
for hvert trin på stigen, over hele fordelingen af resterende efterspørgsel, og
vælger det trin der giver mest. Belægningen bliver et resultat, ikke et input.

## Regnestykket

Forventet salg ved kapacitet C og efterspørgsel D er en sum af haler:

    E[min(D, C)] = sum over x = 1..C af P(D >= x)

Det er hele grunden til at app/demand.py leverer netop P(D >= x): forventet
salg, og dermed forventet omsætning, falder direkte ud af halen. For hvert trin
r med nettopris n(r) og efterspørgselsfaktor m(r):

    omsætning(r) = n(r) x E[min(D x m(r), C)]
    bid price    = n(r) x P(D x m(r) >= C)      (værdien af den sidste enhed)

## Den ene antagelse, og hvorfor den står ét sted

m(r) er hvor meget efterspørgslen flytter sig med prisen:

    m(r) = (pris(r) / pris(reference)) ^ (-elasticitet)

Elasticiteten er ikke målt. Den er det eneste ukendte tal tilbage i
prisbeslutningen, og det er med vilje: ældre motorer havde flere triggervægte,
målbelægninger og trinafstande, som alle var gæt. Her er der ét, det står i
config, og det er præcis det tal `pricing.v4.exploration` begynder at måle fra
første dag. Indtil det er målt, er standardværdierne branchelitteratur og ikke
jeres gæster.

Bid price samler tre beslutninger i samme marginale regnestykke:

**Flex-allokering.** Et flex-rum solgt privat lægger beslag på otte
sengepladser. Sælg det som rum, hvis værdien af den ekstra rumkapacitet
overstiger summen af de otte sengeenheder der ryger — en aftagende sum, fordi
den ottende seng er mindre værd end den første. Det nuværende RevPAB-check
sammenligner gennemsnit og fanger ikke den aftagende del.

**Gruppeforskydning.** Minimumsprisen på en blok er fortrængningsomkostningen:
summen af bid price for hver enhed over hver dato.

**Opholdslængde.** Værdien af et treniters ophold er summen af bid price over
de tre nætter. En enkelt lørdag der blokerer tre nætter afvises af sig selv,
når lørdagens bid price er høj og søndagens er lav. Minimumsophold holder op
med at være en regel nogen skal vedligeholde.

Ren beregning: ingen database, intet netværk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from .demand import DemandModel

DEFAULT_ELASTICITY_ROOMS = 1.6
DEFAULT_ELASTICITY_BEDS = 2.0
"""Egenpriselasticitet, ikke målt på egne gæster.

Et enkelt hotels egenpriselasticitet ligger typisk over 1 — gæsten kan vælge
nabohuset — og sengesalget er mere prisfølsomt end værelsessalget, fordi
backpackere sammenligner på pris og familier på produkt. Tallene her er
litteraturniveau og skal erstattes af målinger fra eksplorationen. Sættes de
til 0, bliver den højeste pris altid den bedste, og modellen mister sin bremse.
"""


@dataclass(frozen=True)
class V4Config:
    """pricing.v4 i config.yaml. Projektet kører kun v4 i drift."""
    enabled: bool = True
    model_path: str = "config/demand_model.json"
    level_path: str = "data/demand_level.json"
    elasticity_rooms: float = DEFAULT_ELASTICITY_ROOMS
    elasticity_beds: float = DEFAULT_ELASTICITY_BEDS
    level_alpha: float = 0.07
    level_min: float = 0.6
    level_max: float = 1.6
    use_for_flex: bool = True
    use_for_groups: bool = True

    # Så længe elasticiteten er et litteraturtal og ikke en måling, må version 4
    # ikke rabattere dybere end ét trin under reference. Grunden er ikke forsigtighed
    # for forsigtighedens skyld: med en konstant elasticitet over 1 og en lav
    # variabel omkostning ligger det ubegrænsede optimum under prisgulvet, så
    # modellen vil stå på nederste trin på enhver dato hvor kapaciteten ikke
    # binder. Det er en ekstrapolation langt væk fra de priser der er observeret,
    # og den rammer samtidig den svageste delscore, værdi for pengene (7,1).
    # Sæt elasticity_measured: true når eksplorationen har målt den, så løftes
    # loftet og modellen får lov at gå hele vejen ned.
    elasticity_measured: bool = False
    max_discount_rungs: int = 1

    # Gruppegulvet regnes uden elasticitetsdæmpning, så længe elasticiteten er
    # et litteraturtal. Fortrængningen afhænger af hvor mange værelser der
    # ellers ville være solgt, og dæmper man efterspørgslen med en antagelse
    # ingen har målt, bliver gulvet lavere end det burde være. Et for højt gulv
    # koster en forespørgsel; et for lavt gulv er bindende i et år.
    group_undamped_until_measured: bool = True

    # Mindste forskel i kroner før et flex-rum anbefales flyttet. Uden den
    # flytter modellen sovesale til private værelser på en død december-søndag,
    # fordi to en halv krone er mere end nul. Regnestykket har ret og
    # anbefalingen er stadig forkert: den koster personalet tid, og en
    # anbefaling man lærer at ignorere er værre end ingen anbefaling.
    flex_min_gain: float = 25.0

    def __post_init__(self):
        if min(self.elasticity_rooms, self.elasticity_beds) < 0:
            raise ValueError("Elasticitet kan ikke være negativ")
        if not 0 < self.level_min <= 1 <= self.level_max:
            raise ValueError("Niveaugrænserne skal omslutte 1,0")


def demand_multiplier(price: float, reference_price: float, elasticity: float) -> float:
    """Hvor meget efterspørgslen flytter sig, når prisen flytter sig."""
    if reference_price <= 0 or price <= 0:
        return 1.0
    return (price / reference_price) ** (-elasticity)


def expected_sales(model: DemandModel, lead: int, day: date, capacity: float,
                   *, level: float = 1.0, multiplier: float = 1.0,
                   cap: int = 400) -> float:
    """E[min(efterspørgsel, kapacitet)] = summen af halerne P(D >= x).

    Kapaciteten afrundes op til hele enheder; et halvt værelse kan ikke sælges.
    """
    units = int(min(cap, max(0.0, capacity)) + 0.999)
    if units <= 0 or multiplier <= 0:
        return 0.0
    total = 0.0
    for x in range(1, units + 1):
        total += model.survival(x / multiplier, lead, day, level)
    return total


def bid_price(model: DemandModel, lead: int, day: date, capacity: float,
              net_price: float, *, level: float = 1.0,
              multiplier: float = 1.0) -> float:
    """Værdien af at beholde den sidste ledige enhed i stedet for at sælge den.

    Ved nul ledig kapacitet er værdien af en enhed mere ikke defineret som en
    alternativomkostning — der er intet at fortrænge — og funktionen svarer med
    nettoprisen, som er det en ekstra enhed ville indbringe.
    """
    if capacity <= 0:
        return max(0.0, net_price)
    if multiplier <= 0:
        return 0.0
    units = int(max(1.0, capacity) + 0.999)
    return max(0.0, net_price) * model.survival(units / multiplier, lead, day, level)


# --------------------------------------------------------------------------
# Trinvalg
# --------------------------------------------------------------------------

@dataclass
class RungChoice:
    """Resultatet af at regne hele stigen igennem på én dato."""
    rung: int
    price: float
    net_price: float
    expected_sales: float
    expected_revenue: float
    bid_price: float
    p_sellout: float
    capacity_remaining: float
    elasticity: float
    multiplier: float = 1.0
    table: list = field(default_factory=list)   # (trin, pris, salg, omsætning)
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rung": self.rung, "price": self.price, "net_price": round(self.net_price, 2),
            "expected_sales": round(self.expected_sales, 2),
            "expected_revenue": round(self.expected_revenue, 1),
            "bid_price": round(self.bid_price, 1),
            "p_sellout": round(self.p_sellout, 4),
            "capacity_remaining": round(self.capacity_remaining, 2),
            "elasticity": self.elasticity, "multiplier": round(self.multiplier, 3),
            "table": [(r, p, round(s, 2), round(v, 1)) for r, p, s, v in self.table],
            "reasons": list(self.reasons),
        }


def choose_rung(*, model: DemandModel, rung_prices: Sequence[float],
                reference_rung: int, lead: int, day: date,
                capacity_remaining: float, commission: float,
                variable_cost: float, elasticity: float,
                level: float = 1.0) -> RungChoice:
    """Vælg det trin der maksimerer forventet omsætning over fordelingen.

    Ingen målbelægning indgår. Trinnet falder ud af to ting: hvor meget
    efterspørgsel der er tilbage, og hvor hurtigt den forsvinder når prisen
    stiger.
    """
    reference_price = rung_prices[reference_rung]
    table, best = [], None
    for index, price in enumerate(rung_prices):
        net = price * (1 - commission) - variable_cost
        multiplier = demand_multiplier(price, reference_price, elasticity)
        sales = expected_sales(model, lead, day, capacity_remaining,
                               level=level, multiplier=multiplier)
        revenue = net * sales
        table.append((index, price, sales, revenue))
        if best is None or revenue > best[3] + 1e-9:
            best = (index, price, sales, revenue)

    index, price, sales, revenue = best
    net = price * (1 - commission) - variable_cost
    multiplier = demand_multiplier(price, reference_price, elasticity)
    bp = bid_price(model, lead, day, capacity_remaining, net,
                   level=level, multiplier=multiplier)
    p_sellout = (model.survival(max(1.0, capacity_remaining) / multiplier, lead, day, level)
                 if capacity_remaining > 0 else 1.0)

    reasons = []
    if index == len(rung_prices) - 1:
        reasons.append("Øverste trin: efterspørgslen bærer mere end stigen rækker til")
    if index == 0:
        reasons.append("Nederste trin: forventet omsætning falder på alle højere trin")
    if capacity_remaining <= 0:
        reasons.append("Ingen ledig kapacitet: prisen er et tilbud om overbooking")
    return RungChoice(
        rung=index, price=price, net_price=net, expected_sales=sales,
        expected_revenue=revenue, bid_price=bp, p_sellout=p_sellout,
        capacity_remaining=capacity_remaining, elasticity=elasticity,
        multiplier=multiplier, table=table, reasons=reasons,
    )


# --------------------------------------------------------------------------
# Flex-allokering
# --------------------------------------------------------------------------

def flex_allocation(*, room_model: DemandModel, bed_model: DemandModel,
                    lead: int, day: date,
                    rooms_free: float, beds_free: float,
                    flex_rooms: int, beds_per_flex_room: int,
                    room_net: float, bed_net: float,
                    lower: int = 0, upper: int | None = None,
                    room_level: float = 1.0, bed_level: float = 1.0,
                    room_multiplier: float = 1.0,
                    bed_multiplier: float = 1.0,
                    min_gain: float = 0.0) -> tuple:
    """Hvor mange flex-rum skal sælges som private værelser?

    Marginal og grådig: begge sider er aftagende i antal, så det er nok at
    lægge et rum til så længe det tjener mere end de senge det koster.
    `rooms_free` og `beds_free` er ledig kapacitet ved nul flex til private.
    """
    upper = flex_rooms if upper is None else min(upper, flex_rooms)
    lower = max(0, min(lower, upper))
    steps = []
    n = lower
    rooms = rooms_free + lower
    beds = beds_free - lower * beds_per_flex_room
    while n < upper:
        gain = (max(0.0, room_net) *
                room_model.survival((rooms + 1) / max(room_multiplier, 1e-9),
                                    lead, day, room_level))
        loss = 0.0
        for j in range(beds_per_flex_room):
            unit = beds - j
            if unit < 1:
                break
            loss += (max(0.0, bed_net) *
                     bed_model.survival(unit / max(bed_multiplier, 1e-9), lead, day, bed_level))
        steps.append((n + 1, round(gain, 2), round(loss, 2)))
        if gain <= loss + max(min_gain, 1e-9):
            # Uafgjort går til sengene, og en forskel under min_gain tæller som
            # uafgjort. Langt ude er begge sider nær nul — ingen af lagrene er
            # knappe — og uden den regel ville modellen lave sovesale om til
            # private værelser på en stille mandag, fordi to en halv krone er
            # mere end nul.
            break
        n += 1
        rooms += 1
        beds -= beds_per_flex_room
    return n, steps


# --------------------------------------------------------------------------
# Grupper og ophold
# --------------------------------------------------------------------------

def displacement_cost(*, model: DemandModel, lead: int, day: date,
                      capacity_remaining: float, units: int, net_price: float,
                      level: float = 1.0, multiplier: float = 1.0) -> float:
    """Hvad koster det at tage `units` enheder ud af markedet på én dato?

    Summen af bid price for hver enkelt enhed. De første enheder er næsten
    gratis, hvis der er rigeligt tilbage; de sidste er dyre. Et gennemsnit
    rammer ingen af delene.
    """
    total = 0.0
    free = max(0.0, capacity_remaining)
    for i in range(max(0, units)):
        total += bid_price(model, lead, day, max(0.0, free - i), net_price,
                           level=level, multiplier=multiplier)
    return total


def group_floor(*, model: DemandModel, days: Sequence[tuple], rooms_requested: int,
                net_price_by_day: dict, variable_cost: float,
                level: float = 1.0, multiplier: float = 1.0) -> dict:
    """Gulvet under et gruppetilbud: fortrængning plus variable omkostninger.

    `days` er (dato, lead, ledig kapacitet). Resultatet er et gulv pr. værelse
    pr. nat, ikke et tilbud. Afgivne gruppetilbud er bindende.
    """
    per_day, total = [], 0.0
    for day, lead, free in days:
        net = net_price_by_day[day]
        cost = displacement_cost(model=model, lead=lead, day=day,
                                 capacity_remaining=free, units=rooms_requested,
                                 net_price=net, level=level, multiplier=multiplier)
        per_day.append({"dato": day.isoformat(), "fortraengning": round(cost, 1)})
        total += cost
    nights = max(1, len(days))
    floor = total / (nights * max(1, rooms_requested)) + variable_cost
    return {"gulv_pr_rum_pr_nat": round(floor, 1),
            "fortraengning_i_alt": round(total, 1),
            "pr_dato": per_day}


def stay_value(*, model: DemandModel, days: Sequence[tuple], net_price_by_day: dict,
               level: float = 1.0, multiplier: float = 1.0) -> float:
    """Værdien af at give ét ophold alle nætterne: summen af nætternes bid price.

    Et tilbud på et ophold accepteres, når den samlede nettopris overstiger
    denne sum. Det er den samme regel som minimumsophold forsøger at ramme med
    en tommelfingerregel.
    """
    return sum(
        bid_price(model, lead, day, free, net_price_by_day[day],
                  level=level, multiplier=multiplier)
        for day, lead, free in days
    )

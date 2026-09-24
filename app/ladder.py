"""Prisstigen — trinbaseret prissætning oven på prognosen.

Den kontinuerlige faktormodel flytter prisen en smule hver dag. Det er præcist
på papiret, men i praksis giver det uro: 612, 598, 634, 621 kr. Gæsten ser en
pris der vibrerer, OTA'erne ser ustabilitet, og personalet kan ikke forklare
hvorfor.

Stigen gør det omvendte. Hver dato har et fast sæt prisniveauer — trin — fra
et gulv til en top, forankret i dagens grundpris (sæson x ugedag). Prisen står
på ét trin og flytter sig kun, når triggerne samlet siger det tydeligt nok.

    Trin = f(prognose mod mål, bookingtempo, markedsposition, lead time)
           + eventtrin
           + knaphedsbeskyttelse
           med hysterese og trinbegrænsning

Fem ting gør den mere end en simpel belægningstrappe:

1. **Den trigger på prognose, ikke på belægning i dag.** En dato 40 dage ude
   med 20 % solgt er ikke svag, hvis den er på vej til 78 %.

2. **Den måler tempo.** Hvor mange der er booket de sidste syv dage mod hvor
   mange kurven forventede i samme vindue. Det er det tidligste signal der
   findes — det kommer før prognosen flytter sig.

3. **Den beskytter kapacitet på samme måde som flyselskaber.** Prognosen har
   en usikkerhed der vokser med lead time. Er sandsynligheden for udsolgt høj,
   lukkes de nederste trin (Littlewoods regel i forenklet form): en seng solgt
   billigt i dag er en seng der ikke kan sælges dyrt i morgen.

4. **Den er træg med vilje.** Hysterese kræver at trykket er tydeligt over
   grænsen før stigen skifter trin, så prisen ikke hopper frem og tilbage om
   den samme grænse. Den går op hurtigere end den går ned (op to, ned ét), og
   den sænker aldrig prisen på en dato der er på vej mod målet.

5. **Den forklarer sig selv.** Hvert trinskifte har en begrundelse på dansk,
   og hvert triggertryk står i resultatet.

Ren beregning: ingen database, intet netværk, ingen sideeffekter.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Sequence


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _normal_sf(z: float) -> float:
    """P(Z > z) for en standardnormalfordeling."""
    return 0.5 * math.erfc(z / math.sqrt(2))


@dataclass(frozen=True)
class LadderConfig:
    # Trinene som faktor på dagens grundpris. Det trin der er 1,00 er
    # referencetrinnet: der hvor prisen står når alle triggere er neutrale.
    # Afstanden vokser opad (7 % -> 11 %), fordi et hostel har mere at hente
    # på toppen end at tabe i bunden.
    rungs_rooms: tuple = (0.80, 0.86, 0.93, 1.00, 1.07, 1.15, 1.24, 1.35, 1.48)
    rungs_beds: tuple = (0.80, 0.86, 0.93, 1.00, 1.07, 1.15, 1.24, 1.35, 1.48)

    # Triggernes vægt. Summen bør være 1, så det samlede tryk ligger i [-1; 1].
    w_forecast: float = 0.40
    w_pace: float = 0.25
    w_market: float = 0.20
    w_lead: float = 0.15

    # Hvad giver fuldt tryk (+/-1) på hver trigger?
    forecast_scale: float = 0.20   # prognose 20 pp over/under målet
    pace_z_full: float = 2.5       # tempo 2,5 standardafvigelser fra forventet = fuldt tryk
    market_scale: float = 0.20     # markedet 20 % over/under vores referencetrin

    score_per_rung: float = 0.15   # samlet tryk pr. trin
    hysteresis: float = 0.25       # ekstra trin-brøkdel ud over 0,5 før et skift
    max_up: int = 2                # trin op pr. kørsel
    max_down: int = 1              # trin ned pr. kørsel
    max_down_close_in: int = 2     # trin ned pr. kørsel inden for close_in_days

    close_in_days: int = 3         # sidste-øjebliks-vindue
    far_out_days: int = 60         # herudover ingen rabat under referencetrin - 1
    far_out_min_offset: int = -1

    # Knaphedsbeskyttelse: (sandsynlighed for udsolgt, mindste trin over reference)
    sellout_protect: tuple = ((0.25, 1), (0.50, 2), (0.75, 3))
    forecast_cv_near: float = 0.10  # prognoseusikkerhed ved ankomst
    forecast_cv_far: float = 0.35   # prognoseusikkerhed 60+ dage ude

    event_rung_step: float = 0.10  # 10 % eventtillæg = ét trin
    ratchet: bool = True           # aldrig ned på en dato der er på vej mod målet
    smoothing: float = 0.5         # vægt på dagens tryk; resten er gårsdagens (EWMA)

    def __post_init__(self):
        for name in ("rungs_rooms", "rungs_beds"):
            rungs = getattr(self, name)
            if not 5 <= len(rungs) <= 12:
                raise ValueError(f"{name}: brug 5-12 trin")
            if any(b <= a for a, b in zip(rungs, rungs[1:])):
                raise ValueError(f"{name}: trinene skal stige")
            if 1.0 not in rungs:
                raise ValueError(f"{name}: ét trin skal være 1,00 (referencetrinnet)")
        if self.score_per_rung <= 0 or min(self.max_up, self.max_down) < 0:
            raise ValueError("Ugyldig trinopsætning")
        if abs(self.w_forecast + self.w_pace + self.w_market + self.w_lead - 1.0) > 0.01:
            raise ValueError("Triggervægtene skal summere til 1")


@dataclass
class LadderResult:
    rung: int                 # 0-baseret
    n_rungs: int
    reference_rung: int
    price: float
    rung_prices: list
    score: float              # samlet tryk, [-1; 1]
    position: float           # kontinuerlig trinposition før hysterese
    smoothed_position: float  # udglattet position uden eventtrin — gemmes til i morgen
    triggers: dict            # hver triggers tryk, [-1; 1]
    p_sellout: float
    min_rung: int
    previous_rung: int | None
    reasons: list = field(default_factory=list)
    off_ladder: bool = False  # sand hvis bremse/lås gav en pris mellem trinene

    @property
    def label(self) -> str:
        return f"{self.rung + 1}/{self.n_rungs}"

    def as_dict(self) -> dict:
        out = asdict(self)
        out["label"] = self.label
        return out


def rung_prices(base: float, rungs: Sequence[float], rounder) -> list:
    """Prisen på hvert trin. `rounder` håndterer gulv, loft og afrunding."""
    return [rounder(base * m) for m in rungs]


def forecast_cv(lead_days: int, cfg: LadderConfig) -> float:
    share = min(1.0, max(0, lead_days) / 60)
    return cfg.forecast_cv_near + (cfg.forecast_cv_far - cfg.forecast_cv_near) * share


def sellout_probability(forecast_units: float, capacity: float, lead_days: int,
                        cfg: LadderConfig) -> float:
    """Sandsynligheden for at efterspørgslen når kapaciteten.

    Prognosen behandles som middelværdien af en normalfordeling, hvis spredning
    vokser med lead time. Tæt på ankomst ved vi meget; tre måneder ude ved vi
    lidt, og så skal der mere til før trinene lukkes.
    """
    if capacity <= 0:
        return 1.0
    if forecast_units <= 0:
        return 0.0
    sigma = max(1.0, forecast_cv(lead_days, cfg) * forecast_units)
    return _normal_sf((capacity - 0.5 - forecast_units) / sigma)


def ladder_step(*, base: float, rungs: Sequence[float], rounder, cfg: LadderConfig,
                forecast: float, target: float, occ_now: float, lead_days: int,
                pickup: float | None, expected_pickup: float | None,
                comp_price: float | None, quality_index: float,
                event_factor: float, capacity: float, forecast_units: float,
                previous_rung: int | None,
                previous_position: float | None = None) -> LadderResult:
    """Vælg trin for ét produkt på én dato."""
    n = len(rungs)
    ref = list(rungs).index(1.0)
    prices = rung_prices(base, rungs, rounder)
    reasons: list = []

    # -- triggere --------------------------------------------------------
    t_forecast = _clip((forecast - target) / cfg.forecast_scale)

    # Tempo som signifikans, ikke som rå forskel. Bookinger ankommer som en
    # Poisson-proces, så forventet X bookinger på en uge har en naturlig
    # spredning på kvadratroden af X. To bookinger for meget på en stille
    # tirsdag er støj; tyve for meget på en lørdag er et signal.
    if pickup is None or expected_pickup is None or capacity <= 0:
        t_pace = 0.0
    else:
        actual_units = pickup * capacity
        expected_units = expected_pickup * capacity
        # Gulv på 4 forventede: under det er Poisson-tal så små, at én
        # booking mere eller mindre ikke siger noget om efterspørgslen.
        z = (actual_units - expected_units) / math.sqrt(max(4.0, expected_units))
        t_pace = _clip(z / cfg.pace_z_full)

    if comp_price and base > 0:
        t_market = _clip((comp_price * quality_index / base - 1.0) / cfg.market_scale)
    else:
        t_market = 0.0

    # Lead time: tæt på ankomst forstærkes signalet — ledige senge den sidste
    # nat er værdiløse i morgen, og en næsten fuld nat kan bære en
    # sidste-øjebliks-pris. Langt ude er der intet lead-signal.
    t_lead = t_forecast if 0 <= lead_days <= cfg.close_in_days else 0.0

    score = (cfg.w_forecast * t_forecast + cfg.w_pace * t_pace +
             cfg.w_market * t_market + cfg.w_lead * t_lead)
    event_rungs = round(max(0.0, event_factor - 1.0) / cfg.event_rung_step)
    raw_position = ref + score / cfg.score_per_rung
    # Udglatning: én dags tryk kan ikke alene vælte et trin. Tæt på ankomst er
    # der ikke tid til at vente, så der bruges dagens tryk alene.
    if previous_position is not None and lead_days > cfg.close_in_days:
        raw_position = (cfg.smoothing * raw_position +
                        (1 - cfg.smoothing) * previous_position)
    smoothed = raw_position
    position = raw_position + event_rungs
    triggers = {"prognose": round(t_forecast, 3), "tempo": round(t_pace, 3),
                "marked": round(t_market, 3), "lead": round(t_lead, 3),
                "event_trin": event_rungs}

    # -- hysterese og trinbegrænsning -------------------------------------
    target_rung = int(round(position))
    if previous_rung is None or not 0 <= previous_rung < n:
        rung = target_rung
        previous_rung = None
    else:
        rung = previous_rung
        band = 0.5 + cfg.hysteresis
        max_down = cfg.max_down_close_in if 0 <= lead_days <= cfg.close_in_days else cfg.max_down
        if position >= previous_rung + band:
            rung = min(target_rung, previous_rung + cfg.max_up)
        elif position <= previous_rung - band:
            rung = max(target_rung, previous_rung - max_down)
            if cfg.ratchet and forecast >= target and rung < previous_rung:
                rung = previous_rung
                reasons.append("Holdt: prognosen er på vej mod målet, prisen sænkes ikke")

    # -- gulve på stigen ----------------------------------------------------
    min_rung = 0
    if lead_days >= cfg.far_out_days:
        min_rung = max(min_rung, ref + cfg.far_out_min_offset)
    if event_rungs:
        min_rung = max(min_rung, ref)
    p_sellout = sellout_probability(forecast_units, capacity, lead_days, cfg)
    for threshold, offset in cfg.sellout_protect:
        if p_sellout >= threshold:
            min_rung = max(min_rung, ref + offset)
    min_rung = min(min_rung, n - 1)
    rung = max(0, min(n - 1, rung))
    if rung < min_rung:
        if p_sellout >= cfg.sellout_protect[0][0] and min_rung > ref:
            reasons.append(f"Knaphed: {p_sellout:.0%} risiko for udsolgt lukker de nederste trin")
        elif event_rungs:
            reasons.append("Event: ikke under referencetrinnet")
        elif lead_days >= cfg.far_out_days:
            reasons.append(f"Over {cfg.far_out_days} dage ude: ingen dyb rabat endnu")
        rung = min_rung

    # -- begrundelse ------------------------------------------------------
    if previous_rung is not None and rung != previous_rung:
        direction = "op" if rung > previous_rung else "ned"
        sign = 1 if rung > previous_rung else -1
        pushes = {k: sign * triggers[k] * getattr(cfg, f"w_{_W[k]}")
                  for k in ("prognose", "tempo", "marked", "lead")}
        strongest = max(pushes, key=pushes.get)
        if pushes[strongest] > 0:
            why = f"drevet mest af {strongest} ({triggers[strongest]:+.2f})"
        elif event_rungs and sign > 0:
            why = "drevet af event"
        else:
            why = "knaphed/gulv på stigen"
        reasons.insert(0, f"Trin {direction} {previous_rung + 1} → {rung + 1}, {why}")
    elif previous_rung is not None and abs(position - previous_rung) >= 0.5 and not reasons:
        reasons.append("Holdt: trykket er ikke tydeligt nok til et trinskifte (hysterese)")
    if pickup is None:
        reasons.append("Intet tempo-signal endnu: kræver en kørsel fra ca. en uge siden")

    return LadderResult(
        rung=rung, n_rungs=n, reference_rung=ref, price=prices[rung],
        rung_prices=prices, score=round(score, 4), position=round(position, 3),
        smoothed_position=round(smoothed, 4),
        triggers=triggers, p_sellout=round(p_sellout, 4), min_rung=min_rung,
        previous_rung=previous_rung, reasons=reasons,
    )


_W = {"prognose": "forecast", "tempo": "pace", "marked": "market", "lead": "lead"}

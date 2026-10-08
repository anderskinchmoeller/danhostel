"""Fordelingen af resterende efterspørgsel — ikke kun dens middelværdi.

V4 bruger hele fordelingen i stedet for én forventet slutbelægning. Det er den
information, prisbeslutningen har brug for: en dato hvor prognosen er 60 % ± 5
skal ikke prissættes som en hvor den er 60 % ± 25.

Modulet her estimerer hele fordelingen af *netto pickup* — hvor mange enheder
der endnu kommer ind, efter annulleringer — ud fra bookingkuben, og svarer på
det ene spørgsmål bid price har brug for:

    P(resterende efterspørgsel >= x)

## Hvorfor ikke Poisson

Stigens tempo-trigger bruger en Poisson-z-score, som antager at variansen er
lig middelværdien. På egne data er det ikke i nærheden: 30 dage ude er
middelværdien for rum ca. 21 enheder og spredningen ca. 14 — altså en varians
omkring ni gange middelværdien. Grunden er grupper: sovesalene sælges samlet,
og en enkelt skoleklasse flytter tyve enheder på én dag. En Poisson-z på de
data slår ud på støj.

## Formen i stedet for en formel

I stedet for at vælge en fordelingsfamilie bruges den empiriske:

    netto pickup  ~  middelværdi(lead, ugedag) x sæson(måned) x niveau x form

hvor `form` er den samlede fordeling af observerede forhold mellem faktisk og
forventet pickup, poolet pr. lead-interval. Den rummer gruppespring, skæve
haler og negativ pickup (annulleringer der overstiger nysalg — 2 % af
observationerne) uden at nogen af delene skal antages væk.

`niveau` er den adaptive faktor fra app/level.py: den fanger strukturelle skift
som efteråret 2025, hvor efterspørgslen faldt 25-35 % uden at nogen ved hvorfor.

Ren beregning: ingen database, intet netværk.
"""

from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

LEAD_BUCKETS = ((0, 1), (2, 3), (4, 6), (7, 10), (11, 17), (18, 30),
                (31, 45), (46, 70), (71, 365))
"""Form-fordelingen pooles over disse lead-intervaller.

Middelværdien estimeres pr. lead og ugedag (ca. 100 observationer pr. celle på
to års historik). Formen kræver flere observationer end det, og den ændrer sig
langsommere end middelværdien, så den pooles.
"""

QUANTILE_STEPS = 200
"""Formen gemmes som 201 kvantiler, ikke som alle observationer. Det er præcist
nok til halen og gør modelfilen læsbar."""

MIN_MEAN_FOR_SHAPE = 2.0
"""Celler med lavere forventet pickup end dette bidrager ikke til formen: et
forhold mellem faktisk og forventet på 0,4 enheder siger ingenting."""


def _bucket(lead: int) -> int:
    for index, (low, high) in enumerate(LEAD_BUCKETS):
        if low <= lead <= high:
            return index
    return len(LEAD_BUCKETS) - 1


@dataclass
class Shape:
    """Den empiriske formfordeling som kvantiler, q = 0,000 … 1,000."""
    quantiles: list
    n: int

    def survival(self, ratio: float) -> float:
        """P(form >= ratio) ved lineær interpolation mellem kvantilerne."""
        q = self.quantiles
        if not q:
            return 1.0 if ratio <= 1.0 else 0.0
        if ratio <= q[0]:
            return 1.0
        if ratio > q[-1]:
            # Over den højeste observation: aftagende hale, aldrig nul.
            # Nul ville sige "kan ikke ske", og det ved to års data ikke.
            top = max(q[-1], 1e-9)
            return max(1e-4, (1.0 / len(q)) * math.exp(-(ratio - top) / max(top * 0.25, 1e-9)))
        index = bisect_left(q, ratio)
        lo, hi = q[index - 1], q[index]
        within = 0.0 if hi <= lo else (ratio - lo) / (hi - lo)
        position = (index - 1 + within) / (len(q) - 1)
        return max(0.0, min(1.0, 1.0 - position))

    def quantile(self, p: float) -> float:
        if not self.quantiles:
            return 1.0
        p = min(1.0, max(0.0, p))
        position = p * (len(self.quantiles) - 1)
        low = int(position)
        high = min(low + 1, len(self.quantiles) - 1)
        return (self.quantiles[low] +
                (position - low) * (self.quantiles[high] - self.quantiles[low]))


@dataclass
class BoundDemand:
    """En efterspørgselsmodel bundet til en konkret belægning på bøgerne.

    Krympningen (se `DemandModel.shrink`) afhænger af hvor meget der allerede
    står på bøgerne, så bid price-beregningen skal kende tallet. I stedet for at
    føre det gennem hver eneste funktionssignatur bindes det én gang.
    """
    model: "DemandModel"
    otb: float

    def expected(self, lead: int, day: date, level: float = 1.0) -> float:
        return self.model.expected(lead, day, level, otb=self.otb)

    def survival(self, units: float, lead: int, day: date, level: float = 1.0) -> float:
        return self.model.survival(units, lead, day, level, otb=self.otb)

    def quantile(self, p: float, lead: int, day: date, level: float = 1.0) -> float:
        return self.model.quantile(p, lead, day, level, otb=self.otb)


@dataclass
class DemandModel:
    """Fordelingen af netto pickup for ét lager (rum eller senge)."""
    kind: str
    mean: dict = field(default_factory=dict)      # "lead|ugedag" -> enheder
    season: dict = field(default_factory=dict)    # måned -> faktor
    shape: list = field(default_factory=list)     # pr. lead-interval
    var_over_mean: dict = field(default_factory=dict)
    seasonal_final: dict = field(default_factory=dict)  # "ugedag|måned" -> enheder
    shrink: dict = field(default_factory=dict)          # lead -> vægt på bøgerne
    n_dates: int = 0
    note: str = ""

    # -- opslag ----------------------------------------------------------
    def bind(self, otb: float) -> BoundDemand:
        return BoundDemand(self, float(otb))

    def _raw_expected(self, lead: int, day: date, level: float = 1.0) -> float:
        lead = max(0, lead)
        key = f"{min(lead, 365)}|{day.weekday()}"
        base = self.mean.get(key)
        if base is None:
            base = self._nearest_mean(lead, day.weekday())
        return max(0.0, base * self.season.get(str(day.month), 1.0) * level)

    def shrink_weight(self, lead: int) -> float:
        """Hvor meget bøgerne vejer mod kalenderen ved dette lead time.

        1,0 = stol fuldt på bøgerne plus den forventede pickup. 0,0 = se bort
        fra bøgerne og brug sæsongennemsnittet for ugedagen i måneden.

        Vægten er ikke valgt, den er estimeret: for hvert lead time den vægt der
        ville have ramt bedst på historikken. Målingen viser at bøgerne 60+ dage
        ude tilfører støj frem for information — 43 % af alle reservationslinjer
        ender annulleret, og en dato der ser stærk ud fordi en skoleklasse har
        booket tre måneder frem, er ikke stærk.
        """
        if not self.shrink:
            return 1.0
        key = str(max(0, lead))
        if key in self.shrink:
            return self.shrink[key]
        nearest = min(self.shrink, key=lambda k: abs(int(k) - lead))
        return self.shrink[nearest]

    def expected(self, lead: int, day: date, level: float = 1.0,
                 otb: float | None = None) -> float:
        """Forventet netto pickup fra nu til ankomst.

        Uden `otb` er det den rå forventning. Med `otb` krympes prognosen for
        slutbelægningen mod sæsongennemsnittet, og den resterende pickup er
        forskellen mellem den krympede prognose og det der allerede står.
        """
        raw = self._raw_expected(lead, day, level)
        if otb is None or not self.shrink:
            return raw
        seasonal = self.seasonal_final.get(f"{day.weekday()}|{day.month}")
        if seasonal is None:
            return raw
        weight = self.shrink_weight(lead)
        final = weight * (otb + raw) + (1 - weight) * seasonal * level
        return max(0.0, final - otb)

    def _nearest_mean(self, lead: int, weekday: int) -> float:
        candidates = [(abs(int(k.split("|")[0]) - lead), v)
                      for k, v in self.mean.items() if k.endswith(f"|{weekday}")]
        if not candidates:
            return 0.0
        return min(candidates)[1]

    def survival(self, units: float, lead: int, day: date, level: float = 1.0,
                 otb: float | None = None) -> float:
        """P(resterende efterspørgsel >= units)."""
        if units <= 0:
            return 1.0
        mu = self.expected(lead, day, level, otb=otb)
        if mu <= 0:
            return 0.0
        return self._shape(lead).survival(units / mu)

    def quantile(self, p: float, lead: int, day: date, level: float = 1.0,
                 otb: float | None = None) -> float:
        return self._shape(lead).quantile(p) * self.expected(lead, day, level, otb=otb)

    def _shape(self, lead: int) -> Shape:
        if not self.shape:
            return Shape([], 0)
        return self.shape[min(_bucket(lead), len(self.shape) - 1)]

    # -- serialisering ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "mean": self.mean, "season": self.season,
            "shape": [{"quantiles": [round(v, 5) for v in s.quantiles], "n": s.n}
                      for s in self.shape],
            "var_over_mean": self.var_over_mean,
            "seasonal_final": self.seasonal_final, "shrink": self.shrink,
            "n_dates": self.n_dates, "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "DemandModel":
        return cls(
            kind=raw["kind"], mean=raw["mean"], season=raw["season"],
            shape=[Shape(list(s["quantiles"]), int(s["n"])) for s in raw["shape"]],
            var_over_mean=raw.get("var_over_mean", {}),
            seasonal_final=raw.get("seasonal_final", {}),
            shrink=raw.get("shrink", {}),
            n_dates=int(raw.get("n_dates", 0)), note=raw.get("note", ""),
        )


# --------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------

def _quantiles(values: Sequence[float], steps: int = QUANTILE_STEPS) -> list:
    data = sorted(values)
    if not data:
        return []
    return [data[min(len(data) - 1, int(round(i / steps * (len(data) - 1))))]
            for i in range(steps + 1)]


def fit(rows: Sequence[dict], *, kind: str, column: str,
        final_column: str, drop_censored: bool = True,
        exclude_ranges: Sequence[tuple] = ()) -> DemandModel:
    """Estimér fordelingen af netto pickup fra bookingkuben.

    `exclude_ranges` findes for renoveringen 1. januar til 7. marts 2025, hvor
    en tredjedel af huset var ude af drift. Tages den periode med, lærer
    modellen en efterspørgsel der aldrig manglede — den var bare lukket inde.
    Perioden udelades som datointerval, ikke som måneder: januar 2024 var en
    normal januar og skal blive i grundlaget.
    """
    def excluded(day: str) -> bool:
        return any(low <= day <= high for low, high in exclude_ranges)

    usable = [r for r in rows
              if not (drop_censored and r["censureret"]) and not excluded(r["dato"])]
    if not usable:
        raise ValueError("Ingen brugbare rækker i kuben")

    # 1. Sæson: gennemsnitlig slutbelægning pr. måned mod årsgennemsnittet.
    by_date = {}
    for row in usable:
        by_date.setdefault(row["dato"], row if row["lead"] == 0 else by_date.get(row["dato"]))
    finals = {}
    for row in usable:
        if row["lead"] == 0:
            finals[row["dato"]] = row[final_column]
    overall = sum(finals.values()) / max(1, len(finals))
    month_sum, month_n = {}, {}
    for day, value in finals.items():
        month = date.fromisoformat(day).month
        month_sum[month] = month_sum.get(month, 0.0) + value
        month_n[month] = month_n.get(month, 0) + 1
    season = {str(m): round((month_sum[m] / month_n[m]) / overall, 4) if overall > 0 else 1.0
              for m in sorted(month_sum)}

    # 2. Middelværdi pr. (lead, ugedag), sæsonrenset.
    sums, counts, squares = {}, {}, {}
    for row in usable:
        factor = season.get(str(row["maaned"]), 1.0) or 1.0
        value = row[column] / factor
        key = f"{row['lead']}|{row['ugedag']}"
        sums[key] = sums.get(key, 0.0) + value
        squares[key] = squares.get(key, 0.0) + value * value
        counts[key] = counts.get(key, 0) + 1
    mean = {k: round(sums[k] / counts[k], 4) for k in sums}

    # 3. Spredning i forhold til middelværdi — tallet der afviser Poisson.
    var_over_mean = {}
    for lead in sorted({int(k.split("|")[0]) for k in mean}):
        keys = [k for k in mean if int(k.split("|")[0]) == lead]
        n = sum(counts[k] for k in keys)
        mu = sum(sums[k] for k in keys) / max(1, n)
        var = sum(squares[k] for k in keys) / max(1, n) - mu * mu
        if mu > 0.5:
            var_over_mean[str(lead)] = round(var / mu, 3)

    # 4. Formen: faktisk pickup i forhold til forventet, poolet pr. lead-interval.
    buckets: list = [[] for _ in LEAD_BUCKETS]
    for row in usable:
        key = f"{row['lead']}|{row['ugedag']}"
        mu = mean.get(key, 0.0)
        if mu < MIN_MEAN_FOR_SHAPE:
            continue
        factor = season.get(str(row["maaned"]), 1.0) or 1.0
        buckets[_bucket(row["lead"])].append((row[column] / factor) / mu)

    shape = [Shape(_quantiles(values), len(values)) for values in buckets]
    # Tomme intervaller (typisk lead 0-1, hvor der intet er tilbage at hente)
    # arver den nærmeste udfyldte form, så opslag altid kan svare.
    for index, item in enumerate(shape):
        if item.n == 0:
            donor = next((s for s in shape[index + 1:] if s.n), None) or \
                    next((s for s in reversed(shape[:index]) if s.n), Shape([], 0))
            shape[index] = Shape(list(donor.quantiles), 0)

    # 5. Sæsongennemsnit pr. ugedag og måned — referencen krympningen trækker mod.
    final_sum, final_n = {}, {}
    for row in usable:
        if row["lead"]:
            continue
        key = f"{row['ugedag']}|{row['maaned']}"
        final_sum[key] = final_sum.get(key, 0.0) + row[final_column]
        final_n[key] = final_n.get(key, 0) + 1
    seasonal_final = {k: round(final_sum[k] / final_n[k], 3) for k in final_sum}

    # 6. Krympningsvægt pr. lead time, estimeret og ikke valgt.
    #
    # Prognosen er w x (bøger + forventet pickup) + (1-w) x sæsongennemsnit.
    # Den w der minimerer den kvadrerede fejl har en lukket form: med
    # d = bøger + pickup - sæson og e = sæson - faktisk er w = -sum(d x e)/sum(d x d).
    # Vægten bindes til [0; 1], så prognosen altid ligger mellem de to.
    shrink_num, shrink_den = {}, {}
    for row in usable:
        lead = row["lead"]
        day = date.fromisoformat(row["dato"])
        seasonal = seasonal_final.get(f"{row['ugedag']}|{row['maaned']}")
        if seasonal is None:
            continue
        key = f"{row['lead']}|{row['ugedag']}"
        mu = mean.get(key, 0.0) * season.get(str(row["maaned"]), 1.0)
        d = (row[final_column] - row[column]) + mu - seasonal
        e = seasonal - row[final_column]
        shrink_num[lead] = shrink_num.get(lead, 0.0) - d * e
        shrink_den[lead] = shrink_den.get(lead, 0.0) + d * d
    shrink = {str(lead): round(max(0.0, min(1.0, shrink_num[lead] / shrink_den[lead])), 4)
              for lead in shrink_den if shrink_den[lead] > 1e-9}

    return DemandModel(
        kind=kind, mean=mean, season=season, shape=shape,
        var_over_mean=var_over_mean, seasonal_final=seasonal_final, shrink=shrink,
        n_dates=len(finals),
        note=("Netto pickup: nysalg minus annulleringer. Formen er empirisk og "
              "rummer gruppespring og negativ pickup. Prognosen krympes mod "
              "sæsongennemsnittet med en vægt estimeret pr. lead time. "
              "Ikke valideret elasticitet."),
    )


def fit_all(rows: Sequence[dict], *, exclude_ranges: Sequence[tuple] = ()) -> dict:
    """De fire modeller v4 bruger: rum og senge, i alt og kun transient."""
    for row in rows:
        row["rest_rum_transient"] = row["rest_rum"] - row["rest_rum_gruppe"]
        row["rest_senge_transient"] = row["rest_senge"] - row["rest_senge_gruppe"]
    specs = (
        ("rum", "rest_rum", "endelig_rum"),
        ("senge", "rest_senge", "endelig_senge"),
        ("rum_transient", "rest_rum_transient", "endelig_rum"),
        ("senge_transient", "rest_senge_transient", "endelig_senge"),
    )
    return {name: fit(rows, kind=name, column=col, final_column=final,
                      exclude_ranges=exclude_ranges)
            for name, col, final in specs}


def save(models: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"models": {k: m.to_dict() for k, m in models.items()}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: str | Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: DemandModel.from_dict(v) for k, v in raw["models"].items()}


# --------------------------------------------------------------------------
# Kommandolinje
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    from . import cube

    ap = argparse.ArgumentParser(
        description="Estimér fordelingen af resterende efterspørgsel fra bookingkuben")
    ap.add_argument("kube", nargs="?", default="kali/cube.csv")
    ap.add_argument("--ud", default="config/demand_model.json")
    ap.add_argument("--udelad", default="2025-01-01:2025-03-07",
                    help="datointervaller der udelades, fx renoveringen; "
                         "flere adskilles med komma, tom streng udelader intet")
    args = ap.parse_args(argv)

    ranges = tuple(tuple(part.split(":")) for part in args.udelad.split(",") if part.strip())
    rows = cube.load(args.kube)
    models = fit_all(rows, exclude_ranges=ranges)
    save(models, args.ud)

    print(f"{len(rows)} kuberækker, {models['rum'].n_dates} datoer efter frasortering")
    print(f"Udeladte perioder: {' '.join('-'.join(r) for r in ranges) or 'ingen'}")
    print()
    print("Varians i forhold til middelværdi (Poisson ville give 1,0):")
    print(f"{'lead':>6} {'rum':>8} {'senge':>8}")
    for lead in (3, 7, 14, 30, 60, 90, 120):
        rooms = models["rum"].var_over_mean.get(str(lead))
        beds = models["senge"].var_over_mean.get(str(lead))
        print(f"{lead:>6} {rooms if rooms is not None else '-':>8} "
              f"{beds if beds is not None else '-':>8}")
    print()
    print("Formens haler (forhold mellem faktisk og forventet pickup):")
    print(f"{'lead':>6} {'p10':>7} {'p50':>7} {'p90':>7} {'p99':>7} {'n':>7}")
    for lead in (7, 30, 90):
        s = models["rum"]._shape(lead)
        print(f"{lead:>6} {s.quantile(0.1):>7.2f} {s.quantile(0.5):>7.2f} "
              f"{s.quantile(0.9):>7.2f} {s.quantile(0.99):>7.2f} {s.n:>7}")
    print()
    print("Krympning: hvor meget bøgerne vejer mod kalenderen (1,0 = kun bøgerne):")
    print(f"{'lead':>6} {'rum':>8} {'senge':>8}")
    for lead in (3, 7, 14, 30, 60, 90, 120):
        print(f"{lead:>6} {models['rum'].shrink_weight(lead):>8.2f} "
              f"{models['senge'].shrink_weight(lead):>8.2f}")
    print()
    print(f"Skrevet til {args.ud}")
    print("# Modellen beskriver historisk pickup, ikke målt priselasticitet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

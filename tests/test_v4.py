"""Tests for version 4: bookingkube, efterspørgselsfordeling, bid price, niveau.

Testene her holder især øje med de steder hvor et rigtigt regnestykke giver en
forkert anbefaling: uafgjorte flex-valg, nul-signaler i niveauet og rabat der
ikke er dækket af en måling.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml

from app import bidprice, cube, demand, level
from app.bidprice import V4Config
from app.config import load_settings
from app.engine import DayInput, price_day


# --------------------------------------------------------------------------
# Bookingkuben
# --------------------------------------------------------------------------

def _line(**kw):
    base = dict(booked=date(2025, 1, 1), arrival=date(2025, 3, 1), nights=1,
                quantity=1, price=500.0, is_bed=False, cancelled=None, is_group=False)
    base.update(kw)
    return cube.Line(**base)


def _rows(lines, **kw):
    return {(r["dato"], r["lead"]): r for r in cube.build(lines, **kw)}


def test_otb_vokser_naar_lead_falder():
    rows = _rows([_line(booked=date(2025, 2, 1)), _line(booked=date(2025, 2, 20))])
    assert rows[("2025-03-01", 0)]["otb_rum"] == 2
    assert rows[("2025-03-01", 20)]["otb_rum"] == 1    # kun den tidlige booking
    assert rows[("2025-03-01", 60)]["otb_rum"] == 0


def test_annullering_fjerner_bookingen_fra_den_dag_den_skete():
    rows = _rows([_line(booked=date(2025, 1, 1), cancelled=date(2025, 2, 14))])
    assert rows[("2025-03-01", 30)]["otb_rum"] == 1    # 30 dage ude: stadig booket
    assert rows[("2025-03-01", 10)]["otb_rum"] == 0    # annulleret inden da
    assert rows[("2025-03-01", 0)]["endelig_rum"] == 0


def test_ophold_taelles_paa_alle_naetter():
    rows = _rows([_line(nights=3)], trim_to_arrivals=False)
    for day in ("2025-03-01", "2025-03-02", "2025-03-03"):
        assert rows[(day, 0)]["otb_rum"] == 1
    assert ("2025-03-04", 0) not in rows


def test_natter_efter_sidste_ankomst_skaeres_fra():
    """De sidste datoer har kun de gæster med der nåede at ankomme inden
    periodens slutning. Tages de med, ser efterspørgslen ud til at falde."""
    rows = _rows([_line(nights=3)])
    assert ("2025-03-01", 0) in rows
    assert ("2025-03-02", 0) not in rows


def test_senge_og_rum_holdes_adskilt():
    rows = _rows([_line(is_bed=True, quantity=4), _line()])
    row = rows[("2025-03-01", 0)]
    assert (row["otb_senge"], row["otb_rum"]) == (4, 1)


def test_rest_er_netto_og_kan_vaere_negativ():
    """Annulleringer kan overstige nysalg. Fordelingen skal kunne rumme det."""
    rows = _rows([_line(booked=date(2025, 1, 1), cancelled=date(2025, 2, 20))])
    assert rows[("2025-03-01", 30)]["rest_rum"] == -1


def test_gruppe_markeres_pr_reservationsnummer():
    lines = cube.read_lines  # dokumenterer hvor markeringen sker
    assert callable(lines)
    assert cube.normalise_ref("094613/002") == "94613"
    assert cube.normalise_ref("94613") == "94613"


# --------------------------------------------------------------------------
# Efterspørgselsfordelingen
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def models():
    return demand.load("config/demand_model.json")


def test_halen_falder_monotont(models):
    day, lead = date(2026, 7, 15), 30
    values = [models["rum"].survival(x, lead, day) for x in range(1, 80)]
    assert all(a >= b for a, b in zip(values, values[1:]))
    assert values[0] > values[-1]


def test_boegerne_vejer_mindre_jo_laengere_ude(models):
    """Målt på historikken: 60+ dage ude tilfører bookingerne støj frem for
    information. 43 % af reservationslinjerne ender annulleret."""
    weights = [models["rum"].shrink_weight(lead) for lead in (3, 14, 30, 60, 120)]
    assert all(a >= b for a, b in zip(weights, weights[1:]))
    assert weights[0] > 0.7 and weights[-1] < 0.4


def test_krympning_traekker_mod_saesongennemsnittet(models):
    """En dato med usædvanligt mange bookinger langt ude får ikke tallet for
    pålydende: prognosen trækkes mod hvad den ugedag i den måned plejer at give."""
    day, lead = date(2026, 11, 20), 90
    model = models["rum"]
    rå = model.expected(lead, day)
    travl = model.expected(lead, day, otb=55)
    tom = model.expected(lead, day, otb=0)
    assert travl < rå < tom
    assert model.bind(55).expected(lead, day) == travl


def test_niveauet_skalerer_efterspoergslen(models):
    day, lead = date(2026, 7, 15), 30
    høj = models["rum"].survival(20, lead, day, level=1.3)
    lav = models["rum"].survival(20, lead, day, level=0.7)
    assert høj > lav


def test_variansen_er_langt_over_middelvaerdien(models):
    """Poisson antager forholdet 1,0. Er det tæt på 1, er tempo-triggerens
    z-score brugbar; er det 6-11, slår den ud på støj."""
    ratios = [v for v in models["rum"].var_over_mean.values()]
    assert min(ratios) > 3.0


# --------------------------------------------------------------------------
# Bid price
# --------------------------------------------------------------------------

def test_forventet_salg_kan_ikke_overstige_kapaciteten(models):
    sales = bidprice.expected_sales(models["rum"], 60, date(2026, 7, 18), 12)
    assert 0 <= sales <= 12


def test_bid_price_stiger_naar_der_er_mindre_tilbage(models):
    day = date(2026, 7, 18)
    meget = bidprice.bid_price(models["rum"], 30, day, 60, 500)
    lidt = bidprice.bid_price(models["rum"], 30, day, 5, 500)
    assert lidt > meget


def test_knaphed_loefter_trinnet(models):
    rungs = [400, 450, 500, 550, 600, 650, 700, 760, 820]
    kw = dict(model=models["rum"], rung_prices=rungs, reference_rung=3,
              lead=30, day=date(2026, 7, 18), commission=0.16,
              variable_cost=95, elasticity=1.6)
    rigeligt = bidprice.choose_rung(capacity_remaining=65, **kw)
    knapt = bidprice.choose_rung(capacity_remaining=4, **kw)
    assert knapt.rung >= rigeligt.rung
    assert knapt.p_sellout > rigeligt.p_sellout


def test_uafgjort_flex_gaar_til_sengene(models):
    """Begge lagre tomme: begge sider er nul værd, og så flyttes der ingenting."""
    n, _ = bidprice.flex_allocation(
        room_model=models["rum"], bed_model=models["senge"],
        lead=120, day=date(2027, 1, 17), rooms_free=49, beds_free=163,
        flex_rooms=20, beds_per_flex_room=8, room_net=1200, bed_net=150,
        min_gain=25.0)
    assert n == 0


def test_fortraengning_vokser_med_antal_vaerelser(models):
    kw = dict(model=models["rum"], lead=14, day=date(2026, 7, 18),
              capacity_remaining=10, net_price=500)
    assert (bidprice.displacement_cost(units=8, **kw)
            > bidprice.displacement_cost(units=3, **kw) > 0)


def test_elasticitet_kan_ikke_vaere_negativ():
    with pytest.raises(ValueError):
        V4Config(elasticity_rooms=-1)


# --------------------------------------------------------------------------
# Motoren med version 4
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def params(models):
    base = replace(load_settings().params, v4=None, demand_models={},
                   demand_level=(1.0, 1.0))
    return (base, replace(base, v4=V4Config(enabled=True), demand_models=models))


def test_v4_stopper_uden_modelfil(tmp_path):
    raw = load_settings().raw
    missing_model = tmp_path / "manglende_model.json"
    raw["pricing"]["v4"] = dict(enabled=True, model_path=str(missing_model))
    raw["pricing"]["ladder"]["enabled"] = True
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    assert not missing_model.exists()
    with pytest.raises(FileNotFoundError, match="pricing.v4.model_path"):
        load_settings(config_path)


def test_v4_respekterer_gulv_og_loft(params):
    p3, p4 = params
    for day in (date(2027, 1, 17), date(2026, 9, 30)):
        rec = price_day(DayInput(day=day, rooms_otb=5, beds_otb=1), p4,
                        today=date(2026, 9, 21))
        assert p4.price_floor <= rec.room_price <= p4.price_ceiling
        assert p4.bed_floor <= rec.bed_price <= p4.bed_ceiling


def test_v4_kan_koere_uden_daglig_aendringsbremse(params):
    _, p4 = params
    item = DayInput(day=date(2026, 9, 30), rooms_otb=35, beds_otb=1,
                    current_room_price=450, current_bed_price=170)
    bremset = price_day(item, p4, today=date(2026, 9, 21))
    fri = price_day(item, replace(p4, v4=replace(p4.v4, unbounded_daily_change=True)),
                    today=date(2026, 9, 21))
    assert bremset.room_price <= 450 * 1.15 + 1
    assert fri.room_price >= bremset.room_price
    assert "Ændringsbremse" not in " ".join(fri.warnings)


def test_v4_config_laeser_heltal_og_ubegrenset_bremse(tmp_path):
    raw = load_settings().raw
    root = Path(__file__).resolve().parent.parent
    raw["pricing"]["v4"]["model_path"] = str(root / "config" / "demand_model.json")
    raw["pricing"]["v4"]["level_path"] = str(root / "data" / "demand_level.json")
    raw["pricing"]["v4"]["max_discount_rungs"] = 3
    raw["pricing"]["v4"]["unbounded_daily_change"] = True
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    cfg = load_settings(config_path).params.v4
    assert cfg.max_discount_rungs == 3
    assert cfg.unbounded_daily_change is True


def test_rabatten_holdes_indtil_elasticiteten_er_maalt(params):
    """Uden måling må version 4 ikke gå dybere end ét trin under reference."""
    _, p4 = params
    svag = DayInput(day=date(2027, 1, 17), rooms_otb=1, beds_otb=1)
    holdt = price_day(svag, p4, today=date(2026, 9, 21))
    fri = price_day(svag, replace(p4, v4=replace(p4.v4, elasticity_measured=True)),
                    today=date(2026, 9, 21))
    assert holdt.room_rung >= fri.room_rung


def test_maalbelaegning_paavirker_ikke_v4(params):
    """Hele pointen: belægningsmålet er ude af prisbeslutningen."""
    _, p4 = params
    item = DayInput(day=date(2026, 9, 30), rooms_otb=35, beds_otb=1)
    a = price_day(item, p4, today=date(2026, 9, 21))
    b = price_day(item, replace(p4, target_occupancy_rooms=0.95),
                  today=date(2026, 9, 21))
    assert a.room_price == b.room_price


def test_eksploration_er_deterministisk_og_hoejst_et_trin(params):
    _, p4 = params
    explorer = replace(p4, ladder=replace(p4.ladder, explore=True, explore_band=1.0))
    item = DayInput(day=date(2026, 10, 15), rooms_otb=20, beds_otb=4)
    today = date(2026, 9, 21)
    rolig = price_day(item, p4, today=today)
    a = price_day(item, explorer, today=today)
    b = price_day(item, explorer, today=today)
    assert a.room_price == b.room_price                       # samme dato, samme svar
    assert abs(a.room_rung - rolig.room_rung) <= 1            # højst ét trin


def test_gruppegulv_regnes_udaempet_indtil_elasticiteten_er_maalt(params):
    """Et gruppetilbud er bindende. Gulvet må ikke hvile på en udæmpet antagelse."""
    from app.engine import group_quote
    _, p4 = params
    item = DayInput(day=date(2026, 9, 26), rooms_otb=50, beds_otb=1)
    today = date(2026, 9, 21)
    spaerret = group_quote([price_day(item, p4, today=today)], 10, p4)[0]
    fri = replace(p4, v4=replace(p4.v4, elasticity_measured=True))
    uden = group_quote([price_day(item, fri, today=today)], 10, fri)[0]
    assert spaerret.minimum_rate > uden.minimum_rate


def test_gruppegulv_oplyser_baade_forventning_og_sikker_prognose(params):
    """På en dato der er på vej mod fuldt hus koster gruppen mere, hvis
    prognosen rammer plet, end den gør i forventning."""
    from app.engine import group_quote
    _, p4 = params
    rec = price_day(DayInput(day=date(2026, 9, 26), rooms_otb=50, beds_otb=1), p4,
                    today=date(2026, 9, 21))
    quote = group_quote([rec], 10, p4)[0]
    assert quote.minimum_rate_certain > quote.minimum_rate
    assert quote.minimum_rate >= p4.price_floor


def test_gruppesiden_viser_begge_gulve_men_kun_naar_de_er_forskellige():
    """Uden version 4 er de to gulve samme formel og samme tal. Så er en ekstra
    kolonne kun støj."""
    import os
    import tempfile

    os.environ.setdefault("RMS_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
    from app import main
    from app.engine import GroupQuote

    quote = GroupQuote(day=date(2026, 11, 20), forecast_rooms=55.0, room_capacity=66,
                       spare_rooms=11.0, displaced_rooms=3.0, transient_price=760.0,
                       minimum_rate=530.0, minimum_rate_certain=730.0)

    def render(show):
        return main.templates.get_template("groups.html").render(
            settings=main.settings, quotes=[quote], rooms=10, start="2026-11-20",
            nights=1, total_min=5300.0, total_normal=7600.0, total_certain=7300.0,
            show_certain=show, error="", days_dk=main.DK_DAYS, user="test", request=None)

    med = render(True)
    assert "Hvis prognosen holder" in med and "730" in med
    assert "Spændet er informationen" in med
    assert "Hvis prognosen holder" not in render(False)


def test_csv_eksporten_aabner_i_dansk_excel():
    """Semikolon, decimalkomma og BOM. Uden kommaet læser Excel 900.00 som
    90000, og det opdages først når prisen er tastet ind i Picasso."""
    from app.main import _kr

    assert _kr(900.0) == "900,00"
    assert _kr(1234.5) == "1234,50"
    assert "." not in _kr(1060.0)


# --------------------------------------------------------------------------
# Adaptivt niveau
# --------------------------------------------------------------------------

def test_niveauet_roerer_sig_ikke_paa_tyndt_signal():
    ny, hvorfor = level.adjust(1.0, expected=5, actual=0)
    assert ny == 1.0 and "signal" in hvorfor


def test_niveauet_falder_naar_pickup_svigter_og_er_bundet():
    state = level.Level()
    for _ in range(200):
        state = level.update(state, rooms=[(100, 10)], beds=[(100, 10)])
    assert state.rooms >= level.DEFAULT_MIN - 1e-9
    assert state.rooms < 1.0


def test_niveauet_stiger_naar_pickup_overgaar_forventningen():
    state = level.update(level.Level(), rooms=[(100, 140)], beds=[(100, 100)])
    assert state.rooms > 1.0
    assert state.beds == pytest.approx(1.0, abs=1e-6)

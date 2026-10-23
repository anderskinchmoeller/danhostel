"""Model policies must hold through the complete pricing pipeline."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app import level, service
from app.config import load_settings
from app.engine import DayInput, price_day
from app import bidprice, demand
from app.engine import Inventory
from app.pricing_basis import PricingBasis


class CertainShape(demand.Shape):
    def survival(self, ratio):
        return float(ratio <= 1)


@pytest.mark.parametrize("ratio", [0.8, 1.2])
def test_persistent_gap_converges_to_level_instead_of_compounding(ratio):
    value = 1.0
    for _ in range(500):
        value, _ = level.adjust(value, expected=100, actual=100 * ratio)
    assert value == pytest.approx((100 * ratio + 1) / 101)
    stable, _ = level.adjust(value, expected=100, actual=100 * ratio)
    assert stable == pytest.approx(value)


def test_level_recovers_from_old_learning_boundary():
    value = 0.6
    for _ in range(100):
        value, _ = level.adjust(value, expected=100, actual=100)
    assert value > 0.99


def test_learning_uses_fixed_baseline_and_includes_cancellations(tmp_path, monkeypatch):
    captured = {}

    class Baseline:
        def expected(self, lead, day):
            return 5 * lead

    params = load_settings().params
    params = replace(params, demand_models={"rum": Baseline(), "senge": Baseline()},
        v4=replace(params.v4, level_path=str(tmp_path / "level.json")))
    today = date(2026, 10, 23)
    items = [DayInput(today + timedelta(days=7), rooms_otb=20, rooms_otb_prior=10,
                     beds_otb=1, beds_otb_prior=4, prior_days=7)]
    original = level.update

    def capture(state, **kwargs):
        captured.update(kwargs)
        return original(state, **kwargs)

    monkeypatch.setattr(level, "update", capture)
    service.update_demand_level(params, items, today)
    assert captured["rooms"] == [(35, 10)]
    assert captured["beds"] == [(35, -3)]


@pytest.mark.parametrize("lead", [0, 1, 2, 7, 30, 61])
@pytest.mark.parametrize("explore", [False, True])
def test_discount_floor_survives_market_smoothing_and_exploration(lead, explore):
    params = load_settings().params
    params = replace(params, ladder=replace(params.ladder, explore=explore, explore_band=1.0))
    today = date(2026, 10, 23)
    item = DayInput(today + timedelta(days=lead), rooms_otb=0, beds_otb=0,
        comp_room=1, comp_bed=1, prev_room_rung=0, prev_bed_rung=0,
        prev_room_position=0, prev_bed_position=0, same_day_fraction=0.5)
    rec = price_day(item, params, today=today)
    for ladder in (rec.room_ladder, rec.bed_ladder):
        floor = ladder["reference_rung"] - params.v4.max_discount_rungs
        assert ladder["rung"] >= floor
        assert ladder["price"] >= ladder["rung_prices"][floor]
        if ladder["exploration"]:
            assert ladder["exploration"]["control_rung"] >= floor
            assert ladder["exploration"]["alternative_rung"] >= floor


def test_discount_floor_and_daily_brake_conflict_is_explicit():
    params = load_settings().params
    today = date(2026, 10, 23)
    item = DayInput(date(2026, 10, 24), current_bed_price=180)
    with pytest.raises(ValueError, match="ændringsbremse"):
        price_day(item, params, today=today)


def test_manual_price_lock_is_respected_below_automatic_discount_floor():
    params = load_settings().params
    rec = price_day(DayInput(date(2026, 10, 24), locked_bed_price=180), params,
                    today=date(2026, 10, 23))
    assert rec.bed_price == 180


def test_optimizer_and_final_diagnostics_share_one_physical_inventory():
    params = load_settings().params
    today, day = date(2026, 10, 23), date(2026, 10, 24)
    item = DayInput(day, rooms_otb=20, beds_otb=30)
    rec = price_day(item, params, today=today)
    room, bed = rec.room_ladder["bid"], rec.bed_ladder["bid"]
    inv = params.inventory
    # Both optimizer capacities must come from the same allocation. Previously
    # this sum contained all twenty flex rooms twice.
    for a, b in ((room, bed), (room["optimizer"], bed["optimizer"])):
        room_flex = a["capacity_remaining"] + item.rooms_otb - inv.private_rooms
        bed_flex = (b["capacity_remaining"] + item.beds_otb - inv.dorm_beds) / inv.beds_per_flex_room
        assert room_flex + bed_flex == inv.flex_rooms
    assert room["capacity_remaining"] == rec.room_capacity - item.rooms_otb
    assert bed["capacity_remaining"] == rec.bed_capacity - item.beds_otb


def test_final_demand_uses_locked_price_rather_than_raw_optimizer_price():
    params = load_settings().params
    day = date(2026, 10, 24)
    rec = price_day(DayInput(day, beds_otb=2, locked_bed_price=450), params,
                    today=date(2026, 10, 23))
    bid = rec.bed_ladder["bid"]
    reference = rec.bed_ladder["rung_prices"][rec.bed_ladder["reference_rung"]]
    multiplier = bidprice.demand_multiplier(450, reference, params.v4.elasticity_beds)
    expected = bidprice.expected_sales(params.demand_models["senge"].bind(2), 1, day,
        rec.bed_capacity - 2, level=params.demand_level[1], multiplier=multiplier)
    assert bid["price"] == 450
    assert bid["multiplier"] == pytest.approx(multiplier, abs=0.0005)
    assert bid["expected_sales"] == pytest.approx(expected, abs=0.005)


def test_allocation_keeps_beds_when_they_earn_more_at_actual_locked_prices():
    params = load_settings().params
    day = date(2026, 10, 24)
    def model(kind, units):
        return demand.DemandModel(kind=kind, mean={"1|5": units},
            shape=[CertainShape([1, 1], 10)] * len(demand.LEAD_BUCKETS))
    params = replace(params, inventory=Inventory(private_rooms=2, flex_rooms=1,
        beds_per_flex_room=4, dorm_beds=0, private_beds=4,
        private_room_types={"D2": 2}, flex_room_types={"V6": 1}, confirmed=True),
        room_types={"D2": 1, "V6": 1}, variable_cost=0, variable_cost_bed=0, commission=0,
        demand_level=(1, 1), demand_models={"rum": model("rum", 3), "senge": model("senge", 4)},
        v4=replace(params.v4, elasticity_rooms=0, elasticity_beds=0, flex_min_gain=0,
                   elasticity_rooms_scenarios=(0,), elasticity_beds_scenarios=(0,)))
    rec = price_day(DayInput(day, locked_room_price=600, locked_bed_price=200), params,
                    today=date(2026, 10, 23))
    assert rec.flex_to_private == 0  # four beds earn 800; one extra room earns 600
    rec = price_day(DayInput(day, locked_room_price=1000, locked_bed_price=200), params,
                    today=date(2026, 10, 23))
    assert rec.flex_to_private == 1


@pytest.mark.parametrize("commission_basis, expected", [("guest_price", 220), ("ex_vat", 240)])
def test_economic_basis_compares_revenue_cost_and_fees_excluding_vat(commission_basis, expected):
    basis = PricingBasis("incl_25", "ex_vat", commission_basis)
    # Guest pays 625; net revenue is 500; cost is 180; fee is 100 or 80.
    assert basis.contribution(625, 0.16, 180) == pytest.approx(expected)
    assert basis.price_for_contribution(expected, 0.16, 180) == pytest.approx(625)
    gross_cost = PricingBasis("incl_25", "incl_25", commission_basis)
    assert gross_cost.contribution(625, 0.16, 225) == pytest.approx(expected)


def test_unknown_economic_basis_is_explicit_without_partial_conversion():
    basis = PricingBasis("incl_25", "unknown", "unknown")
    assert not basis.confirmed
    assert basis.contribution(625, 0.16, 180) == pytest.approx(345)
    params = replace(load_settings().params, pricing_basis=basis)
    rec = price_day(DayInput(date(2026, 10, 24)), params, today=date(2026, 10, 23))
    assert any("provisionsgrundlag" in w for w in rec.warnings)


def test_opening_expensive_flex_rooms_does_not_reprice_fixed_room_demand():
    params = load_settings().params
    day = date(2026, 10, 24)
    def model(kind, units):
        return demand.DemandModel(kind=kind, mean={"1|5": units},
            shape=[CertainShape([1, 1], 10)] * len(demand.LEAD_BUCKETS))
    params = replace(params, inventory=Inventory(private_rooms=2, flex_rooms=1,
        beds_per_flex_room=4, dorm_beds=0, private_beds=4,
        private_room_types={"D2": 2}, flex_room_types={"V6": 1}, confirmed=True),
        room_types={"D2": 1, "V6": 3}, variable_cost=0, variable_cost_bed=0, commission=0,
        pricing_basis=PricingBasis("ex_vat", "ex_vat", "ex_vat"),
        demand_level=(1, 1), demand_models={"rum": model("rum", 1), "senge": model("senge", 0)},
        v4=replace(params.v4, elasticity_rooms=0, elasticity_beds=0, flex_min_gain=25,
                   elasticity_rooms_scenarios=(0,), elasticity_beds_scenarios=(0,)))
    rec = price_day(DayInput(day, locked_room_price=600, locked_bed_price=200), params,
                    today=date(2026, 10, 23))
    assert rec.flex_to_private == 0
    assert rec.room_ladder["bid"]["expected_revenue"] == 600


def _training_rows():
    rows = []
    for year, beds in [(2024, 50), (2025, 5)]:
        for offset in range(100):
            day = date(year, 1, 1) + timedelta(days=offset)
            for lead in (0, 7, 30):
                rows.append(dict(dato=day.isoformat(), lead=lead, ugedag=day.weekday(),
                    maaned=day.month, censureret=False, endelig_rum=20, endelig_senge=beds,
                    rest_rum=20 if lead else 0, rest_senge=beds if lead else 0,
                    rest_rum_gruppe=0, rest_senge_gruppe=0))
    return rows


def test_new_bed_training_period_changes_only_bed_models_and_records_dates():
    rows = _training_rows()
    old = demand.fit_all(rows)
    new = demand.fit_all(rows, bed_training_from="2025-01-01")
    assert old["rum"].to_dict() == new["rum"].to_dict()
    assert old["rum_transient"].to_dict() == new["rum_transient"].to_dict()
    for product in ("senge", "senge_transient"):
        assert new[product].n_dates == 100
        assert new[product].training_start == "2025-01-01"
        assert new[product].training_end == "2025-04-10"
        assert new[product].expected(7, date(2026, 3, 8)) < old[product].expected(7, date(2026, 3, 8))
        restored = demand.DemandModel.from_dict(new[product].to_dict())
        assert restored.training_start == new[product].training_start


def test_recent_bed_training_rejects_an_insufficient_sample():
    with pytest.raises(ValueError, match="90"):
        demand.fit_all(_training_rows(), bed_training_from="2025-03-10")


def test_group_floor_converts_displacement_back_to_the_guest_price():
    from app.engine import Params, group_quote
    params = Params(pricing_basis=PricingBasis("incl_25", "ex_vat", "guest_price"),
        variable_cost=180, commission=0.16, price_floor=1, price_ceiling=2000, rounding=.01)
    day = date(2026, 10, 24)
    rec = price_day(DayInput(day, rooms_otb=params.inventory.max_room_capacity,
                            locked_room_price=625), params, today=day)
    quote = group_quote([rec], 10, params)[0]
    assert quote.minimum_rate == pytest.approx(625)

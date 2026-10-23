from dataclasses import replace
from datetime import date, timedelta
import itertools
import json

import pytest

from app import bidprice, demand, engine, forecast_monitor, level, service, uncertainty
from app.config import _v4, load_settings


def test_minimax_compromise_is_not_nominal_or_pessimistic_choice():
    actions = [uncertainty.Action(("low",), (100, 40)),
               uncertainty.Action(("middle",), (85, 85)),
               uncertainty.Action(("high",), (60, 95))]
    best, regret = uncertainty.minimax_regret(actions)
    assert best.key == ("middle",) and regret == 15
    # No assumption that the scenarios have equal probabilities.
    assert best.values == (85, 85)


@pytest.mark.parametrize("values", [(float('nan'),), (float('inf'),)])
def test_nonfinite_scenario_values_rejected(values):
    with pytest.raises(ValueError):
        uncertainty.minimax_regret([uncertainty.Action((0,), values)])


def test_scenarios_are_configurable_validated_and_include_nominal():
    cfg = _v4(dict(elasticity_rooms=1.7, elasticity_rooms_scenarios=[.5, 2.8]))
    assert cfg.elasticities() == (.5, 1.7, 2.8)
    assert isinstance(cfg.elasticity_rooms_scenarios, tuple)
    for kwargs in (dict(elasticity_rooms=float('nan')), dict(elasticity_beds_scenarios=(-1,)),
                   dict(uncertainty_mode='mystery'), dict(elasticity_rooms_scenarios=())):
        with pytest.raises(ValueError):
            bidprice.V4Config(**kwargs)


def small_params():
    p = load_settings().params
    models = {kind: demand.DemandModel(kind=kind, mean={"1|5": units},
        shape=[demand.Shape([.1, .4, 1, 1.6, 3], 100)] * len(demand.LEAD_BUCKETS))
        for kind, units in (("rum", 3), ("senge", 7))}
    return replace(p, inventory=engine.Inventory(private_rooms=2, flex_rooms=2,
        beds_per_flex_room=4, dorm_beds=4, private_beds=4,
        private_room_types={"D2": 2}, flex_room_types={"V6": 2}, confirmed=True),
        room_types={"D2": 1, "V6": 1.5}, demand_models=models, demand_level=(1, 1),
        v4=replace(p.v4, uncertainty_mode="minimax_regret", flex_min_gain=0),
        ladder=replace(p.ladder, explore=False))


def test_joint_optimizer_matches_independent_exhaustive_oracle():
    p = small_params()
    item = engine.DayInput(date(2026, 10, 24), rooms_otb=1, beds_otb=2)
    room_base, bed_base = engine.base_prices(item.day, p)
    chosen = engine._bid_choices(item, p, 1, room_base, bed_base)
    rp = engine.rung_prices_for(room_base, p.ladder.rungs_rooms, p, beds=False)
    bp = engine.rung_prices_for(bed_base, p.ladder.rungs_beds, p, beds=True)
    er, eb = p.v4.elasticities(), p.v4.elasticities(beds=True)
    scenarios = list(itertools.product(er, eb))
    rm = engine._discount_minimum(p, p.ladder.rungs_rooms)
    bm = engine._discount_minimum(p, p.ladder.rungs_beds)
    rr = list(p.ladder.rungs_rooms).index(1.)
    br = list(p.ladder.rungs_beds).index(1.)
    records = []
    # This oracle computes revenues directly; it does not call choose_rung or
    # minimax_regret, and explicitly enforces one physical allocation per action.
    for n, r, b in itertools.product(range(3), range(rm, len(rp)), range(bm, len(bp))):
        values = []
        for re, be in scenarios:
            rs = bidprice.expected_sales(p.demand_models['rum'].bind(1), 1, item.day, 1+n,
                multiplier=(rp[r]/rp[rr])**(-re))
            fixed = bidprice.expected_sales(p.demand_models['rum'].bind(1), 1, item.day, 1,
                multiplier=(rp[r]/rp[rr])**(-re))
            bs = bidprice.expected_sales(p.demand_models['senge'].bind(2), 1, item.day, 10-4*n,
                multiplier=(bp[b]/bp[br])**(-be))
            values.append(p.contribution(rp[r])*fixed + p.contribution(rp[r]*1.5)*(rs-fixed)
                          + p.contribution(bp[b], beds=True)*bs)
        records.append(((n, r, b), values))
    oracle = [max(v[i] for _, v in records) for i in range(len(scenarios))]
    expected_regret = min(max(o-v for o, v in zip(oracle, values)) for _, values in records)
    metadata = chosen[0].uncertainty
    assert metadata['joint_worst_regret'] == pytest.approx(expected_regret, abs=.011)
    key = (metadata['allocation'], chosen[0].rung, chosen[1].rung)
    values = dict(records)[key]
    assert max(o-v for o, v in zip(oracle, values)) == pytest.approx(expected_regret)
    # Fixing the chosen prices must not change the joint-optimal allocation.
    final_n, _ = engine._allocation_at_prices(item, p, 1, room_base, bed_base,
        chosen[0].price, chosen[1].price, oracle=metadata['scenario_oracle'])
    assert final_n == metadata['allocation']


def test_single_scenario_recovers_nominal_optimum():
    p = small_params()
    p = replace(p, v4=replace(p.v4, elasticity_rooms_scenarios=(1.6,),
                              elasticity_beds_scenarios=(2.,)))
    item = engine.DayInput(date(2026, 10, 24))
    bases = engine.base_prices(item.day, p)
    robust = engine._bid_choices(item, p, 1, *bases)
    nominal = engine._bid_choices(item, replace(p, v4=replace(p.v4, uncertainty_mode='diagnostics')), 1, *bases)
    assert [(r.rung, r.capacity_remaining) for r in robust] == [(r.rung, r.capacity_remaining) for r in nominal]


def test_stress_results_describe_locked_final_price_and_capacity():
    p = small_params()
    rec = engine.price_day(engine.DayInput(date(2026, 10, 24), locked_room_price=777,
        locked_bed_price=211), p, today=date(2026, 10, 23))
    assert (rec.room_price, rec.bed_price) == (777, 211)
    for ladder in (rec.room_ladder, rec.bed_ladder):
        bid = ladder['bid']
        stress = bid['uncertainty']
        nominal = next(r for r in stress['rows'] if r['elasticity'] == bid['elasticity'])
        assert nominal['expected_contribution'] == pytest.approx(bid['expected_revenue'], abs=.06)
        assert stress['contribution_min'] <= nominal['expected_contribution'] <= stress['contribution_max']
        assert 'not confidence' in stress['basis']
    json.dumps(rec.as_dict(), allow_nan=False)


def test_limited_inventory_does_not_create_false_downward_learning(tmp_path):
    class Model:
        def expected(self, lead, day):
            return lead * 10
    p = small_params()
    path = tmp_path/'level.json'
    p = replace(p, demand_models={'rum': Model(), 'senge': Model()},
                v4=replace(p.v4, level_path=str(path)))
    today = date(2026, 10, 23)
    items = [engine.DayInput(today+timedelta(days=i), rooms_otb=4, rooms_otb_prior=4,
        beds_otb=0, beds_otb_prior=0, blocked_beds=1, prior_days=7) for i in range(1, 4)]
    updated, note = service.update_demand_level(p, items, today)
    assert updated.demand_level == (1, 1)
    audit = level.load(path).history[-1]['learning_windows']
    assert audit['excluded']['rooms']['capacity_limited'] == 3
    assert audit['excluded']['beds']['blocked'] == 3
    assert '6 produktvinduer' in note


def test_sold_out_prior_endpoint_is_excluded_even_after_cancellations(tmp_path):
    p = small_params()
    path = tmp_path/'level.json'
    p = replace(p, v4=replace(p.v4, level_path=str(path)))
    today = date(2026, 10, 23)
    service.update_demand_level(p, [engine.DayInput(today+timedelta(days=7), rooms_otb=1,
        rooms_otb_prior=3, room_capacity_prior=3, beds_otb_prior=0, prior_days=7)], today)
    assert level.load(path).history[-1]['learning_windows']['excluded']['rooms']['capacity_limited'] == 1


def forecast_rows(today, *, forecast=10, actual=10, count=20):
    return [dict(day=(today-timedelta(days=i+1)).isoformat(), product='rum', lead=7,
        forecast=forecast, actual=actual, baseline=actual, intervals={'80':[9,11]}, model_hash='frozen')
        for i in range(count)]


def test_monitor_needs_independent_recent_completed_dates():
    today = date(2026, 10, 23)
    rows = forecast_rows(today, count=5)
    rows += rows * 3  # Repeated evaluations are not new stay dates.
    rows += [dict(rows[0], day=today.isoformat()), dict(rows[0], day='2026-11-01'),
             dict(rows[0], day='2026-08-01'), dict(rows[0], day='2026-10-01', actual=None)]
    r = next(r for r in forecast_monitor.recent(rows, today)['rows'] if r['lead']==7 and r['product']=='rum')
    assert r['n'] == 5 and r['status'] == 'insufficient'


def test_monitor_flags_bad_forecasts_and_leaves_good_forecasts_unflagged():
    today = date(2026, 10, 23)
    report = forecast_monitor.recent(forecast_rows(today, forecast=20), today)
    row = next(r for r in report['rows'] if r['product']=='rum' and r['lead']==7)
    assert row['status'] == 'review' and len(row['reasons']) == 2
    report = forecast_monitor.recent(forecast_rows(today), today)
    row = next(r for r in report['rows'] if r['product']=='rum' and r['lead']==7)
    assert row['status'] == 'no_flag' and row['reasons'] == []

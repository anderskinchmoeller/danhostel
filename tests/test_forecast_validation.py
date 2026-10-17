"""Operational forecasts must be immutable and use only information available then."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db, demand, forecast_validation as f, validation
from app.config import load_settings
from app.engine import DayInput, price_day


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    engine = db.init_db(f"sqlite:///{tmp_path}/forecasts.db")
    with db.get_session() as s:
        yield s
    engine.dispose()


def test_checkpoint_freezes_both_products_with_same_engine_forecast(session, monkeypatch):
    today = date(2026, 10, 16)
    monkeypatch.setattr(db, "utcnow", lambda: datetime(2026, 10, 16, 8))
    settings = load_settings()
    inputs = [DayInput(today+timedelta(days=lead), rooms_otb=20, beds_otb=5,
                       blocked_rooms=2, blocked_beds=10) for lead in (1, 7, 14, 30, 2)]
    recs = [price_day(item, settings.params, today=today) for item in inputs]
    run = SimpleNamespace(id=99)
    assert f.save_checkpoints(session, run, recs, inputs, settings.params, today, settings.timezone) == 8
    session.commit()
    snapshots = session.scalars(select(db.ForecastSnapshot)).all()
    old = [r.payload for r in snapshots]
    changed = replace(settings.params, demand_level=(1.4, .7))
    new = [price_day(item, changed, today=today) for item in inputs]
    assert f.save_checkpoints(session, SimpleNamespace(id=100), new, inputs, changed, today, settings.timezone) == 0
    assert [r.payload for r in session.scalars(select(db.ForecastSnapshot))] == old
    assert all(r.run_id == 99 for r in snapshots)
    assert {r.product for r in snapshots} == {"rum", "senge"}
    for r in snapshots:
        p = json.loads(r.payload)
        assert p["capacity"] == (settings.params.inventory.max_room_capacity-2 if r.product == "rum"
                                 else settings.params.inventory.max_bed_capacity-10)
        assert p["baseline"] is not None
        assert all(0 <= low <= high <= p["capacity"] for low, high in p["intervals"].values())
    # No backdated, retrospectively reconstructed operational checkpoint.
    assert f.save_checkpoints(session, run, recs, inputs, changed, today-timedelta(days=1), settings.timezone) == 0


def test_final_sales_need_no_financial_estimates_and_latest_final_wins(session):
    day = date(2026, 10, 10)
    for product in f.PRODUCTS:
        session.add(db.ForecastSnapshot(day=day, lead=7, product=product, run_id=1,
            observed_at=datetime(2026, 10, 3), payload=json.dumps(dict(
                forecast=10, baseline=12, intervals={"80": [0, 20]}, capacity=100, weekend=True))))
    session.flush()
    for stamp, rooms, beds, final in [("2026-10-11T08:00", 4, 0, "ja"),
                                     ("2026-10-12T08:00", 5, 2, "ja"),
                                     ("2026-10-13T08:00", 99, 99, "nej")]:
        prepared = validation.prepare("outcome", dict(day=day.isoformat(), observed_at=stamp,
            final_rooms=str(rooms), final_beds=str(beds), final=final), "Europe/Copenhagen")
        validation.record(session, "outcome", prepared, actor="test", source="final")
    rows, n, dates = f.live_rows(session, date(2026, 10, 16))
    assert n == 2 and dates == 1
    assert {r["product"]: r["actual"] for r in rows} == {"rum": 5, "senge": 2}
    assert all(r["groups"] is None for r in rows)
    assert "unknown_groups" in f.tags(rows[0]) and "no_groups" not in f.tags(rows[0])
    assert f.live_rows(session, day)[0] == []


def test_paired_reference_denominator_and_coverage_are_separate():
    rows = [dict(day="2025-04-01", product="rum", lead=7, forecast=10, actual=8,
                 baseline=12, capacity=8, groups=0, weekend=False, intervals={"80": [5, 12]}),
            dict(day="2025-04-02", product="rum", lead=7, forecast=20, actual=0,
                 baseline=None, capacity=30, groups=None, weekend=True, intervals={"80": [5, 22]})]
    all_rows = next(r for r in f.score(rows) if r["segment"] == "all")
    assert all_rows["n"] == 2 and all_rows["n_baseline"] == 1
    assert all_rows["mae"] == 11 and all_rows["paired_model_mae"] == 2
    assert all_rows["advantage"] == 2 and all_rows["advantage_ci"] is None
    assert all_rows["coverage"]["80"] == .5
    assert "busy" in f.tags(rows[0])


def test_missing_historical_reference_never_uses_test_answer():
    from app.evaluate import errors_by_date
    models = load_settings().params.demand_models
    row = dict(dato="2025-04-01", lead=7, censureret=0, otb_rum=10,
               endelig_rum=15, ugedag=1, maaned=4)
    assert errors_by_date([row], models, {}) == {}
    assert f.baseline([dict(row, lead=0)], "rum") == {"1|4": 15}


def test_interval_summary_matches_bound_model_and_retains_cancellation_tail():
    model = load_settings().params.demand_models["rum"]
    day = date(2026, 10, 17)
    r = demand.sales_forecast(model, 20, 7, day, 25, 1.1)
    assert r["forecast"] == min(25, 20+model.expected(7, day, 1.1, otb=20))
    assert r["intervals"]["80"][0] == max(0, min(25, 20+model.quantile(.1, 7, day, 1.1, otb=20)))


def test_failed_calculation_does_not_consume_checkpoints(session, monkeypatch):
    from zoneinfo import ZoneInfo
    from app import service, skyggedrift
    settings = load_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    session.add(db.DayState(day=today+timedelta(days=7), rooms_otb=10, beds_otb=0,
                            otb_updated=db.utcnow()))
    session.commit()
    monkeypatch.setattr(service, "get_pms_adapter", lambda _: SimpleNamespace(fetch_inventory=lambda *_: []))
    monkeypatch.setattr(service, "get_rateshop_adapter", lambda _: SimpleNamespace(fetch_comp_prices=lambda *_: []))
    monkeypatch.setattr(service, "update_demand_level", lambda params, *_: (params, ""))
    def fail(*args, **kwargs):
        raise RuntimeError("controlled failure after checkpoint")
    monkeypatch.setattr(skyggedrift, "compare_inputs", fail)
    result = service.run_pricing(settings, today=today)
    assert result["status"] == "failed"
    assert "controlled failure" in result["error"]
    session.expire_all()
    assert session.scalars(select(db.ForecastSnapshot)).all() == []
    assert session.scalars(select(db.Recommendation)).all() == []

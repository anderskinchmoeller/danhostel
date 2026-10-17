"""Regression coverage for intraday sales, learning and market snapshots."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
import json

import pytest
from sqlalchemy import select

from app import db, demand, level, service
from app.adapters import CompRow, parse_comp
from app.config import load_settings
from app.engine import BookingCurve, CurvePoint, DayInput, price_day


def test_arrival_day_has_remaining_sales_and_keeps_historical_final():
    settings = load_settings()
    today = date(2026, 10, 16)
    model = settings.params.demand_models["rum"]
    assert model.bind(44).expected(0, today) == 0
    morning = replace(model, same_day_fraction=0.75)
    evening = replace(model, same_day_fraction=0.125)
    assert morning.bind(44).expected(0, today) > evening.bind(44).expected(0, today) > 0
    assert morning.bind(44).expected(0, today) == pytest.approx(
        model.expected(1, today) * 0.75)
    item = DayInput(today, rooms_otb=44, beds_otb=2, same_day_fraction=0.75)
    rec = price_day(item, settings.params, today=today)
    assert rec.forecast_rooms > 44
    assert rec.forecast_beds > 2
    assert rec.room_ladder["bid"]["expected_sales"] > 0
    assert rec.bed_ladder["bid"]["expected_sales"] > 0
    assert rec.forecast_rooms <= settings.params.inventory.max_room_capacity
    assert any("bookingtidspunkter mangler" in w for w in rec.warnings)
    assert model.same_day_fraction is None  # runtime copies cannot change backtests
    closed = price_day(replace(item, same_day_fraction=0), settings.params, today=today)
    assert closed.forecast_rooms == 44
    assert closed.forecast_beds == 2


def test_same_day_demand_does_not_change_future_dates():
    model = demand.load("config/demand_model.json")["rum"]
    day = date(2026, 10, 17)
    for lead in (1, 7, 30):
        assert replace(model, same_day_fraction=0.5).bind(20).expected(lead, day) == \
            model.bind(20).expected(lead, day)


def test_learning_only_once_per_date_and_not_backwards():
    today = date(2026, 10, 16)
    first = level.update(level.Level(), rooms=[(100, 150)], today=today)
    assert level.update(first, rooms=[(100, 200)], today=today) is first
    assert level.update(first, rooms=[(100, 200)], today=today-timedelta(days=1)) is first
    next_day = level.update(first, rooms=[(100, 150)], today=today+timedelta(days=1))
    assert next_day.rooms > first.rooms
    assert len(next_day.history) == 2


def test_concurrent_runs_and_stale_settings_share_one_learning_step(tmp_path):
    settings = load_settings()
    path = tmp_path / "level.json"
    params = replace(settings.params, v4=replace(settings.params.v4, level_path=str(path)))
    today = date(2026, 10, 16)
    inputs = [DayInput(today + timedelta(days=i), rooms_otb=25, beds_otb=10,
                       rooms_otb_prior=1, beds_otb_prior=0, prior_days=7)
              for i in range(1, 40)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.update_demand_level(params, inputs, today),
                                range(8)))
    saved = level.load(path)
    assert len(saved.history) == 1
    assert saved.rooms != 1
    assert all(p.demand_level == (saved.rooms, saved.beds) for p, _ in results)


def test_arrival_day_is_not_used_as_completed_learning_signal(tmp_path):
    settings = load_settings()
    path = tmp_path / "level.json"
    params = replace(settings.params, v4=replace(settings.params.v4, level_path=str(path)))
    today = date(2026, 10, 16)
    service.update_demand_level(params, [DayInput(today, rooms_otb=60,
        rooms_otb_prior=0, beds_otb_prior=0, prior_days=7)], today)
    assert not path.exists()


def test_market_snapshot_clears_blank_beds_and_excludes_stale_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    engine = db.init_db(f"sqlite:///{tmp_path}/market.db")
    settings = load_settings()
    now = datetime(2026, 10, 16, 5, 0)
    today = date(2026, 10, 16)
    try:
        with db.get_session() as session:
            service.upsert_comp(session, [CompRow(today, 800, 216)], observed_at=now)
            session.commit()
            rows = parse_comp("dato;medianpris_vaerelse;medianpris_seng\n2026-10-16;820;\n")
            service.upsert_comp(session, rows, observed_at=now)
            session.commit()
            state = session.get(db.DayState, today)
            assert state.comp_room == 820
            assert state.comp_bed is None
            item = service.build_inputs([state], settings, now=now)[0]
            assert item.comp_room == 820 and item.comp_bed is None
            assert item.same_day_fraction == pytest.approx(17/24)  # Copenhagen 07:00
            late = service.build_inputs([state], settings, now=now+timedelta(hours=37))[0]
            assert late.comp_room is None and late.comp_bed is None
            assert late.market_notes
            assert state.comp_room == 820  # raw observation stays available for audit
            state.comp_updated = now+timedelta(hours=1)
            assert service.build_inputs([state], settings, now=now)[0].comp_room is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("lead", [1, 7, 30, 90, 120])
def test_forecast_uses_bid_price_demand_and_not_the_old_booking_curves(lead):
    params = load_settings().params
    today = date(2026, 10, 16)
    day = today + timedelta(days=lead)
    item = DayInput(day, rooms_otb=10, beds_otb=2)
    rec = price_day(item, params, today=today)
    expected_rooms = min(params.inventory.max_room_capacity,
        10 + params.demand_models["rum"].bind(10).expected(lead, day, params.demand_level[0]))
    expected_beds = min(params.inventory.max_bed_capacity,
        2 + params.demand_models["senge"].bind(2).expected(lead, day, params.demand_level[1]))
    assert rec.forecast_rooms == pytest.approx(expected_rooms)
    assert rec.forecast_beds == pytest.approx(expected_beds)
    assert rec.room_forecast == pytest.approx(expected_rooms / params.inventory.max_room_capacity)
    assert rec.bed_forecast == pytest.approx(expected_beds / params.inventory.max_bed_capacity)
    unrelated_curve = BookingCurve([CurvePoint(0, 0.99, 0.99), CurvePoint(120, 0.01, 0.01)])
    changed = price_day(item, replace(params, booking_curve=unrelated_curve,
                                     booking_curve_beds=unrelated_curve), today=today)
    assert changed.forecast_rooms == rec.forecast_rooms
    assert changed.forecast_beds == rec.forecast_beds
    assert (changed.room_price, changed.bed_price) == (rec.room_price, rec.bed_price)


def test_adaptive_level_changes_both_forecast_and_bid_demand_with_capacity_limits():
    params = load_settings().params
    today = date(2026, 10, 16)
    item = DayInput(today + timedelta(days=30), rooms_otb=0, beds_otb=0,
                    blocked_rooms=3, blocked_beds=8)
    low = price_day(item, replace(params, demand_level=(0.6, 0.6)), today=today)
    high = price_day(item, replace(params, demand_level=(1.6, 1.6)), today=today)
    assert high.forecast_rooms > low.forecast_rooms
    assert high.forecast_beds > low.forecast_beds
    for rec in (low, high):
        assert 0 <= rec.forecast_rooms <= params.inventory.max_room_capacity - 3
        assert 0 <= rec.forecast_beds <= params.inventory.max_bed_capacity - 8
    # Candidate prices stay fixed; their expected sales respond to the same level.
    assert high.room_ladder["bid"]["table"][3][2] > low.room_ladder["bid"]["table"][3][2]
    assert high.bed_ladder["bid"]["table"][3][2] > low.bed_ladder["bid"]["table"][3][2]


def test_repeated_runs_and_reimports_do_not_advance_the_daily_ladder(tmp_path, monkeypatch):
    from app.adapters import InventoryRow

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    now = datetime(2026, 10, 16, 6)
    monkeypatch.setattr(db, "utcnow", lambda: now)
    # The ORM captures/caches the SQL default callable separately from db.utcnow.
    from sqlalchemy import event
    def freeze_run_start(mapper, connection, target):
        if target.started is None:
            target.started = now
    event.listen(db.Run, "before_insert", freeze_run_start)
    engine = db.init_db(f"sqlite:///{tmp_path}/repeat.db")
    settings = load_settings()
    settings.horizon_days = 30
    settings.params = replace(settings.params, v4=replace(settings.params.v4,
        level_path=str(tmp_path / "level.json")))
    today = date(2026, 10, 16)
    day = today + timedelta(days=30)
    try:
        with db.get_session() as session:
            before = db.Run(status="done", started=now-timedelta(days=1),
                            finished=now-timedelta(days=1))
            session.add(before)
            session.flush()
            session.add(db.Recommendation(run_id=before.id, day=day,
                room_rung=8, bed_rung=8, ladder=json.dumps({
                    "rooms": {"smoothed_position": 8}, "beds": {"smoothed_position": 8}})))
            session.add(db.DayState(day=day, rooms_otb=10, beds_otb=2, otb_updated=now))
            session.commit()

        proposals = []
        for _ in range(5):
            # Even a fresh import timestamp with identical values must not advance a rung.
            with db.get_session() as session:
                service.upsert_inventory(session, [InventoryRow(day, rooms_otb=10, beds_otb=2)])
                session.commit()
            result = service.run_pricing(settings, today=today)
            assert result["status"] == "done"
            with db.get_session() as session:
                rec = session.scalars(select(db.Recommendation).where(
                    db.Recommendation.run_id == result["run_id"])).one()
                proposals.append((rec.room_price, rec.bed_price, rec.room_rung, rec.bed_rung,
                                  rec.forecast_rooms, rec.forecast_beds, rec.flex_to_private,
                                  rec.ladder))
        assert all(p == proposals[0] for p in proposals)

        with db.get_session() as session:
            item = DayInput(day, rooms_otb=69, beds_otb=2)
            changed = service.with_ladder_history(session, [item], today, -1)[0]
            assert changed.prev_room_rung == 8
            rec = price_day(changed, settings.params, today=today)
            assert rec.forecast_rooms > proposals[0][4]
            # Tomorrow's baseline advances to today's last completed proposal.
            tomorrow = service.with_ladder_history(session, [item], today+timedelta(days=1), -1)[0]
            assert tomorrow.prev_room_rung == proposals[-1][2]
            assert tomorrow.prev_room_position == json.loads(proposals[-1][-1])["rooms"]["smoothed_position"]
    finally:
        event.remove(db.Run, "before_insert", freeze_run_start)
        engine.dispose()


def test_daily_baseline_uses_local_midnight_instead_of_utc(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    engine = db.init_db(f"sqlite:///{tmp_path}/midnight.db")
    today = date(2026, 10, 16)
    day = today+timedelta(days=30)
    try:
        with db.get_session() as session:
            for hour, rung in ((21, 2), (22, 5)):
                run = db.Run(status="done", started=datetime(2026, 10, 15, hour, 30))
                session.add(run)
                session.flush()
                session.add(db.Recommendation(run_id=run.id, day=day,
                                               room_rung=rung, bed_rung=rung))
            session.commit()
            item = service.with_ladder_history(session, [DayInput(day)], today, -1)[0]
            assert item.prev_room_rung == 2  # 22:30 UTC was already October 16 locally
            assert item.prev_bed_rung == 2
    finally:
        engine.dispose()

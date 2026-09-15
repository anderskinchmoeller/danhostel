"""Business regressions: displaced beds, historical snapshots, stale data, revenue."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json

import pytest
from sqlalchemy import select

from app import db, service
from app.adapters.base import InventoryRow
from app.adapters.csv_adapter import parse_inventory
from app.calibration import read_reservations, build_units_by_day, booking_curve, suggest_base
from app.config import load_settings
from app.engine import DayInput, Inventory, Params, allocate_flex, price_day, round_within

TODAY = date(2026, 9, 15)


def test_conversion_accounts_for_four_displaced_beds():
    assert allocate_flex(Inventory(), 21, 280, 1000, 270) == 0
    assert allocate_flex(Inventory(), 21, 280, 1100, 270) == 1


def test_committed_beds_and_rooms_are_hard_constraints():
    assert allocate_flex(Inventory(), 36, 280, 2000, 100, beds_otb=280) == 0
    assert allocate_flex(Inventory(), 36, 280, 100, 500, rooms_otb=36) == 16
    with pytest.raises(ValueError, match="samtidigt"):
        price_day(DayInput(TODAY, rooms_otb=36, beds_otb=280), Params(), today=TODAY)


def test_pms_whole_room_limits_apply_over_each_night_of_a_stay():
    for n in range(3):
        r = price_day(DayInput(TODAY + timedelta(days=n), rooms_otb=21, beds_otb=20,
                              flex_private_min=4, flex_private_max=4), Params(), today=TODAY)
        assert r.flex_to_private == 4
    with pytest.raises(ValueError):
        allocate_flex(Inventory(), 30, 100, 1000, 200, private_min=5, private_max=4)


def test_rounding_respects_narrow_interval():
    assert 171 <= round_within(170, 171, 179, 10) <= 179


def test_family_comparison_cannot_override_bed_brake():
    r = price_day(DayInput(TODAY + timedelta(days=30), rooms_otb=30, beds_otb=0,
                          current_room_price=1000, current_bed_price=170), Params(), today=TODAY)
    assert 165 <= r.bed_price <= 170 * 1.15
    assert any("underbyder" in warning for warning in r.warnings)


def test_conflicting_hard_price_limits_freeze_instead_of_breaking_one():
    with pytest.raises(ValueError, match="samtidigt"):
        price_day(DayInput(TODAY, current_bed_price=100), Params(), today=TODAY)


def test_actual_private_bed_count_and_room_mix():
    inv = Inventory(private_rooms=2, flex_rooms=0, dorm_beds=4, private_beds=6,
                    private_room_types={"dobbelt_med_bad": 1, "familie_4": 1}, confirmed=True)
    assert inv.total_beds == 10
    params = Params(inventory=inv)
    item = DayInput(TODAY + timedelta(days=20), rooms_otb=1, beds_otb=2,
                    room_type_otb={"dobbelt_med_bad": 1},
                    booked_room_revenue=500, booked_bed_revenue=300)
    r = price_day(item, params, today=TODAY)
    expected = (800 + (min(r.forecast_rooms, 2) - 1) * r.room_types["familie_4"]
                + (min(r.forecast_beds, 4) - 2) * r.bed_price) / 10
    assert r.revpab == pytest.approx(expected)
    assert r.revenue_basis == "booked_plus_forecast"
    # All bookings are already sold on arrival: changing asking prices changes no revenue.
    for base in (600, 900):
        arrived = price_day(replace(item, day=TODAY), replace(params, bar_base=base), today=TODAY)
        assert arrived.revpab == pytest.approx(80)


def test_incomplete_revenue_is_explicitly_estimated():
    r = price_day(DayInput(TODAY, rooms_otb=10), Params(), today=TODAY)
    assert r.revenue_basis == "estimated"
    assert any("RevPAB er et skøn" in w for w in r.warnings)


def test_confirmed_inventory_requires_actual_beds_and_types():
    with pytest.raises(ValueError, match="Bekræftet"):
        Inventory(confirmed=True)
    with pytest.raises(ValueError, match="summere"):
        Inventory(private_room_types={"dobbelt_med_bad": 3})


def test_inventory_csv_preserves_new_data():
    rows = parse_inventory('dato;solgte_vaerelser;solgte_senge;booked_room_revenue;booked_bed_revenue;flex_private_min;flex_private_max;room_type_otb\n'
                           '2026-09-15;2;10;900;2000;2;4;{"familie_4":2}\n')
    assert rows[0].room_type_otb == {"familie_4": 2}
    assert rows[0].booked_room_revenue == 900
    assert rows[0].flex_private_max == 4


@pytest.mark.parametrize("text", [
    'dato;solgte_vaerelser\n2026-09-15;2',
    'dato;solgte_vaerelser;solgte_senge\n2026-09-15;2;',
    'dato;solgte_vaerelser;solgte_senge\n2026-09-15;-2;3',
    'dato;solgte_vaerelser;solgte_senge\n2026-09-15;2.5;3',
    'dato;solgte_vaerelser;solgte_senge\n2026-09-15;nan;3',
    'dato;solgte_vaerelser;solgte_senge\n2026-09-15;2;3\n2026-09-15;3;4',
])
def test_incomplete_or_invalid_inventory_is_rejected(text):
    with pytest.raises(ValueError):
        parse_inventory(text)


def test_calibration_counts_group_units_and_zero_sales_dates():
    rows, _ = read_reservations('bookingdato;ankomstdato;naetter;pris;enhed;quantity\n'
                                '2026-01-01;2026-01-05;1;200;bed;8\n')
    days = build_units_by_day(rows, "bed", date(2026, 1, 5), date(2026, 1, 7))
    assert len(days[date(2026, 1, 5)]) == 8
    assert days[date(2026, 1, 6)] == []
    assert booking_curve(days, 10)[0]["weekday"] == 0
    assert suggest_base(days, 10) == 200


def test_calibration_reconstructs_cancellations_at_cutoff():
    rows, _ = read_reservations('bookingdato;ankomstdato;naetter;pris;enhed;quantity;status;cancelled_at\n'
        '2026-01-01;2026-01-15;1;600;room;8;cancelled;2026-01-14\n')
    curve = booking_curve(build_units_by_day(rows, "room"), 10)
    assert curve[0]["weekday"] == 0
    assert curve[1]["weekday"] == 0.8  # two days out, booking still existed


@pytest.mark.parametrize("extra", ["cancelled;", "unknown;2026-01-14"])
def test_calibration_rejects_ambiguous_status(extra):
    with pytest.raises(ValueError, match="Linje 2"):
        read_reservations('bookingdato;ankomstdato;enhed;status;cancelled_at\n'
                          f'2026-01-01;2026-01-15;room;{extra}\n')


def test_calibration_never_mistakes_private_four_bed_room_for_one_bed():
    with pytest.raises(ValueError, match="Ukendt enhed"):
        read_reservations('bookingdato;ankomstdato;enhed\n2026-01-01;2026-01-15;4 bed room\n')


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    engine = db.init_db(f"sqlite:///{tmp_path}/regression.db")
    settings = load_settings()
    settings.horizon_days = 5
    yield settings
    engine.dispose()


def test_one_fresh_day_does_not_refresh_other_dates(database):
    now = db.utcnow()
    with db.get_session() as session:
        session.add_all([
            db.DayState(day=TODAY, rooms_otb=5, beds_otb=30, otb_updated=now),
            db.DayState(day=TODAY + timedelta(days=1), rooms_otb=5, beds_otb=30,
                        otb_updated=now - timedelta(hours=40)),
            db.DayState(day=TODAY + timedelta(days=2), comp_room=600),
        ])
        session.commit()
    result = service.run_pricing(database, today=TODAY)
    assert result["n_days"] == 1
    with db.get_session() as session:
        recs = session.scalars(select(db.Recommendation)).all()
        assert [r.day for r in recs] == [TODAY]
        assert "Fastfrosset" in session.get(db.Run, result["run_id"]).note


def test_delivery_checks_freshness_again_and_inventory_confirmation(database):
    now = db.utcnow()
    with db.get_session() as session:
        state = db.DayState(day=TODAY, otb_updated=now)
        run = db.Run(status="done", finished=now)
        rec = db.Recommendation(day=TODAY, run_id=1, status="approved")
        session.add_all([state, run, rec]); session.commit()
        assert "ikke bekræftet" in service.delivery_error(session, database, run, [rec])
        database.params = replace(database.params, inventory=Inventory(
            confirmed=True, private_beds=40, private_room_types={"dobbelt_uden_bad": 20}))
        assert service.delivery_error(session, database, run, [rec]) is None
        state.otb_updated = now - timedelta(hours=40)
        assert "gamle" in service.delivery_error(session, database, run, [rec])
        state.otb_updated = now + timedelta(microseconds=1)
        assert "ændret" in service.delivery_error(session, database, run, [rec])


def test_reimports_do_not_compound_daily_price_changes(database):
    with db.get_session() as session:
        service.upsert_inventory(session, [InventoryRow(TODAY, current_room_price=600,
                                                        current_bed_price=200)])
        session.commit()
        service.upsert_inventory(session, [InventoryRow(TODAY, current_room_price=690,
                                                        current_bed_price=230)])
        session.commit()
        state = session.get(db.DayState, TODAY)
        item = service.build_inputs([state], database)[0]
        assert item.current_room_price == 600
        assert item.current_bed_price == 200
        r = price_day(item, database.params, today=TODAY)
        assert r.room_price <= 690
        assert r.bed_price <= 230


def test_version_two_schema_migration_preserves_data(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    path = tmp_path / 'v2.db'
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE day_state (day DATE PRIMARY KEY, rooms_otb INTEGER)')
        con.execute("INSERT INTO day_state VALUES ('2026-09-15', 12)")
    engine = db.init_db(f'sqlite:///{path}')
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT rooms_otb, booked_room_revenue FROM day_state').fetchone() == (12, None)
    engine.dispose()


def test_booked_family_types_reserve_flex_even_when_fixed_rooms_are_empty():
    inv = Inventory(private_beds=40, private_room_types={"dobbelt_med_bad": 20}, confirmed=True)
    item = DayInput(TODAY, rooms_otb=5, beds_otb=0, room_type_otb={"familie_4": 5})
    r = price_day(item, Params(inventory=inv), today=TODAY)
    assert r.flex_to_private >= 5
    assert r.room_capacity >= r.rooms_otb


def test_uploaded_booked_revenue_reaches_stored_forecast(database):
    database.params = replace(database.params, inventory=Inventory(
        private_rooms=2, flex_rooms=0, dorm_beds=4, private_beds=6,
        private_room_types={"dobbelt_med_bad": 1, "familie_4": 1}, confirmed=True))
    rows = parse_inventory('dato;solgte_vaerelser;solgte_senge;booked_room_revenue;booked_bed_revenue;room_type_otb\n'
                           '2026-09-15;1;2;500;300;{"dobbelt_med_bad":1}\n')
    with db.get_session() as session:
        service.upsert_inventory(session, rows)
        session.commit()
    result = service.run_pricing(database, today=TODAY)
    assert result['status'] == 'done'
    with db.get_session() as session:
        rec = session.scalars(select(db.Recommendation)).one()
        assert rec.revpab == 80
        assert rec.revenue_basis == 'booked_plus_forecast'


def test_manual_prices_recompute_related_prices_and_revenue():
    inv = Inventory(private_rooms=1, flex_rooms=0, dorm_beds=4, private_beds=2,
                    private_room_types={"dobbelt_med_bad": 1}, confirmed=True)
    item = DayInput(TODAY + timedelta(days=30), locked_room_price=999,
                    locked_bed_price=333, booked_room_revenue=0, booked_bed_revenue=0)
    rec = price_day(item, Params(inventory=inv), today=TODAY)
    assert rec.room_types['dobbelt_med_bad'] == 1180
    assert rec.room_types['familie_4'] == 1550
    assert rec.net_room == pytest.approx(999 * .84 - 95)
    assert rec.revpab == pytest.approx((rec.forecast_rooms * 1180 + rec.forecast_beds * 333) / 6)


def test_price_anchor_initializes_when_rates_first_become_available(database):
    with db.get_session() as session:
        service.upsert_inventory(session, [InventoryRow(TODAY)])
        session.commit()
        service.upsert_inventory(session, [InventoryRow(TODAY, current_room_price=600)])
        session.commit()
        service.upsert_inventory(session, [InventoryRow(TODAY, current_room_price=690)])
        session.commit()
        assert service.build_inputs([session.get(db.DayState, TODAY)], database)[0].current_room_price == 600

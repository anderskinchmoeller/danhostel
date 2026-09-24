"""Test af prisstigen.

Stigen skal gøre fire ting: stå stille ved støj, gå op når efterspørgslen
siger det, beskytte kapacitet når huset er ved at blive fyldt, og aldrig
forlade trinene uden at sige det.
"""

from dataclasses import replace
from datetime import date

import pytest

from app.engine import DayInput, EventUplift, Params, price_day
from app.ladder import LadderConfig, ladder_step, sellout_probability

TODAY = date(2026, 9, 15)
CFG = LadderConfig()
LP = Params(ladder=CFG)


def step(**kw):
    base = dict(base=600.0, rungs=CFG.rungs_rooms, rounder=lambda v: round(v / 10) * 10,
                cfg=CFG, forecast=0.85, target=0.85, occ_now=0.4, lead_days=20,
                pickup=None, expected_pickup=None, comp_price=None, quality_index=1.0,
                event_factor=1.0, capacity=36, forecast_units=30.6, previous_rung=None)
    base.update(kw)
    return ladder_step(**base)


def test_neutral_triggers_land_on_reference_rung():
    r = step()
    assert r.rung == r.reference_rung
    assert r.price == 600


def test_strong_forecast_climbs():
    assert step(forecast=1.0, forecast_units=36).rung > step().rung


def test_weak_forecast_descends():
    assert step(forecast=0.50, forecast_units=18).rung < step().rung


def test_rung_prices_increase():
    prices = step().rung_prices
    assert prices == sorted(prices)
    assert len(prices) == 9


def test_hysteresis_holds_on_small_pressure():
    # Lidt over grænsen til næste trin, men ikke over hysteresebåndet
    r = step(forecast=0.85 + 0.04, previous_rung=3)
    assert r.rung == 3


def test_step_limits_up_two_down_one():
    up = step(forecast=1.0, forecast_units=36, comp_price=900, previous_rung=3,
              pickup=0.3, expected_pickup=0.05)
    assert up.rung <= 5
    down = step(forecast=0.2, forecast_units=7, previous_rung=6, comp_price=400)
    assert down.rung == 5


def test_close_in_may_drop_two():
    down = step(forecast=0.2, forecast_units=7, previous_rung=6, lead_days=2, comp_price=400)
    assert down.rung == 4


def test_ratchet_never_drops_on_track_date():
    # Prognosen er over målet, men markedet er billigt: ingen nedtur
    r = step(forecast=0.90, previous_rung=6, comp_price=420)
    assert r.rung == 6
    assert any("Holdt" in x for x in r.reasons)


def test_sellout_protection_closes_bottom_rungs():
    r = step(forecast=1.0, forecast_units=36, capacity=36, lead_days=2,
             comp_price=420, previous_rung=None)
    assert r.p_sellout >= 0.5
    assert r.rung >= r.reference_rung + 2


def test_sellout_probability_grows_with_certainty():
    far = sellout_probability(34, 36, 60, CFG)
    near = sellout_probability(34, 36, 1, CFG)
    assert 0 < far < 1 and 0 < near < 1
    assert sellout_probability(10, 36, 1, CFG) < 0.01


def test_far_out_no_deep_discount():
    r = step(forecast=0.3, forecast_units=10, lead_days=90)
    assert r.rung >= r.reference_rung - 1


def test_event_adds_rungs_and_floors_at_reference():
    r = step(forecast=0.5, forecast_units=18, event_factor=1.25)
    assert r.rung >= r.reference_rung
    assert r.triggers["event_trin"] == 2


def test_pace_noise_is_ignored_on_small_numbers():
    # Én booking over det forventede på en stille dag er ikke et signal
    quiet = step(pickup=2 / 36, expected_pickup=1 / 36)
    assert abs(quiet.triggers["tempo"]) < 0.25
    busy = step(pickup=20 / 36, expected_pickup=8 / 36)
    assert busy.triggers["tempo"] > 0.9


def test_smoothing_needs_two_days_of_pressure():
    # Prognosen springer fra mål til fuldt hus på én dag. Uden udglatning går
    # stigen de to trin op den må; med udglatning kun det ene.
    kw = dict(forecast=1.0, forecast_units=25, previous_rung=3)
    assert step(**kw).rung == 5
    assert step(**kw, previous_position=3.0).rung == 4


def test_config_validation():
    with pytest.raises(ValueError):
        LadderConfig(rungs_rooms=(0.9, 1.1, 1.2, 1.3, 1.4))       # intet 1,00-trin
    with pytest.raises(ValueError):
        LadderConfig(rungs_rooms=(0.8, 1.0, 0.9, 1.2, 1.3))       # ikke stigende
    with pytest.raises(ValueError):
        LadderConfig(w_forecast=0.9)                             # vægte summerer ikke


# -- gennem hele motoren ----------------------------------------------------

def test_engine_prices_on_the_ladder():
    rec = price_day(DayInput(date(2026, 10, 14), rooms_otb=10, beds_otb=60,
                             comp_room=650, comp_bed=285), LP, today=TODAY)
    assert rec.room_ladder and rec.bed_ladder
    assert rec.room_price in rec.room_ladder["rung_prices"]
    assert rec.bed_price in rec.bed_ladder["rung_prices"]


def test_brake_snaps_to_a_rung():
    rec = price_day(DayInput(date(2026, 10, 14), rooms_otb=30, beds_otb=150,
                             comp_room=900, comp_bed=400, current_room_price=500,
                             current_bed_price=200), LP, today=TODAY)
    assert rec.room_price <= 500 * 1.15
    assert rec.room_price in rec.room_ladder["rung_prices"] or rec.room_ladder["off_ladder"]


def test_locked_price_is_respected_and_recorded():
    rec = price_day(DayInput(date(2026, 10, 14), rooms_otb=10, locked_room_price=777,
                             comp_room=650), LP, today=TODAY)
    assert rec.room_price == 777
    assert rec.room_ladder["off_ladder"]


def test_event_lifts_ladder():
    day = date(2026, 10, 14)
    item = DayInput(day, rooms_otb=10, beds_otb=60, comp_room=650, comp_bed=285)
    plain = price_day(item, LP, today=TODAY)
    ev = price_day(item, LP, [EventUplift(day, day, "Koncert", 0.2)], today=TODAY)
    assert ev.room_rung > plain.room_rung


def test_factor_model_untouched_when_ladder_off():
    item = DayInput(date(2026, 10, 14), rooms_otb=10, comp_room=650, comp_bed=285)
    assert price_day(item, Params(), today=TODAY).room_ladder is None


def test_ladder_is_stable_under_repeated_runs():
    """Samme data to dage i træk må ikke flytte prisen."""
    day = date(2026, 10, 14)
    item = DayInput(day, rooms_otb=12, beds_otb=70, comp_room=650, comp_bed=285)
    first = price_day(item, LP, today=TODAY)
    again = price_day(replace(item, prev_room_rung=first.room_rung,
                              prev_bed_rung=first.bed_rung,
                              prev_room_position=first.room_ladder["smoothed_position"],
                              prev_bed_position=first.bed_ladder["smoothed_position"],
                              current_room_price=first.room_price,
                              current_bed_price=first.bed_price), LP, today=TODAY)
    assert again.room_price == first.room_price
    assert again.bed_price == first.bed_price

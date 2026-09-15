"""Test af prismodellen.

De vigtigste tests er dem der beskytter de tre ting version 2 handler om:
prognose frem for reaktion, to lagre i stedet for ét, og flex-rummene som en
beslutning frem for en bremse.
"""

from datetime import date

import pytest

from app.engine import (
    BookingCurve, DayInput, EventUplift, Inventory, Params,
    allocate_flex, base_prices, forecast_occupancy, group_quote,
    price_day, price_range, quality_index, round_to, round_within,
)

TODAY = date(2026, 9, 15)


@pytest.fixture
def params():
    return Params()


def rec(day, rooms=0, beds=0, comp_room=650, comp_bed=285, params=None, **kw):
    params = params or Params()
    return price_day(
        DayInput(day, rooms_otb=rooms, beds_otb=beds,
                 comp_room=comp_room, comp_bed=comp_bed, **kw),
        params, today=TODAY,
    )


# -- lager -----------------------------------------------------------------

def test_inventory_adds_up():
    inv = Inventory(private_rooms=20, flex_rooms=16, beds_per_flex_room=4, dorm_beds=216)
    assert inv.max_room_capacity == 36
    assert inv.max_bed_capacity == 280
    # nævneren i RevPAB er konstant, uanset hvordan flex-rummene fordeles
    assert inv.total_beds == 320


# -- grundpriser -----------------------------------------------------------

def test_base_prices_follow_season_and_weekday(params):
    room, bed = base_prices(date(2026, 9, 18), params)   # fredag i september
    assert room == round(645 * 1.05 * 1.08)
    assert bed == round(270 * 1.05 * 1.05)
    # senge er mindre weekendfølsomme end værelser
    assert params.weekday[4] > params.weekday_beds[4]


def test_rounding():
    assert round_to(731.4, 10) == 730
    assert round_to(736.0, 10) == 740


def test_rounding_never_breaks_a_floor():
    """165 rundet til nærmeste ti bliver 160 — fem kroner under gulvet, hver nat."""
    assert round_within(165, 165, 600, 10) == 170
    assert round_within(596, 165, 600, 10) == 600   # 600 er inden for loftet
    assert round_within(604, 165, 600, 10) == 600   # 610 ville bryde det
    assert round_within(412, 165, 600, 10) == 410


# -- prognose --------------------------------------------------------------

def test_forecast_is_above_todays_occupancy(params):
    curve = params.booking_curve
    assert forecast_occupancy(0.20, 40, False, curve) > 0.20
    # ved ankomst er prognosen lig det der står på bøgerne
    assert forecast_occupancy(0.61, 0, False, curve) == pytest.approx(0.61)


def test_forecast_is_capped_at_full_house(params):
    assert forecast_occupancy(0.95, 30, True, params.booking_curve) <= 1.0


def test_same_occupancy_prices_opposite_by_lead_time(params):
    """Hele pointen med version 2: 20 % solgt betyder to modsatte ting."""
    far = rec(date(2026, 10, 25), rooms=7, beds=20, params=params)
    near = rec(date(2026, 9, 17), rooms=7, beds=20, params=params)
    assert far.room_occ_now == pytest.approx(near.room_occ_now)
    assert far.room_forecast > near.room_forecast + 0.4
    assert far.room_price > near.room_price
    assert far.f_pace_rooms > near.f_pace_rooms


def test_weak_date_far_out_does_not_get_a_price_cut(params):
    """Version 1's fejl: den skar prisen på en dato der var helt normal."""
    r = rec(date(2026, 10, 25), rooms=7, beds=20, params=params)
    assert r.room_forecast > 0.80
    assert r.f_pace_rooms >= 1.0


# -- to lagre --------------------------------------------------------------

def test_beds_and_rooms_are_priced_independently(params):
    """Sengene er ved at være udsolgt, værelserne halter. Sengeprisen skal op,
    værelsesprisen ikke."""
    r = rec(date(2026, 10, 3), rooms=12, beds=120, params=params)
    assert r.bed_forecast > r.room_forecast
    assert r.f_pace_beds > r.f_pace_rooms


def test_bed_curve_differs_from_room_curve(params):
    """Senge bookes senere. Samme belægning 30 dage ude betyder derfor mere
    for senge end for værelser."""
    assert params.booking_curve_beds.reference(30, False) < params.booking_curve.reference(30, False)


def test_missing_comp_prices_fall_back_without_breaking(params):
    r = rec(date(2026, 10, 20), rooms=12, beds=90, comp_room=None, comp_bed=None, params=params)
    assert r.f_market_rooms == 1.0 and r.f_market_beds == 1.0
    assert any("værelser" in w for w in r.warnings)
    assert r.room_price > 0 and r.bed_price > 0


def test_market_factor_is_damped_not_copied(params):
    """Konkurrenten halverer prisen. Modellen følger kun et stykke af vejen —
    og for senge endnu mindre end for værelser, fordi produkterne ikke er
    sammenlignelige."""
    r = rec(date(2026, 10, 20), rooms=12, beds=90, comp_room=300, comp_bed=140, params=params)
    assert r.f_market_rooms == params.market_min
    assert r.room_price > 300


# -- flex-allokering -------------------------------------------------------

def test_flex_goes_to_beds_when_beds_are_in_demand():
    inv = Inventory(private_rooms=20, flex_rooms=16, beds_per_flex_room=4, dorm_beds=216)
    # ingen efterspørgsel på værelser, masser på senge
    assert allocate_flex(inv, forecast_rooms=5, forecast_beds=280, family_price=1000, bed_price=300) == 0


def test_flex_goes_to_private_when_rooms_are_in_demand():
    inv = Inventory(private_rooms=20, flex_rooms=16, beds_per_flex_room=4, dorm_beds=216)
    got = allocate_flex(inv, forecast_rooms=36, forecast_beds=100, family_price=1400, bed_price=180)
    assert got >= 10


def test_allocation_shifts_capacity_between_the_two_pools(params):
    busy_rooms = rec(date(2026, 10, 20), rooms=20, beds=30, params=params)
    busy_beds = rec(date(2026, 10, 20), rooms=2, beds=200, params=params)
    assert busy_rooms.flex_to_private > busy_beds.flex_to_private
    assert busy_rooms.room_capacity > busy_beds.room_capacity
    assert busy_rooms.bed_capacity < busy_beds.bed_capacity


def test_capacities_never_exceed_the_house(params):
    inv = params.inventory
    for r in price_range(
        [DayInput(date(2026, 10, 1), rooms_otb=n, beds_otb=280 - n * 4, comp_room=650, comp_bed=285)
         for n in range(0, 36, 5)],
        params, today=TODAY,
    ):
        assert r.room_capacity <= inv.max_room_capacity
        assert r.bed_capacity <= inv.max_bed_capacity
        assert r.room_capacity + r.bed_capacity / inv.beds_per_flex_room <= \
            inv.max_room_capacity + inv.dorm_beds / inv.beds_per_flex_room + 0.01


# -- RevPAB ----------------------------------------------------------------

def test_revpab_rises_with_demand(params):
    quiet = rec(date(2026, 10, 20), rooms=3, beds=20, params=params)
    busy = rec(date(2026, 10, 20), rooms=30, beds=220, params=params)
    assert busy.revpab > quiet.revpab


def test_revpab_uses_a_constant_denominator(params):
    """Man må ikke kunne pynte på RevPAB ved at flytte lager rundt."""
    a = rec(date(2026, 10, 20), rooms=25, beds=60, params=params)
    b = rec(date(2026, 10, 20), rooms=5, beds=240, params=params)
    assert a.flex_to_private != b.flex_to_private
    for r in (a, b):
        room_rate = (20 * r.room_price + r.flex_to_private * r.room_types["familie_4"]) / (20 + r.flex_to_private)
        expected = (min(r.forecast_rooms, r.room_capacity) * room_rate
                    + min(r.forecast_beds, r.bed_capacity) * r.bed_price) / params.inventory.total_beds
        assert r.revpab == pytest.approx(expected)


# -- guardrails ------------------------------------------------------------

def test_floors_and_ceilings_hold_for_both_pools(params):
    low = rec(date(2027, 1, 13), rooms=0, beds=0, comp_room=200, comp_bed=80, params=params)
    assert low.room_price >= params.price_floor
    assert low.bed_price >= params.bed_floor
    high = rec(date(2027, 7, 11), rooms=36, beds=216, comp_room=4000, comp_bed=2000, params=params)
    assert high.room_price <= params.price_ceiling
    assert high.bed_price <= params.bed_ceiling


def test_daily_change_brake_applies_to_both_prices(params):
    r = rec(date(2027, 7, 11), rooms=34, beds=224, comp_room=1200, comp_bed=500,
            params=params, current_room_price=600, current_bed_price=250)
    assert r.room_price <= 600 * 1.15 + 1
    assert r.bed_price <= 250 * 1.15 + 1
    assert any("Ændringsbremse" in w for w in r.warnings)


def test_beds_never_undercut_the_family_room(params):
    for rooms, beds in [(2, 260), (30, 20), (18, 140), (0, 0), (36, 216)]:
        r = rec(date(2026, 10, 20), rooms=rooms, beds=beds, params=params)
        family = r.room_types["familie_4"]
        assert r.bed_price * params.inventory.beds_per_flex_room >= family * 0.9 - 0.01 \
            or any("underbyder" in w for w in r.warnings)


def test_events_stack_and_are_capped(params):
    events = [
        EventUplift(date(2026, 10, 10), date(2026, 10, 18), "Efterårsferie", 0.10),
        EventUplift(date(2026, 10, 17), date(2026, 10, 17), "Koncert", 0.15),
    ]
    one = price_day(DayInput(date(2026, 10, 12), 12, 100, 650, 285), params, events, TODAY)
    two = price_day(DayInput(date(2026, 10, 17), 12, 100, 650, 285), params, events, TODAY)
    assert one.f_event == pytest.approx(1.10)
    assert two.f_event == pytest.approx(1.25)

    absurd = [EventUplift(date(2026, 10, 12), date(2026, 10, 12), "Tastefejl", 5.0)]
    capped = price_day(DayInput(date(2026, 10, 12), 12, 100, 650, 285), params, absurd, TODAY)
    assert capped.f_event == params.event_max


def test_blocked_units_reduce_capacity(params):
    free = rec(date(2026, 10, 20), rooms=18, beds=140, params=params)
    blocked = rec(date(2026, 10, 20), rooms=18, beds=140, params=params,
                  blocked_rooms=8, blocked_beds=40)
    assert blocked.room_occ_now > free.room_occ_now
    assert blocked.room_forecast >= free.room_forecast


def test_net_uses_the_right_variable_cost(params):
    r = rec(date(2026, 10, 20), rooms=15, beds=120, params=params)
    assert r.net_room == pytest.approx(r.room_price * (1 - params.commission) - params.variable_cost)
    assert r.net_bed == pytest.approx(r.bed_price * (1 - params.commission) - params.variable_cost_bed)


# -- gruppeforespørgsler ---------------------------------------------------

def test_group_quote_is_cheap_when_the_date_will_stay_empty(params):
    """Tæt på ankomst og næsten intet solgt: gruppen fortrænger ingenting."""
    recs = [rec(date(2026, 9, 17), rooms=1, beds=5, params=params)]
    quote = group_quote(recs, rooms_requested=10, params=params)[0]
    assert quote.displaced_rooms == 0
    assert params.price_floor <= quote.minimum_rate < params.price_floor + params.rounding


def test_forecast_reverts_to_the_curve_without_signal(params):
    """Langt ude og næsten ingen bookinger: prognosen er sæsonens normal, ikke nul.

    Det er med vilje. En pickup-prognose uden signal skal falde tilbage på
    historikken — ellers ville hver dato 90 dage ude se ud som en katastrofe og
    udløse panikrabatter.
    """
    r = rec(date(2026, 11, 20), rooms=1, beds=5, params=params)
    assert r.room_occ_now < 0.05
    assert r.room_forecast > 0.5


def test_group_quote_approaches_normal_price_when_full(params):
    recs = [rec(date(2026, 9, 19), rooms=30, beds=220, params=params)]
    quote = group_quote(recs, rooms_requested=15, params=params)[0]
    assert quote.displaced_rooms > 10
    assert quote.minimum_rate > params.price_floor * 1.3


def test_group_quote_never_goes_below_the_floor(params):
    recs = price_range(
        [DayInput(date(2026, 11, 20), rooms_otb=0, beds_otb=0)], params, today=TODAY)
    for q in group_quote(recs, rooms_requested=30, params=params):
        assert q.minimum_rate >= params.price_floor


# -- kvalitetsindeks -------------------------------------------------------

def test_quality_index_follows_cornell():
    assert quality_index(8.0, [(0.5, 7.5), (0.5, 7.5)]) == pytest.approx(1.0445, abs=1e-4)
    assert quality_index(8.0, [(1.0, 8.0)]) == pytest.approx(1.0)
    assert quality_index(10.0, [(1.0, 1.0)]) == 1.15
    assert quality_index(1.0, [(1.0, 10.0)]) == 0.85


def test_quality_index_shifts_both_prices():
    cheap = Params(quality_index=0.90)
    dear = Params(quality_index=1.10)
    a = rec(date(2026, 10, 20), rooms=12, beds=100, params=cheap)
    b = rec(date(2026, 10, 20), rooms=12, beds=100, params=dear)
    assert b.room_price > a.room_price
    assert b.bed_price >= a.bed_price


# -- serier ----------------------------------------------------------------

def test_price_range_keeps_order_and_respects_floors(params):
    days = [DayInput(date(2026, 9, 16) + __import__("datetime").timedelta(days=i),
                     rooms_otb=10, beds_otb=80, comp_room=650, comp_bed=285)
            for i in range(30)]
    recs = price_range(days, params, today=TODAY)
    assert [r.day for r in recs] == [d.day for d in days]
    assert all(r.room_price >= params.price_floor for r in recs)
    assert all(r.bed_price >= params.bed_floor for r in recs)


def test_explain_mentions_both_pools(params):
    text = rec(date(2026, 10, 20), rooms=12, beds=100, params=params).explain()
    assert "seng" in text and "RevPAB" in text and "prognose" in text

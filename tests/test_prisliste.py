"""Prislisten skal passe til Picassos rumtyper (filen rooms, 28-09-2026)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.config import load_settings
from app.engine import DayInput, Inventory, price_day

TODAY = date(2026, 9, 28)

# Picassos rumtyper: hele rum og sengekoder (B-typer), med antal enheder
ROOMS = {"D1": 1, "D2": 13, "D4": 8, "F1": 17, "F3": 11, "FS": 1, "V8": 6, "V6": 7, "V10": 6}
BEDS = {"BD1": 6, "BD2": 16, "BD3": 8, "BD4": 16, "BF1": 64, "BF3": 40, "BFS": 8,
        "BSK4": 4, "BV6": 42, "BV8": 48, "BV10": 60}


@pytest.fixture
def params():
    return load_settings().params


def test_prislisten_har_praecis_picassos_koder(params):
    assert list(params.room_types) == list(ROOMS)
    assert list(params.bed_types) == list(BEDS)


def test_lageret_matcher_rumlisten(params):
    inv = params.inventory
    assert {**inv.private_room_types, **inv.flex_room_types} == ROOMS
    # Sovesalene: senge pr. rum x antal rum = sengekoderne
    for code, beds_per_room in {"V6": 6, "V8": 8, "V10": 10, "FS": 8}.items():
        assert inv.flex_room_types[code] * beds_per_room == BEDS["B" + code]
    assert inv.dorm_beds == BEDS["BSK4"]


def test_hver_kode_faar_en_pris(params):
    r = price_day(DayInput(TODAY + timedelta(days=20)), params, today=TODAY)
    assert set(r.room_types) == set(ROOMS) | set(BEDS)
    assert r.room_types["D2"] == r.room_price
    assert all(p > 0 for p in r.room_types.values())
    # Sovesale solgt samlet koster mere jo større de er
    assert r.room_types["V6"] < r.room_types["V8"] < r.room_types["V10"]


def test_bookede_sovesale_reserverer_flex_og_respekterer_antal(params):
    item = DayInput(TODAY + timedelta(days=5), rooms_otb=3, beds_otb=0,
                    room_type_otb={"V6": 2, "V10": 1})
    r = price_day(item, params, today=TODAY)
    assert r.flex_to_private >= 3
    with pytest.raises(ValueError):
        price_day(DayInput(TODAY, rooms_otb=8, beds_otb=0, room_type_otb={"V6": 8}),
                  params, today=TODAY)


def test_flex_typer_skal_summere_til_flex_rooms():
    with pytest.raises(ValueError):
        Inventory(flex_rooms=20, flex_room_types={"V6": 7, "V8": 6})

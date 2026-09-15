"""Rimelighedstjek på uploadet belægning.

Det scenarie der bærer hele modulet er det fra risikovurderingen: Picasso-
eksporten mister sengekolonnen ved en rutineopdatering, servicen kører
igennem uden en lyd, og 64 senge tages af markedet på en aften hvor de var i
høj kurs. Den fil skal stoppes, ikke prissættes.
"""

import csv
import io
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters import parse_inventory
from app.config import load_settings
from app.sanity import check_inventory

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def params():
    return load_settings().params


def _rows(records):
    """Byg en CSV og parse den, så tjekket ser præcis det upload ser."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["dato", "solgte_vaerelser", "solgte_senge",
                     "blokerede_vaerelser", "blokerede_senge",
                     "vaerelsespris", "sengepris"])
    writer.writerows(records)
    return parse_inventory(buffer.getvalue())


def _normal(n=30, start=None, **overrides):
    first = start or date.today() + timedelta(days=1)
    out = []
    for i in range(n):
        day = first + timedelta(days=i)
        out.append([day.isoformat(),
                    overrides.get("rooms", 24),
                    overrides.get("beds", 148),
                    0, 0, 690, 280])
    return out


def codes(findings):
    return {f.code for f in findings}


# --------------------------------------------------------------------------

def test_normal_file_passes(params):
    assert check_inventory(_rows(_normal()), params) == []


def test_real_sample_passes(params):
    text = (ROOT / "samples" / "belaegning_eksempel.csv").read_text(encoding="utf-8-sig")
    assert check_inventory(parse_inventory(text), params) == []


def test_missing_bed_column_is_stopped(params):
    """Scenariet fra risikovurderingen: sengekolonnen ryger, alt læses som nul."""
    findings = check_inventory(_rows(_normal(beds=0)), params)
    assert "zero_beds_otb" in codes(findings)


def test_a_genuinely_empty_week_is_not_flagged(params):
    """Under en uge må gerne være nul hele vejen — lavsæson findes."""
    assert "zero_beds_otb" not in codes(check_inventory(_rows(_normal(n=5, beds=0)), params))


def test_more_sold_than_the_house_has(params):
    findings = check_inventory(_rows(_normal(rooms=400)), params)
    assert "over_capacity_rooms_otb" in codes(findings)


def test_occupancy_jump_is_flagged(params):
    records = _normal(n=10)
    records[5][2] = 0  # sengesalget forsvinder på én dag
    assert "jump_beds" in codes(check_inventory(_rows(records), params))


def test_price_in_wrong_unit_is_flagged(params):
    records = _normal(n=10)
    records[3][5] = 69000  # ører i stedet for kroner
    assert "price_range_current_room_price" in codes(check_inventory(_rows(records), params))


def test_swapped_day_and_month_is_flagged(params):
    records = _normal(n=10, start=date.today() - timedelta(days=400))
    assert "date_range" in codes(check_inventory(_rows(records), params))


def test_empty_file(params):
    assert codes(check_inventory([], params)) == {"empty"}


# --------------------------------------------------------------------------
# Gennem selve upload-fladen
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    tmp = tempfile.mkdtemp()
    os.environ["RMS_DATABASE_URL"] = f"sqlite:///{tmp}/sanity.db"
    os.environ.pop("RMS_USER", None)
    from app import main
    with TestClient(main.app) as c:
        yield c


def _post(client, records, override=False):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["dato", "solgte_vaerelser", "solgte_senge",
                     "blokerede_vaerelser", "blokerede_senge",
                     "vaerelsespris", "sengepris"])
    writer.writerows(records)
    data = {"kind": "inventory"}
    if override:
        data["override"] = "1"
    return client.post("/data/upload", data=data,
                       files={"file": ("eksport.csv", buffer.getvalue().encode(), "text/csv")},
                       follow_redirects=False)


# Eget datovindue, så disse to tests hverken ser eller forstyrrer
# belægningen de øvrige moduler har uploadet.
FAR = date.today() + timedelta(days=300)


def _day_state(day):
    from app import db
    session = db.get_session()
    try:
        return session.get(db.DayState, day)
    finally:
        session.close()


def test_upload_is_blocked_and_nothing_is_imported(client):
    response = _post(client, _normal(n=30, start=FAR, beds=0))
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert _day_state(FAR) is None, "en stoppet import må ikke skrive noget i basen"


def test_upload_can_be_overridden_by_a_human(client):
    response = _post(client, _normal(n=30, start=FAR, beds=0), override=True)
    assert response.status_code == 303
    assert "message=" in response.headers["location"]
    state = _day_state(FAR)
    assert state is not None and state.beds_otb == 0

"""Røgtest af hele servicen: upload, kørsel, allokering, godkendelse, eksport.

Kører mod en midlertidig database, så rigtige data ikke røres.
"""

import os
import csv
import io
from datetime import date, timedelta
from dataclasses import replace
import tempfile
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.mkdtemp()
    os.environ["RMS_DATABASE_URL"] = f"sqlite:///{tmp}/test.db"
    os.environ.pop("RMS_USER", None)
    from app import main  # importeres efter miljøvariablen er sat
    with TestClient(main.app) as c:
        yield c


def _upload(client, kind, path):
    # Keep sample scenarios reproducible regardless of today's calendar date.
    rows = list(csv.reader((ROOT / path).read_text().splitlines(), delimiter=";"))
    offset = date.today() - date(2026, 9, 15)
    for row in rows[1:]:
        row[0] = (date.fromisoformat(row[0]) + offset).isoformat()
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=";").writerows(rows)
    return client.post("/data/upload", data={"kind": kind},
        files={"file": (Path(path).name, buffer.getvalue().encode(), "text/csv")},
        follow_redirects=False)


def test_health_starts_in_shadow_mode(client):
    assert client.get("/health").json()["auto_publish"] is False


def test_upload_run_and_approve(client):
    assert _upload(client, "inventory", "samples/belaegning_eksempel.csv").status_code == 303
    assert _upload(client, "comp", "samples/konkurrentpriser_eksempel.csv").status_code == 303
    assert client.post("/run", follow_redirects=False).status_code == 303

    payload = client.get("/api/prices?days=30").json()
    assert payload["run"]["status"] == "done"
    assert payload["run"]["avg_revpab"] > 0
    assert payload["inventory"]["total_beds"] == 320
    assert len(payload["prices"]) >= 25

    first = payload["prices"][0]
    assert first["room_price"] > 0 and first["bed_price"] > 0
    assert "familie_4" in first["room_types"]
    assert first["forecast"]["rooms"] >= first["forecast"]["rooms_now"]
    assert first["status"] == "pending"

    page = client.get("/")
    assert page.status_code == 200 and "Anbefalede priser" in page.text

    client.post("/decide", data={"action": "approve", "scope": "all"}, follow_redirects=False)
    after = client.get("/api/prices?days=30").json()
    assert all(p["status"] == "approved" for p in after["prices"])

    # Example inventory may be reviewed, but cannot be delivered as live prices.
    assert client.get("/export.csv").status_code == 409
    assert client.get("/export.csv?only_approved=false").status_code == 409
    from app import main
    from app.engine import Inventory
    original = main.settings.params
    main.settings.params = replace(original, inventory=Inventory(
        confirmed=True, private_beds=40, private_room_types={"dobbelt_uden_bad": 20}))
    try:
        export = client.get("/export.csv")
        assert export.status_code == 200
        assert "dato;vaerelsespris;sengepris" in export.text
    finally:
        main.settings.params = original


def test_both_pools_are_priced_and_allocated(client):
    prices = client.get("/api/prices?days=120").json()["prices"]
    assert all(p["room_price"] > 0 and p["bed_price"] > 0 for p in prices)
    # flex-allokeringen skal variere over horisonten, ellers træffer den ingen beslutning
    allocations = {p["allocation"]["flex_to_private"] for p in prices}
    assert len(allocations) > 1
    for p in prices:
        assert 0 <= p["allocation"]["flex_to_private"] <= 16
        assert p["allocation"]["room_capacity"] <= 36
        assert p["allocation"]["bed_capacity"] <= 280


def test_forecast_beats_naive_occupancy_far_out(client):
    """Datoer langt ude har lav belægning men høj prognose — og derfor ikke
    bundpriser. Det er hele forskellen fra version 1."""
    prices = client.get("/api/prices?days=120").json()["prices"]
    far = [p for p in prices if p["forecast"]["rooms_now"] < 0.25][-5:]
    assert far, "eksempeldata burde indeholde datoer langt ude"
    for p in far:
        assert p["forecast"]["rooms"] > p["forecast"]["rooms_now"]


def test_lock_keeps_a_date_out_of_the_model(client):
    day = client.get("/api/prices?days=30").json()["prices"][3]["date"]
    client.post("/lock", data={"day": day, "room_price": "999", "bed_price": "333"},
                follow_redirects=False)
    client.post("/run", follow_redirects=False)
    prices = {p["date"]: p for p in client.get("/api/prices?days=30").json()["prices"]}
    assert prices[day]["status"] == "locked"
    assert prices[day]["room_price"] == 999
    assert prices[day]["bed_price"] == 333


def test_events_lift_the_price(client):
    prices = client.get("/api/prices?days=60").json()["prices"]
    target = prices[40]
    client.post("/events/add", data={
        "name": "Testkoncert", "start": target["date"], "end": target["date"],
        "uplift": "0,20", "source": "test",
    }, follow_redirects=False)
    client.post("/run", follow_redirects=False)
    after = {p["date"]: p for p in client.get("/api/prices?days=60").json()["prices"]}[target["date"]]
    assert after["factors"]["event"] == pytest.approx(1.20)


def test_group_quote_endpoint(client):
    start = client.get("/api/prices?days=60").json()["prices"][30]["date"]
    body = client.get(f"/api/group-quote?rooms=12&start={start}&nights=2").json()
    assert body["nights"] == 2
    assert len(body["dates"]) == 2
    assert body["minimum_total"] > 0
    assert body["minimum_total"] <= body["transient_total"]
    for d in body["dates"]:
        assert d["minimum_rate"] >= 375


def test_pages_render(client):
    for path in ("/data", "/events", "/settings", "/grupper"):
        assert client.get(path).status_code == 200
    assert client.get("/grupper?rooms=10&start=2026-10-20&nights=2").status_code == 200


def test_malformed_csv_is_rejected_with_a_useful_message(client):
    resp = client.post(
        "/data/upload",
        data={"kind": "inventory"},
        files={"file": ("junk.csv", b"foo;bar\n1;2\n", "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Mangler datokolonne" in unquote(resp.headers["location"])


def test_v1_database_is_refused_clearly(tmp_path):
    """En gammel base skal fejle med en forklaring, ikke med en stacktrace."""
    import sqlite3

    from app import db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE day_state (day DATE PRIMARY KEY, capacity INT, otb INT)")
    con.commit()
    con.close()

    with pytest.raises(db.SchemaMismatch) as exc:
        db.init_db(f"sqlite:///{path}")
    assert "version 1" in str(exc.value)

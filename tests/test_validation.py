"""Evidence must preserve unknowns, provenance and actual observation times."""
from datetime import date, datetime
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, picasso_belaegning as pdf, validation


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    monkeypatch.setattr(validation, "PDF_ARCHIVE_ROOT", tmp_path / "pdfs")
    engine = db.init_db(f"sqlite:///{tmp_path}/validation.db")
    with db.get_session() as s:
        yield s
    engine.dispose()


def test_price_confirmation_links_original_recommendation_and_keeps_revisions(session):
    day = date(2026, 10, 16)
    run = db.Run(status="done", started=datetime(2026, 10, 15), finished=datetime(2026, 10, 15, 1))
    session.add(run); session.flush()
    rec = db.Recommendation(run_id=run.id, day=day, room_price=680, bed_price=320, status="approved")
    session.add(rec); session.flush()
    assert not session.scalars(select(db.ValidationRecord)).all()
    raw = {"day": day.isoformat(), "observed_at": "2026-10-16T07:00", "run_id": str(run.id),
           "baseline_room": "720", "applied_room": "700", "applied_bed": "320"}
    prepared = validation.prepare("applied", raw, "Europe/Copenhagen")
    assert validation.record(session, "applied", prepared, actor="Anders", source="manual")
    assert not validation.record(session, "applied", prepared, actor="Anders", source="same via CSV")
    first = session.scalars(select(db.ValidationRecord)).one()
    assert first.observed_at == datetime(2026, 10, 16, 5)
    data = json.loads(first.payload)
    assert data["model_room"] == 680 and data["manual_override"] is True
    assert "baseline_bed" not in data
    rec.room_price = 990  # historical comparison retains the original price
    assert json.loads(first.payload)["model_room"] == 680
    changed = validation.prepare("applied", {**raw, "applied_room": "710"}, "Europe/Copenhagen")
    assert validation.record(session, "applied", changed, actor="Anders", source="correction")
    assert len(session.scalars(select(db.ValidationRecord)).all()) == 2
    assert validation.overview(session, day, day)[0]["applied"]["data"]["applied_room"] == 710


def test_unknown_costs_are_not_estimated_and_zero_is_preserved():
    raw = {"day": "2026-10-15", "observed_at": "2026-10-16T07:00", "final": "ja",
           "final_rooms": "10", "final_beds": "0", "room_revenue": "5000", "bed_revenue": 0}
    _, _, p = validation.prepare("outcome", raw, "Europe/Copenhagen")
    assert p["bed_revenue"] == 0
    assert validation.contribution(p) == (None, None)
    p.update(commission=0, variable_cost=1000, available_beds=100)
    assert validation.contribution(p) == (4000, 40)
    p["available_beds"] = 0
    assert validation.contribution(p) == (4000, None)
    p["final"] = False
    assert validation.contribution(p) == (None, None)


@pytest.mark.parametrize("extra", [
    {"new_rooms": "2"},
    {"new_rooms": "2", "window_start": "2026-10-16T09:00", "window_end": "2026-10-16T08:00"},
    {"new_rooms": "2", "window_start": "2026-10-15T08:00", "window_end": "2026-10-16T09:00"},
    {"final": "ja", "final_rooms": "2"}, {"commission": "NaN"}, {"final_rooms": "1.5"},
])
def test_invalid_outcomes_are_rejected(extra):
    with pytest.raises(ValueError):
        validation.prepare("outcome", {"day": "2026-10-16", "observed_at": "2026-10-16T08:00", **extra}, "Europe/Copenhagen")


def test_booking_flow_requires_explicit_window_and_utc_is_unambiguous():
    raw = {"day": "2026-10-20", "observed_at": "2026-10-16T08:00", "new_rooms": "2",
           "cancelled_rooms": "1", "window_start": "2026-10-15T08:00", "window_end": "2026-10-16T08:00"}
    _, _, p = validation.prepare("outcome", raw, "Europe/Copenhagen")
    assert p["window_start"] == "2026-10-15T06:00:00Z"
    assert p["window_end"] == "2026-10-16T06:00:00Z"
    assert p["new_rooms"] == 2 and p["cancelled_rooms"] == 1


PDF_TEXT = """Status: Confirmed, In-House              LENE 16-10-2026 kl. 07:00
Arrivals on period: 01-10-2026 to 31-10-2026
51001 D2 C Guest                      121315       20-10 15:00 22-10     2    3    6 GRP   1.200,00
      BV8 G Guest                     120456       20-10 15:00 21-10     1    2    2 YP   400,00
      D2 T Guest                      113981       20-10 15:00 21-10     1   12   24 ROOM 0,00
"""


def test_pdf_records_report_time_and_amount_as_proxy_not_actual_revenue(session, monkeypatch):
    monkeypatch.setattr(pdf, "pdf_text", lambda _: PDF_TEXT)
    data = b"%PDF-synthetic-for-test"
    assert validation.record_arrivals(session, data, filename="Arrivals.pdf", tz="Europe/Copenhagen", actor="test") == 31
    assert validation.record_arrivals(session, data, filename="renamed.pdf", tz="Europe/Copenhagen", actor="test") == 0
    records = session.scalars(select(db.ValidationRecord).where(db.ValidationRecord.day == date(2026, 10, 20))).all()
    assert len(records) == 1
    r = records[0]; p = json.loads(r.payload)
    assert r.observed_at == datetime(2026, 10, 16, 5)
    assert (p["rooms_otb"], p["beds_otb"]) == (3, 2)
    assert p["room_type_otb"] == {"D2": 3, "BV8": 2}
    assert p["reservation_amount_allocated"] == 1000  # Total 1200 / 2 nights + 400; no extra qty factor
    assert "room_revenue" not in p and "applied_room" not in p
    assert "new_rooms" not in p and "cancelled_rooms" not in p and "final" not in p
    assert validation.contribution(p) == (None, None)
    assert (validation.PDF_ARCHIVE_ROOT / (p["pdf_hash"]+".pdf")).read_bytes() == data


def test_pdf_balance_cannot_be_mistaken_for_missing_total():
    header = " " * 90 + "Product" + " "*9 + "Price Agency Balance"
    line = "      BV8 G Guest                      120456       20-10 15:00 21-10     1    2    2 YP"
    line = line.ljust(header.index("Agency")+12) + "400,00"
    _, _, rows = pdf.parse(PDF_TEXT.splitlines()[0]+"\nArrivals on period: 01-10-2026 to 31-10-2026\n"+header+"\n"+line)
    assert rows[0]["total_price"] is None


def test_past_nights_in_active_arrivals_report_are_unknown_not_zero(session, monkeypatch):
    monkeypatch.setattr(pdf, "pdf_text", lambda _: PDF_TEXT)
    validation.record_arrivals(session, b"%PDF-past-night", filename="Arrivals.pdf",
                               tz="Europe/Copenhagen", actor="test")
    rows = validation.overview(session, date(2026, 10, 1), date(2026, 10, 1))
    assert len(rows) == 1 and "pdf" not in rows[0]
    partial = rows[0]["history"][0]["data"]
    assert partial["rooms_otb"] is None and partial["beds_otb"] is None
    assert partial["reservation_amount_allocated"] is None
    assert partial["coverage_complete"] is False
    assert partial["reported_active_rooms"] == 0


def test_page_and_pdf_upload_are_connected_without_approving_or_confirming_prices(tmp_path, monkeypatch):
    from app import main
    from dataclasses import replace
    monkeypatch.setattr(main.settings, "database_url", f"sqlite:///{tmp_path}/web.db")
    monkeypatch.setattr(main.settings, "basic_auth_user", None)
    monkeypatch.setattr(validation, "PDF_ARCHIVE_ROOT", tmp_path / "pdfs")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "SessionLocal", None)
    monkeypatch.setattr(pdf, "pdf_text", lambda _: PDF_TEXT)
    with TestClient(main.app) as client:
        page = client.get("/resultater")
        assert page.status_code == 200 and "Upload Arrivals fra Picasso" in page.text
        response = client.post("/data/upload", data={"kind": "inventory", "return_to": "resultater", "override": "1"},
            files={"file": ("Arrivals.pdf", b"%PDF-test", "application/pdf")}, follow_redirects=False)
        assert response.status_code == 303 and "/resultater?message=" in response.headers["location"]
        with db.get_session() as s:
            state = s.get(db.DayState, date(2026, 10, 20))
            assert state.otb_updated == datetime(2026, 10, 16, 5)
            assert not s.scalars(select(db.ValidationRecord).where(db.ValidationRecord.kind == "applied")).all()
            snapshot_count = len(s.scalars(select(db.ValidationRecord)).all())
        client.post("/data/upload", data={"kind": "inventory", "return_to": "resultater", "override": "1"},
            files={"file": ("Arrivals.pdf", b"%PDF-test", "application/pdf")})
        with db.get_session() as s:
            assert len(s.scalars(select(db.ValidationRecord)).all()) == snapshot_count
        page = client.get("/resultater?start=2026-10-01&end=2026-10-31")
        assert page.status_code == 200 and "Reservationsbeløb · skøn" in page.text
        assert "Ikke registreret" in page.text
        export = client.get("/resultater/export.csv?start=2026-10-01&end=2026-10-31")
        assert export.status_code == 200 and "pdf_reservationsbeloeb_fordelt" in export.text
        assert client.get("/resultater/historik.csv").status_code == 200
        assert client.get("/resultater/skabelon.csv?kind=outcome").status_code == 200
        assert client.get("/resultater/pdf/not-a-valid-id").status_code == 404


def test_whole_csv_batch_rolls_back_on_invalid_run_link(session):
    text = ("dato;tidspunkt;koersel;lagt_ind_vaerelsespris\n"
            "2026-10-16;2026-10-16T07:00;;680\n"
            "2026-10-17;2026-10-16T07:00;99999;700\n")
    prepared = validation.parse_csv(text, "applied", "Europe/Copenhagen")
    with pytest.raises(ValueError):
        for row in prepared:
            validation.record(session, "applied", row, actor="test", source="CSV")
    session.rollback()
    assert not session.scalars(select(db.ValidationRecord)).all()

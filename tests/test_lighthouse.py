import pytest

from app.lighthouse import COMPS_SET, ROOM_MAPPING, summarize_model_vs_lighthouse


def test_lighthouse_summary_counts_gap_and_levels():
    rows = [
        {
            "dato": "2026-10-08",
            "lighthouse_my_price": "700,0",
            "room_diff_lighthouse_minus_model": "70,0",
            "room_diff_pct_lighthouse_vs_model": "0,10",
            "lighthouse_my_price_level": "Normal",
            "lighthouse_compset_price_level": "Elevated",
            "lighthouse_demand_level": "High",
            "lighthouse_flight_level": "Higher",
            "lighthouse_hotel_level": "Normal",
            "lighthouse_events_holidays": "0",
        },
        {
            "dato": "2026-10-09",
            "lighthouse_my_price": "600,0",
            "room_diff_lighthouse_minus_model": "-30,0",
            "room_diff_pct_lighthouse_vs_model": "-0,05",
            "lighthouse_my_price_level": "Low",
            "lighthouse_compset_price_level": "Low",
            "lighthouse_demand_level": "Normal",
            "lighthouse_flight_level": "Lower",
            "lighthouse_hotel_level": "Higher",
            "lighthouse_events_holidays": "1",
        },
    ]

    summary = summarize_model_vs_lighthouse(rows)

    gap = summary["model_gap_to_lighthouse_room_price"]
    assert gap["average_dkk"] == pytest.approx(20)
    assert gap["dates_model_below_lighthouse"] == 1
    assert gap["dates_model_above_lighthouse"] == 1
    assert summary["level_counts"]["compset_price"] == {"Elevated": 1, "Low": 1}
    assert summary["level_scores"]["flight"] == pytest.approx(0)


def test_lighthouse_compset_and_mapping_from_screenshots_are_recorded():
    assert COMPS_SET["primary"][0]["name"] == "Roberta's Society Aarhus"
    assert {item["name"] for item in COMPS_SET["secondary"]} == {
        "Cabinn Aarhus",
        "Wakeup - Aarhus",
    }
    assert "Double/2 person with private bathroom" in (
        ROOM_MAPPING["danhostel_aarhus_city"]["standard_room"]
    )
